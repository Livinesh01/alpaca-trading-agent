"""Risk-guard MCP proxy. Claude Code talks to THIS server (.mcp.json), not Alpaca's official one.

Spawns alpaca-mcp-server as a subprocess, passes read-only tools through, and
intercepts every order-placement tool: checks against risk_rules.check_order(),
forwards on pass, returns structured rejections otherwise. Decisions land in journal + Prometheus.
"""
import asyncio
import hashlib
import json
import os
import sys
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any

import yaml
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.dirname(__file__))
import journal
import metrics
from risk_rules import AccountState, OrderRequest, check_order
from signals import compute_technical_signals

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "strategy.yaml")

# order-placement tools; everything else is read-only passthrough
ORDER_TOOLS = {
    "place_stock_order",
    "place_crypto_order",
    "place_option_order",
}

# for building AccountState from upstream
ACCOUNT_INFO_TOOL = "get_account_info"
POSITIONS_TOOL = "get_all_positions"
STOCK_BARS_TOOL = "get_stock_bars"
ORDER_LOOKUP_TOOLS = ("get_order_by_client_id", "get_order_by_client_order_id")
ORDER_LIST_TOOLS = ("get_orders", "get_all_orders", "list_orders")
CLIENT_ORDER_ID_BUCKET_SECONDS = int(os.environ.get("CLIENT_ORDER_ID_BUCKET_SECONDS", "300"))
SIGNALS_TOOL = "get_technical_signals"
SIGNALS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": "Ticker symbol (e.g. AAPL).",
        },
    },
    "required": ["symbol"],
}

AMBIGUOUS_FAILURE_HINTS = (
    "may have been placed",
    "safe to retry",
    "timed out",
    "timeout",
)
LATEST_TRADE_TOOL = "get_stock_latest_trade"
DEFAULT_MARKET_DATA_MAX_AGE_SECONDS = 120

PENDING_ORDER_STATUSES = {
    "new",
    "accepted",
    "accepted_for_bidding",
    "pending_new",
    "partially_filled",
    "pending_cancel",
    "pending_replace",
    "stopped",
    "calculated",
    "held",
    "suspended",
}

_orders_this_run = 0


def _signal_params_from_limits(limits: dict[str, Any]) -> dict[str, Any]:
    return dict(limits.get("signal_params", {}) or {})


def _market_data_max_age_seconds(limits: dict[str, Any]) -> int:
    raw = limits.get(
        "market_data_max_age_seconds",
        limits.get("stale_quote_threshold_seconds", DEFAULT_MARKET_DATA_MAX_AGE_SECONDS),
    )
    value = int(raw)
    if value <= 0:
        raise ValueError("market_data_max_age_seconds (or stale_quote_threshold_seconds) must be > 0.")
    return value


def load_limits() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    limits = cfg.get("risk_limits", {})
    limits.setdefault("watchlist", cfg.get("watchlist", []))
    limits.setdefault("signal_params", cfg.get("signal_params", {}))
    return limits


def _upstream_server_params() -> StdioServerParameters:
    """Spawn the Alpaca MCP server subprocess.

    Console-script only, no `python -m` entrypoint. Child inherits the full env —
    Windows needs PATH/SystemRoot or the child won't spawn (confirmed bug).
    """
    env = dict(os.environ)
    env.update({
        "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "ALPACA_PAPER_TRADE": "true",
    })
    return StdioServerParameters(
        command=os.environ.get("ALPACA_MCP_COMMAND", "alpaca-mcp-server"),
        args=json.loads(os.environ.get("ALPACA_MCP_ARGS", "[]")),
        env=env,
    )


def _first_text_block(result: types.CallToolResult) -> str:
    for block in result.content:
        if isinstance(block, types.TextContent):
            return block.text
    return ""


def _reason_category(reason: str) -> str:
    for key in (
        "watchlist", "cap", "limit", "buying power", "Crypto", "Options",
        "Short selling", "loss",
    ):
        if key.lower() in reason.lower():
            return key.lower().replace(" ", "_")
    return "other"


def _stringify_order_field(value: Any) -> str:
    if value is None:
        return "none"
    return str(value).strip().lower()


