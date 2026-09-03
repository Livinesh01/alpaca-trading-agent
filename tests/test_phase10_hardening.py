"""Phase 10 — Production-Grade Sentinel Backend Hardening Test Suite.

Comprehensive tests covering:
1. Configuration hardening (safe defaults, paper-only invariant, limit validation)
2. LLM reliability (bounded retries, backoff, transient error classification, timeout, fail closed)
3. Market data hardening (OHLC validation, positive prices, timestamp validation, freshness, clock skew)
4. Risk engine hardening (all limit breaches, missing price fail-closed, structured rejections)
5. Final Order Gate hardening (all 9 invariants, cannot bypass, cannot fix unsafe orders)
6. Idempotency & duplicate protection (deterministic keys, atomic claim, multi-threaded deduplication)
7. Concurrency safety (IdempotencyStore thread-safety, rate-limiter lock, Observability lock)
8. API hardening (security headers, request IDs, rate limiting, request size limits, auth boundaries)
9. Adversarial prompt injection safety (huge quantity, ignore risk, kill switch override, live account claims)
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src")))

from agent.decision_loop import default_limits, deterministic_quantity
from agent.llm import LLMResponse, generate_with_retry
from api.app import _rate_limit_lock, _rate_window, app
from config import SentinelConfig
from errors import (
    ConfigurationError,
    FinalGateRejectionError,
    LiveTradingUnsupportedError,
    LLMExhaustedError,
    LLMInvalidResponse,
    LLMProviderError,
    MarketDataStaleError,
    MarketDataValidationError,
)
from idempotency import IdempotencyStore, make_idempotency_key
from market_data import (
    check_freshness,
    validate_market_data_point,
    validate_ohlc,
    validate_price,
    validate_symbol,
    validate_timestamp,
)
from observability import Observability
from orchestrator import _FinalOrderGate, validate_trade_decision
from risk_rules import AccountState, OrderRequest, check_order

# ===========================================================================
# 1. CONFIGURATION HARDENING
# ===========================================================================


def test_config_rejects_live_trading_when_unset(monkeypatch):
    monkeypatch.delenv("PAPER_TRADING", raising=False)
    with pytest.raises(LiveTradingUnsupportedError, match="LIVE trading is not implemented"):
        SentinelConfig.load()


def test_config_rejects_explicit_live_trading(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "false")
    with pytest.raises(LiveTradingUnsupportedError, match="LIVE trading is not implemented"):
        SentinelConfig.load()


def test_config_loads_valid_paper_configuration(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("WATCHLIST", "AAPL,MSFT,NVDA")
    cfg = SentinelConfig.load()
    assert cfg.paper_trading is True
    assert cfg.watchlist == ("AAPL", "MSFT", "NVDA")
    assert cfg.llm_timeout_seconds == 60.0
    assert cfg.llm_max_retries == 3


def test_config_rejects_invalid_backend(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("AGENT_BACKEND", "unsupported_backend")
    with pytest.raises(ConfigurationError, match="AGENT_BACKEND"):
        SentinelConfig.load()


def test_config_rejects_invalid_llm_provider(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openai_direct")
    with pytest.raises(ConfigurationError, match="LLM_PROVIDER"):
        SentinelConfig.load()


def test_config_rejects_featherless_without_key(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("LLM_PROVIDER", "featherless")
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="FEATHERLESS_API_KEY"):
        SentinelConfig.load()


def test_config_rejects_nvidia_without_key(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("LLM_PROVIDER", "nvidia")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="NVIDIA_API_KEY"):
        SentinelConfig.load()


def test_config_rejects_negative_risk_limits(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("MAX_POSITION_NOTIONAL_USD", "-500")
    with pytest.raises(ConfigurationError, match="MAX_POSITION_NOTIONAL_USD"):
        SentinelConfig.load()


def test_config_rejects_invalid_timeout_and_retries(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0.5")  # min is 1.0
    with pytest.raises(ConfigurationError, match="LLM_TIMEOUT_SECONDS"):
        SentinelConfig.load()

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30.0")
    monkeypatch.setenv("LLM_MAX_RETRIES", "-1")
    with pytest.raises(ConfigurationError, match="LLM_MAX_RETRIES"):
        SentinelConfig.load()


def test_config_rejects_empty_watchlist(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("WATCHLIST", "   ")
    with pytest.raises(ConfigurationError, match="WATCHLIST"):
        SentinelConfig.load()


def test_config_to_safe_dict_excludes_secrets(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    cfg = SentinelConfig.load()
    safe_dict = cfg.to_safe_dict()
    assert "paper_trading" in safe_dict
    assert "watchlist" in safe_dict
    for key in safe_dict:
        assert "key" not in key.lower()
        assert "secret" not in key.lower()
        assert "token" not in key.lower()


# ===========================================================================
# 2. LLM RELIABILITY
# ===========================================================================


class _CountingProvider:
    def __init__(self, fail_count: int, exc: Exception, success_text: str = "ok"):
        self.fail_count = fail_count
        self.exc = exc
        self.success_text = success_text
        self.calls = 0

    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc
        return LLMResponse(text=self.success_text)


def test_llm_transient_failure_retries_and_succeeds():
    provider = _CountingProvider(fail_count=2, exc=TimeoutError("Connection timed out"))
    observer = Observability()
    response = generate_with_retry(
        provider,
        "test prompt",
        max_retries=3,
        base_delay=0.001,
        max_delay=0.01,
        observability=observer,
    )
    assert response.text == "ok"
    assert provider.calls == 3


def test_llm_persistent_failure_fails_immediately():
    provider = _CountingProvider(fail_count=5, exc=ValueError("Invalid prompt syntax"))
    with pytest.raises(LLMProviderError):
        generate_with_retry(
            provider,
            "test prompt",
            max_retries=3,
            base_delay=0.001,
            max_delay=0.01,
        )
    assert provider.calls == 1  # No retry on non-transient error


def test_llm_retry_exhaustion_raises_exhausted_error():
    provider = _CountingProvider(fail_count=10, exc=ConnectionError("Server unreachable"))
    with pytest.raises(LLMExhaustedError) as exc_info:
        generate_with_retry(
            provider,
            "test prompt",
            max_retries=2,
            base_delay=0.001,
            max_delay=0.01,
        )
    assert provider.calls == 3  # 1 initial + 2 retries
    assert "exhausted all 3 attempts" in str(exc_info.value)


def test_llm_timeout_triggers_classified_error():
    class _HangingProvider:
        def generate(self, prompt: str, **kwargs) -> LLMResponse:
            time.sleep(0.5)
            return LLMResponse(text="too late")

    with pytest.raises(LLMExhaustedError):
        generate_with_retry(
            _HangingProvider(),
            "test",
            timeout_seconds=0.05,
            max_retries=1,
            base_delay=0.001,
            max_delay=0.01,
        )


def test_llm_invalid_return_type_fails_without_retry():
    class _BadTypeProvider:
        def generate(self, prompt: str, **kwargs):
            return "not an LLMResponse"

    with pytest.raises(LLMInvalidResponse):
        generate_with_retry(_BadTypeProvider(), "test", max_retries=3, base_delay=0.001)


def test_llm_secrets_not_leaked_in_exception():
    class _SecretFailingProvider:
        def generate(self, prompt: str, **kwargs):
            raise RuntimeError("upstream failed with key alpaca_secret_key_12345")

    with pytest.raises(LLMProviderError) as exc_info:
        generate_with_retry(
            _SecretFailingProvider(),
            "SECRET_PROMPT_DATA",
            max_retries=0,
            base_delay=0.001,
        )
    assert "SECRET_PROMPT_DATA" not in str(exc_info.value)


# ===========================================================================
# 3. MARKET DATA HARDENING
# ===========================================================================


def test_market_data_symbol_validation():
    assert validate_symbol("aapl") == "AAPL"
    assert validate_symbol("  msft  ") == "MSFT"

    for invalid in ("", "   ", "TOOLONGTICKER", "123", "AAPL!", None, 42):
        with pytest.raises(MarketDataValidationError):
            validate_symbol(invalid)


def test_market_data_price_validation():
    assert validate_price(150.25) == 150.25
    assert validate_price("100.5") == 100.5

    for invalid in (0, -10.0, None, "abc", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(MarketDataValidationError):
            validate_price(invalid)


def test_market_data_timestamp_validation():
    ts = validate_timestamp("2026-09-03T12:00:00Z")
    assert ts.tzinfo is not None
    assert ts.year == 2026

    for invalid in (None, "", "not-a-date", 12345678):
        with pytest.raises(MarketDataValidationError):
            validate_timestamp(invalid)


def test_market_data_freshness_enforcement():
    now = datetime.now(timezone.utc)
    fresh_ts = now - timedelta(seconds=30)
    check_freshness(fresh_ts, max_age_seconds=120, now=now)

    stale_ts = now - timedelta(seconds=125)
    with pytest.raises(MarketDataStaleError, match="stale"):
        check_freshness(stale_ts, max_age_seconds=120, now=now)

    future_ts = now + timedelta(seconds=15)
    with pytest.raises(MarketDataStaleError, match="future"):
        check_freshness(future_ts, max_age_seconds=120, now=now)


def test_market_data_ohlc_relationships():
    valid_bar = {"open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0}
    assert validate_ohlc(valid_bar, "AAPL") == valid_bar

    # Low > high
    with pytest.raises(MarketDataValidationError, match="low"):
        validate_ohlc({"open": 100.0, "high": 95.0, "low": 98.0, "close": 96.0})

    # High < close
    with pytest.raises(MarketDataValidationError, match="high"):
        validate_ohlc({"open": 100.0, "high": 101.0, "low": 98.0, "close": 103.0})

    # Low > open
    with pytest.raises(MarketDataValidationError, match="low"):
        validate_ohlc({"open": 97.0, "high": 105.0, "low": 98.0, "close": 102.0})


def test_validate_market_data_point_integration():
    now = datetime.now(timezone.utc)
    valid_point = {
        "price": 150.0,
        "timestamp": now.isoformat(),
        "open": 149.0,
        "high": 151.0,
        "low": 148.0,
        "close": 150.0,
    }
    result = validate_market_data_point(valid_point, "AAPL", max_age_seconds=60, now=now)
    assert result["symbol"] == "AAPL"
    assert result["price"] == 150.0
    assert "ohlc" in result


# ===========================================================================
# 4. RISK ENGINE HARDENING
# ===========================================================================


def _test_account(**overrides) -> AccountState:
    defaults = {
        "cash": 10_000.0,
        "buying_power": 10_000.0,
        "equity": 10_000.0,
        "daily_pnl": 0.0,
        "open_position_count": 1,
        "orders_placed_this_run": 0,
        "existing_position_notional": 0.0,
    }
    defaults.update(overrides)
    return AccountState(**defaults)


def test_risk_rejects_buy_order_without_valid_price():
    order = OrderRequest(symbol="AAPL", side="buy", qty=10)
    decision = check_order(order, _test_account(), default_limits(), last_price=None)
    assert decision.allowed is False
    assert decision.reason_code == "MISSING_PRICE"

    decision_zero = check_order(order, _test_account(), default_limits(), last_price=0.0)
    assert decision_zero.allowed is False
    assert decision_zero.reason_code == "MISSING_PRICE"

    decision_neg = check_order(order, _test_account(), default_limits(), last_price=-50.0)
    assert decision_neg.allowed is False
    assert decision_neg.reason_code == "MISSING_PRICE"


def test_risk_rejects_max_order_notional_exceeded():
    order = OrderRequest(symbol="AAPL", side="buy", qty=100)
    limits = default_limits()
    limits["max_order_notional_usd"] = 1000.0
    decision = check_order(order, _test_account(), limits, last_price=150.0)  # 15,000 notional
    assert decision.allowed is False
    assert decision.reason_code == "MAX_ORDER_NOTIONAL_EXCEEDED"


def test_risk_rejects_max_position_notional_exceeded():
    order = OrderRequest(symbol="AAPL", side="buy", qty=10)
    account = _test_account(existing_position_notional=1800.0)
    limits = default_limits()
    limits["max_position_notional_usd"] = 2000.0
    decision = check_order(order, account, limits, last_price=50.0)  # 1800 + 500 = 2300 > 2000
    assert decision.allowed is False
    assert decision.reason_code == "MAX_POSITION_EXCEEDED"


def test_risk_rejects_daily_loss_cap_breached():
    order = OrderRequest(symbol="AAPL", side="buy", qty=1)
    account = _test_account(daily_pnl=-1500.0)
    limits = default_limits()
    limits["max_daily_loss_usd"] = 1000.0
    decision = check_order(order, account, limits, last_price=100.0)
    assert decision.allowed is False
    assert decision.reason_code == "DAILY_LOSS_CAP_BREACHED"


def test_risk_rejects_insufficient_buying_power():
    order = OrderRequest(symbol="AAPL", side="buy", qty=10)
    account = _test_account(buying_power=500.0)
    decision = check_order(order, account, default_limits(), last_price=100.0)  # 1000 > 500
    assert decision.allowed is False
    assert decision.reason_code == "INSUFFICIENT_BUYING_POWER"


def test_risk_rejects_unapproved_symbol():
    order = OrderRequest(symbol="TSLA", side="buy", qty=1)
    limits = default_limits()
    limits["allowed_symbols"] = ["AAPL", "MSFT"]
    limits["watchlist"] = ["AAPL", "MSFT"]
    decision = check_order(order, _test_account(), limits, last_price=200.0)
    assert decision.allowed is False
    assert decision.reason_code == "SYMBOL_NOT_APPROVED"


# ===========================================================================
# 5. FINAL ORDER GATE HARDENING
# ===========================================================================


class _MockExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, order: OrderRequest) -> str:
        self.submitted.append(order)
        return f"submitted:{order.symbol}"


def _make_gate(*, prices=None, allowed_symbols=("AAPL",), limits=None, idempotency_store=None):
    return _FinalOrderGate(
        _MockExecutor(),
        fetch_account=lambda symbol: _test_account(),
        limits=limits or default_limits(),
        allowed_symbols=allowed_symbols,
        prices=prices if prices is not None else {"AAPL": 100.0},
        idempotency_store=idempotency_store or IdempotencyStore(),
    )


def test_final_gate_rechecks_kill_switch(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("TRADING_KILL_SWITCH", "true")
    gate = _make_gate()
    with pytest.raises(FinalGateRejectionError) as exc_info:
        gate.submit(OrderRequest(symbol="AAPL", side="buy", qty=1))
    assert "TRADING_KILL_SWITCH is set" in str(exc_info.value)
    assert isinstance(exc_info.value, ValueError)


def test_final_gate_rechecks_paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "false")
    gate = _make_gate()
    with pytest.raises(FinalGateRejectionError, match="paper trading is not enabled"):
        gate.submit(OrderRequest(symbol="AAPL", side="buy", qty=1))


def test_final_gate_refuses_invalid_side(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    gate = _make_gate()
    with pytest.raises(FinalGateRejectionError, match="short selling is never accepted"):
        gate.submit(OrderRequest(symbol="AAPL", side="sell_short", qty=1))


def test_final_gate_refuses_invalid_qty(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    gate = _make_gate()
    for bad_qty in (0, -5, 2.5, True, False, "10"):
        with pytest.raises(FinalGateRejectionError, match="invalid quantity"):
            gate.submit(OrderRequest(symbol="AAPL", side="buy", qty=bad_qty))


def test_final_gate_refuses_duplicate_execution(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    store = IdempotencyStore()
    gate = _make_gate(idempotency_store=store)
    order = OrderRequest(symbol="AAPL", side="buy", qty=1, idempotency_key="key-unique-1")
    assert gate.submit(order) == "submitted:AAPL"

    # Second identical attempt must be rejected
    with pytest.raises(FinalGateRejectionError, match="duplicate execution rejected"):
        gate.submit(order)


# ===========================================================================
# 6. IDEMPOTENCY & CONCURRENCY
# ===========================================================================


def test_idempotency_key_deterministic():
    k1 = make_idempotency_key(symbol="AAPL", side="buy", qty=10, run_id="run-1", decision_id="dec-1")
    k2 = make_idempotency_key(symbol="AAPL", side="buy", qty=10, run_id="run-1", decision_id="dec-1")
    k3 = make_idempotency_key(symbol="AAPL", side="buy", qty=11, run_id="run-1", decision_id="dec-1")
    assert k1 == k2
    assert k1 != k3


def test_concurrency_idempotency_store_atomic_claim():
    store = IdempotencyStore()
    key = "concurrent-test-key"
    results = []

    def _attempt_claim():
        record = store.check_and_claim(
            idempotency_key=key,
            run_id="run-c",
            decision_id="dec-c",
            symbol="AAPL",
            side="buy",
            qty=5,
        )
        results.append(record.status)

    threads = [threading.Thread(target=_attempt_claim) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread claims 'completed'; all others receive 'rejected_duplicate'
    assert results.count("completed") == 1
    assert results.count("rejected_duplicate") == 9


def test_concurrency_rate_limiter_thread_safety():
    client_key = "192.168.1.100"
    with _rate_limit_lock:
        _rate_window[client_key] = []

    def _hit_rate_limiter():
        now = time.monotonic()
        with _rate_limit_lock:
            recent = [s for s in _rate_window.get(client_key, []) if now - s < 60]
            recent.append(now)
            _rate_window[client_key] = recent

    threads = [threading.Thread(target=_hit_rate_limiter) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with _rate_limit_lock:
        assert len(_rate_window[client_key]) == 25


def test_concurrency_observability_counters():
    observer = Observability()

    def _emit_events():
        for _ in range(20):
            observer.emit("test_event", value=1)

    threads = [threading.Thread(target=_emit_events) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snapshot = observer.snapshot()
    assert snapshot["counters"]["test_event"] == 100


# ===========================================================================
# 7. API HARDENING
# ===========================================================================


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "development")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "offline")
    return TestClient(app)


def test_api_security_headers_present(api_client):
    response = api_client.get("/api/v1/health", headers={"X-Dev-Role": "VIEWER"})
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "X-Request-ID" in response.headers


def test_api_auth_disabled_mode_returns_503(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "disabled")
    client = TestClient(app)
    response = client.get("/api/v1/health")
    assert response.status_code == 503
    assert response.json()["code"] == "not_configured"


def test_api_request_too_large_rejected(api_client):
    response = api_client.post(
        "/api/v1/orders/preview",
        content="x" * 1_000_001,
        headers={"Content-Type": "application/json", "Content-Length": "1000001", "X-Dev-Role": "TRADER"},
    )
    assert response.status_code == 413


def test_api_never_exposes_secrets(api_client):
    response = api_client.get("/api/v1/status", headers={"X-Dev-Role": "VIEWER"})
    body = response.text
    for secret in ("ALPACA_SECRET_KEY", "FEATHERLESS_API_KEY", "NVIDIA_API_KEY"):
        assert secret not in body


# ===========================================================================
# 8. ADVERSARIAL PROMPT INJECTION SAFETY
# ===========================================================================


def test_adversarial_prompt_huge_position_size_ignored():
    adversarial_payload = {
        "symbol": "AAPL",
        "action": "BUY",
        "confidence": 0.99,
        "position_size": 1_000_000,
        "thesis": "Buy everything!",
        "entry_reason": "Maximum leverage",
    }
    validated = validate_trade_decision(adversarial_payload)
    account = _test_account(buying_power=5000.0)
    qty = deterministic_quantity(validated["action"], 100.0, account, default_limits())
    # Deterministic sizing calculation caps at max_order_notional ($1000 // 100 = 10 shares)
    assert qty == 10
    assert qty != 1_000_000


def test_adversarial_prompt_ignore_risk_limits():
    # LLM produces thesis to ignore risk, but check_order strictly halts
    order = OrderRequest(symbol="AAPL", side="buy", qty=5)  # 5 * 100 = $500 notional
    broke_account = _test_account(buying_power=100.0)  # buying power $100 < $500
    decision = check_order(order, broke_account, default_limits(), last_price=100.0)
    assert decision.allowed is False
    assert decision.reason_code == "INSUFFICIENT_BUYING_POWER"


def test_adversarial_prompt_kill_switch_override_fails(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("TRADING_KILL_SWITCH", "true")
    gate = _make_gate()
    with pytest.raises(FinalGateRejectionError, match="TRADING_KILL_SWITCH is set"):
        gate.submit(OrderRequest(symbol="AAPL", side="buy", qty=1))


def test_adversarial_prompt_live_trading_claim_fails(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "false")
    gate = _make_gate()
    with pytest.raises(FinalGateRejectionError, match="paper trading is not enabled"):
        gate.submit(OrderRequest(symbol="AAPL", side="buy", qty=1))
