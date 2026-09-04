"""Repository layer for Sentinel persistence.

All database access in the application flows through these repository functions —
raw SQL/SQLAlchemy usage is confined to `src/db.py` and this module. Every
function is best-effort for non-authoritative records (audit, observability,
memory) and raises explicitly for authoritative records when the caller must
fail closed.

Database failures MUST NOT cause unsafe trading behavior: the trading path only
uses `claim_idempotency_guard` for duplicate protection. On DB outage, the system
requires broker-side idempotency reconciliation via Alpaca client_order_id.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
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
    WorkerLease,
    db_session,
    get_session,
)


def _now_epoch() -> float:
    return time.time()


def _now_datetime() -> datetime:
    return datetime.now(timezone.utc)


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
            record.last_heartbeat = _datetime_from_epoch(last_heartbeat)
            record.last_cycle_started = _datetime_or_none(last_cycle_started)
            record.last_cycle_completed = _datetime_or_none(last_cycle_completed)
            record.last_success = _datetime_or_none(last_success)
            record.last_error = last_error
            record.updated_at = _now_datetime()
        return True
    except Exception:  # noqa: BLE001 — heartbeat persistence is best-effort
        return False


def latest_worker_heartbeat(worker_id: str | None = None) -> dict[str, Any] | None:
    """Return the freshest worker heartbeat record, or None when unavailable."""
    if not is_db_configured():
        return None
    try:
        from sqlalchemy import select

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
                "last_heartbeat": _epoch_or_none(row.last_heartbeat),
                "last_cycle_started": _epoch_or_none(row.last_cycle_started),
                "last_cycle_completed": _epoch_or_none(row.last_cycle_completed),
                "last_success": _epoch_or_none(row.last_success),
                "last_error": row.last_error,
            }
    except Exception:  # noqa: BLE001 — heartbeat reads are best-effort
        return None


# ---------------------------------------------------------------------------
# Worker Lease / Leader Election
# ---------------------------------------------------------------------------


def acquire_worker_lease(*, worker_id: str, lease_expiry_seconds: int = 300) -> bool:
    """Acquire the worker lease for leader election.
    
    Returns True if lease acquired or renewed by this worker.
    Returns False if another worker holds a valid lease.
    """
    if not is_db_configured():
        return True  # No DB = single worker mode (development)
    
    try:
        from sqlalchemy import select
        
        with db_session() as session:
            now = _now_datetime()
            
            # Check for existing valid lease
            stmt = select(WorkerLease).where(
                WorkerLease.is_active == True,
                WorkerLease.expires_at > now,
            )
            existing = session.scalars(stmt).first()
            
            if existing is not None:
                if existing.worker_id == worker_id:
                    # Renew our own lease
                    existing.expires_at = now + __import__("datetime").timedelta(seconds=lease_expiry_seconds)
                    existing.last_renewed_at = now
                    return True
                else:
                    # Another worker holds the lease
                    return False
            
            # No valid lease - acquire it
            lease = WorkerLease(
                worker_id=worker_id,
                acquired_at=now,
                expires_at=now + __import__("datetime").timedelta(seconds=lease_expiry_seconds),
                last_renewed_at=now,
                is_active=True,
            )
            session.add(lease)
            return True
    except Exception:
        return False  # Fail closed - don't run if lease can't be acquired


def renew_worker_lease(*, worker_id: str, lease_expiry_seconds: int = 300) -> bool:
    """Renew the worker lease."""
    if not is_db_configured():
        return True
    
    try:
        from sqlalchemy import select
        
        with db_session() as session:
            now = _now_datetime()
            
            stmt = select(WorkerLease).where(
                WorkerLease.worker_id == worker_id,
                WorkerLease.is_active == True,
            )
            lease = session.scalars(stmt).first()
            
            if lease is None:
                return False
            
            lease.expires_at = now + __import__("datetime").timedelta(seconds=lease_expiry_seconds)
            lease.last_renewed_at = now
            return True
    except Exception:
        return False


def release_worker_lease(*, worker_id: str) -> bool:
    """Release the worker lease."""
    if not is_db_configured():
        return True
    
    try:
        from sqlalchemy import select
        
        with db_session() as session:
            stmt = select(WorkerLease).where(
                WorkerLease.worker_id == worker_id,
                WorkerLease.is_active == True,
            )
            lease = session.scalars(stmt).first()
            
            if lease is not None:
                lease.is_active = False
                lease.released_at = _now_datetime()
            return True
    except Exception:
        return False


def get_active_lease() -> dict[str, Any] | None:
    """Get the currently active worker lease."""
    if not is_db_configured():
        return None
    
    try:
        from sqlalchemy import select
        
        with get_session() as session:
            now = _now_datetime()
            stmt = select(WorkerLease).where(
                WorkerLease.is_active == True,
                WorkerLease.expires_at > now,
            ).order_by(WorkerLease.expires_at.desc())
            
            lease = session.scalars(stmt).first()
            if lease is None:
                return None
            
            return {
                "worker_id": lease.worker_id,
                "acquired_at": _epoch_or_none(lease.acquired_at),
                "expires_at": _epoch_or_none(lease.expires_at),
                "last_renewed_at": _epoch_or_none(lease.last_renewed_at),
            }
    except Exception:
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
    the key already exists. 
    
    IMPORTANT: When DB is unavailable, returns ``"db_unavailable"`` to signal
    the caller MUST perform broker-side reconciliation via Alpaca client_order_id
    before submitting any order. This prevents duplicate orders during DB outages.
    
    The caller is responsible for:
    1. Checking if DB claims the key
    2. If DB unavailable, querying Alpaca for existing orders with the same client_order_id
    3. Only submitting if no existing order is found
    """
    if not is_db_configured():
        # No DB configured - signal that broker-side reconciliation is required
        return "db_unavailable"
    
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
                    created_at=_now_datetime(),
                )
            )
        return "completed"
    except Exception:  # noqa: BLE001
        # DB error - signal that broker-side reconciliation is required
        # DO NOT fail open - this prevents duplicate orders during DB outages
        return "db_unavailable"


