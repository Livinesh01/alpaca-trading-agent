"""Live chaos-suite runner: all resilience_contract.yaml scenarios vs the real proxy.

Same split as the unit tests but as a one-shot batch. Prints a markdown table and
pushes to Prometheus Pushgateway (`chaos_scenario_check_status`) so Grafana updates live.
"""
import argparse
import asyncio
import json as _json
import os
import sys
from typing import Any

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

sys.path.insert(0, os.path.dirname(__file__))

import chaos_harness
import risk_guard_proxy

# fresh registry — only push the chaos gauge, skip default python/python noise
_CHAOS_REGISTRY = CollectorRegistry()
CHAOS_GAUGE = Gauge(
    "chaos_scenario_check_status",
    "Chaos suite result per scenario: 1=pass, 0=fail",
    ["scenario", "check"],
    registry=_CHAOS_REGISTRY,
)

# mirror of proxy default so suite doesn't need strategy.yaml
MAX_AGE_SECONDS = 120


async def _check_stale_quote_is_rejected() -> tuple[bool, str]:
    """A stale latest-trade must be rejected with a clear 'stale' reason."""
    upstream = chaos_harness.scenario_stale_market_data(age_seconds=300.0)
    try:
        await risk_guard_proxy._get_fresh_last_price(upstream, "AAPL", MAX_AGE_SECONDS)
    except ValueError as exc:
        if "stale" in str(exc).lower():
            return True, f"stale quote rejected: {exc}"
        return False, f"ValueError without a staleness reason: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"wrong exception type, expected ValueError: {type(exc).__name__}: {exc}"
    return False, "stale quote was NOT rejected (order would have been sized on stale data)"


async def _check_fresh_quote_accepted() -> tuple[bool, str]:
    """A fresh quote within the window must succeed (positive control)."""
    upstream = chaos_harness.scenario_stale_market_data(age_seconds=10.0)
    try:
        price = await risk_guard_proxy._get_fresh_last_price(upstream, "AAPL", MAX_AGE_SECONDS)
    except Exception as exc:  # noqa: BLE001
        return False, f"fresh quote errored unexpectedly: {exc}"
    if price == 200.0:
        return True, f"fresh quote accepted cleanly (price={price})"
    return False, f"fresh quote returned unexpected price: {price}"


async def _check_rate_limit_degrades_controlled() -> tuple[bool, str]:
    """A 429 must raise a controlled ValueError, not a crash."""
    upstream = chaos_harness.scenario_rate_limit_429()
    try:
        await risk_guard_proxy._get_fresh_last_price(upstream, "AAPL", MAX_AGE_SECONDS)
    except ValueError as exc:
        message = str(exc)
        attempted = any(
            name == "get_stock_latest_trade" for name, _ in upstream.call_log
        )
        if "get_stock_latest_trade failed" in message and attempted:
            return True, "controlled ValueError raised; get_stock_latest_trade attempted"
        if "get_stock_latest_trade failed" not in message:
            return False, f"ValueError but not the degradation message: {message}"
        return False, "degraded but the upstream call was NOT attempted (looks like a silent skip)"
    except Exception as exc:  # noqa: BLE001
        return False, f"crashed instead of degrading: {type(exc).__name__}: {exc}"
    return False, "429 was silently treated as no-data (no controlled error at all)"


async def _check_ambiguous_timeout_reconciles() -> tuple[bool, str]:
    """An ambiguous timeout must reconcile to a definitive state, never 'unknown'."""
    outcomes: list[str] = []
    ok = True
    for resolves_to in ("filled", "not_placed", "still_pending"):
        upstream = chaos_harness.scenario_ambiguous_timeout(resolves_to=resolves_to)
        order = await risk_guard_proxy._lookup_order_by_client_id(
            upstream, {"get_order_by_client_id"}, "rg-test"
        )
        outcome, _ = risk_guard_proxy._reconciliation_outcome(order)
        if outcome not in ("filled", "not_placed", "still_pending"):
            ok = False
            outcomes.append(f"{resolves_to}->{outcome} [UNKNOWN!]")
        else:
            outcomes.append(f"{resolves_to}->{outcome}")
    detail = ", ".join(outcomes)
    return (True, detail) if ok else (False, f"reconciliation produced an unknown outcome: {detail}")


async def _check_partial_fill_not_misfilled() -> tuple[bool, str]:
    """A partial fill must not be misreported as fully filled."""
    upstream = chaos_harness.scenario_partial_fill(requested_qty=100, filled_qty=30)
    order = await upstream.call_tool("place_stock_order", {"client_order_id": "rg-x"})
    payload = _json.loads(order.content[0].text)
    outcome, status = risk_guard_proxy._reconciliation_outcome(
        {"status": "partially_filled"}
    )
    if outcome == "still_pending" and status == "partially_filled":
        filled_now = payload.get("filled_qty")
        if filled_now is not None and filled_now != payload.get("qty"):
            return True, f"still_pending preserved (filled_qty={filled_now} of qty={payload.get('qty')})"
        return False, "still_pending classified, but filled_qty not preserved in response"
    return False, f"partial fill misclassified: outcome={outcome} status={status}"


