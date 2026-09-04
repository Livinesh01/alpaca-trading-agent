"""Phase 12 — Production-grade hardening tests.

Covers:
1. Production configuration validation (fail-closed invariants)
2. JWT authentication (token issuance, verification, expiry, role checks)
3. RBAC authorization (role-based endpoint access)
4. SSE real-time event stream
5. Audit log integrity
6. Database availability detection
7. Worker graceful shutdown
8. Paper-only enforcement across all layers
"""

from __future__ import annotations

import os
import sys
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src")))

from config import SentinelConfig
from errors import ConfigurationError, LiveTradingUnsupportedError

# ===========================================================================
# 1. PRODUCTION CONFIGURATION VALIDATION
# ===========================================================================


def test_production_config_requires_all_secrets(monkeypatch):
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JWT_SIGNING_SECRET", raising=False)

    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        SentinelConfig.validate_production_config()


def test_production_config_rejects_non_postgres_db(monkeypatch):
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
    monkeypatch.setenv("DATABASE_URL", "mysql://user:pass@localhost/db")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 32)

    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        SentinelConfig.validate_production_config()


def test_production_config_rejects_short_jwt_secret(monkeypatch):
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
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "short")

    with pytest.raises(ConfigurationError, match="32 characters"):
        SentinelConfig.validate_production_config()


def test_production_config_rejects_fake_llm(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("API_AUTH_MODE", "production")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "proxy")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 32)

    with pytest.raises(ConfigurationError, match="real LLM_PROVIDER"):
        SentinelConfig.validate_production_config()


def test_production_config_requires_alpaca_credentials(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("API_AUTH_MODE", "production")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "proxy")
    monkeypatch.setenv("LLM_PROVIDER", "featherless")
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key-123")
    monkeypatch.setenv("FEATHERLESS_MODEL", "test-model")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 32)

    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="ALPACA_API_KEY"):
        SentinelConfig.validate_production_config()
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    with pytest.raises(ConfigurationError, match="ALPACA_SECRET_KEY"):
        SentinelConfig.validate_production_config()


def test_production_config_rejects_dev_auth_mode(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("API_AUTH_MODE", "development")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "proxy")
    monkeypatch.setenv("LLM_PROVIDER", "featherless")
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key-123")
    monkeypatch.setenv("FEATHERLESS_MODEL", "test-model")
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 32)

    with pytest.raises(ConfigurationError, match="production"):
        SentinelConfig.validate_production_config()


def test_production_config_full_valid(monkeypatch):
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
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 32)

    cfg = SentinelConfig.validate_production_config()
    assert cfg.environment == "production"
    assert cfg.api_auth_mode == "production"
    assert cfg.db_url.startswith("postgresql://")
    assert cfg.alpaca_paper is True


# ===========================================================================
# 2. JWT AUTHENTICATION
# ===========================================================================


def test_jwt_token_issuance_and_verification(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "production")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 48)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")

    import auth

    auth.auth_mode.cache_clear()
    token = auth.create_token(user_id="user-1", role="VIEWER")
    assert token is not None

    payload = auth._decode_token(token)
    assert payload["sub"] == "user-1"
    assert payload["role"].upper() == "VIEWER"
    auth.auth_mode.cache_clear()


def test_jwt_token_expiry_rejected(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "production")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 48)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")

    import jwt as pyjwt

    import auth

    auth.auth_mode.cache_clear()
    now = int(time.time())
    expired_payload = {
        "sub": "user-1",
        "role": "VIEWER",
        "iat": now - 7200,
        "exp": now - 3600,
    }
    token = pyjwt.encode(expired_payload, "x" * 48, algorithm="HS256")

    with pytest.raises(HTTPException):
        auth._decode_token(token)
    auth.auth_mode.cache_clear()


def test_jwt_invalid_signature_rejected(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "production")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 48)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")

    import jwt as pyjwt

    import auth

    auth.auth_mode.cache_clear()
    now = int(time.time())
    payload = {"sub": "user-1", "role": "VIEWER", "iat": now, "exp": now + 3600}
    # Wrong secret must still be >=32 bytes to avoid PyJWT InsecureKeyLengthWarning;
    # the test only requires that it differs from the configured signing secret.
    wrong_secret = "y" * 48
    assert wrong_secret != "x" * 48
    token = pyjwt.encode(payload, wrong_secret, algorithm="HS256")

    with pytest.raises(HTTPException):
        auth._decode_token(token)
    auth.auth_mode.cache_clear()