def _generate_client_order_id(name: str, arguments: dict[str, Any]) -> str:
    bucket = int(datetime.now(timezone.utc).timestamp()) // max(CLIENT_ORDER_ID_BUCKET_SECONDS, 1)
    parts = [
        name,
        _stringify_order_field(arguments.get("symbol")),
        _stringify_order_field(arguments.get("side")),
        _stringify_order_field(arguments.get("qty")),
        _stringify_order_field(arguments.get("notional")),
        _stringify_order_field(arguments.get("type")),
        _stringify_order_field(arguments.get("time_in_force")),
        str(bucket),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:28]
    return f"rg-{digest}"


def _to_json_or_none(text: str) -> Any | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def _unwrap_payload(payload: Any) -> Any:
    """Unwrap the alpaca-mcp-server security envelope.

    Upstream responses are wrapped as {"_alpaca_mcp_security": {...}, "data": {...}}.
    Without unwrapping, every extractor sees a foreign top level and fails (bars,
    trades) or silently reads zeros (account state). Position lists arrive as
    data.result; return that list directly when it is the only data field.
    """
    if (
        isinstance(payload, dict)
        and "_alpaca_mcp_security" in payload
        and isinstance(payload.get("data"), (dict, list))
    ):
        data = payload["data"]
        if (
            isinstance(data, dict)
            and set(data.keys()) == {"result"}
            and isinstance(data["result"], list)
        ):
            return data["result"]
        return data
    return payload


def _order_matches_client_id(order: dict[str, Any], client_order_id: str) -> bool:
    return str(order.get("client_order_id", "")).strip() == client_order_id


def _extract_order_payload(payload: Any, client_order_id: str) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        if _order_matches_client_id(payload, client_order_id):
            return payload

        order_nested = payload.get("order")
        if isinstance(order_nested, dict) and _order_matches_client_id(order_nested, client_order_id):
            return order_nested

        orders_nested = payload.get("orders")
        if isinstance(orders_nested, list):
            for item in orders_nested:
                if isinstance(item, dict) and _order_matches_client_id(item, client_order_id):
                    return item
        return None

    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and _order_matches_client_id(item, client_order_id):
                return item
    return None


def _is_ambiguous_failure(message: str) -> bool:
    lowered = message.lower()
    if "order" not in lowered:
        return False
    return any(hint in lowered for hint in AMBIGUOUS_FAILURE_HINTS)


