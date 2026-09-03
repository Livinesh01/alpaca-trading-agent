"""End-to-end paper-trading safety validation.

Audits the full execution chain with every boundary mocked (no real LLM,
Alpaca, MCP, or network call):

  market data -> validation -> DecisionLoop/LLM -> structured JSON validation
  -> deterministic sizing -> risk_rules -> _FinalOrderGate -> proxy executor
  -> (Alpaca PAPER, outside this process via the risk-guard proxy)

Focus: the gate is the last boundary before an order leaves the process, and
no alternate path can bypass the Python risk guard.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import orchestrator
from agent.decision_loop import default_limits
from risk_rules import AccountState, OrderRequest

CONFIG = {"watchlist": ["AAPL"]}

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


def make_limits(**overrides):
    """Real strategy.yaml risk values (never weakened) + test signal params."""
    limits = default_limits()
    params = dict(SIGNAL_PARAMS)
    params.update(limits.get("signal_params", {}) or {})
    limits["signal_params"] = params
    limits.update(overrides)
    return limits


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


class StubProvider:
    """LLMProvider stub returning one canned completion or raising."""

    def __init__(self, text="", error=None):
        self.text = text
        self.error = error

    def generate(self, prompt, **kwargs):
        if self.error is not None:
            raise self.error
        from agent.llm import LLMResponse

        return LLMResponse(text=self.text)


def decision(symbol, action="BUY", **extra):
    d = {
        "symbol": symbol,
        "action": action,
        "confidence": 0.7,
        "position_size": 0,
        "thesis": "signals align",
        "entry_reason": "gates pass",
    }
    d.update(extra)
    return json.dumps({"decisions": [d]})


class FakeSession:
    """Canned _RiskGuardProxySession stand-in: fresh data, records orders."""

    def __init__(self, *, account=None, positions=None, price=100.0, fail_tools=()):
        self.account = json.dumps(
            account
            if account is not None
            else {
                "cash": 5000.0,
                "buying_power": 5000.0,
                "equity": 25000.0,
                "last_equity": 25000.0,
            }
        )
        self.positions = json.dumps(positions if positions is not None else [])
        now = datetime.now(timezone.utc).isoformat()
        self.latest_trade = json.dumps({"trades": {"AAPL": {"p": price, "t": now}}})
        bars = [{"c": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i} for i in range(12)]
        self.bars = json.dumps({"bars": {"AAPL": bars}})
        self.fail_tools = set(fail_tools)
        self.tool_calls = []
        self.order_calls = []
        self.started = False

    def start(self):
        self.started = True

    def close(self):
        pass

    def call(self, tool, arguments, timeout=None):
        self.tool_calls.append((tool, dict(arguments)))
        if tool in self.fail_tools:
            raise RuntimeError(f"MCP tool {tool!r} failed: boom")
        if tool == "place_stock_order":
            self.order_calls.append(dict(arguments))
            return json.dumps({"id": "order-1", "status": "filled"})
        if tool == "get_stock_bars":
            return self.bars
        if tool == "get_stock_latest_trade":
            return self.latest_trade
        if tool == "get_account_info":
            return self.account
        if tool == "get_all_positions":
            return self.positions
        raise AssertionError(f"unexpected tool call {tool}")


class JournalRecorder:
    def __init__(self):
        self.decisions = []
        self.ends = []

    def log_run_start(self, summary):
        pass

    def log_reasoning(self, text):
        pass

    def log_order_decision(self, symbol, side, qty, allowed, reason):
        self.decisions.append(
            {"symbol": symbol, "side": side, "qty": qty, "allowed": allowed, "reason": reason}
        )

    def log_run_end(self, summary):
        self.ends.append(summary)


def run_cycle(monkeypatch, provider, session, *, config=None, limits=None, dry_run=False):
    """One orchestrator.run_once() with every boundary injected/mocked."""
    monkeypatch.setattr(orchestrator, "load_config", lambda: dict(config or CONFIG))
    monkeypatch.setattr(orchestrator, "market_likely_open", lambda cfg: True)
    monkeypatch.setattr(orchestrator, "build_llm_provider", lambda: provider)
    monkeypatch.setattr(orchestrator, "_RiskGuardProxySession", lambda *a, **k: session)
    if limits is None:
        monkeypatch.setattr("agent.decision_loop.default_limits", make_limits)
    else:
        monkeypatch.setattr("agent.decision_loop.default_limits", lambda: dict(limits))
    recorder = JournalRecorder()
    for name in ("log_run_start", "log_reasoning", "log_order_decision", "log_run_end"):
        monkeypatch.setattr(orchestrator.journal, name, getattr(recorder, name))
    rc = orchestrator.run_once(force=True, dry_run=dry_run)
    return rc, recorder


@pytest.fixture(autouse=True)
def _paper_env(monkeypatch):
    """Explicit paper env: gate on, kill switch off, no inherited settings."""
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.delenv("TRADING_KILL_SWITCH", raising=False)
    monkeypatch.delenv("AGENT_BACKEND", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)


class _StubExecutor:
    def __init__(self, submit):
        self._submit = submit
        self.calls = []

    def submit(self, order):
        self.calls.append(order)
        return self._submit(order)


def _wrapped(order):
    return f"submitted:{order.symbol}"


def _gate(*, account=None, allowed_symbols=("AAPL",), limits=None):
    return orchestrator._FinalOrderGate(
        _StubExecutor(_wrapped),
        fetch_account=lambda symbol: account or make_account(),
        limits=limits if limits is not None else make_limits(),
        allowed_symbols=allowed_symbols,
        prices={"AAPL": 100.0},
    )


# --------------------------------- _FinalOrderGate: the last safety boundary


def test_gate_allows_valid_buy():
    order = OrderRequest(symbol="AAPL", side="buy", qty=10)
    assert _gate().submit(order) == "submitted:AAPL"


def test_gate_allows_valid_sell_of_existing_position():
    account = make_account(existing_position_notional=500.0)
    order = OrderRequest(symbol="AAPL", side="sell", qty=5)
    assert _gate(account=account).submit(order) == "submitted:AAPL"


@pytest.mark.parametrize(
    "order",
    [
        OrderRequest(symbol="", side="buy", qty=1),  # empty symbol
        OrderRequest(symbol="TOOLONGTICKER", side="buy", qty=1),  # malformed shape
        OrderRequest(symbol="DOGE", side="buy", qty=1),  # outside the watchlist
        OrderRequest(symbol="AAPL", side="sell_short", qty=1),  # shorts never pass
        OrderRequest(symbol="AAPL", side="", qty=1),
        OrderRequest(symbol="AAPL", side="buy", qty=0),  # zero quantity
        OrderRequest(symbol="AAPL", side="buy", qty=-5),  # negative quantity
        OrderRequest(symbol="AAPL", side="buy", qty=2.5),  # fractional shares
        OrderRequest(symbol="AAPL", side="buy", qty=True),  # bool is not a qty
        OrderRequest(symbol="AAPL", side="buy", qty=1, asset_class="crypto"),
        OrderRequest(symbol="AAPL", side="buy", qty=1, asset_class="option"),
        OrderRequest(symbol="AAPL", side="buy", qty=1, order_type="stop_limit"),
    ],
)
def test_gate_refuses_invalid_orders(order):
    executor = _StubExecutor(_wrapped)
    gate = orchestrator._FinalOrderGate(
        executor,
        fetch_account=lambda symbol: make_account(),
        limits=make_limits(),
        allowed_symbols=("AAPL",),
        prices={"AAPL": 100.0},
    )
    with pytest.raises(ValueError, match="final order gate refused"):
        gate.submit(order)
    assert executor.calls == []  # the wire executor is never reached


def test_gate_refuses_when_kill_switch_is_set(monkeypatch):
    monkeypatch.setenv("TRADING_KILL_SWITCH", "true")
    executor = _StubExecutor(_wrapped)
    gate = orchestrator._FinalOrderGate(
        executor,
        fetch_account=lambda symbol: make_account(),
        limits=make_limits(),
        allowed_symbols=("AAPL",),
        prices={"AAPL": 100.0},
    )
    with pytest.raises(ValueError, match="TRADING_KILL_SWITCH is set"):
        gate.submit(OrderRequest(symbol="AAPL", side="buy", qty=10))
    assert executor.calls == []


def test_gate_refuses_when_paper_mode_lost_at_submission_time(monkeypatch):
    """The gate re-checks paper mode on EVERY submission, not just at startup."""
    monkeypatch.setenv("PAPER_TRADING", "false")  # flipped after the run started
    executor = _StubExecutor(_wrapped)
    gate = orchestrator._FinalOrderGate(
        executor,
        fetch_account=lambda symbol: make_account(),
        limits=make_limits(),
        allowed_symbols=("AAPL",),
        prices={"AAPL": 100.0},
    )
    with pytest.raises(ValueError, match="paper trading is not enabled"):
        gate.submit(OrderRequest(symbol="AAPL", side="buy", qty=10))
    assert executor.calls == []


def test_gate_rechecks_risk_rules_independently():
    """Third independent check_order evaluation with this run's account state."""
    executor = _StubExecutor(_wrapped)
    broke = make_account(buying_power=5.0, cash=5.0)
    gate = orchestrator._FinalOrderGate(
        executor,
        fetch_account=lambda symbol: broke,
        limits=make_limits(),
        allowed_symbols=("AAPL",),
        prices={"AAPL": 100.0},
    )
    with pytest.raises(ValueError, match="risk re-check rejected"):
        gate.submit(OrderRequest(symbol="AAPL", side="buy", qty=10))
    assert executor.calls == []


