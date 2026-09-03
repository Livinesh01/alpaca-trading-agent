import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from api.app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "development")
    # Never let tests try to spawn the risk-guard proxy / reach Alpaca.
    monkeypatch.setenv("SENTINEL_DATA_MODE", "offline")
    return TestClient(app)


def test_unauthenticated_access_is_rejected(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 401
    assert "request_id" in response.json()
    assert "traceback" not in response.text.lower()


def test_read_only_endpoints_return_typed_envelopes_without_secrets(client):
    for path in ("/health", "/status", "/account", "/positions", "/orders", "/market-data", "/decisions", "/executions", "/risk", "/activity", "/backtests", "/evaluations", "/system"):
        response = client.get(f"/api/v1{path}", headers={"X-Dev-Role": "VIEWER"})
        assert response.status_code == 200, path
        body = response.json()
        assert "data" in body and "request_id" in body
        assert "ALPACA_API_KEY" not in response.text
        assert "ALPACA_SECRET_KEY" not in response.text
        assert "Authorization" not in response.text


def test_health_reuses_safe_observability_state(client):
    response = client.get("/api/v1/health", headers={"X-Dev-Role": "VIEWER", "X-Request-ID": "req-test"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-test"
    data = response.json()["data"]
    assert data["authentication"] == "development-only"
    assert isinstance(data["kill_switch"], bool)


def test_role_authorization_blocks_viewer_preview(client):
    response = client.post("/api/v1/orders/preview", json={"symbol": "AAPL", "side": "BUY"}, headers={"X-Dev-Role": "VIEWER"})
    assert response.status_code == 403


def test_preview_is_explicitly_disabled_and_never_submits(client):
    response = client.post("/api/v1/orders/preview", json={"symbol": "AAPL", "side": "BUY"}, headers={"X-Dev-Role": "TRADER"})
    assert response.status_code == 503
    assert response.json()["data"]["execution"] == "NOT_SUBMITTED"


def test_order_and_kill_switch_mutations_are_disabled(client):
    for path in ("/api/v1/orders", "/api/v1/risk/kill-switch"):
        response = client.post(path, headers={"X-Dev-Role": "ADMIN"})
        assert response.status_code == 501


def test_replay_unknown_id_is_safe(client):
    response = client.get("/api/v1/decisions/unknown/replay", headers={"X-Dev-Role": "VIEWER"})
    assert response.status_code == 404
    assert "unknown" not in response.text


def test_malformed_preview_input_is_rejected_without_execution(client):
    response = client.post("/api/v1/orders/preview", json={"symbol": "AAPL", "side": "DROP", "quantity": 999}, headers={"X-Dev-Role": "TRADER"})
    assert response.status_code == 422
    assert "999" not in response.text


def test_authentication_configuration_is_explicitly_required(client, monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "disabled")
    response = client.get("/api/v1/system", headers={"X-Dev-Role": "ADMIN"})
    assert response.status_code == 503
    assert "not configured" in response.text


def test_rate_limit_is_structured_and_non_authoritative(client, monkeypatch):
    import api.app as api_app

    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "1")
    api_app._rate_window.clear()
    headers = {"X-Dev-Role": "VIEWER"}
    assert client.get("/api/v1/status", headers=headers).status_code == 200
    response = client.get("/api/v1/status", headers=headers)
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"


# ---------------------------------------------------------------------------
# Phase 10: data-driven endpoints (read-only service layer)
# ---------------------------------------------------------------------------


class FakeSource:
    """Canned read-only data source for API integration tests."""

    def __init__(self, *, fail=False, stale=False):
        self.fail = fail
        self.stale = stale
        import datetime

        base = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31 if stale else 30)
        self.account = {"equity": 25000.0, "last_equity": 24900.0, "cash": 5000.0, "buying_power": 5000.0, "currency": "USD", "status": "ACTIVE"}
        self.positions = [{"symbol": "AAPL", "qty": 10, "avg_entry_price": 200.0, "market_value": 2050.0, "current_price": 205.0}]
        self.orders = [{"id": "o1", "symbol": "AAPL", "side": "buy", "qty": 10, "order_type": "market", "status": "filled"}]
        self.bars = [{"t": (base + datetime.timedelta(days=i)).isoformat(), "o": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "c": 100.0 + i, "v": 1000.0} for i in range(30)]
        now = datetime.datetime.now(datetime.timezone.utc)
        latest_ts = now - datetime.timedelta(hours=12) if stale else now
        self.latest = {"trades": {"AAPL": {"p": 130.0, "t": latest_ts.isoformat()}}}

    def get_account(self):
        if self.fail:
            raise RuntimeError("secret-like inner detail ALPACA_API_KEY=sk-hello")
        return dict(self.account)

    def get_positions(self):
        return [dict(item) for item in self.positions]

    def get_orders(self):
        return [dict(item) for item in self.orders]

    def get_bars(self, symbol, timeframe="1Day", days=180, limit=500):
        return [dict(item) for item in self.bars]

    def get_latest_trade(self, symbol):
        return dict(self.latest)


def _use_source(client, monkeypatch, source):
    import api.app as api_app

    monkeypatch.setattr(api_app, "_data_source", lambda: source)


def test_data_endpoints_unavailable_without_credentials_are_explicit(client):
    """No configured source -> explicit unavailable, never fake values."""
    headers = {"X-Dev-Role": "VIEWER"}
    account = client.get("/api/v1/account", headers=headers).json()["data"]
    assert account["available"] is False
    assert account["equity"] is None
    assert "reason" in account
    positions = client.get("/api/v1/positions", headers=headers).json()["data"]
    assert positions["available"] is False and positions["items"] == []
    orders = client.get("/api/v1/orders", headers=headers).json()["data"]
    assert orders["available"] is False and orders["items"] == []
    market = client.get("/api/v1/market-data?symbol=AAPL", headers=headers).json()["data"]
    assert market["available"] is False and market["is_fresh"] is False


def test_wired_account_positions_orders_and_market(client, monkeypatch):
    _use_source(client, monkeypatch, FakeSource())
    headers = {"X-Dev-Role": "VIEWER"}
    account = client.get("/api/v1/account", headers=headers).json()["data"]
    assert account["available"] is True and account["equity"] == 25000.0 and account["daily_pnl"] == 100.0
    positions = client.get("/api/v1/positions", headers=headers).json()["data"]
    assert positions["items"][0]["unrealized_pnl"] == 50.0
    assert "pagination" in positions
    orders = client.get("/api/v1/orders", headers=headers).json()["data"]
    assert orders["items"][0]["display_state"] in {"FILLED", "SUBMITTED", "REQUESTED", "FAILED"}
    market = client.get("/api/v1/market-data?symbol=AAPL", headers=headers).json()["data"]
    assert market["available"] is True and market["signals"]["symbol"] == "AAPL"
    assert "age_seconds" in market and "is_fresh" in market
    risk = client.get("/api/v1/risk", headers=headers).json()["data"]
    assert risk["gross_exposure"] == 2050.0
    assert risk["daily_pnl"] == 100.0
    assert risk["authoritative_source"] != ""


def test_stale_market_data_is_reported_not_disguised(client, monkeypatch):
    _use_source(client, monkeypatch, FakeSource(stale=True))
    market = client.get("/api/v1/market-data?symbol=AAPL", headers={"X-Dev-Role": "VIEWER"}).json()["data"]
    assert market["available"] is True
    assert market["is_fresh"] is False
    assert market["age_seconds"] > 120


def test_market_data_signals_endpoint(client, monkeypatch):
    _use_source(client, monkeypatch, FakeSource())
    payload = client.get("/api/v1/market-data/AAPL/signals", headers={"X-Dev-Role": "VIEWER"}).json()["data"]
    assert payload["symbol"] == "AAPL"
    assert payload["signals"]["symbol"] == "AAPL"
    assert "is_fresh" in payload


def test_decisions_support_pagination_and_filters(client, monkeypatch, tmp_path):
    import api.app as api_app
    from memory import MemoryStore

    store = MemoryStore(tmp_path / "mem")
    for i in range(4):
        store.save_decision({"decision_id": f"d{i}", "run_id": "r1", "timestamp": f"2026-08-0{i + 1}T14:00:00+00:00", "symbol": "AAPL", "action": "HOLD", "confidence": 0.5, "position_size": 0, "thesis": "t", "entry_reason": "e"})
    monkeypatch.setattr(api_app, "_memory", lambda: store)
    headers = {"X-Dev-Role": "VIEWER"}
    payload = client.get("/api/v1/decisions?page=1&page_size=2&symbol=AAPL", headers=headers).json()["data"]
    assert payload["pagination"]["total"] == 4
    assert len(payload["items"]) == 2


def test_decision_replay_returns_authority_timeline(client, monkeypatch, tmp_path):
    import api.app as api_app
    from memory import MemoryStore

    store = MemoryStore(tmp_path / "mem")
    store.save_decision({"decision_id": "d-replay", "run_id": "r1", "timestamp": "2026-08-01T14:00:00+00:00", "symbol": "AAPL", "action": "BUY", "confidence": 0.8, "position_size": 0, "thesis": "bullish", "entry_reason": "momentum", "decision_price": 200.0, "signals": {"trend": "up"}})
    store.save_execution({"execution_id": "e1", "decision_id": "d-replay", "run_id": "r1", "qty": 10, "risk_allowed": True, "final_gate": True, "submitted": True})
    monkeypatch.setattr(api_app, "_memory", lambda: store)
    payload = client.get("/api/v1/decisions/d-replay/replay", headers={"X-Dev-Role": "VIEWER"}).json()["data"]
    assert payload["read_only"] is True
    assert payload["llm_authority"]["action"] == "BUY"
    assert payload["python_authority"]["deterministic_quantity"] == 10
    stage_names = [stage["stage"] for stage in payload["stages"]]
    assert stage_names == ["RUN", "MARKET DATA", "TECHNICAL SIGNALS", "LLM DECISION", "SCHEMA VALIDATION", "PYTHON SIZING", "RISK CHECK", "FINAL GATE", "EXECUTION", "OUTCOME"]


def test_backtest_creation_requires_real_data_and_never_submits(client, monkeypatch, tmp_path):
    import api.services.backtest_service as bsvc

    monkeypatch.setattr(bsvc, "BACKTEST_PATH", tmp_path / "bt.jsonl")
    headers = {"X-Dev-Role": "TRADER"}
    response = client.post("/api/v1/backtests", json={"symbol": "AAPL"}, headers=headers)
    assert response.status_code == 503  # no credentials -> explicit unavailable
    _use_source(client, monkeypatch, FakeSource())
    response = client.post("/api/v1/backtests", json={"symbol": "AAPL", "starting_capital": 10000}, headers=headers)
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["label"] == "HYPOTHETICAL BACKTEST"
    assert body["metrics"]["total_return"] is not None
    assert body["actual_production_execution"] is False


def test_evaluation_creation_requires_real_data_and_never_submits(client, monkeypatch, tmp_path):
    import api.services.evaluation_service as esvc

    monkeypatch.setattr(esvc, "EVALUATION_PATH", tmp_path / "ev.jsonl")
    headers = {"X-Dev-Role": "TRADER"}
    response = client.post("/api/v1/evaluations", json={"symbol": "AAPL"}, headers=headers)
    assert response.status_code == 503
    _use_source(client, monkeypatch, FakeSource())
    response = client.post("/api/v1/evaluations", json={"symbol": "AAPL"}, headers=headers)
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["label"] == "HYPOTHETICAL EVALUATION RESULT"
    assert body["human_review_required"] is True and body["auto_deployed"] is False


def test_order_and_risk_mutation_endpoints_stay_disabled(client):
    assert client.post("/api/v1/orders", headers={"X-Dev-Role": "ADMIN"}).status_code == 501
    assert client.post("/api/v1/risk/kill-switch", headers={"X-Dev-Role": "ADMIN"}).status_code == 501


def test_unavailable_errors_never_leak_source_details(client, monkeypatch):
    _use_source(client, monkeypatch, FakeSource(fail=True))
    response = client.get("/api/v1/account", headers={"X-Dev-Role": "VIEWER"})
    assert response.status_code == 200
    text = response.text
    assert "sk-hello" not in text
    assert "ALPACA_API_KEY" not in text
    assert "secret" not in text.lower()


def test_no_service_exposes_order_submission_strings():
    """The read-only service layer must not reference order-submission tools."""
    from pathlib import Path

    services_dir = Path(__file__).resolve().parent.parent / "src" / "api" / "services"
    offenders = []
    for path in sorted(services_dir.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "place_stock_order" in line or "submit_order" in line:
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == []


def test_api_response_has_consistent_error_contract(client):
    response = client.get("/api/v1/decisions/does-not-exist/replay", headers={"X-Dev-Role": "VIEWER"})
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert "request_id" in body
    assert "traceback" not in response.text.lower()
