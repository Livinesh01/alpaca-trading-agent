"""C2 (lease renewal fails closed) and C3 (production database mandatory)."""

from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import bootstrap
import lease_guard
import worker
from errors import ConfigurationError, LiveTradingUnsupportedError

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _reset_worker_lease_state():
    worker._shutdown_requested.clear()
    worker._lease_gave_up.clear()
    worker._lease_active.clear()
    lease_guard.reset()


# ---------------------------------------------------------------------------
# C2 — lease renewal fails closed
# ---------------------------------------------------------------------------


class TestLeaseRenewalFailClosed:
    def setup_method(self):
        _reset_worker_lease_state()

    def teardown_method(self):
        _reset_worker_lease_state()

    def test_healthy_renewal_resets_failures_and_recovers(self, monkeypatch):
        monkeypatch.setattr(worker, "_renew_worker_lease", lambda: True)
        failures, action = worker._lease_renewal_iteration(2)
        assert failures == 0
        assert action == "healthy"

    def test_single_failure_marks_lost_and_degraded(self, monkeypatch):
        monkeypatch.setattr(worker, "_renew_worker_lease", lambda: False)
        failures, action = worker._lease_renewal_iteration(0)
        assert failures == 1
        assert action == "degraded"
        assert lease_guard.is_lost()

    def test_recovery_within_grace_period(self, monkeypatch):
        calls = {"n": 0}

        def renew():
            calls["n"] += 1
            # Fail twice, then recover.
            return calls["n"] > 2

        monkeypatch.setattr(worker, "_renew_worker_lease", renew)
        f, a = worker._lease_renewal_iteration(0)
        assert a == "degraded" and lease_guard.is_lost()
        f, a = worker._lease_renewal_iteration(f)
        assert a == "degraded"
        f, a = worker._lease_renewal_iteration(f)
        assert a == "healthy" and not lease_guard.is_lost()

    def test_consecutive_failures_exhaust_grace_and_give_up(self, monkeypatch):
        monkeypatch.setattr(worker, "_renew_worker_lease", lambda: False)
        f, a = worker._lease_renewal_iteration(0)
        assert a == "degraded"
        f, a = worker._lease_renewal_iteration(f)
        assert a == "degraded"
        f, a = worker._lease_renewal_iteration(f)
        assert a == "give_up"
        assert f == worker.LEASE_RENEWAL_FAILURE_LIMIT

    def test_renewal_loop_sets_give_up_and_shutdown(self, monkeypatch):
        _reset_worker_lease_state()
        monkeypatch.setattr(worker, "_renew_worker_lease", lambda: False)
        monkeypatch.setattr(worker, "LEASE_RENEWAL_INTERVAL_SECONDS", 0)
        worker._lease_active.set()
        t = threading.Thread(target=worker._lease_renewal_loop, daemon=True)
        t.start()
        t.join(timeout=5)
        assert worker._lease_gave_up.is_set()
        assert worker._shutdown_requested.is_set()

    def test_db_outage_during_renewal_triggers_give_up(self, monkeypatch):
        _reset_worker_lease_state()
        monkeypatch.setattr(worker, "_renew_worker_lease", lambda: (_ for _ in ()).throw(ConnectionError("db down")))
        monkeypatch.setattr(worker, "LEASE_RENEWAL_INTERVAL_SECONDS", 0)
        worker._lease_active.set()
        t = threading.Thread(target=worker._lease_renewal_loop, daemon=True)
        t.start()
        t.join(timeout=5)
        assert worker._lease_gave_up.is_set()


# ---------------------------------------------------------------------------
# C3 — production database is mandatory
# ---------------------------------------------------------------------------


class TestProductionDatabaseMandatory:
    def test_production_missing_database_url_raises(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("API_AUTH_MODE", "production")
        monkeypatch.setenv("API_AUTH_MODE", "production")
        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("ALPACA_PAPER", "true")
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("SENTINEL_DATA_MODE", "proxy")
        monkeypatch.setenv("LLM_PROVIDER", "featherless")
        monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key-123")
        monkeypatch.setenv("FEATHERLESS_MODEL", "test-model")
        monkeypatch.setenv("ALPACA_API_KEY", "test-key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
        monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 32)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ConfigurationError, match="DATABASE_URL"):
            bootstrap.validate_startup_config()

    def test_production_invalid_database_url_raises(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("API_AUTH_MODE", "production")
        monkeypatch.setenv("PAPER_TRADING", "true")
        monkeypatch.setenv("ALPACA_PAPER", "true")
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("SENTINEL_DATA_MODE", "proxy")
        monkeypatch.setenv("LLM_PROVIDER", "featherless")
        monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key-123")
        monkeypatch.setenv("FEATHERLESS_MODEL", "test-model")
        monkeypatch.setenv("ALPACA_API_KEY", "test-key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
        monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 32)
        monkeypatch.setenv("DATABASE_URL", "mysql://user:pass@localhost/db")
        with pytest.raises((ConfigurationError, LiveTradingUnsupportedError)):
            bootstrap.validate_startup_config()

    def test_production_is_detected(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        assert bootstrap.is_production() is True

    def test_development_is_not_production(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "development")
        assert bootstrap.is_production() is False

    def test_ensure_database_raises_without_url(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ConfigurationError):
            bootstrap.ensure_database()

    def test_worker_lease_acquire_fails_in_production_without_db(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert worker._acquire_worker_lease() is False

    def test_worker_lease_acquire_allows_dev_without_db(self, monkeypatch):
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert worker._acquire_worker_lease() is True

    def test_worker_renew_fails_in_production_without_db(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert worker._renew_worker_lease() is False
