# Deployment Guide

> **THIS SYSTEM IS PAPER TRADING ONLY.** It never executes live-money orders.
> All trading uses Alpaca's paper-trading API exclusively.

## Architecture Overview

```
                    ┌──────────────┐
                    │  Vercel CDN │   (frontend, static build)
                    │  EPSILON UI  │
                    └──────┬───────┘
                           │ HTTPS
                    ┌──────┴───────┐
                    │  FastAPI     │   (backend API, port 8000)
                    │  Persistent  │
                    └──────┬───────┘
                           │
                    ┌──────┴───────┐
                    │  PostgreSQL  │   (primary database)
                    │  (managed)   │
                    └──────┬───────┘
                           │
                    ┌──────┴───────┐
                    │  Worker      │   (src/worker.py, persistent process)
                    │  (separate)  │
                    └──────────────┘
                           │
                    ┌──────┴───────┐
                    │  Alpaca      │   (paper API only)
                    │  Paper API   │
                    └──────────────┘
                           │
                    ┌──────┴───────┐
                    │  Featherless  │  (LLM provider)
                    │  (OpenAI)     │
                    └──────────────┘
```

## Deployment Options

### Option A: Render (Recommended — Simplest)

Render provides managed PostgreSQL and long-running services with zero
configuration overhead.

#### 1. Create PostgreSQL Database

```bash
# Via Render CLI or Dashboard
render db create sentinel-db --database-name sentinel --user sentinel
```

#### 2. Deploy Backend API

Create a **Web Service** on Render:
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `uvicorn src.api.app:app --host 0.0.0.0 --port 8000`
- **Environment**: Python 3.12
- **Plan**: Free (can scale to $7/mo for production)

#### 3. Deploy Worker

Create a **Background Worker** on Render:
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `python src/worker.py`
- **Environment**: Python 3.12
- **Plan**: Free (can scale up)
- **Link to database**: Yes

#### 4. Configure Secrets (All via Render Dashboard)

Set each secret in the Render service's Environment tab:

| Key                  | Value                                      |
|----------------------|--------------------------------------------|
| `APP_ENV`            | `production`                               |
| `TRADING_MODE`       | `paper`                                    |
| `PAPER_TRADING`      | `true`                                     |
| `ALPACA_PAPER`       | `true`                                     |
| `ALPACA_PAPER_TRADE` | `true`                                     |
| `TRADING_ENABLED`    | `true`                                     |
| `TRADING_KILL_SWITCH`| `false`                                    |
| `API_AUTH_MODE`      | `production`                               |
| `JWT_SIGNING_SECRET` | `<random 32+ char string>`                 |
| `DATABASE_URL`       | `<Render provides automatically>`          |
| `LLM_PROVIDER`       | `featherless`                              |
| `FEATHERLESS_API_KEY`| `<your Featherless key>`                   |
| `FEATHERLESS_MODEL`  | `<model name>`                             |
| `SENTINEL_DATA_MODE` | `proxy`                                    |
| `ALPACA_API_KEY`     | `<Alpaca paper API key>`                   |
| `ALPACA_SECRET_KEY`  | `<Alpaca paper secret key>`                |
| `ALPACA_ENDPOINT`    | `https://paper-api.alpaca.markets`         |
| `JWT_ALGORITHM`      | `HS256`                                    |
| `API_CORS_ORIGINS`   | `<your Vercel domain>`                     |

#### 5. Deploy Frontend to Vercel

```bash
vercel --cwd frontend --prod
```

Set `VITE_API_BASE_URL` to your Render backend URL in the Vercel project
settings (Environment Variables tab).

### Option B: Fly.io

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Launch backend
fly launch --name sentinel-api --image sentinel:latest
fly secrets set APP_ENV=production TRADING_MODE=paper ...

# Launch worker (separate service)
fly launch --name sentinel-worker --image sentinel:latest --cmd "python src/worker.py"

# Initialize PostgreSQL
fly postgres create
fly postgres attach sentinel-db
```

### Option C: Docker Compose (Local Production-like)

```bash
POSTGRES_PASSWORD=changeme docker compose up -d
```

This runs PostgreSQL, backend, worker, and monitoring stack (Prometheus +
Grafana + Pushgateway) locally.

## Database Migrations

The database schema is managed via SQLAlchemy ORM. Tables are created
automatically on startup:

```python
from db import init_db
init_db()  # Creates all tables if they don't exist
```

For production, use `pg_upgrade` or a managed PostgreSQL service. The schema
is defined in `src/db.py` using SQLAlchemy ORM models.

## Health Checks

### GET /health (public, no auth)
Returns process health: backend, database, worker, alpaca, market_data, llm,
paper_mode, trading_enabled, last_heartbeat, last_market_data, version.

### GET /ready (public, no auth)
Returns 200 if system is ready for trading, 503 if degraded.

### GET /api/v1/health (authenticated)
Returns full health envelope with structured response.

## Worker

The worker (`src/worker.py`) runs as a separate persistent process:
- Executes trading decisions every `WORKER_INTERVAL_SECONDS` (default 900)
- Maintains heartbeat in `journal/`
- Fails closed on any configuration error
- Restarts automatically via process manager (systemd, Render, Fly.io)

Monitor the worker via the health endpoints — a stale heartbeat (>10 minutes)
makes `/ready` return 503.

## Credential Rotation Checklist

1. Generate new Alpaca paper API keys
2. Generate new Featherless API key
3. Generate new JWT signing secret (≥32 chars)
4. Update secrets in your cloud platform's secret manager
5. Restart both backend and worker services
6. Verify `/health` shows all green
7. Verify a new paper order can be submitted

## Rollback

If the new deployment is broken:
1. Revert to the previous Docker image tag
2. Restore the previous `DATABASE_URL` backup if schema changed
3. Verify `/ready` returns 200
4. Confirm the worker heartbeat is fresh