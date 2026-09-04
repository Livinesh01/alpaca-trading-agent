"""Production history-source selection (C4).

APP_ENV=production reads decision/execution/activity/audit history from
PostgreSQL ONLY — there is no assumption that the API and worker share a
filesystem, and a database failure surfaces as an explicit unavailable state
(never fabricated data, never a silent JSONL fallback).

Development/testing keeps the existing local JSONL behavior.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["is_production", "unavailable", "use_postgres_history"]


def is_production() -> bool:
    """True only when APP_ENV is explicitly 'production'."""
    return os.environ.get("APP_ENV", "").strip().lower() == "production"


def use_postgres_history() -> bool:
    """Production must read persistent state from PostgreSQL (never JSONL)."""
    return is_production()


def unavailable(reason: str, *, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """Explicit unavailable envelope for history endpoints (no fabricated data)."""
    return {
        "items": [],
        "available": False,
        "reason": reason,
        "pagination": {"page": max(int(page), 1), "page_size": max(min(int(page_size), 200), 1), "total": 0},
    }