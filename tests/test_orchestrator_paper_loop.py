"""Orchestrator ↔ DecisionLoop wiring tests (PAPER only).


Every boundary is mocked: the LLM provider is a scripted stub and the
risk-guard MCP proxy session is a canned fake, so no real LLM, Alpaca, MCP,
or network call ever happens. Focus: paper gate, market guard, HOLD → zero
orders, deterministic sizing through the risk-guard executor, fail-closed on
invalid/failed LLM output, provider selection, and API-key redaction.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import agent.decision_loop as decision_loop_module
import orchestrator
from agent.llm import FeatherlessLLMProvider, LLMResponse, NVIDIAProvider
from risk_rules import OrderRequest

CONFIG = {"watchlist": ["AAPL"], "schedule": {"skip_if_market_closed": True, "timezone": "America/New_York"}}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Keep every test's env explicit; nothing inherits a live backend setting."""
    for var in ("PAPER_TRADING", "AGENT_BACKEND", "LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)


def make_bars(count=12, start=100.0, step=1.0):
    """Deterministic uptrend bars so compute_technical_signals() gets real inputs."""
    return [
        {"c": start + i * step, "h": start + i * step + 1.0, "l": start + i * step - 1.0}
        for i in range(count)
    ]


def make_limits(**overrides):
    """Real strategy.yaml risk values (never weakened) + test-sized signal params."""
    limits = decision_loop_module.default_limits()
    params = {
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
    params.update(limits.get("signal_params", {}) or {})
    limits["signal_params"] = params
    limits.update(overrides)
    return limits


class ScriptedProvider:
    """LLMProvider stub; returns one canned completion or raises."""

    def __init__(self, text="", error=None):
        self.text = text
        self.error = error
        self.requests = []

    def generate(self, prompt, **kwargs):
        self.requests.append({"prompt": prompt, "kwargs": dict(kwargs)})
        if self.error is not None:
            raise self.error
        return LLMResponse(text=self.text)


def llm_decision(symbol, action="HOLD", confidence=0.5, position_size=0):
    return {
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "position_size": position_size,
        "thesis": "deterministic signals justify it",
        "entry_reason": "gates aligned",
    }


def llm_response_json(*decisions):
    return json.dumps({"decisions": list(decisions)})


class FakeProxySession:
    """Canned _RiskGuardProxySession stand-in: records tool + order calls."""

    def __init__(self, *, account=None, positions=None, price=100.0, fail_tools=()):
        self.account = json.dumps(
            account
            if account is not None
            else {"cash": 5000.0, "buying_power": 5000.0, "equity": 25000.0, "last_equity": 25000.0}
        )
        self.positions = json.dumps(positions if positions is not None else [])
        now = datetime.now(timezone.utc).isoformat()
        self.latest_trade = json.dumps({"trades": {"AAPL": {"p": price, "t": now}}})
        self.bars = json.dumps({"bars": {"AAPL": make_bars()}})
        self.fail_tools = set(fail_tools)
        self.tool_calls = []
        self.order_calls = []
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def call(self, tool, arguments, timeout=None):
        self.tool_calls.append((tool, dict(arguments)))
        if tool in self.fail_tools:
            raise RuntimeError(f"MCP tool {tool!r} failed: boom")
        if tool == "place_stock_order":
            self.order_calls.append(dict(arguments))
            return json.dumps({"id": "test-order-1", "status": "filled"})
        if tool == "get_stock_bars":
            return self.bars
        if tool == "get_stock_latest_trade":
            return self.latest_trade
        if tool == "get_account_info":
            return self.account
        if tool == "get_all_positions":
            return self.positions
        raise AssertionError(f"unexpected tool call {tool}")


class FailingStartSession(FakeProxySession):
    def start(self):
        raise RuntimeError("spawn failed")


class JournalRecorder:
    def __init__(self):
        self.starts = []
        self.reasonings = []
        self.decisions = []
        self.ends = []

    def log_run_start(self, summary):
        self.starts.append(summary)

    def log_reasoning(self, text):
        self.reasonings.append(text)

    def log_order_decision(self, symbol, side, qty, allowed, reason):
        self.decisions.append(
            {"symbol": symbol, "side": side, "qty": qty, "allowed": allowed, "reason": reason}
        )

    def log_run_end(self, summary):
        self.ends.append(summary)


def run_paper(
    monkeypatch,
    provider,
    session,
    *,
    config=None,
    limits=None,
    market_open=True,
    force=False,
    dry_run=False,
    factory_calls=None,
):
    """Run orchestrator.run_once() with every boundary injected/mocked."""
    monkeypatch.setattr(orchestrator, "load_config", lambda: dict(config or CONFIG))
    monkeypatch.setattr(orchestrator, "market_likely_open", lambda cfg: market_open)

    def _factory():
        if factory_calls is not None:
            factory_calls.append(True)
        return provider

    monkeypatch.setattr(orchestrator, "build_llm_provider", _factory)
    monkeypatch.setattr(orchestrator, "_RiskGuardProxySession", lambda *a, **k: session)
    if limits is not None:
        monkeypatch.setattr(decision_loop_module, "default_limits", lambda: dict(limits))
    recorder = JournalRecorder()
    for name in ("log_run_start", "log_reasoning", "log_order_decision", "log_run_end"):
        monkeypatch.setattr(orchestrator.journal, name, getattr(recorder, name))
    rc = orchestrator.run_once(force=force, dry_run=dry_run)
    return rc, recorder


# ------------------------------------------------------- 1. PAPER mode required


@pytest.mark.parametrize("raw", [None, "", "false", "no", "off", "0"])
def test_paper_gate_fails_closed_when_not_enabled(monkeypatch, raw):
    if raw is not None:
        monkeypatch.setenv("PAPER_TRADING", raw)
    session = FakeProxySession()
    factory_calls = []
    rc, recorder = run_paper(monkeypatch, ScriptedProvider(), session, factory_calls=factory_calls)
    assert rc == 1
    assert session.started is False  # proxy subprocess never spawned
    assert factory_calls == []  # provider/LLM never built
    assert session.order_calls == []
    assert recorder.ends and recorder.ends[0].startswith("Run failed")


def test_force_bypasses_market_guard_but_not_paper_gate(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "not-true")
    rc, _ = run_paper(
        monkeypatch, ScriptedProvider(), FakeProxySession(), market_open=False, force=True
    )
    assert rc == 1  # --force skips the market guard only; the paper gate stands


# ------------------------------------------------- 2. market guard preserved


def test_market_closed_skips_agent_entirely(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    session = FakeProxySession()
    factory_calls = []
    rc, _ = run_paper(
        monkeypatch, ScriptedProvider(), session, market_open=False, factory_calls=factory_calls
    )
    assert rc == 0
    assert factory_calls == []  # LLM provider never consulted
    assert session.started is False and session.order_calls == []


# ------------------------------------------------------- 3. HOLD → zero orders


def test_hold_creates_zero_orders(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    provider = ScriptedProvider(llm_response_json(llm_decision("AAPL", "HOLD")))
    session = FakeProxySession()
    rc, recorder = run_paper(monkeypatch, provider, session, limits=make_limits())
    assert rc == 0
    assert session.order_calls == []
    assert recorder.ends and "0 order(s) submitted" in recorder.ends[0]
    assert provider.requests  # the LLM WAS called with the signal/account prompt


# ------------------------- 4/5. valid BUY → deterministic qty via risk guard


def test_valid_buy_submits_deterministic_qty_via_risk_guard(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    provider = ScriptedProvider(llm_response_json(llm_decision("AAPL", "BUY", position_size=0)))
    session = FakeProxySession()
    rc, recorder = run_paper(monkeypatch, provider, session, limits=make_limits())
    assert rc == 0
    # Real strategy.yaml cap ($1000/order) ÷ $100 price — NOT the LLM's size.
    # qty is a string on the MCP wire (upstream schema: qty: Optional[str]).
    assert session.order_calls == [{"symbol": "AAPL", "side": "buy", "qty": "10"}]
    assert recorder.decisions and recorder.decisions[0]["allowed"] is True
    tools = [t for t, _ in session.tool_calls]
    assert "get_stock_bars" in tools and "get_stock_latest_trade" in tools
    assert "get_account_info" in tools and "get_all_positions" in tools
    assert recorder.ends and "1 order(s) submitted via risk guard" in recorder.ends[0]


def test_llm_position_size_cannot_change_quantity(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    small_session, big_session = FakeProxySession(), FakeProxySession()
    provider_small = ScriptedProvider(
        llm_response_json(llm_decision("AAPL", "BUY", position_size=0))
    )
    provider_big = ScriptedProvider(
        llm_response_json(llm_decision("AAPL", "BUY", position_size=999))
    )
    rc_small, _ = run_paper(monkeypatch, provider_small, small_session, limits=make_limits())
    rc_big, _ = run_paper(monkeypatch, provider_big, big_session, limits=make_limits())
    assert rc_small == rc_big == 0
    expected = [{"symbol": "AAPL", "side": "buy", "qty": "10"}]
    assert small_session.order_calls == expected
    assert big_session.order_calls == expected  # identical: size comes from Python


# ------------------------------------------- 6. invalid JSON → zero orders


def test_invalid_llm_json_creates_zero_orders(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    provider = ScriptedProvider("I strongly recommend buying AAPL right now.")
    session = FakeProxySession()
    rc, recorder = run_paper(monkeypatch, provider, session, limits=make_limits())
    assert rc == 1
    assert session.order_calls == []
    assert recorder.ends and "decisions invalid" in recorder.ends[0]


# ------------------------------------- 7. risk-guard rejection → zero orders


def test_risk_guard_rejection_creates_zero_orders(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    # Tighten the daily-loss limit in THIS TEST ONLY to force a real
    # risk_rules.check_order rejection; strategy.yaml itself is untouched.
    limits = make_limits(max_daily_loss_usd=1)
    session = FakeProxySession(
        account={"cash": 5000.0, "buying_power": 5000.0, "equity": 24900.0, "last_equity": 25000.0},
    )
    provider = ScriptedProvider(llm_response_json(llm_decision("AAPL", "BUY")))
    rc, recorder = run_paper(monkeypatch, provider, session, limits=limits)
    assert rc == 0
    assert session.order_calls == []
    assert recorder.decisions and recorder.decisions[0]["allowed"] is False
    assert "loss" in recorder.decisions[0]["reason"].lower()


# ---------------------------- 8/10. LLM failure → zero orders, keys redacted


def test_llm_failure_creates_zero_orders_and_redacts_key(monkeypatch, capsys):
    monkeypatch.setenv("PAPER_TRADING", "true")
    secret = "sk-live-feather-1234567890"
    monkeypatch.setenv("FEATHERLESS_API_KEY", secret)
    provider = ScriptedProvider(error=RuntimeError(f"Featherless API error: 401 for key {secret}"))
    session = FakeProxySession()
    rc, recorder = run_paper(monkeypatch, provider, session, limits=make_limits())
    assert rc == 1
    assert session.order_calls == []
    captured = capsys.readouterr()
    assert secret not in captured.err and secret not in captured.out
    assert "[REDACTED]" in captured.err
    assert all(secret not in r for r in recorder.reasonings)
    assert recorder.ends and "Run failed" in recorder.ends[0]


def test_redact_scrubs_known_secrets(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-secret-1234")
    text = "request to https://integrate.api.nvidia.com/v1 failed with key nv-secret-1234"
    scrubbed = orchestrator._redact(text)
    assert "nv-secret-1234" not in scrubbed
    assert "[REDACTED]" in scrubbed
    assert "integrate.api.nvidia.com" in scrubbed  # non-secret context survives


# ------------------------------------------------- 9. provider selection


def test_provider_selection_nvidia(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nvidia")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-key")
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)
    provider = orchestrator.build_llm_provider()
    assert isinstance(provider, NVIDIAProvider)
    assert provider.model == NVIDIAProvider.DEFAULT_MODEL
    assert provider.base_url == NVIDIAProvider.DEFAULT_BASE_URL


def test_provider_selection_featherless_and_default(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-feather-key")
    monkeypatch.setenv("FEATHERLESS_MODEL", "test-feather-model")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert isinstance(orchestrator.build_llm_provider(), FeatherlessLLMProvider)
    monkeypatch.setenv("LLM_PROVIDER", "featherless")
    assert isinstance(orchestrator.build_llm_provider(), FeatherlessLLMProvider)


def test_provider_selection_unknown_fails_closed(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        orchestrator.build_llm_provider()


# ------------------------------------------ backend dispatch / failure paths


def test_unknown_backend_fails_closed(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("AGENT_BACKEND", "banana")
    session = FakeProxySession()
    factory_calls = []
    rc, _ = run_paper(monkeypatch, ScriptedProvider(), session, factory_calls=factory_calls)
    assert rc == 1
    assert factory_calls == [] and session.started is False


def test_cline_backend_remains_available(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "cline")
    # PAPER_TRADING deliberately NOT set: the Cline fallback keeps working.
    monkeypatch.setattr(orchestrator, "load_config", lambda: dict(CONFIG))
    monkeypatch.setattr(orchestrator, "market_likely_open", lambda cfg: True)
    monkeypatch.setattr(orchestrator, "invoke_cline", lambda system_prompt, task: '{"type":"text"}')
    recorder = JournalRecorder()
    for name in ("log_run_start", "log_reasoning", "log_order_decision", "log_run_end"):
        monkeypatch.setattr(orchestrator.journal, name, getattr(recorder, name))
    rc = orchestrator.run_once()
    assert rc == 0
    assert recorder.starts and "cline" in recorder.starts[0]
    assert recorder.ends and "structured decisions invalid" in recorder.ends[0]


def test_session_start_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    session = FailingStartSession()
    rc, recorder = run_paper(monkeypatch, ScriptedProvider(), session, limits=make_limits())
    assert rc == 1
    assert session.closed is True  # finally-block teardown always runs
    assert recorder.ends and "Run failed" in recorder.ends[0]


def test_market_data_failure_fails_closed(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    session = FakeProxySession(fail_tools={"get_stock_bars"})
    provider = ScriptedProvider(llm_response_json(llm_decision("AAPL", "BUY")))
    rc, recorder = run_paper(monkeypatch, provider, session, limits=make_limits())
    assert rc == 1
    assert session.order_calls == []
    assert recorder.ends and "market data failure" in recorder.ends[0]


def test_executor_failure_records_error_and_fails_closed(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    session = FakeProxySession(fail_tools={"place_stock_order"})
    provider = ScriptedProvider(llm_response_json(llm_decision("AAPL", "BUY")))
    rc, recorder = run_paper(monkeypatch, provider, session, limits=make_limits())
    assert rc == 1
    order_attempts = [t for t, _ in session.tool_calls if t == "place_stock_order"]
    assert len(order_attempts) == 1  # one attempt, recorded, no retry storm
    assert session.order_calls == []
    assert recorder.ends and "Run failed" in recorder.ends[0]


# ----------------------- MCP place_stock_order schema-contract regressions


class _CapturingSession:
    """Records the exact payload the executor puts on the MCP wire."""

    def __init__(self):
        self.tool = None
        self.arguments = None

    def call(self, tool, arguments, timeout=None):
        self.tool = tool
        self.arguments = dict(arguments)
        return "{}"


def test_executor_qty_is_string_per_upstream_mcp_schema():
    """Regression: the upstream tool declares qty: Optional[str]; an int payload
    is rejected by MCP input validation before the risk guard even sees it."""
    session = _CapturingSession()
    orchestrator._ProxyOrderExecutor(session).submit(
        OrderRequest(symbol="MSFT", side="buy", qty=1)
    )
    assert session.tool == "place_stock_order"
    assert session.arguments == {"symbol": "MSFT", "side": "buy", "qty": "1"}


def test_executor_payload_validates_against_real_upstream_schema():
    """Reproduce the production failure '1 is not valid under any of the given
    schemas' using the REAL upstream alpaca-mcp-server schema and the SAME
    jsonschema validator the MCP SDK applies to call_tool arguments
    (mcp/server/lowlevel/server.py). Offline: build_server() performs no
    network call and needs no credentials."""
    from alpaca_mcp_server.server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    schema = next(t.parameters for t in tools if t.name == "place_stock_order")

    # The upstream contract that broke the real run: qty is string|null.
    assert {"type": "string"} in schema["properties"]["qty"]["anyOf"]

    # The wire payload the executor now sends validates cleanly...
    session = _CapturingSession()
    orchestrator._ProxyOrderExecutor(session).submit(
        OrderRequest(symbol="MSFT", side="buy", qty=1)
    )
    jsonschema.validate(instance=session.arguments, schema=schema)

    # ...while the old int payload fails with the exact observed error.
    with pytest.raises(jsonschema.ValidationError) as excinfo:
        jsonschema.validate(
            instance={"symbol": "MSFT", "side": "buy", "qty": 1}, schema=schema
        )
    assert "not valid under any of the given schemas" in str(excinfo.value)


# ------------------------------------------------- CLI (argparse) behavior


def _forbidden(name):
    def _boom(*args, **kwargs):
        raise AssertionError(f"{name} must not run for this CLI path")

    return _boom


def test_help_exits_immediately_without_workflow(monkeypatch, capsys):
    """--help exits 0 before touching config, market hours, LLM, or proxy."""
    for name in ("load_config", "market_likely_open", "build_llm_provider", "run_once"):
        monkeypatch.setattr(orchestrator, name, _forbidden(name))
    with pytest.raises(SystemExit) as excinfo:
        orchestrator.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--once" in out and "--force" in out and "--dry-run" in out


def test_once_invokes_exactly_one_run(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator, "run_once", lambda **kw: calls.append(kw) or 0)
    assert orchestrator.main(["--once"]) == 0
    assert calls == [{"force": False, "dry_run": False}]


def test_force_passes_force_true(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator, "run_once", lambda **kw: calls.append(kw) or 0)
    assert orchestrator.main(["--force"]) == 0
    assert calls == [{"force": True, "dry_run": False}]
    assert orchestrator.main(["--once", "--force"]) == 0  # flags combine
    assert calls[-1] == {"force": True, "dry_run": False}


def test_no_args_preserves_default_behavior(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator, "run_once", lambda **kw: calls.append(kw) or 0)
    assert orchestrator.main([]) == 0
    assert calls == [{"force": False, "dry_run": False}]


def test_unknown_option_is_argparse_error_not_a_run(monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "load_config", _forbidden("load_config"))
    monkeypatch.setattr(orchestrator, "run_once", _forbidden("run_once"))
    with pytest.raises(SystemExit) as excinfo:
        orchestrator.main(["--unknown-option"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err and "usage:" in err


def test_dry_run_full_workflow_never_submits_order(monkeypatch):
    """--dry-run runs the real workflow (market data, LLM, validation, sizing,
    risk check) but the order tool is NEVER called on the proxy session."""
    monkeypatch.setenv("PAPER_TRADING", "true")
    provider = ScriptedProvider(llm_response_json(llm_decision("AAPL", "BUY")))
    session = FakeProxySession()
    rc, recorder = run_paper(monkeypatch, provider, session, limits=make_limits(), dry_run=True)
    assert rc == 0
    assert session.order_calls == []
    assert [t for t, _ in session.tool_calls if t == "place_stock_order"] == []
    assert recorder.decisions and recorder.decisions[0]["allowed"] is True
    assert "dry run" in recorder.ends[0].lower() and "none sent" in recorder.ends[0]


def test_dry_run_still_fails_closed_on_stale_market_data(monkeypatch):
    """Dry-run preserves fail-closed safety: stale last trade ⇒ zero orders."""
    monkeypatch.setenv("PAPER_TRADING", "true")
    session = FakeProxySession()
    stale_time = datetime.now(timezone.utc) - timedelta(hours=12)
    session.latest_trade = json.dumps(
        {"trades": {"AAPL": {"p": 100.0, "t": stale_time.isoformat()}}}
    )
    provider = ScriptedProvider(llm_response_json(llm_decision("AAPL", "BUY")))
    rc, recorder = run_paper(monkeypatch, provider, session, limits=make_limits(), dry_run=True)
    assert rc == 1
    assert session.order_calls == []
    assert recorder.ends and "market data failure" in recorder.ends[0]


def test_dry_run_refused_for_cline_backend(monkeypatch):
    """Cline drives its own MCP session, so dry-run refuses rather than promise."""
    monkeypatch.setenv("AGENT_BACKEND", "cline")
    monkeypatch.setattr(orchestrator, "invoke_cline", _forbidden("invoke_cline"))
    session = FakeProxySession()
    factory_calls = []
    rc, recorder = run_paper(
        monkeypatch, ScriptedProvider(), session, factory_calls=factory_calls, dry_run=True
    )
    assert rc == 1
    assert factory_calls == [] and session.started is False
    assert recorder.ends and "dry-run" in recorder.ends[0].lower()