"""Order service: projects order history without ever conflating decisions
with executions, executions with fills.

The API exposes distinct concepts — DECISION, ORDER REQUEST, VALIDATION,
RISK RESULT, FINAL GATE, SUBMISSION, FILL, OUTCOME — and this service keeps
them separate. Decision/run correlation is informational only.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from api.services.data_source import MarketDataSource
from api.services.utils import num

# Raw Alpaca order status -> conservative display state. "FILLED" is asserted
# only when the server confirms the fill; anything else is never labeled filled.
_FILL_STATUSES = frozenset({"filled"})
_FAIL_STATUSES = frozenset({"rejected", "canceled", "cancelled", "expired", "suspended", "stopped"})
_OPEN_STATUSES = frozenset({"new", "accepted", "held", "pending_new", "pending_cancel", "pending_replace"})
_PARTIAL_STATUSES = frozenset({"partially_filled"})


def _classify(status_raw: str) -> str:
    status = status_raw.strip().lower()
    if status in _FILL_STATUSES:
        return "FILLED"
    if status in _PARTIAL_STATUSES:
        return "SUBMITTED"  # partial fill -> still a submitted order, not fully filled
    if status in _FAIL_STATUSES:
        return "FAILED"
    if status in _OPEN_STATUSES:
        return "SUBMITTED"
    return "REQUESTED"


def get_orders(source: MarketDataSource, recent_decisions: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    try:
        orders = source.get_orders()
    except Exception:  # noqa: BLE001 — explicit unavailable
        return {"available": False, "items": [], "reason": "orders unavailable (Alpaca connectivity)"}
    if not isinstance(orders, list):
        return {"available": False, "items": [], "reason": "orders payload was not a list"}

    # Informational decision/run correlation: newest decision per symbol+side.
    correlation: dict[tuple[str, str], dict[str, str]] = {}
    for decision in recent_decisions:
        symbol = str(decision.get("symbol") or "").upper()
        action = str(decision.get("action") or "").upper()
        key = (symbol, action)
        correlation.setdefault(
            key,
            {
                "decision_id": str(decision.get("decision_id") or ""),
                "run_id": str(decision.get("run_id") or ""),
            },
        )

    items: list[dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").lower()
        raw_status = str(order.get("status") or "unknown")
        correlated = correlation.get((symbol, side.upper()), {})
        items.append(
            {
                "order_id": str(order.get("id") or order.get("order_id") or ""),
                "symbol": symbol,
                "side": side,
                "quantity": num(order.get("qty", order.get("quantity"))),
                "order_type": str(order.get("order_type") or "market").lower(),
                "status": raw_status,
                "display_state": _classify(raw_status),
                "submitted_at": order.get("submitted_at") or order.get("created_at") or order.get("updated_at"),
                "filled_at": order.get("filled_at"),
                "decision_id": correlated.get("decision_id") or None,
                "run_id": correlated.get("run_id") or None,
                "correlation": "informational" if correlated else None,
            }
        )
    return {"available": True, "items": items, "reason": None}