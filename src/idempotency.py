"""Idempotency / duplicate-execution protection for Sentinel.

The same logical decision/run must not accidentally generate multiple executions.
Uses deterministic identifiers (idempotency keys) so that:
- Same idempotency key cannot create multiple executions
- Retries cannot duplicate an order
- Duplicate requests are safely rejected
- State is observable
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def make_idempotency_key(*, symbol: str, side: str, qty: int, run_id: str, decision_id: str) -> str:
    """Build a deterministic idempotency key from order intent."""
    payload = {
        "symbol": str(symbol).strip().upper(),
        "side": str(side).strip().lower(),
        "qty": int(qty),
        "run_id": str(run_id),
        "decision_id": str(decision_id),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"idem-{digest}"


@dataclass(frozen=True)
class IdempotencyRecord:
    """Immutable record of an execution attempt."""
    idempotency_key: str
    run_id: str
    decision_id: str
    execution_id: str
    symbol: str
    side: str
    qty: int
    status: str
    timestamp: str


class IdempotencyStore:
    """Thread-safe idempotency guard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._completed: dict[str, IdempotencyRecord] = {}

    def check_and_claim(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        decision_id: str,
        symbol: str,
        side: str,
        qty: int,
    ) -> IdempotencyRecord:
        """Atomically check if a key has been used and claim it if not."""
        with self._lock:
            existing = self._completed.get(idempotency_key)
            if existing is not None:
                return IdempotencyRecord(
                    idempotency_key=idempotency_key,
                    run_id=run_id,
                    decision_id=decision_id,
                    execution_id=_new_id("execution"),
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    status="rejected_duplicate",
                    timestamp=_timestamp(),
                )

            record = IdempotencyRecord(
                idempotency_key=idempotency_key,
                run_id=run_id,
                decision_id=decision_id,
                execution_id=_new_id("execution"),
                symbol=symbol,
                side=side,
                qty=qty,
                status="completed",
                timestamp=_timestamp(),
            )
            self._completed[idempotency_key] = record
            return record

    def is_completed(self, idempotency_key: str) -> bool:
        """Check if a key has already completed (non-claiming read)."""
        with self._lock:
            return idempotency_key in self._completed

    def get(self, idempotency_key: str) -> IdempotencyRecord | None:
        """Retrieve the record for a key, or None if never claimed."""
        with self._lock:
            return self._completed.get(idempotency_key)

    def count(self) -> int:
        """Return the number of completed executions tracked."""
        with self._lock:
            return len(self._completed)

    def reset(self) -> None:
        """Clear all records. Intended for tests only."""
        with self._lock:
            self._completed.clear()
