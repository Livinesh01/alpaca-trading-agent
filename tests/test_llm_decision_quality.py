"""Phase 3: LLM decision quality, strict contract, and reliability.

Deterministic evaluation + failure-handling suite. No real LLM or Alpaca calls:
the provider is a scripted stub and market data is canned OHLCV, so every case
here is deterministic and offline.

Covers:
* the canonical structured-decision contract and every rejection rule
* provider-agnostic deterministic generation settings (temperature/seed)
* prompt responsibilities + hygiene (no look-ahead, bounded token budget)
* the 8-case decision-quality evaluation set (consistency, NOT profitability)
* fail-closed handling of every malformed/failed LLM outcome (zero orders)
"""

import json
import os
import re
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import agent.llm as llm_module
from agent.decision_loop import (
    DECISION_INSTRUCTIONS,
    DecisionLoop,
    build_decision_prompt,
)
from agent.llm import LLMResponse
from orchestrator import extract_trade_decisions, validate_trade_decision
from risk_rules import AccountState
from signals import compute_technical_signals

# DEFAULT_GENERATE_KWARGS is a DecisionLoop class attribute; alias for concise
# contract assertions in the tests below.
DEFAULT_GENERATE_KWARGS = DecisionLoop.DEFAULT_GENERATE_KWARGS

SIGNAL_PARAMS = {
    "sma_fast_period": 3,
    "sma_slow_period": 5,
    "rsi_period": 3,
    "rsi_overbought": 70.0,
    "rsi_oversold": 30.0,
    "atr_period": 3,
    "high_volatility_atr_pct": 3.0,
    "momentum_lookback_bars": 3,
    "momentum_flat_threshold_pct": 0.2,
}

LIMITS = {
    "max_position_notional_usd": 2000,
    "max_order_notional_usd": 1000,
    "max_open_positions": 5,
    "max_daily_loss_usd": 500,
    "max_orders_per_run": 3,
    "allow_short_selling": False,
    "allow_options": False,
    "allow_crypto": False,
    "watchlist": ["AAPL"],
    "signal_params": SIGNAL_PARAMS,
}


def make_bars(start=100.0, step=2.0, swing=1.0, count=12):
    """Deterministic OHLCV bars; the last close is the decision-time price."""
    return [
        {"c": start + i * step, "h": start + i * step + swing, "l": start + i * step - swing}
        for i in range(count)
    ]


def make_account(**overrides):
    fields = {
        "cash": 5000.0,
        "buying_power": 5000.0,
        "equity": 25000.0,
        "daily_pnl": 0.0,
        "open_position_count": 0,
        "orders_placed_this_run": 0,
        "existing_position_notional": 0.0,
    }
    fields.update(overrides)
    return AccountState(**fields)


