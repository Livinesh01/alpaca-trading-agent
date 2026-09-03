"""Integration tests: LLMProvider -> DecisionLoop -> risk_rules -> executor.

Every LLM and Alpaca/MCP boundary is mocked (ScriptedProvider /
RecordingExecutor / injected fake OpenAI clients) — no real network calls.
Focus: HOLD produces no order, malformed / unknown-symbol LLM output never
reaches the executor, and final quantities are computed in Python under the
existing risk limits, never from the LLM's position_size.
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent.decision_loop import (
    DECISION_INSTRUCTIONS,
    DecisionLoop,
    build_decision_prompt,
    default_limits,
    deterministic_quantity,
)
from agent.llm import (
    FakeLLMProvider,
    FeatherlessLLMProvider,
    LLMProvider,
    LLMResponse,
    NVIDIAProvider,
)
from memory import MemoryStore
from risk_rules import AccountState
from signals import compute_technical_signals

# --------------------------------------------------------------- test fakes

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

TEST_LIMITS = {
    "max_position_notional_usd": 2000,
    "max_order_notional_usd": 1000,
    "max_open_positions": 5,
    "max_daily_loss_usd": 500,
    "max_orders_per_run": 3,
    "allow_short_selling": False,
    "allow_options": False,
    "allow_crypto": False,
    "watchlist": ["AAPL", "MSFT"],
    "signal_params": SIGNAL_PARAMS,
}

WATCHLIST = ["AAPL", "MSFT"]


def make_bars(count=12, start=100.0, step=1.0):
    """Deterministic uptrend bars so compute_technical_signals() gets real inputs."""
    return [
        {"c": start + i * step, "h": start + i * step + 1.0, "l": start + i * step - 1.0}
        for i in range(count)
    ]


def make_account(**overrides):
    fields = {
        "cash": 5000.0,
        "buying_power": 5000.0,
        "equity": 25000.0,
        "daily_pnl": 0.0,
        "open_position_count": 1,
        "orders_placed_this_run": 0,
        "existing_position_notional": 0.0,
    }
    fields.update(overrides)
    return AccountState(**fields)


class ScriptedProvider:
    """LLMProvider stub returning one canned completion; records generate() calls."""

    def __init__(self, text="", error=None):
        self.text = text
        self.error = error
        self.requests = []

    def generate(self, prompt, **kwargs):
        self.requests.append({"prompt": prompt, "kwargs": dict(kwargs)})
        if self.error is not None:
            raise self.error
        return LLMResponse(text=self.text)


def llm_decision(symbol, action="HOLD", confidence=0.5, position_size=0, **extra):
    d = {
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "position_size": position_size,
        "thesis": "deterministic signals justify it",
        "entry_reason": "gates aligned",
    }
    d.update(extra)
    return d


def llm_response_json(*decisions):
    return json.dumps({"decisions": list(decisions)})


class RecordingExecutor:
    """Stands in for the risk-guard MCP proxy / Alpaca order boundary."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def submit(self, order):
        self.calls.append(order)
        if self.error is not None:
            raise self.error
        return {"order_id": "paper-1"}


def make_loop(
    provider=None,
    response_text="",
    provider_error=None,
    watchlist=None,
    limits=None,
    accounts=None,
    prices=None,
    executor=None,
    generate_kwargs=None,
    memory_store=None,
):
    symbols = list(watchlist if watchlist is not None else WATCHLIST)
    bars = {s: make_bars() for s in symbols}
    accounts = accounts or {s: make_account() for s in symbols}
    prices = prices or {s: 100.0 for s in symbols}
    prov = provider if provider is not None else ScriptedProvider(response_text, provider_error)
    ex = executor if executor is not None else RecordingExecutor()
    loop = DecisionLoop(
        provider=prov,
        fetch_bars=lambda s: bars[s],
        fetch_price=lambda s: prices[s],
        fetch_account=lambda s: accounts[s],
        executor=ex,
        limits=TEST_LIMITS if limits is None else limits,
        watchlist=symbols,
        generate_kwargs=generate_kwargs,
        memory_store=memory_store,
    )
    return loop, prov, ex


# --------------------------------------------- deterministic_quantity (pure)

def test_qty_hold_is_zero():
    assert deterministic_quantity("HOLD", 100.0, make_account(), TEST_LIMITS) == 0


def test_qty_unknown_action_is_zero():
    assert deterministic_quantity("SCALE", 100.0, make_account(), TEST_LIMITS) == 0


