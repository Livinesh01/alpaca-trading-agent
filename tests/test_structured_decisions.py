"""Tests for structured trade-decision parsing/validation in orchestrator.py."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestrator import extract_trade_decisions, validate_trade_decision


def _decision(**overrides):
    d = {
        "symbol": "MSFT",
        "action": "BUY",
        "confidence": 0.7,
        "thesis": "trend up, RSI neutral, momentum positive",
        "position_size": 1,
        "entry_reason": "all long-entry gates true",
    }
    d.update(overrides)
    return d


def _ndjson(payload):
    """Wrap a payload as a cline --json `run_result` line (text is a JSON string)."""
    return json.dumps({"type": "run_result", "text": json.dumps(payload)})


def _block(*decisions):
    return {"decisions": list(decisions)}


def test_validate_accepts_valid_buy():
    assert validate_trade_decision(_decision())["symbol"] == "MSFT"


def test_validate_accepts_boundary_confidence():
    assert validate_trade_decision(_decision(confidence=0.0))["confidence"] == 0.0
    assert validate_trade_decision(_decision(confidence=1.0))["confidence"] == 1.0


def test_validate_accepts_hold_with_zero_position():
    d = validate_trade_decision(_decision(action="HOLD", position_size=0))
    assert d["action"] == "HOLD"
    assert d["position_size"] == 0


def test_validate_rejects_hold_with_nonzero_position():
    with pytest.raises(ValueError, match="HOLD must use position_size 0"):
        validate_trade_decision(_decision(action="HOLD", position_size=1))


def test_validate_rejects_unknown_action():
    with pytest.raises(ValueError, match="action"):
        validate_trade_decision(_decision(action="HODL"))


def test_validate_rejects_confidence_out_of_range():
    with pytest.raises(ValueError, match="confidence"):
        validate_trade_decision(_decision(confidence=1.5))
    with pytest.raises(ValueError, match="confidence"):
        validate_trade_decision(_decision(confidence=-0.1))


def test_validate_rejects_non_numeric_confidence():
    with pytest.raises(ValueError, match="confidence"):
        validate_trade_decision(_decision(confidence="high"))


def test_validate_rejects_float_position_size():
    with pytest.raises(TypeError, match="position_size"):
        validate_trade_decision(_decision(position_size=1.5))


def test_validate_rejects_negative_position_size():
    with pytest.raises(ValueError, match="position_size"):
        validate_trade_decision(_decision(position_size=-1))


def test_validate_rejects_empty_symbol():
    with pytest.raises(ValueError, match="symbol"):
        validate_trade_decision(_decision(symbol=""))


def test_validate_rejects_missing_thesis():
    with pytest.raises(TypeError, match="thesis"):
        validate_trade_decision(_decision(thesis=None))


def test_extract_from_ndjson_text_field():
    decisions = extract_trade_decisions(
        _ndjson(_block(_decision(), _decision(symbol="AAPL", action="HOLD", position_size=0)))
    )
    assert [d["symbol"] for d in decisions] == ["MSFT", "AAPL"]


def test_extract_from_fenced_markdown_block():
    text = f"Verdicts below.\n```json\n{json.dumps(_block(_decision()))}\n```\nDone."
    assert extract_trade_decisions(text)[0]["symbol"] == "MSFT"


def test_extract_normalizes_fields():
    result = extract_trade_decisions(
        _ndjson(_block(_decision(symbol=" msft ", confidence="0.5")))
    )[0]
    assert result["symbol"] == "msft"
    assert result["confidence"] == 0.5


def test_extract_raises_when_no_json_block():
    with pytest.raises(ValueError, match="no JSON decisions block"):
        extract_trade_decisions("no structured output here")


def test_extract_raises_when_decisions_key_missing():
    with pytest.raises(TypeError, match="decisions must be a JSON array"):
        extract_trade_decisions(_ndjson({"foo": 1}))


def test_extract_raises_on_empty_decisions():
    with pytest.raises(ValueError, match="decisions array is empty"):
        extract_trade_decisions(_ndjson(_block()))


def test_extract_rejects_duplicate_symbol():
    with pytest.raises(ValueError, match="duplicate decision"):
        extract_trade_decisions(_ndjson(_block(_decision(), _decision(position_size=2))))


def test_extract_requires_all_expected_symbols():
    with pytest.raises(ValueError, match="missing decisions"):
        extract_trade_decisions(_ndjson(_block(_decision())), expected_symbols={"MSFT", "AAPL"})


def test_extract_rejects_unknown_symbol():
    block = _block(_decision(), _decision(symbol="GME", action="HOLD", position_size=0))
    with pytest.raises(ValueError, match="unknown symbols"):
        extract_trade_decisions(_ndjson(block), expected_symbols={"MSFT"})