class ScriptedLLM:
    """LLMProvider stub: returns one canned completion or raises one error."""

    def __init__(self, text="", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": dict(kwargs)})
        if self.error is not None:
            raise self.error
        return LLMResponse(text=self.text)


class RecordingExecutor:
    """Executor boundary stand-in; records every order submitted."""

    def __init__(self):
        self.orders = []

    def submit(self, order):
        self.orders.append(order)
        return "faked-ok"


def decision(symbol="AAPL", action="HOLD", confidence=0.6, position_size=0, **extra):
    d = {
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "position_size": position_size,
        "thesis": "deterministic signals justify it",
        "entry_reason": "signal gates aligned",
    }
    d.update(extra)
    return d


def response_json(*decisions):
    return json.dumps({"decisions": list(decisions)})


def run_loop(provider, bars_list, account=None, executor=None, watchlist=None,
             limits=None, price=None):
    """Run DecisionLoop directly against canned data.

    Returns (result, executor, provider) for deterministic assertions.
    """
    account = account or make_account()
    price_c = price if price is not None else float(bars_list[-1]["c"])
    executor = executor or RecordingExecutor()
    loop = DecisionLoop(
        provider=provider,
        fetch_bars=lambda symbol: bars_list,
        fetch_price=lambda symbol: price_c,
        fetch_account=lambda symbol: account,
        executor=executor,
        limits=limits or dict(LIMITS),
        watchlist=watchlist or ["AAPL"],
    )
    return loop.run(), executor, provider


# ---------------------------------------------------------------------------
# 1. Canonical decision schema + rejection rules
# ---------------------------------------------------------------------------


def test_valid_decision_validates_to_canonical_fields():
    out = validate_trade_decision(decision("AAPL", "BUY", 0.8))
    assert set(out) == {
        "symbol",
        "action",
        "confidence",
        "position_size",
        "thesis",
        "entry_reason",
    }
    assert out == {
        "symbol": "AAPL",
        "action": "BUY",
        "confidence": 0.8,
        "position_size": 0,
        "thesis": "deterministic signals justify it",
        "entry_reason": "signal gates aligned",
    }


def test_malformed_json_rejected():
    with pytest.raises(ValueError):
        extract_trade_decisions("AAPL looks strong, buy it", expected_symbols={"AAPL"})


def test_missing_required_field_rejected():
    bad = decision("AAPL", "BUY")
    del bad["thesis"]
    with pytest.raises((TypeError, ValueError)):
        validate_trade_decision(bad)


def test_invalid_action_rejected():
    with pytest.raises(ValueError):
        validate_trade_decision(decision("AAPL", "STRONG BUY"))


def test_confidence_type_or_range_rejected():
    with pytest.raises(ValueError):
        validate_trade_decision(decision("AAPL", "BUY", confidence="high"))
    with pytest.raises(ValueError):
        validate_trade_decision(decision("AAPL", "BUY", confidence=1.5))


def test_nonzero_position_size_rejected_for_hold():
    # The contract requires position_size=0 for HOLD. For BUY/SELL the field is
    # free-form because Python sizing always overrides it (see the loop test).
    with pytest.raises(ValueError):
        validate_trade_decision(decision("AAPL", "HOLD", position_size=1))


def test_unknown_symbol_rejected():
    with pytest.raises(ValueError):
        extract_trade_decisions(
            response_json(decision("TSLA", "BUY")), expected_symbols={"AAPL"}
        )


def test_duplicate_symbol_rejected():
    with pytest.raises(ValueError):
        extract_trade_decisions(
            response_json(decision("AAPL"), decision("AAPL")),
            expected_symbols={"AAPL"},
        )


def test_missing_decision_rejected():
    with pytest.raises(ValueError):
        extract_trade_decisions(
            response_json(decision("AAPL")), expected_symbols={"AAPL", "MSFT"}
        )


def test_unexpected_extra_fields_rejected():
    """The LLM cannot smuggle extra fields (e.g. a quantity hint) past validation."""
    with pytest.raises(ValueError, match="unexpected decision field"):
        validate_trade_decision(
            decision("AAPL", "BUY", qty_hint=999, order_type="market")
        )


def test_extremely_long_reasoning_rejected():
    long_text = " ".join(["verbose"] * 500)
    with pytest.raises(ValueError, match="reasoning"):
        validate_trade_decision(decision("AAPL", "HOLD", thesis=long_text))


def test_watchlist_completeness_positive():
    out = extract_trade_decisions(
        response_json(decision("AAPL"), decision("MSFT", "HOLD")),
        expected_symbols={"AAPL", "MSFT"},
    )
    assert [d["symbol"] for d in out] == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# 2. Deterministic generation + prompt design
# ---------------------------------------------------------------------------


def test_generation_kwargs_are_conservative_and_deterministic():
    assert DEFAULT_GENERATE_KWARGS == {"temperature": 0.2, "max_tokens": 2000, "seed": 7}


def test_deterministic_kwargs_are_forwarded_to_llm():
    provider = ScriptedLLM(text=response_json(decision("AAPL", "HOLD")))
    result, _executor, prov = run_loop(provider, make_bars())
    assert result.success is True
    assert prov.calls
    kwargs = prov.calls[0]["kwargs"]
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 2000
    assert kwargs["seed"] == 7


def test_both_providers_allow_seed_in_chat_params():
    assert "seed" in llm_module.FeatherlessLLMProvider._CHAT_PARAMS
    assert "seed" in llm_module.NVIDIAProvider._CHAT_PARAMS


def test_featherless_forwards_seed_to_openai_client():
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="OK"))],
                usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
    provider = llm_module.FeatherlessLLMProvider(
        api_key="test-key", model="test-model", client=client
    )
    resp = provider.generate("Respond with OK.", temperature=0.2, max_tokens=2000, seed=7)
    assert resp.text == "OK"
    assert captured["seed"] == 7
    assert captured["temperature"] == 0.2
    assert captured["max_tokens"] == 2000