# --------------------------------- end-to-end through orchestrator.run_once()


def test_e2e_valid_buy_reaches_proxy_exactly_once(monkeypatch):
    session = FakeSession()
    provider = StubProvider(decision("AAPL", "BUY"))
    rc, recorder = run_cycle(monkeypatch, provider, session)
    assert rc == 0
    assert session.order_calls == [{"symbol": "AAPL", "side": "buy", "qty": "10"}]
    assert recorder.decisions and recorder.decisions[0]["allowed"] is True
    assert recorder.decisions[0]["qty"] == 10


def test_e2e_valid_sell_is_close_only(monkeypatch):
    session = FakeSession(positions=[{"symbol": "AAPL", "market_value": 500.0}])
    provider = StubProvider(decision("AAPL", "SELL"))
    rc, recorder = run_cycle(monkeypatch, provider, session)
    assert rc == 0
    assert session.order_calls == [{"symbol": "AAPL", "side": "sell", "qty": "5"}]
    assert recorder.decisions[0]["allowed"] is True


def test_e2e_insufficient_buying_power_places_no_order(monkeypatch):
    session = FakeSession(
        account={"cash": 5.0, "buying_power": 5.0, "equity": 25000.0, "last_equity": 25000.0}
    )
    provider = StubProvider(decision("AAPL", "BUY"))
    rc, recorder = run_cycle(monkeypatch, provider, session)
    assert rc == 0  # deterministic sizing yields qty=0 -> no order, not an error
    assert session.order_calls == []
    # The decision is journaled with the Python-computed qty of 0: no order.
    assert len(recorder.decisions) == 1
    assert recorder.decisions[0]["qty"] == 0
    assert recorder.decisions[0]["allowed"] is True


