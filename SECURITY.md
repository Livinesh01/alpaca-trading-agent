# Security Policy

## ⚠️ PAPER TRADING ONLY

**THIS SYSTEM IS PAPER TRADING ONLY. It NEVER supports live-money trading.**

All orders are submitted exclusively to Alpaca's paper-trading API
(`https://paper-api.alpaca.markets`). The system enforces paper-only mode
through multiple independent layers of defense (see below).

## Security Model

### Defense-in-Depth Paper-Only Enforcement

The system verifies paper-only mode at **five independent layers**:

1. **Configuration** (`src/config.py`): `validate_production_config()` requires
   `APP_ENV=production`, `TRADING_MODE=paper`, `PAPER_TRADING=true`,
   `ALPACA_PAPER=true`, `ALPACA_PAPER_TRADE=true`, and a `DATABASE_URL`
   pointing to PostgreSQL. Live mode raises `LiveTradingUnsupportedError`.
2. **Order execution** (`src/agent/decision_loop.py`): The final executor gate
   checks `final_gate_must_pass()` and rejects any order if the kill switch is
   active or paper mode is not confirmed.
3. **Order submission** (`src/api/services/order_service.py`): Orders are only
   submitted through the paper API client; live endpoints are not wired.
4. **Worker** (`src/worker.py`): Fails closed on any missing paper-only
   credential or invalid configuration.
5. **Database** (`src/db.py`): Persists every decision, risk event, and audit
   entry immutably for traceability.

### Authentication & Authorization

- **PUBLIC** (no auth): `/health`, `/ready`, `/api/v1/health/stream` (bounded snapshot)
- **AUTHENTICATED** (`X-Dev-Role` header in development, JWT bearer in production):
  `/api/v1/account`, `/api/v1/positions`, `/api/v1/orders`, `/api/v1/decisions`,
  `/api/v1/executions`, `/api/v1/risk`, `/api/v1/activity`, `/api/v1/system`,
  `/api/v1/status`, `/api/v1/audit`, `/api/v1/backtests`
- **OPERATOR** (role escalation): kill-switch mutation, trading enable/disable
- **ADMIN** (role escalation): user/role management, sensitive operations

In production (`API_AUTH_MODE=production`), JWTs are issued by an external
OIDC/OAuth2 identity provider. The `/api/v1/auth/token` endpoint is disabled
in production and exists only for development/testing.

### RBAC Roles

| Role      | Read Data | Create Orders | Kill Switch | Admin Actions |
|-----------|-----------|---------------|-------------|---------------|
| VIEWER    | ✅        | ❌            | ❌          | ❌            |
| TRADER     | ✅        | ✅ (preview only — POST /orders returns 501) | ❌ | ❌ |
| OPERATOR  | ✅        | ✅ (preview only) | ✅      | ❌            |
| ADMIN     | ✅        | ✅ (preview only) | ✅      | ✅            |

**Note:** Order submission via the API (`POST /api/v1/orders`) is disabled
and returns HTTP 501. Paper orders are only submitted by the persistent worker
through the internal decision loop, never from the API layer.

### Secrets Management

- **NEVER** commit `.env` files. Only `.env.example` is tracked in Git.
- **NEVER** expose credentials in frontend code. The browser never calls Alpaca
  or receives API keys — all sensitive calls go through the backend.
- **NEVER** print credentials in logs. Structured JSON logging redacts
  `api_key`, `secret`, `token`, `password`, and `credential` fields.
- Production secrets must be injected via the platform secret manager
  (Vercel / Render / Fly.io / AWS), **not** via `.env` files.

### Rate Limiting

- API rate limiting: per-IP, configurable via `API_RATE_LIMIT_PER_MINUTE`
  (default 120). Returns HTTP 429 with structured error body.
- LLM rate limiting: exponential backoff with bounded retries.
- Database queries: paginated, bounded result sets.

### Audit Trail

Every security-relevant event is logged to an append-only audit trail:

- Configuration changes
- Authentication events
- Authorization failures
- AI decisions
- Risk guard decisions
- Final gate decisions
- Order attempts and submissions
- Kill-switch activation/deactivation
- Worker startup/shutdown

Audit records are immutable and cannot be modified by ordinary users.

### Security Scanning

Automated scanning runs in CI:

- **Secrets scanning**: GitHub Actions scans every commit for leaked credentials.
- **Dependency scanning**: `pip-audit` checks Python dependencies; `npm audit`
  checks frontend dependencies.
- **Container scanning**: Trivy scans the Docker image for HIGH/CRITICAL
  vulnerabilities.
- **Docker lint**: Hadolint validates the Dockerfile.

## Reporting a Vulnerability

If you discover a security issue:

1. **Do NOT** open a public GitHub issue.
2. Email the maintainer immediately.
3. Do not exploit or disclose the vulnerability until it is fixed.
4. Rotate any potentially-exposed credentials immediately.

## Credential Rotation

If credentials were previously exposed during development:

1. **Rotate** all Alpaca paper API keys at https://alpaca.markets
2. **Rotate** all Featherless API keys
3. **Rotate** any JWT signing secrets
4. **Never** reuse previously-exposed credentials.
5. Check git history for leaked secrets: `git log --all --grep='secret'`

## Security Checklist

- [x] No secrets committed to source control
- [x] `.env` is gitignored; only `.env.example` tracked
- [x] Production fails closed without all required credentials
- [x] Paper-only enforcement at 5 independent layers
- [x] JWT auth required in production (`API_AUTH_MODE=production`)
- [x] RBAC enforces role-based access to trading endpoints
- [x] API rate limiting active
- [x] No stack traces leak to API responses
- [x] HTTP security headers set (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`)
- [x] Audit trail for all security-relevant events
- [x] Automated secrets scanning in CI
- [x] Dependency vulnerability scanning in CI
- [x] Container image security scanning in CI