def test_qty_requires_positive_price():
    acct = make_account()
    assert deterministic_quantity("BUY", None, acct, TEST_LIMITS) == 0
    assert deterministic_quantity("BUY", 0.0, acct, TEST_LIMITS) == 0
    assert deterministic_quantity("SELL", -5.0, acct, TEST_LIMITS) == 0


def test_qty_buy_capped_by_order_notional():
    # min($1000 order cap, $2000 headroom, $5000 BP) // $3.00 -> 333 shares ($999)
    assert deterministic_quantity("BUY", 3.0, make_account(), TEST_LIMITS) == 333


def test_qty_buy_capped_by_position_headroom():
    acct = make_account(existing_position_notional=1900.0)
    # headroom $2000-$1900 = $100 // $30 -> 3 shares
    assert deterministic_quantity("BUY", 30.0, acct, TEST_LIMITS) == 3


def test_qty_buy_capped_by_buying_power():
    acct = make_account(buying_power=50.0)
    assert deterministic_quantity("BUY", 40.0, acct, TEST_LIMITS) == 1


def test_qty_buy_zero_when_no_headroom():
    acct = make_account(existing_position_notional=2000.0, buying_power=500.0)
    assert deterministic_quantity("BUY", 100.0, acct, TEST_LIMITS) == 0


def test_qty_sell_capped_at_position_and_order_notional():
    acct = make_account(existing_position_notional=500.0)
    assert deterministic_quantity("SELL", 100.0, acct, TEST_LIMITS) == 5
    tight = dict(TEST_LIMITS, max_order_notional_usd=200)
    assert deterministic_quantity("SELL", 100.0, acct, tight) == 2


def test_qty_sell_without_position_is_zero():
    # close-only sells: nothing held -> no order -> short selling never generated
    assert deterministic_quantity("SELL", 100.0, make_account(), TEST_LIMITS) == 0


# ------------------------------------------------- full loop (LLM mocked)

ALL_HOLD = llm_response_json(llm_decision("AAPL"), llm_decision("MSFT"))


def test_hold_produces_no_order():
    loop, prov, ex = make_loop(response_text=ALL_HOLD)
    result = loop.run()
    assert result.success is True
    assert result.orders_submitted == 0
    assert ex.calls == []
    assert all(o.order is None and o.submitted is False for o in result.outcomes)
    assert all(o.risk.allowed for o in result.outcomes)
    assert len(prov.requests) == 1  # exactly one LLM call per run


@pytest.mark.parametrize("llm_size", [0, 1, 999])
def test_final_quantity_comes_from_python_not_llm(llm_size):
    text = llm_response_json(
        llm_decision("AAPL", "BUY", position_size=llm_size), llm_decision("MSFT")
    )
    loop, _, ex = make_loop(response_text=text)
    result = loop.run()
    assert result.success is True
    assert result.orders_submitted == 1
    buy = ex.calls[0]
    assert buy.symbol == "AAPL" and buy.side == "buy" and buy.order_type == "market"
    assert buy.asset_class == "us_equity"
    assert buy.qty == 10  # $1000 order cap // $100 price — LLM's size ignored
    outcome = next(o for o in result.outcomes if o.symbol == "AAPL")
    assert outcome.requested_qty == 10 and outcome.risk.allowed is True


def test_sell_flow_closes_existing_position_only():
    accounts = {
        "AAPL": make_account(existing_position_notional=500.0),
        "MSFT": make_account(),
    }
    text = llm_response_json(llm_decision("AAPL", "SELL"), llm_decision("MSFT"))
    loop, _, ex = make_loop(response_text=text, accounts=accounts)
    result = loop.run()
    assert result.orders_submitted == 1
    sell = ex.calls[0]
    assert sell.side == "sell" and sell.qty == 5


def test_sell_without_position_never_reaches_executor():
    text = llm_response_json(llm_decision("AAPL", "SELL"), llm_decision("MSFT"))
    loop, _, ex = make_loop(response_text=text)
    result = loop.run()
    assert result.success is True
    assert result.orders_submitted == 0 and ex.calls == []


