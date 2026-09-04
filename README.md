
# EPSILON - Autonomous AI Trading Agent

EPSILON separates **AI reasoning from trade execution**. The agent collects Alpaca Paper market and account data, validates freshness and integrity, computes deterministic technical signals, and sends structured context to Featherless AI for a BUY / SELL / HOLD proposal.

The LLM has **no execution authority**. It cannot choose order quantities, change risk limits, bypass safeguards, or submit orders. Python calculates quantity from current price, buying power, maximum order notional, and position limits. Every AI response is schema-validated before execution.

## Multi-Layer Risk Gates

Every proposed trade passes deterministic safety gates:

1. **Market-data validation** - validates price, timestamps, OHLC consistency, and freshness.
2. **Decision validation** - validates structured AI output and allowed actions.
3. **Watchlist enforcement** - only approved symbols can be traded.
4. **Position controls** - enforces maximum positions and exposure.
5. **Order limits** - enforces maximum notional and per-run limits.
6. **Daily-loss protection** - blocks trading when loss limits are reached.
7. **Buying-power validation** - prevents orders from exceeding available funds.
8. **Market-session validation** - checks Alpaca's authoritative market clock.
9. **Final Order Gate** - revalidates account state and price before submission.
10. **Idempotency and reconciliation** - prevents duplicates and supports recovery with Alpaca `client_order_id`.
11. **Paper-only enforcement** - rejects non-paper production configuration.
12. **Worker lease protection** - PostgreSQL leader election prevents duplicate trading cycles.

The system **fails closed**. If critical data, database state, configuration, lease ownership, or safety validation cannot be confirmed, the agent does not trade.

## Architecture

```text
React/Vercel Frontend -> FastAPI Backend -> PostgreSQL + Persistent Worker
                         -> Alpaca Paper API + Featherless AI
```

EPSILON uses Alpaca Paper Trading for execution and account data. PostgreSQL stores decisions, orders, executions, risk events, agent events, worker heartbeats, worker leases, audit events, and idempotency state.

The persistent worker acquires and renews a PostgreSQL lease, runs trading cycles, and writes authoritative heartbeats. FastAPI readiness checks verify the database, worker, Alpaca, market data, LLM, paper mode, and kill switch before reporting readiness.

Authentication uses production JWT/RBAC controls. Secrets come from environment configuration and are redacted from errors.

## Architecture Audit and Hardening

- Local worker heartbeat -> **PostgreSQL persistence**
- Missing worker singleton protection -> **PostgreSQL lease and leader election**
- Database idempotency fail-open behavior -> **fail-closed checks and Alpaca reconciliation**
- Configuration-only health checks -> **real dependency checks**
- Incomplete readiness reporting -> **dependency-aware readiness**
- Hardcoded frontend portfolio values -> **Alpaca-backed account data**
- Missing market-session validation -> **Alpaca market clock**
- Configuration contradictions -> **fail-closed validation**
- Silent worker and repository failures -> **observable error handling**
- JWT test secret warning -> **hardened test fixture without weaker production security**

## Verification Status

**428 automated tests pass**, Ruff hardening is being completed, and the frontend builds successfully.

The remaining verification step is a real external end-to-end test with valid Alpaca Paper credentials and a Featherless API key. Until that test runs, EPSILON is accurately described as:

> **Production-hardened architecture with real Alpaca and Featherless integration implemented; external end-to-end execution verification pending.**
