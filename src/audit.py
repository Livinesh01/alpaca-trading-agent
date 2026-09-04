"""Append-only audit log for Sentinel.

Records security-relevant events that must be tamper-evident:
configuration changes, authentication events, authorization failures,
AI decisions, risk decisions, final gate decisions, order attempts/submissions,
kill-switch activation, and worker lifecycle events.

Audit entries are written to the database (when available) AND to a local
JSONL file for redundancy. All entries include a UTC timestamp, correlation
IDs (request_id, trace_id), and actor information.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from observability import Observability

DEFAULT_AUDIT_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "journal", "audit")
)

_lock = threading.Lock()

_SENSITIVE_KEYS = frozenset({"secret", "password", "token", "api_key", "credential", "authorization"})


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: "[REDACTED]" if any(w in str(k).lower() for w in _SENSITIVE_KEYS) else _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


def log_audit_event(
    event_type: str,
    *,
    actor_id: str | None = None,
    actor_role: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    resource: str | None = None,
    outcome: str | None = None,
    request_id: str | None = None,
    **fields: Any,
) -> str:
    """Write an append-only audit record. Returns the event ID."""
    event_id = f"audit-{int(time.time() * 1000)}-{os.getpid()}-{threading.get_ident()}"
    entry = {
        "event_id": event_id,
        "event_type": event_type,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "resource": resource,
        "outcome": outcome,
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": _redact(fields),
    }

    # Keep a local JSONL copy when possible.
    try:
        audit_dir = Path(DEFAULT_AUDIT_DIR)
        audit_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = audit_dir / f"{date_str}.jsonl"
        with _lock, path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError:
        pass

    # Emit observability data when possible.
    try:
        obs = Observability()
        obs.emit(
            "audit_event",
            event_id=event_id,
            event_type=event_type,
            actor_id=actor_id,
            actor_role=actor_role,
            outcome=outcome,
            request_id=request_id,
            **{k: v for k, v in fields.items() if k not in _SENSITIVE_KEYS},
        )
    except Exception:  # noqa: BLE001, S110
        pass

    # Best-effort DB persistence
    try:
        from repositories import record_audit_event

        record_audit_event(entry)
    except Exception:  # noqa: BLE001, S110 — audit is informational; never block execution
        pass

    return event_id


def _use_postgres() -> bool:
    from config import is_production_env

    return is_production_env()


def read_audit_log(
    page: int = 1,
    page_size: int = 50,
    event_type: str | None = None,
    actor_id: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    """Read audit entries — PostgreSQL in production, local JSONL in development."""
    page = max(int(page), 1)
    page_size = max(min(int(page_size), 200), 1)

    if _use_postgres():
        # Production audit reads are authoritative PostgreSQL queries; a DB
        # failure surfaces explicitly instead of silently serving local files.
        try:
            from repositories import list_audit_events

            return list_audit_events(
                page=page,
                page_size=page_size,
                event_type=event_type,
                actor_id=actor_id,
                outcome=outcome,
            )
        except Exception as exc:  # noqa: BLE001 — explicit unavailability
            return {
                "items": [],
                "available": False,
                "reason": f"audit history unavailable (PostgreSQL): {type(exc).__name__}",
                "pagination": {"page": page, "page_size": page_size, "total": 0},
            }

    records: list[dict[str, Any]] = []

    audit_dir = Path(DEFAULT_AUDIT_DIR)
    if audit_dir.exists():
        for log_file in sorted(audit_dir.glob("*.jsonl")):
            try:
                for line in log_file.read_text(encoding="utf-8").splitlines():
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict):
                            continue
                        if event_type and entry.get("event_type") != event_type:
                            continue
                        if actor_id and str(entry.get("actor_id") or "") != actor_id:
                            continue
                        if outcome and entry.get("outcome") != outcome:
                            continue
                        records.append(entry)
                    except (TypeError, json.JSONDecodeError):
                        continue
            except OSError:
                continue

    records.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    start = (page - 1) * page_size
    return {
        "items": records[start : start + page_size],
        "available": True,
        "pagination": {"page": page, "page_size": page_size, "total": len(records)},
    }
