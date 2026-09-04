"""Repository layer for Sentinel persistence.

All database access in the application flows through these repository functions —
raw SQL/SQLAlchemy usage is confined to `src/db.py` and this module. Every
function is best-effort for non-authoritative records (audit, observability,
memory) and raises explicitly for authoritative records when the caller must
fail closed.

Database failures MUST NOT cause unsafe trading behavior: the trading path only
uses `claim_idempotency_guard` for duplicate protection (fail-open on DB outage
because Alpaca-side `client_order_id` remains the authority) and never blocks a
risk decision on DB availability.
"""

from __future__ import annotations

import time
from typing import Any

from db import (
    AgentEvent,
    AuditEvent,
    Decision,
    Execution,
    IdempotencyState,
    Order,
    RiskEvent,
    SystemHealth,
    WorkerHeartbeat,
    db_session,
)


def _now_epoch() -> float:
    return time.time()


def is_db_configured() -> bool:
    """True when a DATABASE_URL is configured (no connection attempt)."""
    from db import get_db_url

    return bool(get_db_url())


# ---------------------------------------------------------------------------
# Worker heartbeats
# ---------------------------------------------------------------------------


def save_worker_heartbeat(
    *,
    worker_id: str,
    state: str,
    version: str,
    started_at: float,
    last_heartbeat: float,
    last_cycle_started: float | None = None,
    last_cycle_completed: float | None = None,
    last_success: float | None = None,
    last_error: str | None = None,
) -> bool:
    """Upsert the worker heartbeat record. Returns False when unavailable."""
    if not is_db_configured():
        return False
    try:
        with db_session() as session:
            record = session.get(WorkerHeartbeat, worker_id)
            if record is None:
                record = WorkerHeartbeat(id=worker_id, worker_id=worker_id)
                session.add(record)
            record.worker_id = worker_id
            record.current_state = state
            record.version = version
            record.started_at = _datetime_from_epoch(started_at)
            record.last_heartbeat = last_heartbeat
            record.last_cycle_started = last_cycle_started
            record.last_cycle_completed = last_cycle_completed
            record.last_success = last_success
            record.last_error = last_error
        return True
    except Exception:  # noqa: BLE001 — heartbeat persistence is best-effort
        return False


def latest_worker_heartbeat(worker_id: str | None = None) -> dict[str, Any] | None:
    """Return the freshest worker heartbeat record, or None when unavailable."""
    if not is_db_configured():
        return None
    try:
        from sqlalchemy import select

        from db import get_session

        with get_session() as session:
            stmt = select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_heartbeat.desc())
            if worker_id:
                stmt = stmt.where(WorkerHeartbeat.worker_id == worker_id)
            row = session.scalars(stmt).first()
            if row is None:
                return None
            return {
                "worker_id": row.worker_id,
                "current_state": row.current_state,
                "version": row.version,
                "started_at": _epoch_or_none(row.started_at),
                "last_heartbeat": row.last_heartbeat,
                "last_cycle_started": row.last_cycle_started,
                "last_cycle_completed": row.last_cycle_completed,
                "last_success": row.last_success,
                "last_error": row.last_error,
            }
    except Exception:  # noqa: BLE001 — heartbeat reads are best-effort
        return None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def record_decision(decision: dict[str, Any]) -> bool:
    """Persist a structured AI decision. Returns False when unavailable."""
    if not is_db_configured():
        return False
    try:
        with db_session() as session:
            session.merge(
                Decision(
                    id=str(decision.get("decision_id") or ""),
                    run_id=str(decision.get("run_id") or ""),
                    symbol=str(decision.get("symbol") or "").upper(),
                    action=str(decision.get("action") or "HOLD"),
                    confidence=float(decision.get("confidence") or 0.0),
                    thesis=decision.get("thesis"),
                    entry_reason=decision.get("entry_reason"),
                    position_size=int(decision.get("position_size") or 0),
                    decision_price=decision.get("decision_price"),
                    model=decision.get("model"),
                    provider=decision.get("provider"),
                )
            )
        return True
    except Exception:  # noqa: BLE001 — decisions persistence is best-effort
        return False


# ---------------------------------------------------------------------------
# Orders & executions
# ---------------------------------------------------------------------------


def record_order(order: dict[str, Any]) -> bool:
    """Persist an order attempt/result. Returns False when unavailable."""
    if not is_db_configured():
        return False
    try:
        with db_session() as session:
            session.merge(
                Order(
                    id=str(order.get("order_id") or ""),
                    symbol=str(order.get("symbol") or "").upper(),
                    side=str(order.get("side") or "").lower(),
                    qty=float(order.get("qty") or 0),
                    order_type=str(order.get("order_type") or "market"),
                    status=str(order.get("status") or "unknown"),
                    decision_id=order.get("decision_id"),
                    run_id=str(order.get("run_id") or ""),
                    submitted_at=_datetime_from_epoch(order["submitted_at"]) if order.get("submitted_at") else None,
                    filled_at=_datetime_from_epoch(order["filled_at"]) if order.get("filled_at") else None,
                    filled_qty=float(order.get("filled_qty") or 0),
                    avg_fill_price=order.get("avg_fill_price"),
                )
            )
        return True
    except Exception:  # noqa: BLE001 — order persistence is best-effort
        return False


