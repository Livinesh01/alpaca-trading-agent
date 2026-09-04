
# EPSILON — AI Trading Agent (Paper Only)

> **THIS SYSTEM IS PAPER TRADING ONLY. It NEVER trades live money.**
> All orders are submitted exclusively to Alpaca's paper-trading API.

An autonomous AI trading agent built on Alpaca's paper trading API, driven by
the Cline CLI as the agent loop — with a hard risk-guard layer, CI/CD, full
observability, PostgreSQL persistence, JWT authentication, RBAC, and
production deployment configuration wrapped around it.

## Architecture

```
                    ┌──────────────────┐
                    │   Vercel CDN     │
                    │   EPSILON UI     │  (React, static build)
                    └────────┬─────────┘
                             │ HTTPS (CORS-allowlisted)
                    ┌────────┴─────────┐
                    │   FastAPI        │
                    │   Persistent     │  (backend API, port 8000)
                    │   PostgreSQL     │  (primary data store)
                    └────────┬─────────┘
                             │
                    ┌────────┴─────────┐
                    │   Worker         │  (src/worker.py, separate process)
                    │   (persistent)   │
                    └────────┬─────────┘
                             │
                    ┌────────┴─────────┐
                    │   Alpaca         │  (PAPER API ONLY)
                    │   Paper API      │
                    └────────┬─────────┘
                             │
                    ┌────────┴─────────┐
                    │   Featherless     │  (OpenAI-compatible LLM)
                    │   (or NVIDIA NIM) │
                    └──────────────────┘
```
## Components

- `src/api/app.py` — FastAPI backend with RBAC, rate limiting, health checks, SSE streaming, audit logging.
- `src/api/auth.py` — RBAC role definitions and authorization helpers.
- `src/auth.py` — JWT authentication (HS256/RS256/ES256), OIDC-ready.
- `src/config.py` — Strict production configuration validation. Fails closed.
- `src/db.py` — SQLAlchemy ORM persistence (PostgreSQL/SQLite).
- `src/repositories.py` — Repository/service abstractions over the ORM.
- `src/worker.py` — Persistent paper-only trading supervisor.
- `src/agent/decision_loop.py` — Structured decision-making loop with mandatory safety chain.
- `src/agent/llm.py` — LLM provider abstraction (Featherless, NVIDIA).
- `src/market_data.py` — Real-time market data from Alpaca paper account.
- `src/observability.py` — Structured JSON logging with correlation IDs.
- `src/metrics.py` — Prometheus metrics.
- `src/journal.py` — Append-only trade journal and audit log.
- `src/idempotency.py` — Idempotency key derivation and storage.
- `src/risk_rules.py` — Pure, unit-tested risk logic.
- `src/risk_guard_proxy.py` — MCP proxy that intercepts order calls.
- `src/orchestrator.py` — Headless entry point that invokes the Cline CLI.
- `config/strategy.yaml` — Risk limits and watchlist.
- `frontend/` — EPSILON React dashboard with real-time SSE/polling.

## Safety Chain

The trading safety chain is **mandatory** — no code path may bypass any stage:

```
Market Data -> Data Validation -> AI Analysis -> Structured Decision ->
Decision Validation -> Risk Guard -> Final Gate -> Idempotency Check ->
Paper Trading API -> Execution Verification
```

- **No live-money trading**: Verified at 5 independent layers.
- **Fail-closed design**: The agent does not trade if any component is unavailable, stale, or misconfigured.

## Quick Start (Local Development)

```
cp .env.example .env
pip install -r requirements.txt
uvicorn src.api.app:app --reload --port 8000
python src/worker.py
cd frontend && npm install && npm run dev
```

Use `X-Dev-Role: VIEWER`, `TRADER`, `OPERATOR`, or `ADMIN` to test RBAC.

## Production Deployment

See DEPLOYMENT.md. Deploy backend + worker to Render/Fly.io (persistent process), PostgreSQL (managed), frontend to Vercel.

## Documentation

- Architecture Deep Dive: ARCHITECTURE.md
- Deployment Guide: DEPLOYMENT.md
- Operations Guide: OPERATIONS.md
- Security Policy: SECURITY.md

## Testing

```
python -m pytest tests/ -v
cd frontend && npm run test
ruff check src/ tests/
```

## Status

**Paper trading only.** Not production-deployed. See DEPLOYMENT.md.

### Credential Safety

If credentials were previously exposed during development, they **must** be rotated before any production deployment. See SECURITY.md.
