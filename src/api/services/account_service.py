"""Account service: projects account information from the read-only data source.

Only necessary account fields are exposed; browser never receives Alpaca
credentials. An unavailable Alpaca connection yields an explicit unavailable
state — never a frozen demo value.
"""

from __future__ import annotations

import os
from typing import Any

from api.services.data_source import MarketDataSource
from api.services.utils import iso_now, num

_PAPER_TRUTHY = frozenset({"1", "true", "yes", "on"})


def paper_trading_enabled() -> bool:
    return os.environ.get("PAPER_TRADING", "").strip().lower() in _PAPER_TRUTHY


def _trading_mode() -> str:
    return "paper" if paper_trading_enabled() else "paper_required_not_enabled"


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "portfolio_value": None,
        "equity": None,
        "cash": None,
        "buying_power": None,
        "daily_pnl": None,
        "currency": None,
        "account_status": None,
        "trading_mode": _trading_mode(),
        "paper_trading": paper_trading_enabled(),
        "as_of": None,
        "reason": reason,
    }


def get_account(source: MarketDataSource) -> dict[str, Any]:
    try:
        raw = source.get_account()
    except Exception:  # noqa: BLE001 — explicit unavailable, never fabricated
        return _unavailable("account data unavailable (Alpaca connectivity)")
    if not isinstance(raw, dict):
        return _unavailable("account payload was not an object")

    equity = num(raw.get("equity"))
    last_equity = num(raw.get("last_equity"))
    daily_pnl = equity - last_equity if (equity is not None and last_equity is not None) else None
    return {
        "available": True,
        "portfolio_value": equity,
        "equity": equity,
        "cash": num(raw.get("cash")),
        "buying_power": num(raw.get("buying_power")),
        "daily_pnl": daily_pnl,
        "currency": str(raw.get("currency") or "USD"),
        "account_status": str(raw.get("status") or raw.get("account_status") or "unknown"),
        "trading_mode": _trading_mode(),
        "paper_trading": paper_trading_enabled(),
        "as_of": iso_now(),
        "reason": None,
    }