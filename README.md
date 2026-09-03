# Alpaca AI Trading Agent — Hackathon Build

An autonomous trading agent built on Alpaca's paper trading API, driven by the
**Cline CLI** (`cline`) as the agent loop — with a hard risk-guard layer, CI/CD,
and full observability wrapped around it.

The Cline CLI *is* the agent. `src/orchestrator.py` invokes `cline` headlessly
(`--json`, non-interactive), pointing it at the project-local MCP config
(`.cline-config/`), which wires the risk-guard proxy up as the `alpaca` MCP
server. There is **no custom Python agentic loop** and **no direct Anthropic SDK
dependency** — the Cline CLI's own tool-use loop does the agentic work.

## Why this is different from a typical "LLM calls a trading API" hackathon entry

Most agent demos let the LLM call `place_order` directly. That means the safety of the
whole system depends entirely on the model's judgment in that one turn. This project
does not do that.

**The Cline agent never talks to Alpaca directly.** It talks to a *risk-guard proxy*
MCP server that we own. The proxy forwards read-only tools (quotes, bars, account info)
straight through, but every order-placement call is intercepted, checked against
deterministic Python rules (position size, daily loss cap, max open positions, symbol
allow-list), logged to a trade journal, and only then forwarded to Alpaca's real
MCP server. If a rule fails, the order never leaves the proxy — Cline gets a structured
rejection message back, not a silent failure.

The proxy also closes an idempotency gap in upstream order submission: it always stamps
orders with a deterministic `client_order_id`, and if Alpaca returns an ambiguous timeout
("order may have been placed"), it actively reconciles by querying order state before
responding. The agent gets a definitive outcome (`filled`, `not placed`, or `still pending`)
instead of an "unknown, maybe retry" guess.
It also enforces fresh market-data preconditions for order risk checks: if latest trade
data is missing, invalid, or stale beyond `risk_limits.market_data_max_age_seconds`, the
order is explicitly rejected before it can be sent upstream.

```
cline CLI (ClinePass / Cline provider — the agent loop)
        │  MCP (stdio, from .cline-config/cline_mcp_settings.json)
        ▼
risk_guard_proxy.py  ──────► deterministic risk checks (risk_rules.py)
        │                            │
        │  forwards read tools       │  blocks/allows orders
        ▼                            ▼
alpaca-mcp-server (official)  ──► trade journal + Prometheus metrics
        │
        ▼
Alpaca Paper Trading API
```

`src/orchestrator.py` is the scheduled entry point: it checks market hours, then invokes
`cline` headlessly with the project-local `--config .cline-config`, captures its
newline-delimited JSON output, and logs the outcome to the journal.

## Components

- `src/orchestrator.py` — headless entry point: checks market hours, invokes the Cline
  CLI (`--cwd`, `--config .cline-config`, `--system`, `--timeout`, `--retries`, `--json`),
  and logs the outcome. Triggered by cron or a GitHub Actions schedule.
- `src/risk_guard_proxy.py` — the MCP proxy. This is the core deliverable; it is the
  `alpaca` MCP server in `.cline-config/cline_mcp_settings.json`.
- `src/risk_rules.py` — pure, unit-tested risk logic (no I/O).
- `src/signals.py` — deterministic SMA/RSI/ATR/momentum calculations used by the proxy's
  virtual `get_technical_signals` tool.
- `src/journal.py` — appends a markdown entry per decision to `journal/`.
- `src/metrics.py` — Prometheus counters/gauges, exposed on `:9108/metrics`.
- `config/strategy.yaml` — risk limits and watchlist, edit this without touching code.
- `config/claude_system_prompt.md` — the system prompt passed to `cline` (`--system`).
- `.cline-config/cline_mcp_settings.json` — project-local MCP config that declares the
  `alpaca` server (the risk-guard proxy). Passed to cline via `--config .cline-config`.
- `docker-compose.yml` — agent + Prometheus + Grafana, one command to run the full stack.
- `.github/workflows/` — CI (lint/test/risk-rule tests) and a scheduled run workflow.


## Setup

1. Get free paper-trading API keys at https://alpaca.markets (no card required).
2. Set up Cline: `npm i -g cline`, then `cline auth` (ClinePass or a Cline
   provider/API key — that is the model access that actually runs the agent).
3. Copy `.env.example` to `.env` and fill in `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`.
4. `pip install -r requirements.txt`
5. Edit `config/strategy.yaml` with your watchlist and risk limits.
6. Run one agent session directly: `cline --cwd . --config .cline-config --json \
   "Check my account status and summarize it."` — this is the fastest way to confirm
   the proxy and Alpaca connection both work before wiring up scheduling. The
   `.cline-config/cline_mcp_settings.json` declares the `alpaca` MCP server (the
   risk-guard proxy) for cline.
7. To run headless on a schedule: `python src/orchestrator.py`
8. To bring up the full stack with monitoring: `docker compose up`

## Demo script (for judging)

1. Show `config/strategy.yaml` — the human-set boundaries.
2. Run the orchestrator live, show Cline reasoning about a symbol in the terminal.
3. Show one trade that the risk guard **rejected** (deliberately trip a limit) —
   this is the moment that proves the guard is real, not decorative.
4. Show the Grafana dashboard updating with the trade.
5. Show `journal/` with the reasoning trace next to the resulting order.

## Status

Scaffolded for the Alpaca AI Trading Agents Hackathon (28 Aug–4 Sep 2026). Risk rules
and strategy in `config/strategy.yaml` are starter defaults — tune before running for
real, even against paper trading.
