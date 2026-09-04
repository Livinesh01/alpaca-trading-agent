
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from mcp import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_guard_proxy import (
    _extract_fresh_last_price,
    _extract_order_payload,
    _generate_client_order_id,
    _get_fresh_last_price,
    _is_ambiguous_failure,
    _market_data_max_age_seconds,
    _reconcile_ambiguous_order_failure,
    _reconciliation_outcome,
)


def test_generates_deterministic_client_order_id_within_bucket():
    args = {
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "type": "market",
        "time_in_force": "day",
    }
    first = _generate_client_order_id("place_stock_order", args)
    second = _generate_client_order_id("place_stock_order", args)
    assert first == second
    assert first.startswith("rg-")


def test_generates_different_client_order_id_for_different_order_intent():
    base = {
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "type": "market",
        "time_in_force": "day",
    }
    changed = dict(base)
    changed["qty"] = "2"
    assert _generate_client_order_id("place_stock_order", base) != _generate_client_order_id(
        "place_stock_order",
        changed,
    )


def test_extract_order_payload_finds_nested_order_match():
    payload = {
        "orders": [
            {"client_order_id": "rg-x", "status": "new"},
            {"client_order_id": "rg-y", "status": "filled"},
        ]
    }
    order = _extract_order_payload(payload, "rg-y")
    assert order is not None
    assert order["status"] == "filled"


def test_classifies_reconciliation_outcomes():
    assert _reconciliation_outcome({"status": "filled"}) == ("filled", "filled")
    assert _reconciliation_outcome({"status": "new"}) == ("still_pending", "new")
    assert _reconciliation_outcome({"status": "canceled"}) == ("not_placed", "canceled")
    assert _reconciliation_outcome(None) == ("not_placed", "")


def test_detects_ambiguous_failure_messages():
    assert _is_ambiguous_failure("Order may have been placed due to timeout; safe to retry")
    assert not _is_ambiguous_failure("validation error: missing symbol")


def test_extract_fresh_last_price_returns_price_for_fresh_trade():
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"trades": {"AAPL": {"p": 123.45, "t": "2026-08-19T11:59:20Z"}}}
    assert _extract_fresh_last_price("AAPL", payload, max_age_seconds=120, now_utc=now) == 123.45


def test_extract_fresh_last_price_rejects_missing_trade():
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"trades": {"MSFT": {"p": 123.45, "t": "2026-08-19T11:59:20Z"}}}
    with pytest.raises(ValueError, match="latest trade not present"):
        _extract_fresh_last_price("AAPL", payload, max_age_seconds=120, now_utc=now)


def test_extract_fresh_last_price_rejects_stale_trade():
    now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"trades": {"AAPL": {"p": 123.45, "t": "2026-08-19T11:40:00Z"}}}
    with pytest.raises(ValueError, match="stale"):
        _extract_fresh_last_price("AAPL", payload, max_age_seconds=120, now_utc=now)


class _FakeResult:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [types.TextContent(type="text", text=text)]
        self.isError = is_error


class _StructuredResult:
    def __init__(self, payload: dict, *, text: str = "ok", is_error: bool = False):
        self.content = [types.TextContent(type="text", text=text)]
        self.structuredContent = payload
        self.isError = is_error


class _FakeUpstream:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return next(self._responses)


def test_get_fresh_last_price_uses_symbols_and_returns_price():
    fresh_time = datetime.now(timezone.utc) - timedelta(seconds=20)
    payload = {
        "trades": {
            "AAPL": {
                "p": 222.0,
                "t": fresh_time.isoformat().replace("+00:00", "Z"),
            }
        }
    }
    upstream = _FakeUpstream([
        _FakeResult(json.dumps(payload)),
    ])

    price = asyncio.run(_get_fresh_last_price(upstream, "AAPL", max_age_seconds=120))
    assert price == 222.0
    assert upstream.calls[0] == ("get_stock_latest_trade", {"symbols": ["AAPL"]})


