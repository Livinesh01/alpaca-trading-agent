"""Market service: bars + deterministic technical signals + freshness.

Technical signals are computed by the existing authoritative Python
implementation (signals.compute_technical_signals) — never recreated in the
browser. Every response exposes freshness (data_timestamp, age_seconds,
is_fresh, source); stale data is reported as stale, never disguised.
"""

from __future__ import annotations

from typing import Any

from api.services.data_source import MarketDataSource, MarketDataUnavailable
from api.services.utils import age_seconds, iso_now, num
from signals import compute_technical_signals

_DEFAULT_MAX_AGE_SECONDS = 120


def _max_age_seconds(limits: dict[str, Any]) -> int:
    try:
        return int(limits.get("market_data_max_age_seconds", _DEFAULT_MAX_AGE_SECONDS))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AGE_SECONDS


def _normalize_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        normalized.append(
            {
                "timestamp": bar.get("t") or bar.get("timestamp") or bar.get("time"),
                "open": num(bar.get("o", bar.get("open"))),
                "high": num(bar.get("h", bar.get("high"))),
                "low": num(bar.get("l", bar.get("low"))),
                "close": num(bar.get("c", bar.get("close"))),
                "volume": num(bar.get("v", bar.get("volume"))),
            }
        )
    return normalized


def _unavailable(symbol: str, reason: str) -> dict[str, Any]:
    return {
        "symbol": symbol.upper(),
        "available": False,
        "is_fresh": False,
        "age_seconds": None,
        "data_timestamp": None,
        "current_price": None,
        "bars": [],
        "signals": {},
        "latest_trade": None,
        "source": "not_connected",
        "reason": reason,
    }


def get_market_data(
    source: MarketDataSource,
    symbol: str,
    *,
    limits: dict[str, Any],
    timeframe: str = "1Day",
    days: int = 180,
    limit: int = 500,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = str(symbol).upper()
    try:
        bars = source.get_bars(symbol, timeframe=timeframe, days=days, limit=limit)
    except MarketDataUnavailable as exc:
        return _unavailable(symbol, f"market data unavailable: {exc.reason}")
    except Exception:  # noqa: BLE001 — explicit unavailable
        return _unavailable(symbol, "market data unavailable (Alpaca connectivity)")
    if not bars:
        return _unavailable(symbol, "no bar data returned")

    signals = compute_technical_signals(symbol, bars, params or limits.get("signal_params") or {})
    signal_params = params or limits.get("signal_params") or {}
    close = num(signals.get("close"))

    last_timestamp = bars[-1].get("t") or bars[-1].get("timestamp") or bars[-1].get("time")
    last_age = age_seconds(last_timestamp)
    max_age = _max_age_seconds(limits)

    latest_trade: dict[str, Any] | None = None
    try:
        trade = source.get_latest_trade(symbol)
        trade_price = num(trade.get("p") or trade.get("price"))
        trade_timestamp = trade.get("t") or trade.get("timestamp")
        trade_age = age_seconds(trade_timestamp)
        if trade_price is not None:
            latest_trade = {
                "price": trade_price,
                "timestamp": trade_timestamp,
                "age_seconds": round(trade_age, 2) if trade_age is not None else None,
            }
    except Exception:  # noqa: BLE001 — freshness best-effort, never fake
        trade_price = None
        trade_age = None

    is_fresh = last_age is not None and 0 <= last_age <= max_age
    if trade_age is not None and (trade_age < 0 or trade_age > max_age):
        is_fresh = False

    return {
        "symbol": symbol,
        "available": True,
        "is_fresh": bool(is_fresh),
        "age_seconds": round(last_age, 2) if last_age is not None else None,
        "data_timestamp": last_timestamp,
        "current_price": trade_price if latest_trade is not None else close,
        "bars": _normalize_bars(bars),
        "signals": signals,
        "signal_params": signal_params,
        "latest_trade": latest_trade,
        "source": "alpaca_paper",
        "timeframe": timeframe,
        "as_of": iso_now(),
        "reason": None,
    }