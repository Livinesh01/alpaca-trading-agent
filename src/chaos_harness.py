"""Fault-injection harness for chaos-testing the risk-guard proxy.

FakeUpstream stands in for the real Alpaca MCP server with canned/broken responses,
so the proxy's real functions run against controlled failures — nothing here mocks
the proxy's own logic. Scenarios map 1:1 to config/resilience_contract.yaml.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp import types


class FakeResult:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [types.TextContent(type="text", text=text)]
        self.isError = is_error


def _json_result(payload: Any, is_error: bool = False) -> FakeResult:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return FakeResult(text, is_error=is_error)


def clean_account_state(
    equity: float = 10000, cash: float = 10000, buying_power: float = 10000
) -> dict:
    return {
        "equity": equity,
        "cash": cash,
        "buying_power": buying_power,
        "last_equity": equity,
    }


def fresh_quote_payload(symbol: str, price: float = 200.0, age_seconds: float = 1.0) -> dict:
    ts = (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).isoformat().replace("+00:00", "Z")
    return {"trades": {symbol.upper(): {"p": price, "t": ts}}}


def clean_bars_payload(symbol: str, count: int = 60, start_price: float = 100.0) -> dict:
    bars = [
        {
            "c": start_price + i * 0.5,
            "h": start_price + i * 0.5 + 1,
            "l": start_price + i * 0.5 - 1,
            "o": start_price + i * 0.5,
            "v": 1000,
        }
        for i in range(count)
    ]
    return {"bars": {symbol.upper(): bars}}


class FakeUpstream:
    """Configurable ClientSession stand-in; responses maps tool name -> FakeResult/callable/Exception."""

    def __init__(self, responses: dict[str, Any] | None = None, default_symbol: str = "AAPL"):
        self.responses = responses or {}
        self.default_symbol = default_symbol
        self.call_log: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> FakeResult:
        self.call_log.append((name, dict(arguments or {})))

        if name in self.responses:
            handler = self.responses[name]
            if isinstance(handler, BaseException):
                raise handler
            if callable(handler) and not isinstance(handler, FakeResult):
                outcome = handler(arguments)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            return handler

        if name == "get_account_info":
            return _json_result(clean_account_state())
        if name == "get_all_positions":
            return _json_result([])
        if name == "get_stock_latest_trade":
            symbol = arguments.get("symbols", self.default_symbol)
            symbol = symbol[0] if isinstance(symbol, list) else symbol
            return _json_result(fresh_quote_payload(symbol))
        if name == "get_stock_bars":
            symbol = arguments.get("symbols", self.default_symbol)
            return _json_result(clean_bars_payload(symbol))

        return _json_result({}, is_error=False)



def scenario_stale_market_data(
    symbol: str = "AAPL", age_seconds: float = 300.0
) -> FakeUpstream:
    return FakeUpstream(
        responses={
            "get_stock_latest_trade": lambda args: _json_result(
                fresh_quote_payload(symbol, age_seconds=age_seconds)
            ),
        }
    )


def scenario_rate_limit_429(symbol: str = "AAPL") -> FakeUpstream:
    return FakeUpstream(
        responses={
            "get_stock_latest_trade": lambda args: _json_result(
                "HTTP error 429: Too Many Requests - rate limit exceeded",
                is_error=True,
            ),
        }
    )


def scenario_ambiguous_timeout(
    symbol: str = "AAPL", resolves_to: str = "filled"
) -> FakeUpstream:
    """resolves_to: 'filled' | 'not_placed' | 'still_pending'."""

    def place_order_handler(args):
        raise RuntimeError(
            "Request was sent but timed out waiting for a response. "
            "The order MAY have been placed. Check open orders before retrying."
        )

    def lookup_handler(args):
        cid = (
            args.get("client_order_id")
            or args.get("client_id")
            or args.get("id")
        )
        if resolves_to == "not_placed":
            return _json_result({"orders": []})
        status = "filled" if resolves_to == "filled" else "new"
        return _json_result({"client_order_id": cid, "status": status})

    return FakeUpstream(
        responses={
            "place_stock_order": place_order_handler,
            "get_order_by_client_id": lookup_handler,
        }
    )


def scenario_partial_fill(
    symbol: str = "AAPL", requested_qty: int = 100, filled_qty: int = 30
) -> FakeUpstream:
    def place_order_handler(args):
        return _json_result(
            {
                "symbol": symbol,
                "qty": str(requested_qty),
                "filled_qty": str(filled_qty),
                "status": "partially_filled",
                "client_order_id": args.get("client_order_id"),
            }
        )

    return FakeUpstream(responses={"place_stock_order": place_order_handler})


def scenario_flapping_connectivity(symbol: str = "AAPL") -> FakeUpstream:
    def place_order_handler(args):
        raise ConnectionError("Connection to upstream MCP server lost mid-request.")

    return FakeUpstream(responses={"place_stock_order": place_order_handler})


def scenario_malformed_response(symbol: str = "AAPL") -> FakeUpstream:
    return FakeUpstream(
        responses={
            "get_stock_bars": lambda args: _json_result(
                {
                    "bars": {
                        symbol.upper(): [
                            {"c": "not_a_number", "h": None, "l": None, "o": None}
                        ]
                    }
                }
            ),
        }
    )