@pytest.mark.parametrize("payload", ["not json at all", '{"decisions": [{"symbol":'])
def test_e2e_malformed_llm_json_creates_zero_orders(monkeypatch, payload):
    session = FakeSession()
    rc, recorder = run_cycle(monkeypatch, StubProvider(payload), session)
    assert rc == 1
    assert session.order_calls == []
    assert recorder.ends and "Run failed" in recorder.ends[0]


def test_e2e_missing_decision_fails_closed(monkeypatch):
    """A 2-symbol watchlist with only one decision is rejected wholesale."""
    session = FakeSession()
    bars = {"AAPL": [{"c": 100.0 + i, "h": 101.0, "l": 99.0} for i in range(12)]}
    bars["MSFT"] = [{"c": 200.0 + i, "h": 201.0, "l": 199.0} for i in range(12)]
    session.bars = json.dumps({"bars": bars})
    provider = StubProvider(decision("AAPL", "BUY"))  # MSFT decision missing
    rc, recorder = run_cycle(
        monkeypatch, provider, session, config={"watchlist": ["AAPL", "MSFT"]}
    )
    assert rc == 1
    assert session.order_calls == []
    assert recorder.ends and "Run failed" in recorder.ends[0]


def test_e2e_stale_market_data_fails_closed(monkeypatch):
    session = FakeSession()
    stale = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    session.latest_trade = json.dumps({"trades": {"AAPL": {"p": 100.0, "t": stale}}})
    rc, recorder = run_cycle(monkeypatch, StubProvider(decision("AAPL", "BUY")), session)
    assert rc == 1
    assert session.order_calls == []
    assert recorder.ends and "market data failure" in recorder.ends[0]


