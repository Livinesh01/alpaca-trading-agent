import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from signals import (
    atr_wilder,
    compute_technical_signals,
    momentum_pct,
    rsi_wilder,
    sma,
)


def _bars_from_closes(closes: list[float], spread: float = 1.0) -> list[dict]:
    bars = []
    for close in closes:
        bars.append({"h": close + spread, "l": close - spread, "c": close})
    return bars


def test_sma_returns_none_when_not_enough_values():
    assert sma([1.0, 2.0], 3) is None


def test_sma_returns_expected_average():
    assert sma([1.0, 2.0, 3.0, 4.0], 2) == 3.5


def test_sma_rejects_invalid_period():
    with pytest.raises(ValueError):
        sma([1.0], 0)


def test_rsi_returns_none_when_not_enough_closes():
    assert rsi_wilder([1.0] * 10, period=14) is None


def test_rsi_is_100_on_strictly_increasing_series():
    closes = [float(i) for i in range(1, 30)]
    assert rsi_wilder(closes, period=14) == 100.0


def test_rsi_is_0_on_strictly_decreasing_series():
    closes = [float(i) for i in range(30, 1, -1)]
    assert rsi_wilder(closes, period=14) == 0.0


def test_rsi_is_50_on_flat_series():
    closes = [100.0] * 30
    assert rsi_wilder(closes, period=14) == 50.0


def test_atr_returns_none_when_not_enough_values():
    highs = [11.0] * 10
    lows = [9.0] * 10
    closes = [10.0] * 10
    assert atr_wilder(highs, lows, closes, period=14) is None


def test_atr_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        atr_wilder([10.0, 11.0], [9.0], [9.5, 10.5], period=1)


def test_atr_is_constant_for_constant_true_range():
    highs = [11.0] * 20
    lows = [9.0] * 20
    closes = [10.0] * 20
    atr = atr_wilder(highs, lows, closes, period=14)
    assert atr == pytest.approx(2.0)


def test_momentum_returns_none_when_not_enough_values():
    assert momentum_pct([100.0] * 5, lookback_bars=10) is None


def test_momentum_matches_expected_percent_change():
    closes = [100.0, 110.0]
    assert momentum_pct(closes, lookback_bars=1) == pytest.approx(10.0)


def test_momentum_returns_none_when_prior_is_zero():
    assert momentum_pct([0.0, 10.0], lookback_bars=1) is None


def test_compute_signals_detects_uptrend_positive_momentum():
    closes = [float(i) for i in range(1, 90)]
    bars = _bars_from_closes(closes, spread=0.5)
    signals = compute_technical_signals(
        "AAPL",
        bars,
        {
            "sma_fast_period": 5,
            "sma_slow_period": 20,
            "rsi_period": 14,
            "atr_period": 14,
            "momentum_lookback_bars": 10,
        },
    )
    assert signals["trend"] == "up"
    assert signals["rsi_state"] == "overbought"
    assert signals["momentum_state"] == "positive"


def test_compute_signals_detects_downtrend_negative_momentum():
    closes = [float(i) for i in range(100, 10, -1)]
    bars = _bars_from_closes(closes, spread=0.5)
    signals = compute_technical_signals(
        "TSLA",
        bars,
        {
            "sma_fast_period": 5,
            "sma_slow_period": 20,
            "rsi_period": 14,
            "atr_period": 14,
            "momentum_lookback_bars": 10,
        },
    )
    assert signals["trend"] == "down"
    assert signals["rsi_state"] == "oversold"
    assert signals["momentum_state"] == "negative"


def test_compute_signals_marks_high_volatility_when_atr_pct_exceeds_threshold():
    closes = [100.0] * 60
    bars = _bars_from_closes(closes, spread=10.0)
    signals = compute_technical_signals(
        "MSFT",
        bars,
        {
            "sma_fast_period": 5,
            "sma_slow_period": 20,
            "atr_period": 14,
            "high_volatility_atr_pct": 5.0,
        },
    )
    assert signals["volatility_state"] == "high"
    assert signals["atr_pct"] > 5.0


def test_compute_signals_supports_long_form_bar_keys():
    bars = [{"high": 11.0, "low": 9.0, "close": 10.0} for _ in range(80)]
    signals = compute_technical_signals(
        "NVDA",
        bars,
        {
            "sma_fast_period": 5,
            "sma_slow_period": 20,
            "rsi_period": 14,
            "atr_period": 14,
            "momentum_lookback_bars": 10,
        },
    )
    assert signals["symbol"] == "NVDA"
    assert signals["bar_count"] == 80


def test_compute_signals_raises_when_bar_missing_price_fields():
    bars = [{"open": 10.0}] * 60
    with pytest.raises(ValueError):
        compute_technical_signals("AAPL", bars, {})
