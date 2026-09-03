"""Deterministic technical signal calculations used by the proxy."""

from collections.abc import Sequence
from typing import Any


def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def rsi_wilder(closes: Sequence[float], period: int = 14) -> float | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period + 1:
        return None

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for idx in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[idx]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[idx]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr_wilder(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, closes must have same length")
    if len(closes) < period + 1:
        return None

    true_ranges: list[float] = []
    for idx in range(1, len(closes)):
        high = highs[idx]
        low = lows[idx]
        prev_close = closes[idx - 1]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    atr = sum(true_ranges[:period]) / period
    for idx in range(period, len(true_ranges)):
        atr = ((atr * (period - 1)) + true_ranges[idx]) / period
    return atr


def momentum_pct(closes: Sequence[float], lookback_bars: int = 10) -> float | None:
    if lookback_bars <= 0:
        raise ValueError("lookback_bars must be positive")
    if len(closes) < lookback_bars + 1:
        return None
    prior = closes[-(lookback_bars + 1)]
    if prior == 0:
        return None
    return ((closes[-1] - prior) / prior) * 100.0


def _bar_value(bar: dict[str, Any], short_key: str, long_key: str) -> float:
    if short_key in bar:
        return float(bar[short_key])
    if long_key in bar:
        return float(bar[long_key])
    raise ValueError(f"Bar is missing '{short_key}'/'{long_key}' fields.")


def _parse_all_bars(
    bars: list[dict[str, Any]],
) -> tuple[list[float], list[float], list[float]] | None:
    """Parse close/high/low from every bar into (closes, highs, lows).

    Missing price fields raise ValueError (unusable history). Present-but-malformed
    fields return None so the caller degrades to insufficient_data instead of crashing.
    """
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []

    for bar in bars:
        try:
            closes.append(_bar_value(bar, "c", "close"))
            highs.append(_bar_value(bar, "h", "high"))
            lows.append(_bar_value(bar, "l", "low"))
        except ValueError as exc:
            # Distinguish "missing field" (raise) from "malformed value" (degrade).
            if "is missing" in str(exc):
                raise
            return None

    return closes, highs, lows


def _insufficient_data_result(symbol: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Uniform degradation object for malformed-but-present bar data.

    Keeps the agent run alive with a clearly-marked "not enough trustworthy
    data" outcome instead of letting bad upstream data crash the run.
    """
    return {
        "symbol": symbol.upper(),
        "bar_count": len(bars),
        "insufficient_data": True,
        "close": None,
        "sma_fast": None,
        "sma_slow": None,
        "trend": "unknown",
        "rsi": None,
        "rsi_state": "unknown",
        "atr": None,
        "atr_pct": None,
        "volatility_state": "unknown",
        "momentum_pct": None,
        "momentum_state": "unknown",
        "params_used": {},
    }


def compute_technical_signals(
    symbol: str,
    bars: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = params or {}
    sma_fast_period = int(cfg.get("sma_fast_period", 20))
    sma_slow_period = int(cfg.get("sma_slow_period", 50))
    rsi_period = int(cfg.get("rsi_period", 14))
    rsi_overbought = float(cfg.get("rsi_overbought", 70.0))
    rsi_oversold = float(cfg.get("rsi_oversold", 30.0))
    atr_period = int(cfg.get("atr_period", 14))
    high_volatility_atr_pct = float(cfg.get("high_volatility_atr_pct", 3.0))
    momentum_lookback_bars = int(cfg.get("momentum_lookback_bars", 10))
    momentum_flat_threshold_pct = float(cfg.get("momentum_flat_threshold_pct", 0.2))

    parsed = _parse_all_bars(bars)
    if parsed is None:
        return _insufficient_data_result(symbol, bars)
    closes, highs, lows = parsed

    close = closes[-1]
    fast = sma(closes, sma_fast_period)
    slow = sma(closes, sma_slow_period)
    if fast is None or slow is None:
        trend = "unknown"
    elif fast > slow:
        trend = "up"
    elif fast < slow:
        trend = "down"
    else:
        trend = "flat"

    rsi_value = rsi_wilder(closes, rsi_period)
    if rsi_value is None:
        rsi_state = "unknown"
    elif rsi_value >= rsi_overbought:
        rsi_state = "overbought"
    elif rsi_value <= rsi_oversold:
        rsi_state = "oversold"
    else:
        rsi_state = "neutral"

    atr_value = atr_wilder(highs, lows, closes, atr_period)
    atr_pct = (atr_value / close * 100.0) if (atr_value is not None and close > 0) else None
    if atr_pct is None:
        volatility_state = "unknown"
    elif atr_pct >= high_volatility_atr_pct:
        volatility_state = "high"
    else:
        volatility_state = "normal"

    momentum_value = momentum_pct(closes, momentum_lookback_bars)
    if momentum_value is None:
        momentum_state = "unknown"
    elif momentum_value > momentum_flat_threshold_pct:
        momentum_state = "positive"
    elif momentum_value < -momentum_flat_threshold_pct:
        momentum_state = "negative"
    else:
        momentum_state = "flat"

    return {
        "symbol": symbol.upper(),
        "bar_count": len(bars),
        "insufficient_data": False,
        "close": close,
        "sma_fast": fast,
        "sma_slow": slow,
        "trend": trend,
        "rsi": rsi_value,
        "rsi_state": rsi_state,
        "atr": atr_value,
        "atr_pct": atr_pct,
        "volatility_state": volatility_state,
        "momentum_pct": momentum_value,
        "momentum_state": momentum_state,
        "params_used": {
            "sma_fast_period": sma_fast_period,
            "sma_slow_period": sma_slow_period,
            "rsi_period": rsi_period,
            "rsi_overbought": rsi_overbought,
            "rsi_oversold": rsi_oversold,
            "atr_period": atr_period,
            "high_volatility_atr_pct": high_volatility_atr_pct,
            "momentum_lookback_bars": momentum_lookback_bars,
            "momentum_flat_threshold_pct": momentum_flat_threshold_pct,
        },
    }
