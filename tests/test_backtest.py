import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from backtest import (
    BacktestDataError,
    BacktestEngine,
    FrozenDecisionProvider,
    SimulatedExecutor,
    validate_bars,
)
from observability import Observability
from risk_rules import OrderRequest

LIMITS = {
    "max_position_notional_usd": 5000,
    "max_order_notional_usd": 1000,
    "max_open_positions": 5,
    "max_daily_loss_usd": 500,
    "max_orders_per_run": 3,
    "allow_short_selling": False,
    "allow_options": False,
    "allow_crypto": False,
    "watchlist": ["AAPL"],
    "signal_params": {"sma_fast_period": 2, "sma_slow_period": 3, "rsi_period": 2, "atr_period": 2, "momentum_lookback_bars": 2},
}


def bars(values):
    return [{"timestamp": f"2026-01-01T00:0{i}:00+00:00", "open": value, "high": value + 1, "low": value - 1, "close": value, "volume": 100} for i, value in enumerate(values)]


class SequenceProvider:
    def __init__(self, actions):
        self.actions = iter(actions)

    def decide(self, symbol, signals, account, timestamp):
        action = next(self.actions)
        return {"symbol": symbol, "action": action, "confidence": 0.5, "position_size": 0, "thesis": "sequence", "entry_reason": "test"}


def test_validate_bars_rejects_empty_duplicate_out_of_order_and_invalid_data():
    with pytest.raises(BacktestDataError):
        validate_bars([])
    with pytest.raises(BacktestDataError):
        validate_bars(bars([10, 11])[:1] + [dict(bars([10, 11])[0])])
    with pytest.raises(BacktestDataError):
        validate_bars([bars([10, 11])[1], bars([10, 11])[0]])
    broken = bars([10])
    broken[0]["close"] = 0
    with pytest.raises(BacktestDataError):
        validate_bars(broken)


def test_simulator_applies_slippage_and_transaction_costs():
    simulator = SimulatedExecutor(1000, transaction_cost=0.01, slippage=0.1)
    buy = simulator.submit(OrderRequest("AAPL", "buy", 2), 100, "t0")
    assert buy.entry_price == pytest.approx(110)
    assert buy.fees == pytest.approx(2.2)
    assert simulator.cash == pytest.approx(777.8)
    sell = simulator.submit(OrderRequest("AAPL", "sell", 2), 100, "t1")
    assert sell.exit_price == pytest.approx(90)
    assert sell.pnl == pytest.approx(-41.8)


def test_backtest_is_chronological_and_future_candles_do_not_change_earlier_decisions():
    first = bars([100, 101, 102, 103])
    changed_future = first[:2] + [{**first[2], "close": 999, "high": 1000}, {**first[3], "close": 998, "high": 999}]
    provider = FrozenDecisionProvider({"AAPL": "HOLD"})
    one = BacktestEngine(symbol="AAPL", bars=first, provider=provider, limits=LIMITS).run()
    two = BacktestEngine(symbol="AAPL", bars=changed_future, provider=provider, limits=LIMITS).run()
    assert one.decisions[0]["signals"] == two.decisions[0]["signals"]
    assert one.decisions[0]["decision_price"] == two.decisions[0]["decision_price"]


def test_backtest_uses_python_sizing_risk_and_local_only_execution():
    report = BacktestEngine(symbol="AAPL", bars=bars([100, 101, 102, 103]), provider=SequenceProvider(["BUY", "SELL", "HOLD", "BUY"]), limits=LIMITS, starting_capital=5000).run()
    assert report.metrics["number_of_trades"] == 3
    assert report.metrics["BUY_decisions"] == 2
    assert report.metrics["SELL_decisions"] == 1
    assert report.metrics["HOLD_decisions"] == 1
    assert all(trade.qty <= 10 for trade in report.trades)
    assert report.config["provider"] == "SequenceProvider"


def test_backtest_is_reproducible_and_observability_failure_does_not_change_result(tmp_path, monkeypatch):
    first = BacktestEngine(symbol="AAPL", bars=bars([100, 101, 102, 103]), provider=FrozenDecisionProvider({"AAPL": "BUY"}), limits=LIMITS, starting_capital=5000).run()
    observer = Observability(tmp_path)
    monkeypatch.setattr(type(observer.events_path), "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unavailable")))
    second = BacktestEngine(symbol="AAPL", bars=bars([100, 101, 102, 103]), provider=FrozenDecisionProvider({"AAPL": "BUY"}), limits=LIMITS, starting_capital=5000, observability=observer).run()
    assert first.metrics == second.metrics


def test_llm_failure_and_malformed_decision_fail_without_simulated_orders():
    class FailingProvider:
        def decide(self, *args):
            raise RuntimeError("offline provider failed")

    with pytest.raises(RuntimeError):
        BacktestEngine(symbol="AAPL", bars=bars([100, 101]), provider=FailingProvider(), limits=LIMITS).run()

    class MalformedProvider:
        def decide(self, *args):
            return {"symbol": "AAPL", "action": "BUY"}

    with pytest.raises((ValueError, TypeError)):
        BacktestEngine(symbol="AAPL", bars=bars([100, 101]), provider=MalformedProvider(), limits=LIMITS).run()


def test_backtest_has_no_production_order_surface():
    with open(os.path.join(os.path.dirname(__file__), "..", "src", "backtest.py"), encoding="utf-8") as stream:
        source = stream.read()
    assert "place_stock_order" not in source
