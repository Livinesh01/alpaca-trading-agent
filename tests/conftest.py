"""Pytest configuration for the Sentinel test suite.

Guarantees that unit tests never inherit a live DATABASE_URL from the developer
environment: the production persistence adapter is only wired when a database is
configured, so an inherited DATABASE_URL would silently route tests through
PostgreSQL. We delete it (and reset the cached DB engine) for every test.

Tests marked ``postgres`` opt out and talk to a real PostgreSQL instance.
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "postgres: requires a real PostgreSQL instance (never SQLite)")


@pytest.fixture(autouse=True)
def _isolate_database_url(monkeypatch, request):
    """Ensure unit tests run without an inherited DATABASE_URL."""
    import db as db_module

    if request.node.get_closest_marker("postgres"):
        yield
        db_module.reset_db_state()
        return

    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_module.reset_db_state()
    yield
    db_module.reset_db_state()
