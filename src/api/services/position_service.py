"""Position service: projects and enriches positions server-side.

Authoritative risk values (exposure, unrealized P&L) are calculated here in
Python, never in the browser. Unavailable data surfaces explicitly.
"""

from __future__ import annotations

from typing import Any

from api.services.data_source import MarketDataSource
from api.services.utils import num


def get_positions(source: MarketDataSource) -> dict[str, Any]:
    try:
        positions = source.get_positions()
        account = source.get_account()
    except Exception:  # noqa: BLE001 — explicit unavailable
        return {"available": False, "items": [], "equity": None, "reason": "positions unavailable (Alpaca connectivity)"}
    if not isinstance(positions, list):
        return {"available": False, "items": [], "equity": None, "reason": "positions payload was not a list"}
    account_equity = num(account.get("equity")) if isinstance(account, dict) else None

    items: list[dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or "").upper()
        quantity = num(position.get("qty", position.get("quantity")))
        average_entry = num(position.get("avg_entry_price", position.get("average_entry_price")))
        market_value = num(position.get("market_value"))
        current_price = num(position.get("current_price", position.get("asset_current_price")))
        if current_price is None and market_value is not None and quantity:
            current_price = market_value / quantity

        unrealized_pnl = None
        if market_value is not None and average_entry is not None and quantity is not None:
            unrealized_pnl = market_value - average_entry * quantity
        unrealized_pnl_percent = (
            unrealized_pnl / (average_entry * quantity)
            if unrealized_pnl is not None and average_entry and quantity
            else None
        )
        exposure = market_value if market_value is not None else (quantity * current_price if quantity and current_price else None)
        items.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "average_entry": average_entry,
                "current_price": current_price,
                "market_value": market_value,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_percent": unrealized_pnl_percent,
                "exposure": exposure,
                "portfolio_percent": (exposure / account_equity if exposure is not None and account_equity else None),
            }
        )
    return {"available": True, "items": items, "equity": account_equity, "reason": None}