def test_risk_rules_are_final_authority():
    # daily loss cap breached -> risk_guard must veto even a well-sized BUY
    accounts = {"AAPL": make_account(daily_pnl=-500.0), "MSFT": make_account()}
    text = llm_response_json(llm_decision("AAPL", "BUY"), llm_decision("MSFT"))
    loop, _, ex = make_loop(response_text=text, accounts=accounts)
    result = loop.run()
    assert result.success is True
    assert result.orders_submitted == 0 and ex.calls == []
    outcome = next(o for o in result.outcomes if o.symbol == "AAPL")
    assert outcome.order is not None  # order built...
    assert outcome.submitted is False  # ...but blocked before the executor
    assert "Daily loss cap" in outcome.risk.reason


def test_per_run_order_cap_enforced():
    limits = dict(TEST_LIMITS, max_orders_per_run=1)
    text = llm_response_json(llm_decision("AAPL", "BUY"), llm_decision("MSFT", "BUY"))
    loop, _, _ = make_loop(response_text=text, limits=limits)
    result = loop.run()
    assert result.orders_submitted == 1
    second = next(o for o in result.outcomes if o.symbol == "MSFT")
    assert second.risk.allowed is False
    assert "Per-run order cap" in second.risk.reason


def test_unknown_llm_symbol_fails_closed():
    text = llm_response_json(
        llm_decision("AAPL", "BUY"), llm_decision("MSFT"), llm_decision("GME", "BUY")
    )
    loop, _, ex = make_loop(response_text=text)
    result = loop.run()
    assert result.success is False
    assert "unknown symbols" in result.error
    assert result.orders_submitted == 0 and ex.calls == []


def test_missing_llm_symbol_fails_closed():
    text = llm_response_json(llm_decision("AAPL", "BUY"))  # MSFT decision absent
    loop, _, ex = make_loop(response_text=text)
    result = loop.run()
    assert result.success is False
    assert "missing decisions" in result.error
    assert result.orders_submitted == 0 and ex.calls == []


def test_malformed_llm_output_fails_closed():
    loop, _, ex = make_loop(response_text="AAPL looks great, I would buy some.")
    result = loop.run()
    assert result.success is False
    assert "decisions invalid" in result.error
    assert result.orders_submitted == 0 and ex.calls == []


def test_llm_exception_fails_closed():
    loop, prov, ex = make_loop(provider_error=RuntimeError("provider down"))
    result = loop.run()
    assert result.success is False
    assert "LLM call failed" in result.error
    assert result.decisions == []
    assert prov.requests and ex.calls == []


def test_empty_llm_response_fails_closed():
    loop, _, ex = make_loop(response_text="   ")
    result = loop.run()
    assert result.success is False
    assert "empty response" in result.error
    assert result.orders_submitted == 0 and ex.calls == []


def test_market_data_failure_fails_closed():
    loop, prov, ex = make_loop()

    def bad_bars(symbol):
        if symbol == "MSFT":
            raise ValueError("no bars available")
        return make_bars()

    loop.fetch_bars = bad_bars
    result = loop.run()
    assert result.success is False
    assert "market data failure for MSFT" in result.error
    assert prov.requests == [] and ex.calls == []  # LLM never even called


def test_prompt_contains_signals_and_account_context():
    loop, prov, _ = make_loop(response_text=ALL_HOLD)
    loop.run()
    prompt = prov.requests[0]["prompt"]
    assert DECISION_INSTRUCTIONS in prompt
    assert '"AAPL"' in prompt and '"MSFT"' in prompt
    assert '"cash": 5000.0' in prompt and '"buying_power": 5000.0' in prompt
    assert '"current_price": 100.0' in prompt
    assert '"trend": "up"' in prompt  # deterministic technical signals fed to LLM
    assert '"existing_position_notional"' in prompt
    assert '"daily_pnl": 0.0' in prompt
    assert "position_size must be 0" in prompt  # LLM is told never to size orders


def test_malformed_memory_cannot_change_quantity_or_authorize_order(tmp_path):
    store = MemoryStore(tmp_path)
    store.decisions_path.parent.mkdir(parents=True, exist_ok=True)
    store.decisions_path.write_text("malformed\n", encoding="utf-8")
    text = llm_response_json(
        llm_decision("AAPL", "BUY", position_size=999), llm_decision("MSFT")
    )
    loop, prov, ex = make_loop(response_text=text, memory_store=store)
    result = loop.run()
    assert result.orders_submitted == 1
    assert ex.calls[0].qty == 10
    assert "HISTORICAL / INFORMATIONAL ONLY" in prov.requests[0]["prompt"]


