"""Risk service: an informational read-only projection of existing safety state.

This does NOT create a second risk engine — every value is derived from the
existing authoritative components (risk_rules limits, account/positions from
the data source, observability events, memory outcomes). The authoritative
enforcement chain (risk_rules.check_order + _FinalOrderGate) is untouched.
"""

from __future__ import annotations

from typing import Any

from api.services import account_service, position_service
from api.services.data_source import MarketDataSource
from api.services.records import read_records
from api.services.utils import iso_now, num
from memory import MemoryStore
from observability import Observability


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
    state = observability.read_status()

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
        "kill_switch": bool(state.get("kill_switch_enabled", False)),
        "trading_mode": account.get("trading_mode"),
        "paper_trading": account.get("paper_trading", False),
        "authoritative_source": "risk_rules.check_order + _FinalOrderGate (read-only projection)",
        "computed_at": iso_now(),
        "reason": None,
    }