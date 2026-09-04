# Architecture Deep Dive

> **THIS SYSTEM IS PAPER TRADING ONLY.** All orders go to Alpaca's paper-trading
> endpoint. No live-money orders are ever submitted.

## System Overview

EPSILON is an autonomous AI trading agent that uses a Large Language Model
(Featherless or NVIDIA NIM) to analyze market data and propose trading
decisions. These proposals are then validated by a deterministic risk-guard
layer before any order is submitted to Alpaca's paper-trading API.

The LLM **never** submits orders directly. It only produces a proposed decision
that the deterministic system evaluates for safety.

## Component Diagram

```
                         ┌─────────────────────┐
                         │   EPSILON Frontend   │
                         │   (React/Vercel)     │
                         └──────────┬──────────┘
                                    │ HTTPS (CORS)
                         ┌──────────┴──────────┐
                         │   FastAPI Backend    │
                         │   (Persistent)       │
                         │   Port 8000          │
                         └──────────┬──────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
    ┌─────────┴─────────┐ ┌────────┴────────┐ ┌─────────┴─────────┐
    │   PostgreSQL      │ │   Worker        │ │   Observability   │
    │   (Primary DB)    │ │   (src/worker)  │   (JSONL + Prometheus)
    └───────────────────┘ └────────┬────────┘ └───────────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
           ┌────────┴───────┐ ┌───┴────┐ ┌───────┴────────┐
           │ Alpaca Paper   │ │ LLM    │ │ Market Data    │
           │ API            │ │ (Featherless/NVIDIA) │ (Alpaca) │
           └────────────────┘ └────────┘ └────────────────┘
```
## Data Flow

### Decision Loop (Worker)

1. Market Data Fetch: Alpaca Paper API, staleness check
2. Data Validation: Schema validation, missing/null checks
3. AI Analysis: LLM prompt, inference, structured JSON response
4. Structured Decision: Pydantic validation, bounds checks, sizing
5. Decision Validation: Schema completeness, confidence threshold
6. Risk Guard: Position size, daily loss, max positions, notional
7. Final Gate: Kill switch, paper-only, account state, buying power
8. Idempotency Check: Key derivation, duplicate detection
9. Paper Trading API: Order submission, client order ID
10. Execution Verification: Status polling, fill confirmation, journal

### API Request Flow

Client Request -> Rate Limiter -> Request ID -> Payload Size Check ->
Authentication -> Authorization -> Handler -> Service -> Repository ->
Response Envelope -> Security Headers

## Database Schema

| Table               | Purpose                                    |
|---------------------|--------------------------------------------|
| users               | User accounts                              |
| accounts            | Alpaca account snapshots                   |
| positions           | Current/open positions                     |
| orders              | Order history                              |
| executions          | Fill/execution records                     |
| decisions           | AI decision proposals                      |
| risk_events         | Risk guard rejections                      |
| agent_events        | Agent lifecycle events                     |
| worker_heartbeats   | Worker health timestamps                   |
| system_health       | System health snapshots                    |
| audit_events        | Security/audit trail                       |

## Authentication & Authorization

| Mode         | Auth Method            | Use Case              |
|--------------|------------------------|-----------------------|
| disabled     | None                   | Testing only          |
| development  | X-Dev-Role header      | Local development     |
| production   | JWT Bearer (OIDC/OAuth2)| Production deployment |

### RBAC Roles

| Role     | Permissions                                          |
|----------|------------------------------------------------------|
| VIEWER   | Read-only access to all data endpoints               |
| TRADER   | VIEWER + order preview (POST /orders returns 501)    |
| OPERATOR | TRADER + kill-switch mutation                        |
| ADMIN    | OPERATOR + user/role management                      |

## Frontend Connection States

- CONNECTED: Backend reachable, all systems healthy
- DEGRADED: Backend reachable but some component unhealthy
- OFFLINE: Backend unreachable
- NOT CONFIGURED: VITE_API_BASE_URL not set (demo mode)
- STARTING: Initial connection attempt in progress
- TRADING DISABLED: Kill switch active

Real-time updates use SSE with automatic fallback to polling (5s interval,
exponential backoff up to 30s, stale stream detection at 60s).

## Observability

Structured JSON logs with correlation fields (request_id, trace_id,
decision_id, execution_id, order_id, worker_id). Credentials are never logged.

Prometheus metrics available at /metrics: decision_count, llm_latency,
risk_rejections, order_attempts/success/failures, worker_cycles/failures,
market_data_age, database_latency.

## Security Architecture

### Defense-in-Depth Paper-Only Enforcement

1. Configuration: validate_production_config() requires paper-only settings
2. Order execution: Final gate checks paper mode before every order
3. Order submission: Only paper API client is wired up
4. Worker: Fails closed if paper mode cannot be confirmed
5. Database: All decisions and orders persisted immutably

### Network Security

- CORS allowlist, per-IP rate limiting, payload size limits
- No SSRF vectors, HTTP security headers on every response
- Secrets via platform secret manager in production

## Failure Modes

| Failure                  | Behavior                                         |
|--------------------------|--------------------------------------------------|
| Alpaca unavailable       | Stop trading, emit critical event, retry later   |
| Market data stale        | Skip affected symbols, emit warning              |
| LLM failure              | Skip decision cycle, retry with backoff          |
| Database unavailable     | Stop new trades, preserve risk controls, retry   |
| Worker crash             | Auto-restart via process manager                 |
| Worker heartbeat stale   | /ready returns 503, alert operators              |
| Kill switch activated    | All orders rejected immediately                  |
| Invalid configuration    | Fail closed - application refuses to start       |