def test_prompt_states_llm_responsibilities_and_limits():
    text = DECISION_INSTRUCTIONS
    assert "invent" in text  # never invent market/account data
    assert "position_size" in text
    assert "HOLD" in text  # prefer HOLD when evidence is weak/conflicting
    assert "place orders" in text  # no order authority
    assert '{"decisions":' in text


def test_prompt_is_single_json_object_output_contract():
    text = DECISION_INSTRUCTIONS
    assert "exactly one JSON object" in text
    assert "first character" in text
    assert "No prose" in text


def test_prompt_sections_separated_and_budget_bounded():
    bars = make_bars()
    sig = compute_technical_signals("AAPL", bars, SIGNAL_PARAMS)
    prompt = build_decision_prompt(
        watchlist=["AAPL"],
        signals_by_symbol={"AAPL": sig},
        prices={"AAPL": float(bars[-1]["c"])},
        account_by_symbol={"AAPL": make_account()},
    )
    assert "WATCHLIST:" in prompt
    assert "ACCOUNT CONTEXT:" in prompt
    assert "MARKET CONTEXT" in prompt
    assert len(prompt) < 3500  # one symbol: well inside the 2000-token budget


def test_prompt_has_no_lookahead_or_future_timestamps():
    bars = make_bars()
    sig = compute_technical_signals("AAPL", bars, SIGNAL_PARAMS)
    last_close = float(bars[-1]["c"])
    prompt = build_decision_prompt(
        watchlist=["AAPL"],
        signals_by_symbol={"AAPL": sig},
        prices={"AAPL": last_close},
        account_by_symbol={"AAPL": make_account()},
    )
    # The only price the LLM sees is the latest HISTORICAL close.
    assert f'"current_price": {last_close}' in prompt
    # No calendar timestamps anywhere -> future information cannot be embedded.
    assert not re.search(r"\d{4}-\d{2}-\d{2}", prompt)


# ---------------------------------------------------------------------------
# 3. Deterministic evaluation set (decision consistency; NOT profitability)
# ---------------------------------------------------------------------------

EVAL_FIXTURES = {
    "strong_bullish": {"bars": make_bars(start=100.0, step=2.0), "action": "BUY"},
    "strong_bearish": {"bars": make_bars(start=122.0, step=-2.0), "action": "SELL"},
    "conflicting": {"bars": make_bars(start=100.0, step=0.05, swing=0.05), "action": "HOLD"},
    "insufficient": {"bars": make_bars(count=2, step=1.0), "action": "HOLD"},
    "high_volatility": {"bars": make_bars(start=100.0, step=0.5, swing=8.0), "action": "HOLD"},
    "weak_evidence": {"bars": make_bars(start=100.0, step=0.3), "action": "HOLD"},
}


