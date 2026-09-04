# Operations Guide

> **THIS SYSTEM IS PAPER TRADING ONLY.** All orders go to Alpaca's paper-trading
> endpoint. No live-money orders are ever submitted.

## Monitoring

### Health Endpoints

| Endpoint          | Auth     | Purpose                                    |
|-------------------|----------|--------------------------------------------|
| `GET /health`     | Public   | Process liveness + sanitized system status |
| `GET /ready`      | Public   | Ready for trading (503 if degraded)        |
| `GET /api/v1/health` | Auth  | Full health envelope with detail           |

### Health Response Fields

The `/health` response includes: `status`, `backend`, `database`, `worker`,
`alpaca`, `market_data`, `llm`, `paper_trading`, `alpaca_paper`,
`kill_switch`, `last_heartbeat`, `heartbeat_fresh`, `auth_mode`, `version`.

### Prometheus Metrics

Scrape `http://<backend>:8000/metrics`. Key metrics:

- `sentinel_decision_count` — total decisions evaluated
- `sentinel_decision_latency_seconds` — decision processing latency
- `sentinel_llm_latency_seconds` — LLM inference latency
- `sentinel_llm_failures` — LLM call failures
- `sentinel_risk_rejections` — decisions rejected by risk guard
- `sentinel_final_gate_rejections` — decisions rejected by final gate
- `sentinel_order_attempts` — order submission attempts
- `sentinel_order_success` — successful order submissions
- `sentinel_order_failures` — order submission failures
- `sentinel_worker_cycles` — total worker cycles
- `sentinel_worker_failures` — worker cycle failures
- `sentinel_market_data_age_seconds` — age of latest market data
- `sentinel_database_latency_seconds` — database query latency

## Worker Management

### Starting the Worker

```bash
# Via Docker Compose
docker compose up -d worker

# Via systemd
systemctl start sentinel-worker
```

### Checking Worker Health

```bash
curl http://localhost:8000/health
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ready
```

### Worker Restart

The worker restarts automatically after crash. The idempotency system ensures
a restarted worker does not submit duplicate orders.

## Kill Switch

```bash
# Via API (requires OPERATOR+ role)
curl -X POST http://localhost:8000/api/v1/risk/kill-switch \
  -H "Authorization: Bearer <jwt>" -d '{"enable": true}'

# Via environment variable
TRADING_KILL_SWITCH=true
```

When active: market data continues, risk analysis continues, **no new orders
are submitted**.

## Audit Logs

Events are stored in `journal/audit.jsonl`. Query via:

```bash
curl -H "Authorization: Bearer <jwt>" \
  "http://localhost:8000/api/v1/audit?event_type=config_change"
```

Event types: `config_change`, `auth_success`, `auth_failure`, `authz_failure`,
`ai_decision`, `risk_decision`, `final_gate`, `order_attempt`,
`order_submitted`, `order_failed`, `kill_switch_activated`,
`kill_switch_deactivated`, `worker_started`, `worker_stopped`.

## Incident Response

### Worker is stale (heartbeat > 10 minutes)

1. Check worker logs: `docker compose logs worker`
2. Verify Alpaca connectivity: `curl http://localhost:8000/health`
3. Restart worker: `docker compose restart worker`
4. Verify heartbeat is fresh again

### Database unavailable

1. `/health` returns `database: "unavailable"`
2. `/ready` returns 503
3. Worker stops submitting new trades (fail-closed)
4. Restart PostgreSQL then the worker

### Market data stale

1. Health shows `market_data_fresh: false`
2. Worker skips trading for affected symbols
3. Check Alpaca API status
4. Verify `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` are valid paper credentials

### LLM failure

1. Worker logs LLM failures and skips the decision for that cycle
2. Verify `FEATHERLESS_API_KEY` is valid
3. Worker retries with exponential backoff

### Kill switch is on

1. Health shows `kill_switch: true`
2. No orders are submitted
3. To re-enable: send `{"enable": false}` to the kill-switch endpoint
4. Check audit log for who activated it

## Log Management

Structured JSON logs include: `request_id`, `trace_id`, `decision_id`,
`execution_id`, `order_id`, `worker_id`. Credentials are never logged.

## Backup and Recovery

```bash
# Database backup
pg_dump $DATABASE_URL > backup_$(date +%Y%m%d).sql

# Journal backup
tar -czf journal_backup_$(date +%Y%m%d).tar.gz journal/
```

## Scheduled Maintenance

1. **Weekly**: Review audit log for anomalies
2. **Monthly**: Rotate API credentials (Alpaca, Featherless, JWT)
3. **Quarterly**: Review risk limits in `config/strategy.yaml`

## Troubleshooting

### "authentication is not configured" (401)

Use `X-Dev-Role` header in development. In production, use JWT from OIDC.

### "Trader role required" (403)

Your role lacks TRADER/OPERATOR/ADMIN privileges.

### "order submission is disabled at the API layer" (501)

Orders can only be submitted by the worker's decision loop — not via the API.