def _parse_timestamp_utc(raw: Any) -> datetime:
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("missing timestamp in latest trade data.")
    timestamp = raw.strip()
    if timestamp.endswith("Z"):
        timestamp = f"{timestamp[:-1]}+00:00"
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_latest_trade(symbol: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("latest trade payload is not an object.")

    symbol_upper = symbol.upper()
    trades = payload.get("trades")
    if isinstance(trades, dict):
        trade = trades.get(symbol_upper) or trades.get(symbol)
        if isinstance(trade, dict):
            return trade
        raise ValueError(f"latest trade not present for {symbol_upper}.")

    if "p" in payload and "t" in payload:
        return payload

    raise ValueError("latest trade payload missing 'trades' map.")


def _extract_fresh_last_price(
    symbol: str,
    payload: Any,
    max_age_seconds: int,
    now_utc: datetime | None = None,
) -> float:
    trade = _extract_latest_trade(symbol, payload)

    price_raw = trade.get("p", trade.get("price"))
    if price_raw is None:
        raise ValueError("latest trade payload missing price.")
    price = float(price_raw)
    if price <= 0:
        raise ValueError("latest trade price must be > 0.")

    trade_time_raw = trade.get("t", trade.get("timestamp"))
    trade_time = _parse_timestamp_utc(trade_time_raw)
    now = now_utc or datetime.now(timezone.utc)
    age_seconds = (now - trade_time).total_seconds()
    if age_seconds > max_age_seconds:
        raise ValueError(
            f"latest trade is stale ({age_seconds:.0f}s old; max {max_age_seconds}s)."
        )

    return price


async def _get_fresh_last_price(
    upstream: ClientSession,
    symbol: str,
    max_age_seconds: int,
) -> float:
    symbol_upper = symbol.upper()
    last_error = "unknown latest-trade failure"

    for symbols_value in ([symbol_upper], symbol_upper):
        result = await upstream.call_tool(LATEST_TRADE_TOOL, {"symbols": symbols_value})
        if getattr(result, "isError", False):
            last_error = _first_text_block(result) or f"{LATEST_TRADE_TOOL} returned an error."
            continue

        quote_json = _unwrap_payload(_to_json_or_none(_first_text_block(result)))
        if quote_json is None:
            raise ValueError(f"could not parse JSON from {LATEST_TRADE_TOOL} response.")

        return _extract_fresh_last_price(
            symbol=symbol_upper,
            payload=quote_json,
            max_age_seconds=max_age_seconds,
        )

    raise ValueError(f"{LATEST_TRADE_TOOL} failed: {last_error}")


def _extract_stock_bars(payload: Any, symbol: str) -> list[dict[str, Any]]:
    normalized = symbol.upper()
    if isinstance(payload, dict):
        bars_block = payload.get("bars", payload)
        if isinstance(bars_block, dict):
            bars = bars_block.get(normalized, bars_block.get(symbol))
            if isinstance(bars, list):
                return [b for b in bars if isinstance(b, dict)]
        raise ValueError(f"No bars found for symbol {normalized}.")
    if isinstance(payload, list):
        return [b for b in payload if isinstance(b, dict)]
    raise ValueError("Unexpected get_stock_bars response format.")


async def _get_technical_signal(
    upstream: ClientSession,
    symbol: str,
    signal_params: dict[str, Any],
) -> dict[str, Any]:
    bars_payload = {
        "symbols": symbol.upper(),
        "timeframe": signal_params.get("timeframe", "1Day"),
        "days": int(signal_params.get("lookback_days", 180)),
        "limit": int(signal_params.get("bars_limit", 500)),
        "sort": "asc",
    }
    result = await upstream.call_tool(STOCK_BARS_TOOL, bars_payload)
    if getattr(result, "isError", False):
        raise ValueError(_first_text_block(result) or "Upstream get_stock_bars failed.")

    bars_json = _unwrap_payload(_to_json_or_none(_first_text_block(result)))
    if bars_json is None:
        raise ValueError("Could not parse JSON from get_stock_bars response.")

    bars = _extract_stock_bars(bars_json, symbol)
    if not bars:
        raise ValueError(f"No bar data returned for {symbol.upper()}.")

    return compute_technical_signals(symbol.upper(), bars, signal_params)


async def _lookup_order_by_client_id(
    upstream: ClientSession,
    upstream_tool_names: set[str],
    client_order_id: str,
) -> dict[str, Any] | None:
    for tool_name in ORDER_LOOKUP_TOOLS:
        if tool_name not in upstream_tool_names:
            continue
        for payload in (
            {"client_order_id": client_order_id},
            {"client_id": client_order_id},
            {"id": client_order_id},
        ):
            try:
                result = await upstream.call_tool(tool_name, payload)
            except Exception:  # noqa: BLE001, S112 — try next payload variant
                continue
            parsed = _extract_order_payload(_to_json_or_none(_first_text_block(result)), client_order_id)
            if parsed is not None:
                return parsed

    for tool_name in ORDER_LIST_TOOLS:
        if tool_name not in upstream_tool_names:
            continue
        for payload in (
            {"status": "all", "limit": 200},
            {"status": "all"},
            {},
        ):
            try:
                result = await upstream.call_tool(tool_name, payload)
            except Exception:  # noqa: BLE001, S112 — try next payload variant
                continue
            parsed = _extract_order_payload(_to_json_or_none(_first_text_block(result)), client_order_id)
            if parsed is not None:
                return parsed

    return None


def _reconciliation_outcome(order: dict[str, Any] | None) -> tuple[str, str]:
    if order is None:
        return "not_placed", ""

    status = str(order.get("status", "")).strip().lower()
    if status == "filled":
        return "filled", status
    if status in PENDING_ORDER_STATUSES:
        return "still_pending", status
    return "not_placed", status


async def _reconcile_ambiguous_order_failure(
    upstream: ClientSession,
    upstream_tool_names: set[str],
    *,
    client_order_id: str,
    symbol: str,
    side: str,
    qty: Any,
) -> tuple[str, str, str]:
    matched_order = await _lookup_order_by_client_id(
        upstream,
        upstream_tool_names,
        client_order_id,
    )
    outcome, upstream_status = _reconciliation_outcome(matched_order)

    if matched_order is not None:
        global _orders_this_run
        _orders_this_run += 1
        metrics.ORDERS_ALLOWED.inc()

    journal.log_order_state(
        client_order_id,
        symbol,
        side,
        qty,
        outcome,
        f"Ambiguous upstream failure reconciled. upstream_status={upstream_status or 'none'}",
    )

    if outcome == "filled":
        text = (
            f"RECONCILED: order filled. client_order_id={client_order_id}. "
            f"upstream_status={upstream_status}."
        )
    elif outcome == "still_pending":
        text = (
            f"RECONCILED: order exists and is still pending. client_order_id={client_order_id}. "
            f"upstream_status={upstream_status}."
        )
    else:
        text = (
            f"RECONCILED: order not placed. client_order_id={client_order_id}. "
            "Safe to retry with the same intent."
        )
    return outcome, upstream_status, text


async def _build_account_state(upstream: ClientSession, symbol: str) -> AccountState:
    """Call the upstream server for live account/position state."""
    account_result = await upstream.call_tool(ACCOUNT_INFO_TOOL, {})
    account_raw = _first_text_block(account_result)
    positions_result = await upstream.call_tool(POSITIONS_TOOL, {})
    positions_raw = _first_text_block(positions_result)

    try:
        account_json = _unwrap_payload(json.loads(account_raw))
    except (json.JSONDecodeError, TypeError):
        account_json = {}
    if not isinstance(account_json, dict):
        account_json = {}
    try:
        positions_json = _unwrap_payload(json.loads(positions_raw))
        if not isinstance(positions_json, list):
            positions_json = []
    except (json.JSONDecodeError, TypeError):
        positions_json = []

    existing_notional = 0.0
    for p in positions_json:
        if str(p.get("symbol", "")).upper() == symbol.upper():
            existing_notional = float(p.get("market_value", 0) or 0)

    metrics.ACCOUNT_EQUITY.set(float(account_json.get("equity", 0) or 0))
    metrics.OPEN_POSITIONS.set(len(positions_json))

    daily_pnl = float(account_json.get("equity", 0) or 0) - float(
        account_json.get("last_equity", account_json.get("equity", 0)) or 0
    )
    metrics.DAILY_PNL.set(daily_pnl)

    return AccountState(
        cash=float(account_json.get("cash", 0) or 0),
        buying_power=float(account_json.get("buying_power", 0) or 0),
        equity=float(account_json.get("equity", 0) or 0),
        daily_pnl=daily_pnl,
        open_position_count=len(positions_json),
        orders_placed_this_run=_orders_this_run,
        existing_position_notional=existing_notional,
    )


async def run_proxy() -> None:
    metrics.start_metrics_server()
    limits = load_limits()

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(_upstream_server_params()))
        upstream = await stack.enter_async_context(ClientSession(read, write))
        await upstream.initialize()

        upstream_tools = (await upstream.list_tools()).tools
        virtual_signals_tool = types.Tool(
            name=SIGNALS_TOOL,
            description=(
                "Compute deterministic technical signals (SMA trend, RSI state, ATR volatility, "
                "momentum percent) from Alpaca bars for one symbol."
            ),
            inputSchema=SIGNALS_TOOL_SCHEMA,
        )
        upstream_tool_names = {tool.name for tool in upstream_tools}

        proxy_server: Server = Server("alpaca-risk-guard-proxy")

        @proxy_server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [*upstream_tools, virtual_signals_tool]

        @proxy_server.call_tool()
        async def call_tool(name: str, arguments: dict) -> types.CallToolResult | list[types.TextContent]:
            global _orders_this_run
            metrics.TOOL_CALLS.labels(tool_name=name).inc()

            if name == SIGNALS_TOOL:
                symbol_arg = str((arguments or {}).get("symbol", "")).strip().upper()
                if not symbol_arg:
                    return [types.TextContent(
                        type="text",
                        text="Error: 'symbol' is required for get_technical_signals.",
                    )]
                try:
                    signal_result = await _get_technical_signal(
                        upstream,
                        symbol_arg,
                        _signal_params_from_limits(limits),
                    )
                except ValueError as exc:
                    return [types.TextContent(type="text", text=f"Error: {exc}")]
                return [types.TextContent(type="text", text=json.dumps(signal_result))]

            if name not in ORDER_TOOLS:
                result = await upstream.call_tool(name, arguments)
                # Pass through the full result, including structuredContent: newer
                # MCP clients validate advertised outputSchema and reject text-only
                # responses, which made every read-only passthrough tool unusable.
                return types.CallToolResult(
                    content=result.content,
                    structuredContent=getattr(result, "structuredContent", None),
                    isError=result.isError,
                )

            symbol = arguments.get("symbol", "UNKNOWN")
            side = arguments.get("side", "buy")
            qty = arguments.get("qty")
            notional = arguments.get("notional")
            asset_class = (
                "crypto" if name == "place_crypto_order"
                else "option" if name == "place_option_order"
                else "us_equity"
            )

            order = OrderRequest(
                symbol=symbol, side=side, qty=qty, notional=notional,
                asset_class=asset_class,
            )
            forwarded_args = dict(arguments)
            client_order_id = _generate_client_order_id(name, forwarded_args)
            forwarded_args["client_order_id"] = client_order_id

            try:
                account_state = await _build_account_state(upstream, symbol)
                max_age_seconds = _market_data_max_age_seconds(limits)
                last_price = await _get_fresh_last_price(upstream, symbol, max_age_seconds)
            except ValueError as exc:
                reason = f"Market data guard: {exc}"
                journal.log_order_decision(symbol, side, qty or notional, False, reason)
                metrics.ORDERS_BLOCKED.labels(reason_category="market_data").inc()
                return [types.TextContent(
                    type="text",
                    text=f"REJECTED by risk guard: {reason} Order not sent to Alpaca.",
                )]
            except Exception as exc:  # noqa: BLE001
                metrics.PROXY_ERRORS.inc()
                journal.log_order_decision(symbol, side, qty or notional, False, f"Proxy error: {exc}")
                return [types.TextContent(
                    type="text",
                    text=f"REJECTED by risk guard: internal error building account state ({exc})."
                    " Order not sent to Alpaca.",
                )]

            decision = check_order(order, account_state, limits, last_price=last_price)

            journal.log_order_decision(symbol, side, qty or notional, decision.allowed, decision.reason)

            if not decision.allowed:
                metrics.ORDERS_BLOCKED.labels(reason_category=_reason_category(decision.reason)).inc()
                return [types.TextContent(
                    type="text",
                    text=f"REJECTED by risk guard: {decision.reason} Order not sent to Alpaca.",
                )]

            try:
                result = await upstream.call_tool(name, forwarded_args)
                error_text = _first_text_block(result)
                if getattr(result, "isError", False) and _is_ambiguous_failure(error_text):
                    raise RuntimeError(error_text)
                _orders_this_run += 1
                metrics.ORDERS_ALLOWED.inc()
                journal.log_order_state(
                    client_order_id,
                    symbol,
                    side,
                    qty or notional,
                    "still_pending",
                    "Upstream accepted order placement request.",
                )
                return types.CallToolResult(
                    content=result.content,
                    structuredContent=getattr(result, "structuredContent", None),
                    isError=result.isError,
                )
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if not _is_ambiguous_failure(message):
                    metrics.PROXY_ERRORS.inc()
                    journal.log_order_state(
                        client_order_id,
                        symbol,
                        side,
                        qty or notional,
                        "failed",
                        f"Order submit failed with non-ambiguous error: {message}",
                    )
                    return [types.TextContent(
                        type="text",
                        text=(
                            f"Order submit failed before reconciliation. client_order_id={client_order_id}. "
                            f"Error: {message}"
                        ),
                    )]

                outcome, _, text = await _reconcile_ambiguous_order_failure(
                    upstream,
                    upstream_tool_names,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    qty=qty or notional,
                )
                if outcome != "not_placed":
                    return [types.TextContent(type="text", text=text)]
                return [types.TextContent(type="text", text=text)]

        async with stdio_server() as (proxy_read, proxy_write):
            await proxy_server.run(
                proxy_read,
                proxy_write,
                InitializationOptions(
                    server_name="alpaca-risk-guard-proxy",
                    server_version="0.1.0",
                    capabilities=proxy_server.get_capabilities(
                        notification_options=NotificationOptions(), experimental_capabilities={}
                    ),
                ),
            )


if __name__ == "__main__":
    asyncio.run(run_proxy())
