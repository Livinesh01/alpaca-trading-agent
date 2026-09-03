# EPSILON Production Architecture

```text
                  +---------------------+
                  |    EPSILON FRONTEND |
                  |     React / Vite     |
                  | informational/demo  |
                  +----------+----------+
                             |
                             v
                  +---------------------+
                  |     FastAPI API      |
                  | Auth / RBAC / API    |
                  | security-critical    |
                  +----------+----------+
                             |
                             v
                  +---------------------+
                  |   Decision Engine    |
                  | deterministic flow  |
                  +----------+----------+
                             |
                  +----------v----------+
                  | Technical Signals  |
                  | deterministic       |
                  +----------+----------+
                             |
                  +----------v----------+
                  |    LLM Decision     |
                  | advisory authority  |
                  | AI-controlled        |
                  +----------+----------+
                             |
                  +----------v----------+
                  | Python Position     |
                  | Sizing              |
                  | deterministic       |
                  +----------+----------+
                             |
                  +----------v----------+
                  | Deterministic Risk |
                  | Engine              |
                  | security-critical   |
                  +----------+----------+
                             |
                  +----------v----------+
                  |   Final Order Gate  |
                  | security-critical   |
                  +----------+----------+
                             |
                  +----------v----------+
                  | Idempotency / Risk |
                  | Guard / MCP        |
                  | security-critical  |
                  +----------+----------+
                             |
                  +----------v----------+
                  |  Alpaca Paper API  |
                  | paper / production |
                  | paper-only enabled  |
                  +---------------------+
```

## Component Classification

| Component | Classification | Responsibility |
|---|---|---|
| React/Vite frontend | informational, simulated | Read-only terminal views, explicit demo/offline/paper labels; no credentials. |
| FastAPI API | security-critical, production-facing | Typed API boundary, RBAC, request controls, sanitized errors, correlation IDs. |
| Market-data adapter | deterministic, production-facing | Validates provider data and freshness; unavailable data fails closed. |
| Technical signals | deterministic | Calculates explainable indicators from validated bars. |
| LLM decision | AI-controlled, advisory | Direction, confidence, thesis, and entry reasoning only. |
| Decision validation | deterministic, security-critical | Strictly validates structured model output and supported symbols/actions. |
| Python sizing | deterministic, security-critical | Computes quantity from account, price, action, and configured limits. |
| Risk engine | deterministic, security-critical | Enforces exposure, concentration, freshness, account, asset, and kill-switch rules. |
| Final order gate | deterministic, security-critical, production-facing | Rechecks all order invariants immediately before submission. |
| Idempotency store | deterministic, security-critical | Allows one execution claim for an idempotency key, including concurrent duplicates. |
| Risk Guard / MCP | security-critical, production-facing | Isolates provider/tool access from the decision authority and executor. |
| Alpaca Paper API | simulated, production-facing | Paper execution endpoint only; live execution is unsupported. |
| Backtest engine | informational, simulated | Historical data and simulated executor; never reaches Alpaca or MCP. |
| Evaluation harness | informational, simulated | Compares hypothetical candidates; requires human review and cannot auto-deploy. |
| JSONL memory | informational | Audit and replay records; malformed records are skipped safely. |
| Observability | informational | Structured events and metrics; cannot authorize or block safe execution through failure. |

## Authority Chain

```text
Market Data -> Deterministic Signals -> LLM Direction/Reasoning
-> Python Deterministic Sizing -> Risk Engine -> Final Order Gate
-> Idempotency -> Paper Executor
```

The LLM never controls quantity, risk limits, kill-switch state, final authorization, paper/live mode, or execution. No evaluation result automatically changes strategy, risk limits, prompts, models, or production configuration; those actions require human review.

## Deployment Boundaries

`SENTINEL_ENVIRONMENT=development` is the local mode and may use `X-Dev-Role`. `paper` requires explicitly configured authentication. `production` is fail-closed because genuine OIDC/OAuth2 identity and independently verified production infrastructure are not implemented. The frontend accepts only the public `VITE_API_BASE_URL`; trading and model credentials remain backend-only.