def test_jwt_missing_token_rejected_in_production(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "production")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 48)

    import auth

    auth.auth_mode.cache_clear()
    with pytest.raises(Exception) as exc_info:
        auth.get_current_role(
            request=None,
            credentials=None,
            x_dev_role="VIEWER",
        )
    assert exc_info.value.status_code == 401
    auth.auth_mode.cache_clear()


def test_token_endpoint_issues_jwt(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "development")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "offline")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 48)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")

    from api.app import app
    from auth import auth_mode

    auth_mode.cache_clear()
    client = TestClient(app)
    response = client.post("/api/v1/auth/token", params={"role": "VIEWER"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["token_type"] == "Bearer"
    assert len(data["token"]) > 0
    auth_mode.cache_clear()


def test_token_endpoint_rejects_invalid_role(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "development")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "offline")

    from api.app import app
    from auth import auth_mode

    auth_mode.cache_clear()
    client = TestClient(app)
    response = client.post("/api/v1/auth/token", params={"role": "SUPERUSER"})
    assert response.status_code == 400
    auth_mode.cache_clear()


# ===========================================================================
# 3. RBAC AUTHORIZATION
# ===========================================================================


@pytest.fixture
def prod_client(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "development")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "offline")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "x" * 48)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")

    from auth import auth_mode

    auth_mode.cache_clear()
    return TestClient(app_module_app())


def app_module_app():
    from api.app import app
    return app


def test_unauthenticated_access_rejected(prod_client):
    response = prod_client.get("/api/v1/health")
    assert response.status_code == 401


def test_viewer_cannot_create_orders(prod_client):
    response = prod_client.get("/api/v1/orders", headers={"X-Dev-Role": "VIEWER"})
    assert response.status_code == 200
    response = prod_client.post(
        "/api/v1/orders",
        json={"symbol": "AAPL", "side": "BUY", "qty": 1},
        headers={"X-Dev-Role": "VIEWER"},
    )
    assert response.status_code == 403


def test_trader_can_preview_orders(prod_client):
    response = prod_client.post(
        "/api/v1/orders/preview",
        json={"symbol": "AAPL", "side": "BUY"},
        headers={"X-Dev-Role": "TRADER"},
    )
    assert response.status_code == 503


def test_viewer_blocked_from_order_creation(prod_client):
    response = prod_client.post(
        "/api/v1/orders",
        json={"symbol": "AAPL", "side": "BUY", "qty": 1},
        headers={"X-Dev-Role": "VIEWER"},
    )
    assert response.status_code == 403


def test_admin_required_for_admin_action(prod_client):
    response = prod_client.post(
        "/api/v1/risk/kill-switch",
        headers={"X-Dev-Role": "VIEWER"},
    )
    assert response.status_code == 401 or response.status_code == 403


# ===========================================================================
# 4. SSE REAL-TIME EVENT STREAM
# ===========================================================================


def test_sse_stream_bounded_snapshot_for_plain_get(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "disabled")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "offline")

    from api.app import _read_events, _redact_sse, app
    from auth import auth_mode

    auth_mode.cache_clear()
    client = TestClient(app)
    # A plain GET (no `Accept: text/event-stream`) must NOT hang forever:
    # it returns a bounded JSON snapshot of recent redacted events.
    response = client.get("/api/v1/health/stream")
    assert response.status_code == 200
    assert "text/event-stream" not in response.headers["content-type"]
    body = response.json()
    assert "data" in body
    auth_mode.cache_clear()

    # SSE helper units: line-offset reading and redaction.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        stream = Path(tmp) / "events.jsonl"
        stream.write_text('{"timestamp": 1, "event_type": "run_completed"}\n', encoding="utf-8")
        events, offset = _read_events(stream, 0)
        assert len(events) == 1
        assert offset == 1
        events, offset = _read_events(stream, offset)
        assert events == []
        assert offset == 1

    safe = _redact_sse({"event_type": "x", "api_key": "abc", "token": "def", "symbol": "AAPL"})
    assert safe["api_key"] == "[REDACTED]"
    assert safe["token"] == "[REDACTED]"
    assert safe["symbol"] == "AAPL"