def test_generate_kwargs_forwarded_with_safe_defaults():
    loop, prov, _ = make_loop(response_text=ALL_HOLD)
    loop.run()
    kwargs = prov.requests[0]["kwargs"]
    assert kwargs["temperature"] == 0.2 and kwargs["max_tokens"] == 2000

    loop2, prov2, _ = make_loop(response_text=ALL_HOLD, generate_kwargs={"temperature": 0.1})
    loop2.run()
    assert prov2.requests[0]["kwargs"]["temperature"] == 0.1
    assert prov2.requests[0]["kwargs"]["max_tokens"] == 2000


def test_executor_object_and_callable_boundaries():
    text = llm_response_json(llm_decision("AAPL", "BUY"), llm_decision("MSFT", "BUY"))
    loop, _, ex = make_loop(response_text=text)  # object with .submit()
    result = loop.run()
    assert result.orders_submitted == 2
    assert [o.symbol for o in ex.calls] == ["AAPL", "MSFT"]

    calls = []
    loop2, _, _ = make_loop(response_text=text, executor=calls.append)  # plain callable
    result2 = loop2.run()
    assert result2.orders_submitted == 2 and len(calls) == 2


def test_executor_failure_recorded_not_raised():
    text = llm_response_json(llm_decision("AAPL", "BUY"), llm_decision("MSFT", "BUY"))
    ex = RecordingExecutor(error=RuntimeError("alpaca down"))
    loop, _, _ = make_loop(response_text=text, executor=ex)
    result = loop.run()
    assert result.success is True  # run completes; failures recorded per outcome
    assert len(ex.calls) == 2  # both orders reached the executor; both failed there
    outcome = next(o for o in result.outcomes if o.symbol == "AAPL")
    assert outcome.submitted is False and "alpaca down" in outcome.executor_error
    assert outcome.risk.allowed is True
    assert result.orders_submitted == 0


# ------------------------------- real provider classes (OpenAI SDK mocked)

BUY_TEXT = llm_response_json(llm_decision("AAPL", "BUY"), llm_decision("MSFT"))


class MockCompletions:
    def __init__(self, text):
        self.text = text
        self.create_calls = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        message = SimpleNamespace(content=self.text)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


class MockOpenAIClient:
    def __init__(self, text):
        self.chat = SimpleNamespace(completions=MockCompletions(text))


@pytest.mark.parametrize(
    "provider_cls, model, api_key",
    [
        (FeatherlessLLMProvider, "meta-llama/test-model", "featherless-test-key"),
        (NVIDIAProvider, "deepseek-ai/deepseek-v4-pro-0813", "nvidia-test-key"),
    ],
)
def test_featherless_and_nvidia_providers_drive_the_loop(provider_cls, model, api_key):
    client = MockOpenAIClient(BUY_TEXT)
    provider = provider_cls(api_key=api_key, model=model, client=client)
    loop, _, ex = make_loop(provider=provider)
    result = loop.run()
    assert result.success is True and result.orders_submitted == 1
    assert ex.calls[0].symbol == "AAPL" and ex.calls[0].qty == 10
    call = client.chat.completions.create_calls[0]
    assert call["model"] == model
    assert "WATCHLIST" in call["messages"][0]["content"]
    assert call["temperature"] == 0.2 and call["max_tokens"] == 2000
    assert api_key not in json.dumps(call)  # key never leaks into the request


def test_fake_llm_provider_also_accepted():
    bars = {s: make_bars() for s in WATCHLIST}
    accounts = {s: make_account() for s in WATCHLIST}
    prices = {s: 100.0 for s in WATCHLIST}
    signals = {s: compute_technical_signals(s, bars[s], SIGNAL_PARAMS) for s in WATCHLIST}
    prompt = build_decision_prompt(WATCHLIST, signals, prices, accounts)
    provider = FakeLLMProvider({prompt: BUY_TEXT})
    loop = DecisionLoop(
        provider=provider,
        fetch_bars=lambda s: bars[s],
        fetch_price=lambda s: prices[s],
        fetch_account=lambda s: accounts[s],
        executor=RecordingExecutor(),
        limits=TEST_LIMITS,
        watchlist=WATCHLIST,
    )
    result = loop.run()
    assert result.success is True and result.orders_submitted == 1


def test_all_provider_implementations_satisfy_protocol():
    assert isinstance(ScriptedProvider(), LLMProvider)
    assert isinstance(FakeLLMProvider(), LLMProvider)
    assert isinstance(
        FeatherlessLLMProvider(api_key="k", model="m", client=MockOpenAIClient("")),
        LLMProvider,
    )
    assert isinstance(
        NVIDIAProvider(api_key="k", model="m", client=MockOpenAIClient("")),
        LLMProvider,
    )


