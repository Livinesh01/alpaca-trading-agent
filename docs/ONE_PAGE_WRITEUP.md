# EPSILON AI Trading Agent

## AI Logic

EPSILON receives market data through the backend data-source boundary. Data is validated for symbol, timestamps, OHLC consistency, freshness, and availability before it can influence a run. Python then calculates deterministic technical signals such as trend, momentum, volatility, and RSI state.

The LLM receives the validated signals and bounded account/context information. It is an advisory reasoning layer: it returns a strict structured decision containing direction, confidence, thesis, and entry reasoning. Schema validation rejects malformed, incomplete, malicious, unsupported, or out-of-range output. Decision records and replay expose the input signals, AI reasoning, confidence, and each subsequent gate so the result is auditable.

AI reasoning is separate from execution authority. The LLM may recommend direction and explain its reasoning, but Python owns quantity, risk limits, kill-switch behavior, paper/live mode, final authorization, and execution.

## Risk Gates

```text
Market Data Validation
        |
        v
Technical Signals
        |
        v
AI Decision
        |
        v
Decision Validation
        |
        v
Python Position Sizing
        |
        v
Risk Rules
        |
        v
Final Order Gate
        |
        v
Idempotency
        |
        v
Paper Execution
```

The LLM cannot determine final position size, modify risk limits, disable the kill switch, bypass risk validation, or authorize an unsafe order. LLM-provided quantity is ignored by deterministic sizing. Every candidate order is checked again immediately before submission, with paper-mode, symbol, quantity, price freshness, order-type, exposure, and kill-switch invariants enforced. Idempotency prevents duplicate claims.

## Alpaca Infrastructure Implementation

SENTINEL integrates with Alpaca only through the backend and its risk-guard/MCP boundary. The browser never calls Alpaca and never receives Alpaca or model-provider credentials. The supported executor is paper-only; live trading is deliberately unsupported and configuration fails closed unless `PAPER_TRADING=true`.

FastAPI provides typed schemas, role checks, request IDs, correlation fields, rate limiting, CORS configuration, sanitized errors, and security headers. Development uses `X-Dev-Role` solely as an explicit local mode. Genuine production OIDC/OAuth2 authentication is not fabricated; production mode remains disabled until that provider and an independently verified paper execution path exist.

The final order gate is the security boundary before Alpaca Paper Trading. Structured observability records run, decision, execution, and outcome identifiers while redacting secrets, and observability failures are informational rather than execution-authorizing. Backtests and evaluations use simulated or historical paths, are labeled hypothetical, and cannot reach Alpaca, MCP, or a production executor. Evaluation results never auto-change models, prompts, strategies, risk limits, or deployment state.

**Final state:** `LIVE TRADING: DISABLED` and `LIVE ORDERS: 0`. SENTINEL is code-ready for its verified paper-only architecture, while genuine production identity and infrastructure remain deployment requirements.