def test_get_fresh_last_price_raises_on_market_data_failure():
    upstream = _FakeUpstream([
        _FakeResult("upstream failure", is_error=True),
        _FakeResult("still failing", is_error=True),
    ])
    with pytest.raises(ValueError, match="get_stock_latest_trade failed"):
        asyncio.run(_get_fresh_last_price(upstream, "AAPL", max_age_seconds=120))


def test_market_data_max_age_seconds_validation():
    assert _market_data_max_age_seconds({"market_data_max_age_seconds": 90}) == 90
    with pytest.raises(ValueError):
        _market_data_max_age_seconds({"market_data_max_age_seconds": 0})


def test_upstream_structured_order_result_preserves_structured_content():
    payload = {"id": "order-123", "status": "filled", "symbol": "MSFT", "qty": "1"}
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=payload,
        isError=False,
    )

    assert result.structuredContent == payload
    assert result.content[0].text == "ok"
    assert result.isError is False


def test_upstream_order_result_is_not_text_only_content():
    payload = {"id": "order-123", "status": "filled", "symbol": "MSFT", "qty": "1"}
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=payload,
        isError=False,
    )

    assert result.structuredContent is not None
    assert result.structuredContent["symbol"] == "MSFT"
    assert result.content[0].text != json.dumps(payload)


def test_upstream_order_result_preserves_error_flag():
    payload = {"error": {"message": "API rejected the order"}}
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text="failed")],
        structuredContent=payload,
        isError=True,
    )

    assert result.isError is True
    assert result.structuredContent == payload
    assert result.content[0].text == "failed"


def test_ambiguous_failure_reconciles_found_order_without_retry():
    upstream = _FakeUpstream([
        _FakeResult(json.dumps({"client_order_id": "rg-abc123", "status": "filled"})),
    ])

    async def _run():
        return await _reconcile_ambiguous_order_failure(
            upstream,
            {"get_order_by_client_id"},
            client_order_id="rg-abc123",
            symbol="MSFT",
            side="buy",
            qty="2",
        )

    outcome, status, text = asyncio.run(_run())
    assert outcome == "filled"
    assert status == "filled"
    assert "RECONCILED: order filled" in text
    assert upstream.calls == [("get_order_by_client_id", {"client_order_id": "rg-abc123"})]


def test_ambiguous_failure_reconciles_missing_order_as_not_placed():
    upstream = _FakeUpstream([
        _FakeResult(json.dumps({"orders": [{"client_order_id": "rg-other", "status": "canceled"}]})),
    ])

    async def _run():
        return await _reconcile_ambiguous_order_failure(
            upstream,
            {"get_orders"},
            client_order_id="rg-missing",
            symbol="MSFT",
            side="buy",
            qty="2",
        )

    outcome, status, text = asyncio.run(_run())
    assert outcome == "not_placed"
    assert status == ""
    assert "RECONCILED: order not placed" in text
    assert "Safe to retry" in text


def test_ambiguous_failure_used_for_accepted_but_response_failed_path():
    upstream = _FakeUpstream([
        _FakeResult(json.dumps({"client_order_id": "rg-accepted", "status": "accepted"})),
    ])

    result = asyncio.run(
        _reconcile_ambiguous_order_failure(
            upstream,
            {"get_order_by_client_id"},
            client_order_id="rg-accepted",
            symbol="MSFT",
            side="buy",
            qty="2",
        )
    )
    outcome, status, text = result
    assert outcome == "still_pending"
    assert status == "accepted"
    assert "RECONCILED: order exists and is still pending" in text


def test_successful_structured_response_remains_structured():
    payload = {"id": "order-456", "status": "accepted", "symbol": "MSFT", "qty": "2"}
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text="accepted")],
        structuredContent=payload,
        isError=False,
    )

    assert result.structuredContent == payload
    assert result.structuredContent["symbol"] == "MSFT"
    assert result.isError is False