# ----------------------------------------------------------- config wiring

def test_default_limits_come_from_strategy_yaml():
    limits = default_limits()
    assert limits["watchlist"] == ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"]
    # risk values are read from config/strategy.yaml, never redefined here
    assert limits["max_order_notional_usd"] == 1000
    assert limits["max_position_notional_usd"] == 2000
    assert limits["max_daily_loss_usd"] == 500
    assert limits["max_orders_per_run"] == 3
    assert limits["allow_short_selling"] is False
    assert limits["allow_options"] is False
    assert limits["allow_crypto"] is False
    assert limits["signal_params"]["sma_slow_period"] == 50


def test_cline_orchestrator_path_untouched():
    import orchestrator

    assert callable(orchestrator.run_once)  # existing entry point intact
    assert callable(orchestrator.extract_trade_decisions)  # same validator reused


# ------------------------------- truncation / output-budget regression tests

FIVE = ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"]
FIVE_LIMITS = dict(TEST_LIMITS, watchlist=FIVE)


def test_truncated_json_fails_closed():
    """Real-run regression: reply cut off before the JSON object closes."""
    truncated = '{"decisions": [{"symbol": "AAPL", "action": "BUY", "confidence": 0.7,'
    loop, _, ex = make_loop(response_text=truncated, watchlist=FIVE, limits=FIVE_LIMITS)
    result = loop.run()
    assert result.success is False
    assert result.decisions == []  # nothing partially parsed
    assert result.orders_submitted == 0
    assert ex.calls == []  # nothing reached the executor
    assert result.error is not None


def test_truncated_block_with_complete_prefix_decisions_never_executes():
    """Three of five decisions fully formed, then EOF: no partial execution,
    no invented decisions for the missing symbols."""
    partial = llm_response_json(
        llm_decision("AAPL", "BUY"), llm_decision("MSFT"), llm_decision("NVDA")
    )
    truncated = partial[:-40]  # cut mid-object before the array/object close
    assert truncated.startswith('{"decisions": [')  # valid-looking prefix
    loop, _, ex = make_loop(response_text=truncated, watchlist=FIVE, limits=FIVE_LIMITS)
    result = loop.run()
    assert result.success is False
    assert result.decisions == []
    assert result.orders_submitted == 0
    assert ex.calls == []


@pytest.mark.parametrize(
    "piece",
    [
        '{"decisions": [',
        '{"decisions": ',
        '{"decisions"',
        '{"decisions": [{"symbol": "AAPL"}]',
    ],
)
def test_other_truncated_shapes_fail_closed(piece):
    loop, _, ex = make_loop(response_text=piece, watchlist=FIVE, limits=FIVE_LIMITS)
    result = loop.run()
    assert result.success is False
    assert result.orders_submitted == 0
    assert ex.calls == []


def test_complete_five_symbol_decision_block_validates():
    """A full, closed 5-symbol block parses, validates, and executes one BUY."""
    text = llm_response_json(
        llm_decision("AAPL", "BUY", confidence=0.8),
        llm_decision("MSFT"),
        llm_decision("NVDA"),
        llm_decision("SPY"),
        llm_decision("TSLA"),
    )
    loop, prov, ex = make_loop(response_text=text, watchlist=FIVE, limits=FIVE_LIMITS)
    result = loop.run()
    assert result.success is True
    assert len(result.decisions) == 5
    assert [o.symbol for o in result.outcomes] == FIVE  # one outcome per symbol
    assert result.orders_submitted == 1
    assert ex.calls[0].symbol == "AAPL" and ex.calls[0].qty == 10  # Python-sized
    assert len(prov.requests) == 1  # still exactly one LLM call per run


def test_output_contract_demands_json_only_reply():
    """The prompt enforces a JSON-only reply with bounded fields (truncation fix)."""
    prompt = build_decision_prompt(
        WATCHLIST,
        {},
        {"AAPL": 100.0, "MSFT": 100.0},
        {s: make_account() for s in WATCHLIST},
    )
    assert 'must be "{" and the last must be "}"' in prompt
    assert "No prose before or after the JSON" in prompt
    assert "at most 12 words" in prompt
    assert "never" in prompt and "partial object" in prompt


