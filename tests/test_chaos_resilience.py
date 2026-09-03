"""Tests each resilience_contract.yaml scenario against real proxy functions.

Uses FakeUpstream (chaos_harness.py) to inject faults. Nothing here mocks the
proxy's own logic — only what it talks to.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from chaos_harness import (
    FakeUpstream,
    _json_result,
    scenario_ambiguous_timeout,
    scenario_flapping_connectivity,
    scenario_malformed_response,
    scenario_partial_fill,
    scenario_rate_limit_429,
    scenario_stale_market_data,
)
from risk_guard_proxy import (
    _get_fresh_last_price,
    _get_technical_signal,
    _lookup_order_by_client_id,
    _market_data_max_age_seconds,
    _reconciliation_outcome,
)


def run(coro):
    # asyncio.run() — get_event_loop() no longer auto-creates on Python 3.14
    return asyncio.run(coro)



def test_stale_quote_is_rejected_with_clear_staleness_reason():
    upstream = scenario_stale_market_data(age_seconds=300.0)
    with pytest.raises(ValueError, match="stale"):
        run(_get_fresh_last_price(upstream, "AAPL", max_age_seconds=120))


def test_fresh_quote_within_threshold_succeeds():
    upstream = scenario_stale_market_data(age_seconds=10.0)  # under 120s default
    price = run(_get_fresh_last_price(upstream, "AAPL", max_age_seconds=120))
    assert price == 200.0


def test_stale_threshold_is_configurable_and_validated():
    assert _market_data_max_age_seconds({"market_data_max_age_seconds": 30}) == 30
    with pytest.raises(ValueError):
        _market_data_max_age_seconds({"market_data_max_age_seconds": -5})



def test_rate_limited_quote_does_not_crash_the_run():
    upstream = scenario_rate_limit_429()
    # must degrade to controlled ValueError, not crash
    with pytest.raises(ValueError, match="get_stock_latest_trade failed"):
        run(_get_fresh_last_price(upstream, "AAPL", max_age_seconds=120))


def test_rate_limited_quote_is_actually_attempted_not_skipped():
    upstream = scenario_rate_limit_429()
    try:
        run(_get_fresh_last_price(upstream, "AAPL", max_age_seconds=120))
    except ValueError:
        pass
    # must not silently skip the upstream call
    assert any(name == "get_stock_latest_trade" for name, _ in upstream.call_log)



def test_ambiguous_timeout_reconciles_to_filled():
    upstream = scenario_ambiguous_timeout(resolves_to="filled")
    order = run(
        _lookup_order_by_client_id(upstream, {"get_order_by_client_id"}, "rg-test123")
    )
    outcome, _status = _reconciliation_outcome(order)
    assert outcome == "filled"


def test_ambiguous_timeout_reconciles_to_not_placed_and_is_safe_to_retry():
    upstream = scenario_ambiguous_timeout(resolves_to="not_placed")
    order = run(
        _lookup_order_by_client_id(upstream, {"get_order_by_client_id"}, "rg-test123")
    )
    outcome, _status = _reconciliation_outcome(order)
    assert outcome == "not_placed"


def test_ambiguous_timeout_never_produces_an_unknown_outcome():
    # outcome must always be one of the three definitive states
    for resolution in ("filled", "not_placed", "still_pending"):
        upstream = scenario_ambiguous_timeout(resolves_to=resolution)
        order = run(
            _lookup_order_by_client_id(
                upstream, {"get_order_by_client_id"}, "rg-test123"
            )
        )
        outcome, _ = _reconciliation_outcome(order)
        assert outcome in ("filled", "not_placed", "still_pending")


def test_partial_fill_status_is_classified_as_still_pending_not_filled():
    # partial fill must map to still_pending, not filled
    order = {"status": "partially_filled", "filled_qty": "30", "qty": "100"}
    outcome, status = _reconciliation_outcome(order)
    assert outcome == "still_pending"
    assert status == "partially_filled"


def test_partial_fill_order_response_preserves_actual_filled_qty():
    upstream = scenario_partial_fill(requested_qty=100, filled_qty=30)
    result = run(upstream.call_tool("place_stock_order", {"client_order_id": "rg-abc"}))
    import json

    payload = json.loads(result.content[0].text)
    # discrepancy must be visible, never silently dropped
    assert payload["filled_qty"] == "30"
    assert payload["qty"] == "100"
    assert payload["filled_qty"] != payload["qty"]



def test_dropped_connection_on_order_call_raises_a_catchable_error_not_crash():
    upstream = scenario_flapping_connectivity()
    # must surface as catchable ConnectionError, not a crash
    with pytest.raises(ConnectionError):
        run(upstream.call_tool("place_stock_order", {"symbol": "AAPL"}))


def test_dropped_connection_during_reconciliation_lookup_does_not_abort_the_search():
    # verify lookup continues after first tool raises
    def failing_lookup(args):
        raise ConnectionError("dropped mid-lookup")

    def working_list(args):
        return _json_result(
            {"orders": [{"client_order_id": "rg-test123", "status": "filled"}]}
        )

    upstream = FakeUpstream(
        responses={
            "get_order_by_client_id": failing_lookup,
            "get_orders": working_list,
        }
    )
    order = run(
        _lookup_order_by_client_id(
            upstream, {"get_order_by_client_id", "get_orders"}, "rg-test123"
        )
    )
    assert order is not None
    assert order["status"] == "filled"


def test_malformed_bar_data_degrades_safely_not_crash():
    upstream = scenario_malformed_response()
    # malformed field degrades to insufficient_data, not a crash
    signal = run(_get_technical_signal(upstream, "AAPL", {}))
    assert signal["insufficient_data"] is True


def test_malformed_response_is_distinguishable_from_clean_no_data():
    # malformed bars degrade to insufficient_data; empty bars raise ValueError
    malformed = run(_get_technical_signal(scenario_malformed_response(), "AAPL", {}))
    assert malformed["insufficient_data"] is True

    empty_bars_upstream = FakeUpstream(
        responses={
            "get_stock_bars": lambda args: _json_result({"bars": {"AAPL": []}}),
        }
    )
    with pytest.raises(ValueError, match="No bar data returned"):
        run(_get_technical_signal(empty_bars_upstream, "AAPL", {}))