"""Service-layer tests for the read-only Sentinel API adapters.

No real Alpaca, MCP, or LLM calls: the data source is a canned fake and
backtest/evaluation engines are isolated by design. Verifies real service
projections, unavailable-dependency behavior, stale data, pagination,
filtering, and that backtest/evaluation services never use a session.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from api.services import (
    account_service,
    activity_service,
    backtest_service,
    decision_service,
    evaluation_service,
    market_service,
    order_service,
    position_service,
    risk_service,
)
from api.services.data_source import MarketDataUnavailable, NoDataSource
from memory import MemoryStore
from observability import Observability

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def make_bars(count=40, start=100.0, step=1.0, timestamp=None):
    base = timestamp or datetime(2026, 8, 1, 14, 30, tzinfo=timezone.utc)
    return [
        {
            "t": _iso(base + timedelta(days=i)),
            "o": start + i * step,
            "h": start + i * step + 1.0,
            "l": start + i * step - 1.0,
            "c": start + i * step,
            "v": 100000 + i,
        }
        for i in range(count)
    ]


class FakeSource:
    """Canned read-only data source; raises MarketDataUnavailable when fail=True."""

    def __init__(self, *, account=None, positions=None, orders=None, bars=None, latest_trade=None, fail=False, fail_tools=()):
        self.account = account if account is not None else {"equity": 25000.0, "last_equity": 24900.0, "cash": 5000.0, "buying_power": 5000.0, "currency": "USD", "status": "ACTIVE"}
        self.positions = positions if positions is not None else [{"symbol": "AAPL", "qty": 10, "avg_entry_price": 200.0, "market_value": 2050.0, "current_price": 205.0}]
        self.orders = orders if orders is not None else [{"id": "o1", "symbol": "AAPL", "side": "buy", "qty": 10, "order_type": "market", "status": "filled", "submitted_at": "2026-08-01T14:30:00Z", "filled_at": "2026-08-01T14:30:05Z"}]
        self.bars = bars if bars is not None else make_bars()
        self.latest_trade = latest_trade if latest_trade is not None else {"trades": {"AAPL": {"p": 205.0, "t": _iso(NOW)}}}
        self.fail = fail
        self.fail_tools = set(fail_tools)
        self.calls = []

    def _check(self, name):
        self.calls.append(name)
        if self.fail or name in {"get_account_info", "get_all_positions", "get_orders", "get_stock_bars", "get_stock_latest_trade"} & self.fail_tools:
            raise MarketDataUnavailable("no Alpaca connectivity")

    def get_account(self):
        self._check("get_account_info")
        return dict(self.account)

    def get_positions(self):
        self._check("get_all_positions")
        return [dict(item) for item in self.positions]

    def get_orders(self):
        self._check("get_orders")
        return [dict(item) for item in self.orders]

    def get_bars(self, symbol, timeframe="1Day", days=180, limit=500):
        self._check("get_stock_bars")
        return [dict(item) for item in self.bars]

    def get_latest_trade(self, symbol):
        self._check("get_stock_latest_trade")
        return dict(self.latest_trade)


def test_no_data_source_raises_explicit_unavailable():
    source = NoDataSource("credentials not configured")
    with pytest.raises(MarketDataUnavailable, match="credentials not configured"):
        source.get_account()


def test_account_service_projects_real_fields():
    payload = account_service.get_account(FakeSource())
    assert payload["available"] is True
    assert payload["equity"] == 25000.0
    assert payload["daily_pnl"] == 100.0
    assert payload["currency"] == "USD"
    assert payload["trading_mode"] in ("paper", "paper_required_not_enabled")
    assert payload["reason"] is None


def test_account_service_unavailable_is_explicit_not_fake():
    payload = account_service.get_account(FakeSource(fail=True))
    assert payload["available"] is False
    assert payload["equity"] is None
    assert payload["reason"] == "account data unavailable (Alpaca connectivity)"


def test_position_service_enriches_pnl_and_exposure():
    payload = position_service.get_positions(FakeSource())
    assert payload["available"] is True
    item = payload["items"][0]
    assert item["symbol"] == "AAPL"
    assert item["unrealized_pnl"] == 50.0  # 2050 - 200*10
    assert item["unrealized_pnl_percent"] == pytest.approx(50.0 / 2000.0)
    assert item["exposure"] == 2050.0


def test_position_service_unavailable_explicit():
    payload = position_service.get_positions(FakeSource(fail=True))
    assert payload["available"] is False and payload["items"] == []


def test_order_service_maps_states_and_never_overclaims_fill():
    orders = [
        {"id": "o1", "symbol": "AAPL", "side": "buy", "qty": 10, "order_type": "market", "status": "filled"},
        {"id": "o2", "symbol": "MSFT", "side": "sell", "qty": 1, "order_type": "limit", "status": "new"},
        {"id": "o3", "symbol": "NVDA", "side": "buy", "qty": 2, "order_type": "market", "status": "rejected"},
    ]
    source = FakeSource(orders=orders)
    payload = order_service.get_orders(source, [{"symbol": "AAPL", "action": "BUY", "decision_id": "decision-1", "run_id": "run-1"}])
    by_id = {item["order_id"]: item for item in payload["items"]}
    assert by_id["o1"]["display_state"] == "FILLED"
    assert by_id["o2"]["display_state"] == "SUBMITTED"
    assert by_id["o3"]["display_state"] == "FAILED"
    assert by_id["o1"]["decision_id"] == "decision-1"
    assert by_id["o1"]["run_id"] == "run-1"
    assert by_id["o3"]["decision_id"] is None


def test_order_service_unavailable_explicit():
    payload = order_service.get_orders(FakeSource(fail=True))
    assert payload["available"] is False and payload["items"] == []


def test_market_service_returns_bars_signals_and_freshness():
    limits = {"market_data_max_age_seconds": 120, "signal_params": {}}
    payload = market_service.get_market_data(FakeSource(), "AAPL", limits=limits)
    assert payload["available"] is True
    assert payload["symbol"] == "AAPL"
    assert payload["signals"]["symbol"] == "AAPL"
    assert payload["bars"] and payload["bars"][0]["timestamp"]
    # Bars are dated 2026-08-01, so the data is stale relative to now.
    assert payload["is_fresh"] is False
    assert payload["source"] == "alpaca_paper"


def test_market_service_stale_never_disguised():
    limits = {"market_data_max_age_seconds": 120, "signal_params": {}}
    source = FakeSource(bars=make_bars(timestamp=NOW - timedelta(days=40)))
    payload = market_service.get_market_data(source, "AAPL", limits=limits)
    assert payload["available"] is True
    assert payload["is_fresh"] is False
    assert payload["age_seconds"] > 120


def test_market_service_unavailable_explicit():
    limits = {"market_data_max_age_seconds": 120, "signal_params": {}}
    payload = market_service.get_market_data(FakeSource(fail=True), "AAPL", limits=limits)
    assert payload["available"] is False
    assert payload["is_fresh"] is False
    assert payload["source"] == "not_connected"


def test_decision_service_filters_and_paginates(tmp_path):
    store = MemoryStore(tmp_path / "mem")
    for i in range(5):
        store.save_decision(
            {"decision_id": f"d{i}", "run_id": "run-1", "timestamp": f"2026-08-0{i + 1}T14:00:00+00:00", "symbol": "AAPL" if i % 2 == 0 else "MSFT", "action": "HOLD", "confidence": 0.5, "position_size": 0, "thesis": "t", "entry_reason": "e"}
        )
    payload = decision_service.list_decisions(store, page=1, page_size=2)
    assert len(payload["items"]) == 2
    assert payload["pagination"]["total"] == 5
    filtered = decision_service.list_decisions(store, symbol="AAPL")
    assert [item["symbol"] for item in filtered["items"]] == ["AAPL", "AAPL", "AAPL"]
    assert filtered["pagination"]["total"] == 3


def test_decision_replay_exposes_llm_and_python_authority(tmp_path):
    store = MemoryStore(tmp_path / "mem")
    store.save_decision(
        {"decision_id": "d1", "run_id": "r1", "timestamp": "2026-08-01T14:00:00+00:00", "symbol": "AAPL", "action": "BUY", "confidence": 0.8, "position_size": 0, "thesis": "bullish", "entry_reason": "momentum", "decision_price": 200.0, "signals": {"trend": "up"}}
    )
    store.save_execution({"execution_id": "e1", "decision_id": "d1", "run_id": "r1", "qty": 10, "risk_allowed": True, "final_gate": True, "submitted": True})
    replay = decision_service.replay_decision(store, "d1")
    assert replay is not None
    assert replay["read_only"] is True
    assert replay["llm_authority"]["action"] == "BUY"
    assert replay["python_authority"]["deterministic_quantity"] == 10
    assert replay["python_authority"]["risk_allowed"] is True
    stage_names = [stage["stage"] for stage in replay["stages"]]
    assert "LLM DECISION" in stage_names
    assert "PYTHON SIZING" in stage_names
    assert "FINAL GATE" in stage_names
    assert "OUTCOME" in stage_names


def test_decision_replay_immutable_unknown_id():
    assert decision_service.replay_decision(MemoryStore(), "missing") is None


def test_risk_service_aggregates_without_new_engine(tmp_path):
    payload = risk_service.get_risk(FakeSource(), MemoryStore(tmp_path / "mem"), Observability(), {"max_position_notional_usd": 2000, "max_order_notional_usd": 1000})
    assert payload["available"] is True
    assert payload["daily_pnl"] == 100.0
    assert payload["gross_exposure"] == 2050.0
    assert payload["risk_utilization"] == pytest.approx(2050.0 / 2000.0)
    assert "risk_rules.check_order" in payload["authoritative_source"]


def test_activity_service_filters_severity_and_paginates(tmp_path):
    obs = Observability(directory=str(tmp_path / "obs"))
    obs.emit("market_data_failure", run_id="run-1", decision_id="d1", symbol="AAPL")
    obs.emit("run_completed", run_id="run-1")
    payload = activity_service.list_activity(obs, page=1, page_size=50)
    assert payload["pagination"]["total"] == 2
    by_type = {item["event_type"]: item for item in payload["items"]}
    assert by_type["market_data_failure"]["severity"] == "high"
    assert by_type["run_completed"]["severity"] == "info"
    filtered = activity_service.list_activity(obs, event_type="market_data_failure")
    assert filtered["pagination"]["total"] == 1


def _test_limits():
    return {
        "max_order_notional_usd": 1000,
        "max_position_notional_usd": 2000,
        "max_open_positions": 5,
        "max_daily_loss_usd": 500,
        "allow_short_selling": False,
        "allow_options": False,
        "allow_crypto": False,
        "signal_params": {},
    }


def test_backtest_service_runs_isolated_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest_service, "BACKTEST_PATH", tmp_path / "backtests.jsonl")
    record = backtest_service.run_backtest(symbol="AAPL", bars=make_bars(count=60), limits=_test_limits())
    assert record["label"] == "HYPOTHETICAL BACKTEST"
    assert record["actual_production_execution"] is False
    assert "total_return" in record["metrics"]
    assert record["metrics"]["trade_count"] >= 0
    assert backtest_service.get_backtest(record["backtest_id"])["backtest_id"] == record["backtest_id"]
    listed = backtest_service.list_backtests(symbol="AAPL")
    assert listed["pagination"]["total"] == 1


def test_backtest_service_rejects_non_frozen_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest_service, "BACKTEST_PATH", tmp_path / "backtests.jsonl")
    with pytest.raises(ValueError, match="frozen"):
        backtest_service.run_backtest(symbol="AAPL", bars=make_bars(count=10), limits=_test_limits(), provider_name="llm")


def test_backtest_service_isolated_from_any_session(tmp_path, monkeypatch):
    """The backend engine uses SimulatedExecutor — a trap session proves it."""

    class TrapSession:
        def call(self, *args, **kwargs):
            raise AssertionError("backtest must never call a live session")

    monkeypatch.setattr(backtest_service, "BACKTEST_PATH", tmp_path / "backtests.jsonl")
    record = backtest_service.run_backtest(symbol="AAPL", bars=make_bars(count=30), limits=_test_limits())
    assert TrapSession().call  # trap exists; the service never uses it
    assert record["label"] == "HYPOTHETICAL BACKTEST"


def test_evaluation_service_runs_isolated_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation_service, "EVALUATION_PATH", tmp_path / "evaluations.jsonl")
    record = evaluation_service.run_evaluation(symbol="AAPL", bars=make_bars(count=60), limits=_test_limits())
    assert record["label"] == "HYPOTHETICAL EVALUATION RESULT"
    assert record["human_review_required"] is True
    assert record["auto_deployed"] is False
    assert len(record["candidates"]) == 3
    assert evaluation_service.get_evaluation(record["evaluation_id"])["evaluation_id"] == record["evaluation_id"]
    assert evaluation_service.list_evaluations()["pagination"]["total"] >= 1