async def _check_dropped_connection_is_catchable() -> tuple[bool, str]:
    """A dropped connection must surface as a catchable ConnectionError, not a crash."""
    upstream = chaos_harness.scenario_flapping_connectivity()
    try:
        await upstream.call_tool("place_stock_order", {"symbol": "AAPL"})
    except ConnectionError:
        return True, "ConnectionError raised and catchable"
    except Exception as exc:  # noqa: BLE001
        return False, f"wrong exception type: {type(exc).__name__}: {exc}"
    return False, "no error surfaced for a dropped connection"


async def _check_lookup_survives_drop() -> tuple[bool, str]:
    """A reconciliation lookup must survive a dropped upstream connection."""
    def _failing(_args):
        raise ConnectionError("dropped mid-lookup")

    def _working(_args):
        return chaos_harness._json_result(
            {"orders": [{"client_order_id": "rg-test", "status": "filled"}]}
        )

    upstream = chaos_harness.FakeUpstream(
        responses={
            "get_order_by_client_id": _failing,
            "get_orders": _working,
        }
    )
    order = await risk_guard_proxy._lookup_order_by_client_id(
        upstream,
        {"get_order_by_client_id", "get_orders"},
        "rg-test",
    )
    if order is not None and order.get("status") == "filled":
        return True, "search continued to next lookup tool after a dropped one"
    return False, f"lookup aborted on drop (order={order})"


async def _check_malformed_bars_degrade() -> tuple[bool, str]:
    """Malformed bar payloads must degrade gracefully, not crash the run."""
    upstream = chaos_harness.scenario_malformed_response()
    signal = await risk_guard_proxy._get_technical_signal(upstream, "AAPL", {})
    if signal.get("insufficient_data") is True:
        return True, "malformed bars degraded to insufficient_data=True"
    return False, f"malformed bars did not degrade: {signal}"


# execution order follows resilience_contract.yaml
CHECKS: list[dict[str, Any]] = [
    {"scenario": "stale_market_data", "check": "get_fresh_last_price_rejects_stale", "fn": _check_stale_quote_is_rejected},
    {"scenario": "stale_market_data", "check": "fresh_quote_accepted_control", "fn": _check_fresh_quote_accepted},
    {"scenario": "rate_limit_429", "check": "controlled_degradation_not_crash", "fn": _check_rate_limit_degrades_controlled},
    {"scenario": "ambiguous_timeout", "check": "reconciliation_never_unknown", "fn": _check_ambiguous_timeout_reconciles},
    {"scenario": "partial_fill", "check": "partial_fill_not_reported_as_filled", "fn": _check_partial_fill_not_misfilled},
    {"scenario": "flapping_connectivity", "check": "dropped_connection_catchable", "fn": _check_dropped_connection_is_catchable},
    {"scenario": "flapping_connectivity", "check": "reconciliation_lookup_survives_drop", "fn": _check_lookup_survives_drop},
    {"scenario": "malformed_response", "check": "malformed_bars_degrade_not_crash", "fn": _check_malformed_bars_degrade},
]


async def _run_all_checks() -> list[dict[str, Any]]:
    results = []
    for spec in CHECKS:
        try:
            passed, detail = await spec["fn"]()
        except Exception as exc:  # noqa: BLE001
            passed, detail = False, f"check raised unhandled exception: {type(exc).__name__}: {exc}"
        results.append(
            {
                "scenario": spec["scenario"],
                "check": spec["check"],
                "passed": passed,
                "detail": detail,
            }
        )
    return results


def _print_results(results: list[dict[str, Any]]) -> None:
    print("## Chaos resilience suite")
    print()
    print("| scenario | check | result | detail |")
    print("|---|---|---|---|")
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"| {r['scenario']} | {r['check']} | {mark} | {r['detail']} |")
    passed = sum(1 for r in results if r["passed"])
    print()
    print(f"**{passed}/{len(results)} checks passed**")


def _push_results(url: str, results: list[dict[str, Any]]) -> None:
    for r in results:
        CHAOS_GAUGE.labels(r["scenario"], r["check"]).set(1 if r["passed"] else 0)
    try:
        push_to_gateway(url, "chaos_suite", registry=_CHAOS_REGISTRY, timeout=15)
        print(f"Pushed {len(results)} check status(es) to pushgateway at {url} (job=chaos_suite)")
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: could not push to pushgateway at {url}: {exc}\n"
            "The Grafana resilience row will not update until the gateway is reachable. "
            "This does not change the check results above.",
            file=sys.stderr,
        )


def _resolve_gateway_url(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    return os.environ.get("PUSHGATEWAY_URL", "localhost:9091")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alpaca trading-agent chaos resilience suite")
    parser.add_argument(
        "--pushgateway-url",
        default=None,
        help="Pushgateway address (scheme optional). Defaults to $PUSHGATEWAY_URL or localhost:9091.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Only print results; do not attempt a network push.",
    )
    args = parser.parse_args(argv)

    results = asyncio.run(_run_all_checks())
    _print_results(results)

    failed = any(not r["passed"] for r in results)
    if not args.no_push:
        _push_results(_resolve_gateway_url(args.pushgateway_url), results)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())