"""Prometheus metrics for the trading agent.

Call start_metrics_server() once; everything else imports and calls .inc()/.set().
Grafana scrapes :9108/metrics.
"""

from prometheus_client import Counter, Gauge, start_http_server

ORDERS_ALLOWED = Counter(
    "agent_orders_allowed_total", "Orders that passed risk checks and were forwarded to Alpaca"
)
ORDERS_BLOCKED = Counter(
    "agent_orders_blocked_total", "Orders rejected by the risk guard", ["reason_category"]
)
TOOL_CALLS = Counter(
    "agent_mcp_tool_calls_total", "MCP tool calls proxied through to Alpaca", ["tool_name"]
)
PROXY_ERRORS = Counter(
    "agent_proxy_errors_total", "Errors raised while proxying a tool call"
)
ACCOUNT_EQUITY = Gauge("agent_account_equity_usd", "Last observed account equity")
DAILY_PNL = Gauge("agent_daily_pnl_usd", "Last observed daily P&L")
OPEN_POSITIONS = Gauge("agent_open_positions", "Last observed open position count")

_server_started = False


def start_metrics_server(port: int = 9108) -> None:
    global _server_started
    if not _server_started:
        start_http_server(port)
        _server_started = True