def check_idempotency_status(key: str) -> dict[str, Any] | None:
    """Check the status of an idempotency key without claiming it."""
    if not is_db_configured():
        return None
    try:
        with get_session() as session:
            record = session.get(IdempotencyState, key)
            if record is None:
                return None
            return {
                "idempotency_key": record.idempotency_key,
                "run_id": record.run_id,
                "decision_id": record.decision_id,
                "execution_id": record.execution_id,
                "symbol": record.symbol,
                "side": record.side,
                "qty": record.qty,
                "status": record.status,
                "created_at": _epoch_or_none(record.created_at),
            }
    except Exception:
        return None


def update_idempotency_execution(key: str, execution_id: str) -> bool:
    """Update the execution_id for an idempotency record after order submission."""
    if not is_db_configured():
        return False
    try:
        with db_session() as session:
            record = session.get(IdempotencyState, key)
            if record is not None:
                record.execution_id = execution_id
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _datetime_from_epoch(value: float) -> Any:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _datetime_or_none(value: float | None) -> Any:
    if value is None:
        return None
    return _datetime_from_epoch(value)


def _epoch_or_none(value: Any) -> float | None:
    if value is None:
        return None
    ts = value.timestamp() if hasattr(value, "timestamp") else float(value)
    return float(ts)


__all__ = [
    "acquire_worker_lease",
    "check_idempotency_status",
    "claim_idempotency_guard",
    "get_active_lease",
    "is_db_configured",
    "latest_worker_heartbeat",
    "record_agent_event",
    "record_audit_event",
    "record_decision",
    "record_execution",
    "record_order",
    "record_risk_event",
    "record_system_health",
    "release_worker_lease",
    "renew_worker_lease",
    "save_worker_heartbeat",
    "update_idempotency_execution",
]