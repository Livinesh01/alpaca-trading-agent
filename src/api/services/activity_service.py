"""Activity service: structured observability events with correlation + filters.

Events correlate run_id -> decision_id -> execution_id -> outcome_id. Filtering
and pagination keep large histories from being dumped into the browser.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from api.services.records import read_records
from api.services.utils import age_seconds, parse_timestamp
from observability import Observability

_HIGH_EVENTS = frozenset({"llm_failure", "market_data_failure", "order_failed", "final_gate_rejection", "risk_rejection", "run_failed", "kill_switch_enabled"})


def _severity(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "")
    if event_type in _HIGH_EVENTS or "reject" in event_type or "fail" in event_type:
        return "high"
    if event_type in {"run_completed", "order_filled", "execution_recorded"}:
        return "info"
    return "info"


def _pg_activity(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape PostgreSQL agent_events into the activity-feed contract."""
    from api.services.utils import age_seconds

    items = []
    for event in events:
        fields = dict(event.get("fields") or {})
        timestamp = event.get("timestamp")
        iso_ts = (
            datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()
            if timestamp is not None
            else None
        )
        items.append(
            {
                "timestamp": iso_ts,
                "age_seconds": age_seconds(iso_ts),
                "event_type": event.get("event_type"),
                "severity": _severity(event),
                "run_id": event.get("run_id"),
                "decision_id": event.get("decision_id"),
                "execution_id": event.get("execution_id"),
                "outcome_id": fields.pop("outcome_id", None),
                "symbol": event.get("symbol"),
                "fields": fields,
            }
        )
    return items


def _pg_unavailable(exc: Exception, page: int, page_size: int) -> dict[str, Any]:
    from api.services.history_source import unavailable

    return unavailable(
        f"activity unavailable (PostgreSQL): {type(exc).__name__}",
        page=page,
        page_size=page_size,
    )


def _matches(event: dict[str, Any], **filters: Any) -> bool:
    for field in ("event_type", "run_id", "decision_id", "execution_id", "outcome_id"):
        value = filters.get(field)
        if value and str(event.get(field) or "") != str(value):
            return False
    symbol = filters.get("symbol")
    if symbol and str(event.get("symbol") or "").upper() != str(symbol).upper():
        return False
    time_from = filters.get("time_from")
    if time_from:
        stamp = event.get("timestamp")
        event_time = parse_timestamp(stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)) if stamp is not None else None
        if event_time is not None and event_time < parse_timestamp(time_from):
            return False
    time_to = filters.get("time_to")
    if time_to:
        stamp = event.get("timestamp")
        event_time = parse_timestamp(stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)) if stamp is not None else None
        if event_time is not None and event_time > parse_timestamp(time_to):
            return False
    return True


def list_activity(
    observability: Observability,
    *,
    page: int = 1,
    page_size: int = 50,
    **filters: Any,
) -> dict[str, Any]:
    from api.services.history_source import use_postgres_history

    if use_postgres_history():
        # C4: production serves the activity feed from PostgreSQL agent_events.
        try:
            from repositories import list_agent_events

            result = list_agent_events(page=page, page_size=page_size, **filters)
        except Exception as exc:  # noqa: BLE001 — explicit unavailability
            return _pg_unavailable(exc, page, page_size)
        return {
            "items": _pg_activity(result["items"]),
            "available": True,
            "reason": None,
            "pagination": result["pagination"],
        }
    records = read_records(observability.events_path)
    records.sort(key=lambda item: float(item.get("timestamp") or 0.0), reverse=True)
    filtered = [event for event in records if _matches(event, **filters)]
    page = max(int(page), 1)
    page_size = max(min(int(page_size), 200), 1)
    start = (page - 1) * page_size
    items = [
        {
            "timestamp": _format_event_time(event.get("timestamp")),
            "age_seconds": age_seconds(_format_event_time(event.get("timestamp"))),
            "event_type": event.get("event_type"),
            "severity": _severity(event),
            "run_id": event.get("run_id"),
            "decision_id": event.get("decision_id"),
            "execution_id": event.get("execution_id"),
            "outcome_id": event.get("outcome_id"),
            "symbol": event.get("symbol"),
            "fields": {key: value for key, value in event.items() if key not in {"timestamp", "event_type", "run_id", "decision_id", "execution_id", "outcome_id", "symbol"}},
        }
        for event in filtered[start : start + page_size]
    ]
    return {
        "items": items,
        "available": True,
        "reason": None,
        "pagination": {"page": page, "page_size": page_size, "total": len(filtered)},
    }


def _format_event_time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)