def test_eval_signals_are_decision_time_only():
    """Each fixture's signals derive only from historical bars (no look-ahead)."""
    for fixture in EVAL_FIXTURES.values():
        sig = compute_technical_signals("AAPL", fixture["bars"], SIGNAL_PARAMS)
        assert sig["bar_count"] == len(fixture["bars"])
        assert sig["close"] == float(fixture["bars"][-1]["c"])  # last available close


def test_eval_strong_bullish_buy_accepted_with_python_sizing():
    bars = EVAL_FIXTURES["strong_bullish"]["bars"]
    sig = compute_technical_signals("AAPL", bars, SIGNAL_PARAMS)
    assert sig["trend"] == "up" and sig["momentum_state"] == "positive"
    result, executor, _prov = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "BUY", 0.9))), bars
    )
    assert result.success is True
    assert len(executor.orders) == 1
    order = executor.orders[0]
    assert order.symbol == "AAPL" and order.side == "buy"
    max_order = float(LIMITS["max_order_notional_usd"])
    assert order.qty == int(max_order // sig["close"])  # Python, not the LLM


def test_eval_strong_bearish_sell_with_position_close_only():
    bars = EVAL_FIXTURES["strong_bearish"]["bars"]
    sig = compute_technical_signals("AAPL", bars, SIGNAL_PARAMS)
    assert sig["trend"] == "down" and sig["momentum_state"] == "negative"
    result, executor, _prov = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "SELL", 0.85))),
        bars,
        account=make_account(existing_position_notional=800.0),
    )
    assert result.success is True
    assert len(executor.orders) == 1
    order = executor.orders[0]
    assert order.symbol == "AAPL" and order.side == "sell"
    assert order.qty == int(min(800.0, float(LIMITS["max_order_notional_usd"])) // sig["close"])


def test_eval_bearish_without_position_yields_no_order():
    bars = EVAL_FIXTURES["strong_bearish"]["bars"]
    result, executor, _prov = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "SELL", 0.8))), bars
    )
    assert result.success is True
    assert executor.orders == []  # close-only: no position, no short


def test_eval_conflicting_signals_hold_produces_zero_orders():
    bars = EVAL_FIXTURES["conflicting"]["bars"]
    sig = compute_technical_signals("AAPL", bars, SIGNAL_PARAMS)
    assert sig["momentum_state"] == "flat"  # uptrend vs flat momentum = conflicted
    result, executor, _prov = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "HOLD", 0.5))), bars
    )
    assert result.success is True
    assert executor.orders == []


def test_eval_insufficient_data_hold_and_unknown_states():
    bars = EVAL_FIXTURES["insufficient"]["bars"]
    sig = compute_technical_signals("AAPL", bars, SIGNAL_PARAMS)
    assert sig["trend"] == "unknown" and sig["momentum_state"] == "unknown"
    result, executor, _prov = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "HOLD", 0.4))), bars
    )
    assert result.success is True
    assert executor.orders == []


def test_eval_high_volatility_hold():
    bars = EVAL_FIXTURES["high_volatility"]["bars"]
    sig = compute_technical_signals("AAPL", bars, SIGNAL_PARAMS)
    assert sig["volatility_state"] == "high"
    result, executor, _prov = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "HOLD", 0.5))), bars
    )
    assert result.success is True
    assert executor.orders == []


def test_eval_no_position_weak_evidence_hold():
    bars = EVAL_FIXTURES["weak_evidence"]["bars"]
    result, executor, _prov = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "HOLD", 0.45))), bars
    )
    assert result.success is True
    assert executor.orders == []


def test_unknown_symbol_from_llm_fails_closed():
    result, executor, _prov = run_loop(
        ScriptedLLM(text=response_json(decision("NVDA", "BUY"))), make_bars()
    )
    assert result.success is False
    assert executor.orders == []
    assert "decisions invalid" in result.error


# ---------------------------------------------------------------------------
# 4. Failure handling: every LLM failure mode fails closed with zero orders
# ---------------------------------------------------------------------------


