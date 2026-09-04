"""Risk service: an informational read-only projection of existing safety state.

This does NOT create a second risk engine — every value is derived from the
existing authoritative components (risk_rules limits, account/positions from
the data source, observability events, memory outcomes). The authoritative
enforcement chain (risk_rules.check_order + _FinalOrderGate) is untouched.
"""

from __future__ import annotations

import os
from typing import Any

from api.services import account_service, position_service
from api.services.data_source import MarketDataSource
from api.services.records import read_records
from api.services.utils import iso_now, num
from memory import MemoryStore
from observability import Observability


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _use_postgres() -> bool:
    from api.services.history_source import use_postgres_history

    return use_postgres_history()


def _kill_switch_enabled() -> bool:
    """The kill switch is env-authoritative; local health files never decide it."""
    return os.environ.get("TRADING_KILL_SWITCH", "").strip().lower() in _TRUTHY


def _pg_rejections_and_blocks() -> tuple[dict[str, int], int]:
    """Risk counters from PostgreSQL (production). Raises on DB failure."""
    from repositories import count_agent_events, count_risk_events

    return (
        {
            "risk_rejections": count_risk_events(allowed=False),
            "final_gate_rejections": count_agent_events("final_gate_rejection"),
        },
        count_agent_events("market_data_failure"),
    )


def _count_events(observability: Observability, event_type: str) -> int:
    events = read_records(observability.events_path)
    return sum(1 for event in events if event.get("event_type") == event_type)


def _memory_rejections(store: MemoryStore) -> dict[str, int]:
    executions = store.executions()
    risk_rejected = sum(bool(item.get("risk_allowed") is False) for item in executions)
    gate_rejected = sum(bool(item.get("final_gate") is False) for item in executions)
    return {"risk_rejections": risk_rejected, "final_gate_rejections": gate_rejected}


def get_risk(
    source: MarketDataSource,
    store: MemoryStore,
    observability: Observability,
    limits: dict[str, Any],
) -> dict[str, Any]:
    account = account_service.get_account(source)
    positions = position_service.get_positions(source)
    if _use_postgres():
        # C4: production counters come from PostgreSQL; the kill switch is
        # env-authoritative. A DB failure must surface, never default to zeros.
        try:
            rejections, stale_blocks = _pg_rejections_and_blocks()
        except Exception as exc:  # noqa: BLE001 — explicit unavailability
            return {
                "available": False,
                "daily_pnl": None,
                "gross_exposure": None,
                "position_concentration": None,
                "risk_utilization": None,
                "max_position_notional_usd": num(limits.get("max_position_notional_usd")),
                "max_order_notional_usd": num(limits.get("max_order_notional_usd")),
                "stale_data_blocks": 0,
                "risk_rejections": 0,
                "final_gate_rejections": 0,
                "kill_switch": _kill_switch_enabled(),
                "trading_mode": account.get("trading_mode"),
                "paper_trading": account.get("paper_trading", False),
                "authoritative_source": "risk_rules.check_order + _FinalOrderGate (PostgreSQL history)",
                "computed_at": iso_now(),
                "reason": f"risk history unavailable (PostgreSQL): {type(exc).__name__}",
            }
        kill_switch = _kill_switch_enabled()
        authoritative_source = "risk_rules.check_order + _FinalOrderGate (PostgreSQL history)"
    else:
        state = observability.read_status()
        rejections = _memory_rejections(store)
        stale_blocks = _count_events(observability, "market_data_failure")
        kill_switch = bool(state.get("kill_switch_enabled", False))
        authoritative_source = "risk_rules.check_order + _FinalOrderGate (read-only projection)"

    daily_pnl = num(account.get("daily_pnl"))
    exposure = sum(num(item.get("exposure")) or 0.0 for item in positions.get("items", []))
    max_position = num(limits.get("max_position_notional_usd"))
    utilization = exposure / max_position if (max_position and max_position > 0) else None
    top_exposure = max((num(item.get("exposure")) or 0.0 for item in positions.get("items", [])), default=0.0)
    concentration = top_exposure / exposure if exposure and exposure > 0 else None

    rejections = _memory_rejections(store)
    stale_blocks = _count_events(observability, "market_data_failure")

    return {
        "available": positions.get("available", False) or account.get("available", False),
        "daily_pnl": daily_pnl,
        "gross_exposure": exposure,
        "position_concentration": concentration,
        "risk_utilization": utilization,
        "max_position_notional_usd": max_position,
        "max_order_notional_usd": num(limits.get("max_order_notional_usd")),
        "stale_data_blocks": stale_blocks,
        "risk_rejections": rejections["risk_rejections"],
        "final_gate_rejections": rejections["final_gate_rejections"],
        "kill_switch": kill_switch,
        "trading_mode": account.get("trading_mode"),
        "paper_trading": account.get("paper_trading", False),
        "authoritative_source": authoritative_source,
        "computed_at": iso_now(),
        "reason": None,
    }