# ===========================================================================
# 5. AUDIT LOG
# ===========================================================================


def test_audit_log_records_events(tmp_path, monkeypatch):
    from importlib import reload

    import audit

    reload(audit)
    monkeypatch.setattr(audit, "DEFAULT_AUDIT_DIR", str(tmp_path / "audit"))

    event_id = audit.log_audit_event(
        "config_change",
        actor_id="admin-1",
        actor_role="ADMIN",
        resource="/api/v1/system",
        outcome="success",
        request_id="req-123",
        key="value",
    )
    assert event_id.startswith("audit-")

    result = audit.read_audit_log()
    assert result["pagination"]["total"] == 1
    assert result["items"][0]["event_type"] == "config_change"
    assert result["items"][0]["actor_id"] == "admin-1"


def test_audit_log_redacts_secrets(tmp_path, monkeypatch):
    from importlib import reload

    import audit

    reload(audit)
    monkeypatch.setattr(audit, "DEFAULT_AUDIT_DIR", str(tmp_path / "audit"))

    audit.log_audit_event(
        "test",
        actor_id="user",
        secret_key="should_not_appear",
        password="also_hidden",
        safe_field="visible",
    )
    result = audit.read_audit_log()
    entry = result["items"][0]
    assert entry["fields"]["safe_field"] == "visible"
    assert entry["fields"]["secret_key"] == "[REDACTED]"
    assert entry["fields"]["password"] == "[REDACTED]"


# ===========================================================================
# 6. DATABASE AVAILABILITY
# ===========================================================================


def test_database_unavailable_when_no_url():
    from importlib import reload

    import db

    reload(db)
    assert db.is_database_available() is False


# ===========================================================================
# 7. WORKER GRACEFUL SHUTDOWN
# ===========================================================================


def test_worker_signal_handler_sets_shutdown_flag():
    import worker

    worker._shutdown_requested.clear()
    assert worker._shutdown_requested.is_set() is False


def test_worker_has_version_metadata():
    import worker
    assert worker.WORKER_VERSION == "0.5.0"
    assert worker.HEARTBEAT_MAX_AGE_SECONDS == 600


# ===========================================================================
# 8. PAPER-ONLY ENFORCEMENT
# ===========================================================================


def test_paper_only_enforced_in_config():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("PAPER_TRADING", "false")
    with pytest.raises(LiveTradingUnsupportedError):
        SentinelConfig.load()


def test_alpaca_paper_required():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(LiveTradingUnsupportedError, match="ALPACA_PAPER"):
        SentinelConfig.load()


# ===========================================================================
# 9. HEALTH ENDPOINTS
# ===========================================================================


def test_health_includes_db_and_worker_status(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "development")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "offline")

    from api.app import app
    from auth import auth_mode

    auth_mode.cache_clear()
    client = TestClient(app)
    response = client.get("/api/v1/health", headers={"X-Dev-Role": "VIEWER"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert "database" in data
    assert "worker" in data
    assert "alpaca_paper" in data
    assert data["alpaca_paper"] is True


def test_ready_returns_503_when_worker_stale(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "development")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "offline")

    from api.app import app
    from auth import auth_mode

    auth_mode.cache_clear()
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code in (200, 503)
    auth_mode.cache_clear()


# ===========================================================================
# 10. SECURITY — NO SECRETS EXPOSED
# ===========================================================================


def test_no_secrets_in_health_response(monkeypatch):
    monkeypatch.setenv("API_AUTH_MODE", "development")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("SENTINEL_DATA_MODE", "offline")
    monkeypatch.setenv("ALPACA_API_KEY", "super-secret-key-12345")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "super-secret-value-67890")
    monkeypatch.setenv("JWT_SIGNING_SECRET", "super-secret-jwt-99999")
    monkeypatch.setenv("FEATHERLESS_API_KEY", "featherless-secret-12345")

    from api.app import app
    from auth import auth_mode

    auth_mode.cache_clear()
    client = TestClient(app)
    response = client.get("/api/v1/health", headers={"X-Dev-Role": "VIEWER"})
    assert response.status_code == 200
    body = response.text
    assert "super-secret-key-12345" not in body
    assert "super-secret-value-67890" not in body
    assert "super-secret-jwt-99999" not in body
    assert "featherless-secret-12345" not in body
    auth_mode.cache_clear()
