import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from memory import (
    HISTORICAL_MARKER,
    HYPOTHETICAL_LABEL,
    MemoryStore,
    evaluate_decision,
    metrics,
    missing_outcome,
)


def decision(**overrides):
    record = {
        "run_id": "run-1",
        "decision_id": "decision-1",
        "timestamp": "2026-09-03T10:00:00+00:00",
        "symbol": "AAPL",
        "action": "BUY",
        "confidence": 0.8,
        "thesis": "trend is aligned",
        "entry_reason": "momentum confirms trend",
        "deterministic_signals": {"trend": "up"},
        "decision_price": 100.0,
        "account_context": {"buying_power": 5000.0},
        "provider": "FakeLLMProvider",
        "model": "test-model",
    }
    record.update(overrides)
    return record


def test_decision_saved_with_ids_and_secrets_rejected(tmp_path):
    store = MemoryStore(tmp_path)
    store.save_decision(decision())
    saved = store.decisions()[0]
    assert saved["run_id"] == "run-1"
    assert saved["decision_id"] == "decision-1"
    with pytest.raises(ValueError, match="secret"):
        store.save_decision(decision(api_key="must-not-persist"))
    assert "must-not-persist" not in store.decisions_path.read_text(encoding="utf-8")


def test_outcome_is_separate_and_duplicate_evaluation_is_ignored(tmp_path):
    store = MemoryStore(tmp_path)
    original = decision()
    store.save_decision(original)
    outcome = evaluate_decision(original, 110.0, horizon="5 minutes")
    assert store.save_outcome(outcome) is True
    duplicate = dict(outcome)
    duplicate["return"] = 0.5
    assert store.save_outcome(duplicate) is False
    assert store.decisions()[0] == original
    assert store.outcomes()[0]["return"] == pytest.approx(0.1)
    assert store.outcomes()[0]["label"] == HYPOTHETICAL_LABEL


def test_buy_sell_and_hold_evaluations_are_deterministic():
    assert evaluate_decision(decision(), 110.0)["return"] == pytest.approx(0.1)
    assert evaluate_decision(decision(action="SELL"), 90.0)["return"] == pytest.approx(0.1)
    hold = evaluate_decision(decision(action="HOLD"), 110.0)
    assert hold["return"] == pytest.approx(0.1)
    assert hold["label"] == HYPOTHETICAL_LABEL


def test_invalid_prices_and_missing_future_data_are_safe():
    with pytest.raises(ValueError):
        evaluate_decision(decision(decision_price=0), 100.0)
    with pytest.raises(ValueError):
        evaluate_decision(decision(), -1)
    missing = missing_outcome("decision-1", horizon="30 minutes")
    assert missing["return"] is None
    assert missing["status"] == "missing"


def test_insufficient_horizon_is_not_evaluated():
    result = evaluate_decision(
        decision(),
        110.0,
        evaluation_timestamp="2026-09-03T10:01:00+00:00",
        horizon="5 minutes",
        horizon_seconds=300,
    )
    assert result["status"] == "insufficient_horizon"
    assert result["return"] is None


def test_historical_retrieval_filters_symbols_limits_and_skips_corruption(tmp_path):
    store = MemoryStore(tmp_path)
    store.save_decision(decision(decision_id="a", symbol="AAPL"))
    store.save_decision(decision(decision_id="m", symbol="MSFT"))
    store.save_decision(decision(decision_id="a", symbol="AAPL", confidence=0.2))
    with store.decisions_path.open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
    records = store.historical("AAPL", limit=5)
    assert len(records) == 1
    assert records[0]["decision_id"] == "a"
    assert store.historical("TSLA") == []
    assert HISTORICAL_MARKER in store.historical_context("AAPL")


def test_empty_memory_and_token_bounded_retrieval(tmp_path):
    store = MemoryStore(tmp_path)
    assert store.historical("AAPL") == []
    for index in range(10):
        store.save_decision(decision(decision_id=f"d-{index}"))
    assert len(store.historical("AAPL", limit=3)) == 3


def test_metrics_keep_hypothetical_and_execution_concepts_separate():
    values = metrics(
        [decision(action="BUY"), decision(decision_id="d2", action="HOLD", accepted=False)],
        [evaluate_decision(decision(), 110.0), missing_outcome("d2")],
        [{"decision_id": "decision-1", "submitted": True}],
    )
    assert values["total_decisions"] == 2
    assert values["BUY_decisions"] == 1
    assert values["HOLD_decisions"] == 1
    assert values["executed_orders"] == 1
    assert values["hypothetical_evaluated_decisions"] == 1
    assert values["missing_evaluations"] == 1
    assert values["average_hypothetical_return"] == pytest.approx(0.1)


def test_historical_context_contains_no_future_outcome_before_evaluation(tmp_path):
    store = MemoryStore(tmp_path)
    store.save_decision(decision())
    context = store.historical_context("AAPL")
    assert HISTORICAL_MARKER in context
    assert "outcome" not in context
    store.save_outcome(evaluate_decision(decision(), 105.0))
    assert "outcome" in store.historical_context("AAPL")


def test_records_are_inspectable_jsonl(tmp_path):
    store = MemoryStore(tmp_path)
    store.save_decision(decision())
    assert json.loads(store.decisions_path.read_text(encoding="utf-8"))[
        "symbol"
    ] == "AAPL"