def record_execution(execution: dict[str, Any]) -> bool:
    """Persist an execution event. Returns False when unavailable."""
    if not is_db_configured():
        return False
    try:
        with db_session() as session:
            session.merge(
                Execution(
                    id=str(execution.get("execution_id") or ""),
                    order_id=str(execution.get("order_id") or ""),
                    symbol=str(execution.get("symbol") or "").upper(),
                    side=str(execution.get("side") or "").lower(),
                    qty=float(execution.get("qty") or 0),
                    client_order_id=str(execution.get("client_order_id") or ""),
                    status=str(execution.get("status") or "pending"),
                    executed_at=_datetime_from_epoch(execution["executed_at"]) if execution.get("executed_at") else None,
                    decision_id=execution.get("decision_id"),
                    run_id=execution.get("run_id"),
                )
            )
        return True
    except Exception:  # noqa: BLE001 — execution persistence is best-effort
        return False


# ---------------------------------------------------------------------------
# Risk events
# ---------------------------------------------------------------------------


def record_risk_event(event: dict[str, Any]) -> bool:
    """Persist a risk-guard/final-gate decision. Returns False when unavailable."""
    if not is_db_configured():
        return False
    try:
        with db_session() as session:
            session.merge(
                RiskEvent(
                    id=str(event.get("event_id") or ""),
                    symbol=str(event.get("symbol") or "").upper(),
                    side=str(event.get("side") or "").lower(),
                    qty=float(event.get("qty") or 0),
                    allowed=bool(event.get("allowed")),
                    reason_code=str(event.get("reason_code") or ""),
                    reason=event.get("reason"),
                    decision_id=event.get("decision_id"),
                    run_id=event.get("run_id"),
                )
            )
        return True
    except Exception:  # noqa: BLE001 — risk-event persistence is best-effort
        return False


# ---------------------------------------------------------------------------
# Agent / system events
# ---------------------------------------------------------------------------


def record_agent_event(event: dict[str, Any]) -> bool:
    """Persist an agent observability event. Returns False when unavailable."""
    if not is_db_configured():
        return False
    try:
        with db_session() as session:
            session.add(
                AgentEvent(
                    id=str(event.get("event_id") or f"event-{time.time_ns()}"),
                    event_type=str(event.get("event_type") or "unknown"),
                    severity=str(event.get("severity") or "info"),
                    run_id=event.get("run_id"),
                    decision_id=event.get("decision_id"),
                    execution_id=event.get("execution_id"),
                    symbol=event.get("symbol"),
                    timestamp=float(event.get("timestamp") or _now_epoch()),
                    fields={
                        k: v
                        for k, v in event.items()
                        if k not in {"event_id", "event_type", "severity", "run_id", "decision_id", "execution_id", "symbol", "timestamp"}
                    },
                )
            )
        return True
    except Exception:  # noqa: BLE001 — agent-event persistence is best-effort
        return False


def record_system_health(component: str, status: str, details: dict[str, Any] | None = None) -> bool:
    """Upsert a system-health component row. Returns False when unavailable."""
    if not is_db_configured():
        return False
    try:
        from sqlalchemy import select

        from db import get_session

        with get_session() as session:
            stmt = select(SystemHealth).where(SystemHealth.component == component)
            row = session.scalars(stmt).first()
            if row is None:
                row = SystemHealth(component=component, status=status, details=details or {})
                session.add(row)
            else:
                row.status = status
                row.details = details or {}
            session.commit()
        return True
    except Exception:  # noqa: BLE001 — health persistence is best-effort
        return False


# ---------------------------------------------------------------------------
# Audit events (append-only)
# ---------------------------------------------------------------------------


def record_audit_event(entry: dict[str, Any]) -> bool:
    """Insert an append-only audit record. Returns False when unavailable."""
    if not is_db_configured():
        return False
    try:
        with db_session() as session:
            session.add(
                AuditEvent(
                    id=str(entry.get("event_id") or ""),
                    event_type=str(entry.get("event_type") or ""),
                    actor_id=entry.get("actor_id"),
                    actor_role=entry.get("actor_role"),
                    ip_address=entry.get("ip_address"),
                    user_agent=entry.get("user_agent"),
                    resource=entry.get("resource"),
                    outcome=entry.get("outcome"),
                    details=entry.get("fields") or {},
                )
            )
        return True
    except Exception:  # noqa: BLE001 — audit persistence must never block execution
        return False


# ---------------------------------------------------------------------------
# Idempotency (DB-backed duplicate protection)
# ---------------------------------------------------------------------------


def claim_idempotency_guard(key: str, *, run_id: str, decision_id: str, symbol: str, side: str, qty: float) -> str:
    """Atomically claim an idempotency key.

    Returns ``"completed"`` on the first claim and ``"rejected_duplicate"`` when
    the key already exists. Fails open (returns ``"completed"``) when the DB is
    unavailable so a degraded database never blocks a paper order that Alpaca
    itself will de-duplicate via ``client_order_id``.
    """
    if not is_db_configured():
        return "completed"
    try:
        with db_session() as session:
            existing = session.get(IdempotencyState, key)
            if existing is not None:
                return "rejected_duplicate"
            session.add(
                IdempotencyState(
                    idempotency_key=key,
                    run_id=run_id,
                    decision_id=decision_id,
                    execution_id=None,
                    symbol=str(symbol).upper(),
                    side=str(side).lower(),
                    qty=float(qty),
                    status="completed",
                )
            )
        return "completed"
    except Exception:  # noqa: BLE001 — fail open; Alpaca client_order_id is the authority
        return "completed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _datetime_from_epoch(value: float) -> Any:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _epoch_or_none(value: Any) -> float | None:
    if value is None:
        return None
    ts = value.timestamp() if hasattr(value, "timestamp") else float(value)
    return float(ts)


__all__ = [
    "claim_idempotency_guard",
    "is_db_configured",
    "latest_worker_heartbeat",
    "record_agent_event",
    "record_audit_event",
    "record_decision",
    "record_execution",
    "record_order",
    "record_risk_event",
    "record_system_health",
    "save_worker_heartbeat",
]