"""Market-data validation and freshness enforcement.

This module validates every market-data point before it enters the decision
pipeline. Invalid, stale, or future-dated data fails closed — no invented
fallback values are ever used for execution.

Data provenance is explicitly tracked:
  - DEMO / SIMULATED: deterministic local fixtures, never live
  - PAPER: Alpaca paper market data via the risk-guard proxy
  - HISTORICAL: backtest/evaluation data, never for live execution
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from errors import MarketDataStaleError, MarketDataValidationError


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def validate_symbol(symbol: Any) -> str:
    """Validate and normalize a trading symbol. Returns uppercase symbol string."""
    if not isinstance(symbol, str):
        raise MarketDataValidationError(f"Symbol must be a string, got {type(symbol).__name__}")
    cleaned = symbol.strip().upper()
    if not cleaned:
        raise MarketDataValidationError("Symbol cannot be empty")
    if not cleaned.isalpha():
        raise MarketDataValidationError(f"Symbol contains invalid characters: {symbol!r}")
    if len(cleaned) > 8:
        raise MarketDataValidationError(f"Symbol too long: {symbol!r}")
    return cleaned


def validate_price(price: Any, field_name: str = "price") -> float:
    """Validate a single price value. Must be positive and finite."""
    if price is None:
        raise MarketDataValidationError(f"{field_name} is missing")
    try:
        value = float(price)
    except (TypeError, ValueError) as exc:
        raise MarketDataValidationError(f"{field_name} must be a number, got: {price!r}") from exc
    if math.isnan(value):  # NaN check (value != value triggers a linter rule)
        raise MarketDataValidationError(f"{field_name} is NaN")
    if value <= 0:
        raise MarketDataValidationError(f"{field_name} must be positive, got {value}")
    if value == float("inf") or value == float("-inf"):
        raise MarketDataValidationError(f"{field_name} is infinite")
    return value


def validate_timestamp(timestamp: Any, field_name: str = "timestamp") -> datetime:
    """Validate and parse an ISO-8601 timestamp. Returns timezone-aware UTC datetime."""
    if timestamp is None:
        raise MarketDataValidationError(f"{field_name} is missing")
    if not isinstance(timestamp, str):
        raise MarketDataValidationError(f"{field_name} must be a string, got {type(timestamp).__name__}")
    ts = timestamp.strip()
    if ts.endswith("Z"):
        ts = f"{ts[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError) as exc:
        raise MarketDataValidationError(f"{field_name} is not valid ISO-8601: {timestamp!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_ohlc(bar: dict[str, Any], symbol: str = "UNKNOWN") -> dict[str, float]:
    """Validate an OHLC bar's price relationships and positivity."""
    if not isinstance(bar, dict):
        raise MarketDataValidationError(f"Bar for {symbol} must be a dict, got {type(bar).__name__}")
    close = validate_price(bar.get("close") or bar.get("c"), f"{symbol}.close")
    open_price = validate_price(bar.get("open") or bar.get("o"), f"{symbol}.open")
    high = validate_price(bar.get("high") or bar.get("h"), f"{symbol}.high")
    low = validate_price(bar.get("low") or bar.get("l"), f"{symbol}.low")
    if low > high:
        raise MarketDataValidationError(f"{symbol}: low ({low}) exceeds high ({high})")
    if high < close or high < open_price:
        raise MarketDataValidationError(f"{symbol}: high ({high}) is below close ({close}) or open ({open_price})")
    if low > close or low > open_price:
        raise MarketDataValidationError(f"{symbol}: low ({low}) is above close ({close}) or open ({open_price})")
    return {"open": open_price, "high": high, "low": low, "close": close}


def check_freshness(
    timestamp: datetime,
    max_age_seconds: float,
    *,
    now: datetime | None = None,
    field_name: str = "market data",
) -> None:
    """Verify market data is not stale or from the future."""
    if now is None:
        now = _now_utc()
    age_seconds = (now - timestamp).total_seconds()
    if age_seconds < -5:  # 5-second tolerance for clock skew
        raise MarketDataStaleError(
            f"{field_name} is from the future: {timestamp.isoformat()} vs now {now.isoformat()}"
        )
    if age_seconds > max_age_seconds:
        raise MarketDataStaleError(f"{field_name} is stale: {age_seconds:.1f}s old, max {max_age_seconds}s")


def validate_market_data_point(
    data: dict[str, Any],
    symbol: str,
    max_age_seconds: float,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Full validation of a single market-data point. Returns normalized validated dict."""
    validated_symbol = validate_symbol(symbol)
    if not isinstance(data, dict):
        raise MarketDataValidationError(f"Market data for {validated_symbol} must be a dict")
    errors: list[str] = []
    try:
        price = validate_price(data.get("price") or data.get("p") or data.get("last"), "price")
    except MarketDataValidationError as exc:
        errors.append(str(exc))
    try:
        timestamp = validate_timestamp(data.get("timestamp") or data.get("t"))
    except MarketDataValidationError as exc:
        errors.append(str(exc))
        timestamp = None
    if errors:
        raise MarketDataValidationError(f"Market data validation failed: {'; '.join(errors)}")
    if timestamp is not None:
        check_freshness(timestamp, max_age_seconds, now=now, field_name=f"market data for {validated_symbol}")
    result: dict[str, Any] = {"symbol": validated_symbol, "price": price}
    if timestamp is not None:
        result["timestamp"] = timestamp.isoformat()
    if any(k in data for k in ("open", "o", "high", "h", "low", "l")):
        result["ohlc"] = validate_ohlc(data, validated_symbol)
    return result