def test_e2e_market_data_api_failure_fails_closed(monkeypatch):
    session = FakeSession(fail_tools={"get_stock_latest_trade"})
    rc, _recorder = run_cycle(monkeypatch, StubProvider(decision("AAPL", "BUY")), session)
    assert rc == 1
    assert session.order_calls == []


def test_e2e_risk_guard_rejection_creates_zero_orders(monkeypatch):
    """Proxy-level rejection (fresh account data) leaves zero orders."""
    session = FakeSession(
        account={
            "cash": 5000.0,
            "buying_power": 5000.0,
            "equity": 24900.0,
            "last_equity": 25000.0,
        }
    )
    rc, recorder = run_cycle(
        monkeypatch,
        StubProvider(decision("AAPL", "BUY")),
        session,
        limits=make_limits(max_daily_loss_usd=1),
    )
    assert rc == 0
    assert session.order_calls == []
    assert recorder.decisions and recorder.decisions[0]["allowed"] is False


def test_e2e_kill_switch_blocks_every_order(monkeypatch):
    monkeypatch.setenv("TRADING_KILL_SWITCH", "true")
    session = FakeSession()
    rc, recorder = run_cycle(monkeypatch, StubProvider(decision("AAPL", "BUY")), session)
    assert rc == 1
    assert session.order_calls == []
    assert [t for t, _ in session.tool_calls if t == "place_stock_order"] == []
    assert recorder.ends and "final order gate" in recorder.ends[0]


def test_e2e_executor_failure_is_handled_safely(monkeypatch):
    session = FakeSession(fail_tools={"place_stock_order"})
    rc, recorder = run_cycle(monkeypatch, StubProvider(decision("AAPL", "BUY")), session)
    assert rc == 1
    assert session.order_calls == []  # the tool raised -> nothing recorded
    assert recorder.ends and "Run failed" in recorder.ends[0]


def test_e2e_dry_run_places_zero_orders(monkeypatch):
    session = FakeSession()
    rc, recorder = run_cycle(
        monkeypatch, StubProvider(decision("AAPL", "BUY")), session, dry_run=True
    )
    assert rc == 0
    assert session.order_calls == []
    assert [t for t, _ in session.tool_calls if t == "place_stock_order"] == []
    assert "dry run" in recorder.ends[0].lower()


# ------------------------------------- bypass prevention / structural checks


def test_normal_path_wraps_proxy_executor_in_final_gate():
    """The built executor is gate -> proxy: no way around the gate in prod."""
    loop = orchestrator._build_decision_loop(
        CONFIG,
        provider=StubProvider(),
        session=FakeSession(),
        limits=make_limits(),
    )
    assert isinstance(loop.executor, orchestrator._FinalOrderGate)
    assert isinstance(loop.executor._executor, orchestrator._ProxyOrderExecutor)


def test_dry_run_path_uses_executor_without_proxy_access():
    loop = orchestrator._build_decision_loop(
        CONFIG,
        provider=StubProvider(),
        session=FakeSession(),
        limits=make_limits(),
        dry_run=True,
    )
    assert isinstance(loop.executor, orchestrator._DryRunExecutor)
    assert not hasattr(loop.executor, "_session")


def test_no_source_file_bypasses_the_proxy_to_alpaca():
    """Tripwire: only risk_guard_proxy.py may talk to Alpaca order APIs."""
    bypass_markers = re.compile(r"submit_order|TradingClient\(|tradeapi|/v2/orders")
    src_dir = Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for path in sorted(src_dir.glob("*.py")):
        if path.name == "risk_guard_proxy.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if bypass_markers.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == []



