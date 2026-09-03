import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from backtest import FrozenDecisionProvider
from evaluation import HYPOTHETICAL_LABEL, CandidateConfig, EvaluationHarness
from observability import Observability

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


def harness(candidates, values=(100, 101, 102, 103), **kwargs):
    return EvaluationHarness(symbol="AAPL", bars=bars(values), limits=LIMITS, candidates=candidates, starting_capital=5000, **kwargs)


def test_candidates_share_dataset_and_report_transparent_categories():
    report = harness([
        CandidateConfig("A", lambda: FrozenDecisionProvider({"AAPL": "HOLD"}), model="m1", prompt_version="v1"),
        CandidateConfig("B", lambda: FrozenDecisionProvider({"AAPL": "BUY"}), model="m2", prompt_version="v2"),
    ]).run()
    assert len(report.candidates) == 2
    assert report.dataset["symbol"] == "AAPL"
    assert report.candidates[0].decision_metrics["HOLD"] == 4
    assert report.candidates[1].decision_metrics["BUY"] == 4
    assert report.to_dict()["label"] == HYPOTHETICAL_LABEL
    assert "HUMAN REVIEW REQUIRED" in report.recommendation


def test_deterministic_candidate_is_reproducible_except_evaluation_timestamp():
    candidate = [CandidateConfig("frozen", lambda: FrozenDecisionProvider({"AAPL": "BUY"}), seed=7)]
    first = harness(candidate).run().to_dict()
    second = harness(candidate).run().to_dict()
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


def test_future_candles_cannot_change_earlier_candidate_decision():
    candidate = [CandidateConfig("frozen", lambda: FrozenDecisionProvider({"AAPL": "HOLD"}))]
    first = harness(candidate, values=(100, 101, 102, 103)).run()
    second = harness(candidate, values=(100, 101, 999, 998)).run()
    assert first.candidates[0].report.decisions[0]["signals"] == second.candidates[0].report.decisions[0]["signals"]
    assert first.candidates[0].report.decisions[0]["decision_price"] == second.candidates[0].report.decisions[0]["decision_price"]


def test_candidates_receive_equivalent_information():
    seen = []

    class Recorder:
        def __init__(self, action):
            self.action = action

        def decide(self, symbol, signals, account, timestamp):
            seen.append((self.action, symbol, timestamp, signals, account.equity))
            return FrozenDecisionProvider({"AAPL": self.action}).decide(symbol, signals, account, timestamp)

    harness([CandidateConfig("A", lambda: Recorder("HOLD")), CandidateConfig("B", lambda: Recorder("HOLD"))]).run()
    assert [(row[1], row[2], row[3], row[4]) for row in seen[:4]] == [(row[1], row[2], row[3], row[4]) for row in seen[4:]]


def test_provider_failure_and_malformed_data_fail_without_production_execution():
    class Failing:
        def decide(self, *args):
            raise TimeoutError("provider timeout")

    result = harness([CandidateConfig("bad", Failing)]).run()
    assert result.candidates[0].report is None
    assert result.candidates[0].reliability_metrics["provider_success"] is False
    bad_data = harness([CandidateConfig("bad-data", lambda: FrozenDecisionProvider())], values=(100, 0)).run()
    assert bad_data.candidates[0].report is None
    assert bad_data.candidates[0].failure


def test_observability_failure_does_not_change_metrics(tmp_path, monkeypatch):
    candidate = [CandidateConfig("frozen", lambda: FrozenDecisionProvider({"AAPL": "BUY"}))]
    baseline = harness(candidate).run().candidates[0].report.metrics
    observer = Observability(tmp_path)
    monkeypatch.setattr(type(observer.events_path), "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")))
    observed = harness(candidate, observability=observer).run().candidates[0].report.metrics
    assert baseline == observed


def test_ranking_cannot_deploy_or_modify_configuration():
    limits_before = json.dumps(LIMITS, sort_keys=True)
    report = harness([CandidateConfig("A", lambda: FrozenDecisionProvider({"AAPL": "HOLD"}))]).run()
    assert "no deployment performed" in report.recommendation
    assert json.dumps(LIMITS, sort_keys=True) == limits_before
    assert "deploy" not in report.to_dict()["recommendation"].lower() or "no deployment" in report.to_dict()["recommendation"].lower()


def test_evaluation_has_no_order_submission_surface():
    with open(os.path.join(os.path.dirname(__file__), "..", "src", "evaluation.py"), encoding="utf-8") as stream:
        source = stream.read()
    assert "place_stock_order" not in source
    assert "_ProxyOrderExecutor" not in source
