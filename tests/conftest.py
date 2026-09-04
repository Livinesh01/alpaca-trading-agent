"""Pytest configuration for the Sentinel test suite.

Guarantees that unit tests never inherit a live DATABASE_URL from the developer
environment: the production persistence adapter is only wired when a database is
configured, so an inherited DATABASE_URL would silently route tests through
PostgreSQL. We delete it (and reset the cached DB engine) for every test.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_database_url(monkeypatch):
    """Ensure unit tests run without an inherited DATABASE_URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Reset any cached SQLAlchemy engine/session state from prior tests.
    import db as db_module

    db_module.reset_db_state()
    yield