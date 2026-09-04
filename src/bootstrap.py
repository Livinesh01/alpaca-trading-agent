"""Production startup bootstrap: configuration validation + database provisioning.

The production startup sequence is:

    load configuration
    -> validate production configuration (fail closed on any unsafe value)
    -> connect PostgreSQL
    -> verify/migrate schema (create missing tables, verify all required tables)
    -> start API/worker (trading stays disabled until /readiness is satisfied)

Every step fails closed: any failure raises and the process refuses to start.
`ensure_database()` is the single, explicit, tested startup dependency for the
schema — the application's SQLAlchemy metadata in `db.py` is the single source
of schema truth (no duplicated definitions), and `metadata.create_all` is
idempotent, so re-running startup against an existing database is safe.
"""

from __future__ import annotations

from errors import ConfigurationError

__all__ = ["database_status", "ensure_database", "is_production", "validate_startup_config"]


def is_production() -> bool:
    """True only when APP_ENV is explicitly 'production'."""
    from config import is_production_env

    return is_production_env()


def validate_startup_config():
    """Validate configuration at startup.

    APP_ENV=production runs the strict production validator (fails closed on any
    missing/invalid/unsafe value, including DATABASE_URL). Non-production
    environments keep their existing, more permissive startup behavior.
    """
    from config import SentinelConfig

    if is_production():
        return SentinelConfig.validate_production_config()
    return SentinelConfig.load()


def ensure_database() -> None:
    """Connect to PostgreSQL, apply the schema, and verify every required table.

    Raises (fail closed) when DATABASE_URL is missing, the server is
    unreachable, or any required table is missing after initialization.
    """
    from db import get_db_url, init_db, is_database_available, verify_schema

    if not get_db_url():
        raise ConfigurationError("DATABASE_URL is required but not configured (fail closed)")
    if not is_database_available():
        raise ConfigurationError("PostgreSQL is unreachable; refusing to start (fail closed)")
    init_db()
    missing = verify_schema()
    if missing:
        raise ConfigurationError(
            f"PostgreSQL schema is incomplete after initialization; missing tables: {missing}"
        )


def database_status() -> dict:
    """Best-effort database status for health/readiness surfaces."""
    from db import get_db_url, is_database_available

    if not get_db_url():
        return {"available": False, "configured": False, "status": "not_configured"}
    if not is_database_available():
        return {"available": False, "configured": True, "status": "unavailable"}
    try:
        from db import verify_schema

        missing = verify_schema()
        if missing:
            return {"available": True, "configured": True, "status": "schema_incomplete", "missing": missing}
        return {"available": True, "configured": True, "status": "healthy"}
    except Exception:  # noqa: BLE001 — status is informational, never fatal
        return {"available": True, "configured": True, "status": "unknown"}