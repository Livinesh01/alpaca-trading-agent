"""Fail-closed persistence adapter for the production execution path.

`SentinelPersistence` wires the repository layer into the DecisionLoop/order
path. When it is constructed (production: DATABASE_URL configured), ANY
persistence failure raises — the run aborts and ZERO orders are submitted.
Database failure is never interpreted as success.

When the database is not configured (development/tests) no adapter is
constructed and existing behavior is unchanged: nothing here talks to a
database that was never configured.

`PgMirroredObservability` additionally mirrors informational observability
events into `agent_events` so the production API can serve /activity from
PostgreSQL without depending on worker-local JSONL files. The mirror is
best-effort: it must never break execution.
"""

from __future__ import annotations

import time
from typing import Any

import repositories as repo
from errors import DuplicateExecutionError, ExecutionError
from observability import Observability

__all__ = ["SentinelPersistence", "PgMirroredObservability"]


class SentinelPersistence:
    """Fail-closed persistence used by the production decision/order path."""

    def healthy(self) -> bool:
        """True when PostgreSQL accepts connections right now."""
        return repo.is_db_configured() and repo.is_database_available()

    def preflight(self) -> None:
        """Fail fast (before any LLM/market work) when PostgreSQL is unavailable."""
        if not self.healthy():
            raise ExecutionError(
                "PostgreSQL unavailable; refusing to run the decision cycle (fail closed)"
            )

    # -- decisions / orders / executions / risk ------------------------------

    def record_decision(self, decision: dict[str, Any]) -> None:
        self._require(repo.record_decision(decision), "decision")

    def record_order(self, order: dict[str, Any]) -> None:
        self._require(repo.record_order(order), "order intent")

    def record_execution(self, execution: dict[str, Any]) -> None:
        self._require(repo.record_execution(execution), "execution result")

    def record_risk_event(self, event: dict[str, Any]) -> None:
        self._require(repo.record_risk_event(event), "risk verdict")

    # -- durable idempotency -------------------------------------------------

    def claim_idempotency(
        self,
        key: str,
        *,
        run_id: str,
        decision_id: str,
        symbol: str,
        side: str,
        qty: float,
    ) -> None:
        """Atomically claim the durable idempotency key BEFORE order submission.

        Raises on duplicate or on any database failure (fail closed — never
        submit an order that cannot be durably claimed).
        """
        if not self.healthy():
            raise ExecutionError(
                "PostgreSQL unavailable; durable idempotency claim refused (fail closed)"
            )
        status = repo.claim_idempotency_guard(
            key,
            run_id=run_id,
            decision_id=decision_id,
            symbol=symbol,
            side=side,
            qty=qty,
        )
        if status == "rejected_duplicate":
            prior = repo.check_idempotency_status(key)
            prior_exec = (prior or {}).get("execution_id")
            raise DuplicateExecutionError(
                f"duplicate execution rejected for idempotency key {key}"
                + (f" (prior execution {prior_exec})" if prior_exec else "")
            )
        if status != "completed":
            raise ExecutionError(
                f"durable idempotency claim unavailable ({status}); order not submitted"
            )

    def update_idempotency_execution(self, key: str, execution_id: str) -> bool:
        """Link the broker submission to its durable claim (best-effort link)."""
        return repo.update_idempotency_execution(key, execution_id)

    # -- helpers -------------------------------------------------------------

    def _require(self, ok: bool, what: str) -> None:
        if not ok:
            raise ExecutionError(
                f"PostgreSQL persistence failed for {what}; run aborted (fail closed)"
            )


class PgMirroredObservability(Observability):
    """Observability that mirrors every event into PostgreSQL `agent_events`.

    JSONL emission keeps its existing behavior (local diagnostics); the
    PostgreSQL mirror makes the production activity feed authoritative without
    requiring the API and worker to share a filesystem. The mirror is
    best-effort and can never break execution.
    """

    def emit(self, event_type: str, **kwargs: Any) -> None:  # noqa: D102 — see base
        super().emit(event_type, **kwargs)
        try:
            event: dict[str, Any] = {"timestamp": time.time(), "event_type": event_type, **kwargs}
            repo.record_agent_event(event)
        except Exception:  # noqa: BLE001 — mirror is informational only
            pass