def _assert_fails_closed(result, executor, needle=None):
    assert result.success is False
    assert executor.orders == []
    if needle is not None:
        assert needle in result.error


def test_llm_timeout_fails_closed():
    result, executor, _ = run_loop(
        ScriptedLLM(error=TimeoutError("LLM timed out")), make_bars()
    )
    _assert_fails_closed(result, executor, "LLM call failed")


def test_llm_api_error_fails_closed():
    result, executor, _ = run_loop(
        ScriptedLLM(error=RuntimeError("Featherless API error: 500")), make_bars()
    )
    _assert_fails_closed(result, executor, "LLM call failed")


def test_llm_empty_response_fails_closed():
    result, executor, _ = run_loop(ScriptedLLM(text=""), make_bars())
    _assert_fails_closed(result, executor, "empty")


def test_llm_malformed_json_fails_closed():
    result, executor, _ = run_loop(
        ScriptedLLM(text="AAPL is a strong buy right now, no doubts."), make_bars()
    )
    _assert_fails_closed(result, executor, "decisions invalid")


def test_llm_incomplete_json_fails_closed():
    result, executor, _ = run_loop(
        ScriptedLLM(text='{"decisions": [{"symbol": "AAPL", "action": "BU'),
        make_bars(),
    )
    _assert_fails_closed(result, executor, "decisions invalid")


def test_llm_invalid_action_fails_closed():
    result, executor, _ = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "STRONG BUY"))), make_bars()
    )
    _assert_fails_closed(result, executor, "decisions invalid")


def test_llm_unknown_symbol_fails_closed():
    result, executor, _ = run_loop(
        ScriptedLLM(text=response_json(decision("TSLA", "BUY"))), make_bars()
    )
    _assert_fails_closed(result, executor, "decisions invalid")


def test_llm_duplicate_symbol_fails_closed():
    result, executor, _ = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL"), decision("AAPL"))), make_bars()
    )
    _assert_fails_closed(result, executor, "decisions invalid")


def test_llm_missing_field_fails_closed():
    bad = decision("AAPL")
    del bad["entry_reason"]
    result, executor, _ = run_loop(ScriptedLLM(text=response_json(bad)), make_bars())
    _assert_fails_closed(result, executor, "decisions invalid")


def test_llm_unexpected_fields_fails_closed():
    result, executor, _ = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "BUY", qty_hint=999))), make_bars()
    )
    _assert_fails_closed(result, executor, "decisions invalid")


def test_llm_extremely_long_response_fails_closed():
    long_thesis = " ".join(["verbose"] * 10000)
    result, executor, _ = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "BUY", thesis=long_thesis))),
        make_bars(),
    )
    _assert_fails_closed(result, executor, "decisions invalid")


def test_llm_refusal_fails_closed():
    result, executor, _ = run_loop(
        ScriptedLLM(text="I cannot provide trading recommendations."), make_bars()
    )
    _assert_fails_closed(result, executor, "decisions invalid")


def test_llm_position_size_never_becomes_order_quantity():
    """A BUY carrying a large LLM position_size still gets the Python-sized order."""
    bars = make_bars()
    price = float(bars[-1]["c"])
    result, executor, _prov = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "BUY", position_size=999))),
        bars,
    )
    assert result.success is True
    assert len(executor.orders) == 1
    order = executor.orders[0]
    max_order = float(LIMITS["max_order_notional_usd"])
    assert order.qty == int(max_order // price)  # Python sizing, not the LLM
    assert order.qty != 999


def test_llm_prompt_grants_no_quantity_or_execution_authority():
    result, executor, prov = run_loop(
        ScriptedLLM(text=response_json(decision("AAPL", "HOLD"))), make_bars()
    )
    assert result.success is True
    prompt = prov.calls[0]["prompt"]
    assert "position_size" in prompt
    assert "deterministic" in DECISION_INSTRUCTIONS
    assert executor.orders == []