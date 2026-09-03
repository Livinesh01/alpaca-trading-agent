"""Small shared helpers for API services (numeric/timestamp coercions)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def num(value: Any) -> float | None:
    """Coerce to a finite float or None; booleans and NaN never pass through."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp (with optional trailing Z) or return None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_seconds(value: Any) -> float | None:
    """Age in seconds from now for a timestamp, or None when unparseable.

    Negative values mean the timestamp is in the future (never clamped to 0 —
    future data must not be presented as fresh).
    """
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()