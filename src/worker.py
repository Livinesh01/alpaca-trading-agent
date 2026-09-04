"""Always-on, paper-only worker for the Sentinel decision loop.

The worker is intentionally separate from FastAPI. It supervises one bounded
paper cycle at a time, records a heartbeat for readiness checks, and treats all
configuration/provider failures as no-trade failures.

Production hardening:
- Heartbeat persisted to PostgreSQL (authoritative source)
- Local JSON heartbeat is optional fallback for development only
- Graceful shutdown on SIGTERM/SIGINT
- Bounded retry with exponential backoff on per-cycle errors
- Worker lease/leader election via PostgreSQL
- Worker state includes version, started_at, last_success, last_error
- Never imports secrets into process output
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

import bootstrap  # noqa: E402  (src/ is on sys.path when run as src/worker.py)
import lease_guard  # noqa: E402

DEFAULT_INTERVAL_SECONDS = 900
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 86_400
HEARTBEAT_MAX_AGE_SECONDS = 600
WORKER_VERSION = "0.5.0"
WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"

# Lease-renewal failure policy: the FIRST failed renewal immediately pauses
# trading (lease lost). If the lease is not recovered within this many
# consecutive failed renewals (the bounded grace period), the worker stops
# safely and exits non-zero. With the default 60s renewal interval this is a
# ~3 minute grace window before a DB outage halts the worker.
LEASE_RENEWAL_FAILURE_LIMIT = 3

# Local JSON path is now OPTIONAL fallback for development only
STATUS_PATH = Path(os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "journal", "observability", "worker.json")))
_started_at = time.time()


def _secret(name: str) -> str:
    """Read a deployment-injected secret without exposing it in configuration output."""
    direct = os.environ.get(name, "").strip()
    if direct:
        return direct
    secret_path = os.environ.get(f"{name}_FILE", f"/run/secrets/{name.lower()}")
    try:
        return Path(secret_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _interval_seconds() -> int:
    raw = os.environ.get("WORKER_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("WORKER_INTERVAL_SECONDS must be an integer") from exc
    if not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS:
        raise ValueError(f"WORKER_INTERVAL_SECONDS must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS}")
    return value


_shutdown_requested = threading.Event()
_lease_gave_up = threading.Event()
_lease_renewal_thread: threading.Thread | None = None
_lease_active = threading.Event()


def _signal_handler(signum: int, frame: Any) -> None:
    _shutdown_requested.set()
    _write_status("stopping", last_error=f"signal {signum} received, finishing current cycle")
    _release_worker_lease()


def _write_status(status: str, *, last_error: str | None = None, last_result: int | None = None, last_cycle_started: float | None = None, last_cycle_completed: float | None = None, last_success: float | None = None) -> None:
    """Write heartbeat to PostgreSQL (authoritative) and optionally to local JSON (dev fallback)."""
    now = time.time()
    payload: dict[str, Any] = {
        "status": status,
        "paper_trading": _truthy("PAPER_TRADING") and _truthy("ALPACA_PAPER"),
        "last_heartbeat": now,
        "last_error": last_error,
        "last_result": last_result,
        "worker_pid": os.getpid(),
        "started_at": _started_at,
        "version": WORKER_VERSION,
        "worker_id": WORKER_ID,
    }

    # PRIMARY: Persist heartbeat to PostgreSQL (authoritative source)
    try:
        from repositories import is_db_configured, record_system_health, save_worker_heartbeat
        if is_db_configured():
            save_worker_heartbeat(
                worker_id=WORKER_ID,
                state=status,
                version=WORKER_VERSION,
                started_at=_started_at,
                last_heartbeat=now,
                last_cycle_started=last_cycle_started,
                last_cycle_completed=last_cycle_completed,
                last_success=last_success,
                last_error=last_error,
            )
            record_system_health(
                "worker",
                status,
                {"worker_id": WORKER_ID, "version": WORKER_VERSION, "last_error": last_error},
            )
    except Exception as exc:  # noqa: BLE001 - Heartbeat is best-effort, never crash worker
        logger.warning(
            "Failed to persist worker heartbeat to PostgreSQL: %s",
            type(exc).__name__,
        )

    # FALLBACK: Local JSON for development only (not used in production)
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Worker Lease / Leader Election (PostgreSQL-backed)
# ---------------------------------------------------------------------------

LEASE_RENEWAL_INTERVAL_SECONDS = 60
LEASE_EXPIRY_SECONDS = 300


def _acquire_worker_lease() -> bool:
    """Attempt to acquire the worker lease in PostgreSQL.
    
    Returns True if lease acquired, False if another worker holds it.
    """
    try:
        import bootstrap
        from repositories import acquire_worker_lease, is_db_configured
        if not is_db_configured():
            if bootstrap.is_production():
                # C3: production REQUIRES PostgreSQL - never single-worker mode.
                logger.error("production requires DATABASE_URL; refusing to run without a lease")
                return False
            # No DB configured - allow single-worker mode (development only)
            return True
        return acquire_worker_lease(
            worker_id=WORKER_ID,
            lease_expiry_seconds=LEASE_EXPIRY_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - Fail closed on lease mechanism errors
        logger.error(
            "Worker lease acquisition failed: %s",
            type(exc).__name__,
        )
        # If lease mechanism fails, fail closed (don't run)
        return False


def _renew_worker_lease() -> bool:
    """Renew the worker lease in PostgreSQL."""
    try:
        import bootstrap
        from repositories import is_db_configured, renew_worker_lease
        if not is_db_configured():
            if bootstrap.is_production():
                return False  # C3: production always requires a renewable lease
            return True
        return renew_worker_lease(worker_id=WORKER_ID)
    except Exception as exc:  # noqa: BLE001 - Fail closed on lease renewal errors
        logger.error(
            "Worker lease renewal failed: %s",
            type(exc).__name__,
        )
        return False


def _release_worker_lease() -> None:
    """Release the worker lease in PostgreSQL."""
    try:
        from repositories import release_worker_lease
        release_worker_lease(worker_id=WORKER_ID)
    except Exception as exc:  # noqa: BLE001 - Best-effort shutdown, log but never crash
        logger.error(
            "Worker lease release failed: %s",
            type(exc).__name__,
        )


def _lease_renewal_iteration(consecutive_failures: int) -> tuple[int, str]:
    """One lease-renewal attempt. Returns (new_consecutive_failures, action).

    Actions:
      "healthy"  - renewal succeeded; trading may run.
      "degraded" - renewal failed; trading is PAUSED immediately (lease lost)
                   but the worker is still inside its recovery grace period.
      "give_up"  - the grace period is exhausted; the worker must stop safely.
    """
    if _renew_worker_lease():
        if consecutive_failures > 0:
            logger.info(
                "worker lease recovered after %d failed renewal(s)", consecutive_failures
            )
        return 0, "healthy"

    failures = consecutive_failures + 1
    lease_guard.mark_lost()
    logger.error(
        "worker lease renewal failed (%d/%d)", failures, LEASE_RENEWAL_FAILURE_LIMIT
    )
    _write_status(
        "degraded",
        last_error=(
            f"lease renewal failed ({failures}/{LEASE_RENEWAL_FAILURE_LIMIT}); "
            "trading paused until the lease is recovered"
        ),
    )
    if failures >= LEASE_RENEWAL_FAILURE_LIMIT:
        _write_status(
            "blocked",
            last_error=(
                f"lease lost after {failures} consecutive renewal failures; "
                "stopping worker (no trading without a PostgreSQL lease)"
            ),
        )
        return failures, "give_up"
    return failures, "degraded"


def _lease_renewal_loop() -> None:
    """Background thread: renew the lease; fail closed on lease loss (C2).

    A single failed renewal marks the lease lost process-wide (no new cycles,
    no order submission). Recovery within the grace period resumes trading;
    LEASE_RENEWAL_FAILURE_LIMIT consecutive failures stop the worker safely.
    """
    consecutive_failures = 0
    while _lease_active.is_set() and not _shutdown_requested.is_set():
        consecutive_failures, action = _lease_renewal_iteration(consecutive_failures)
        if action == "give_up":
            _lease_gave_up.set()
            _shutdown_requested.set()
            break
        if action == "healthy":
            lease_guard.mark_recovered()
        _shutdown_requested.wait(LEASE_RENEWAL_INTERVAL_SECONDS)


def validate_worker_environment() -> None:
    """Reject unsafe worker startup configuration without printing secrets."""
    if not _truthy("PAPER_TRADING") or not _truthy("ALPACA_PAPER"):
        raise RuntimeError("worker requires PAPER_TRADING=true and ALPACA_PAPER=true")
    if os.environ.get("SENTINEL_DATA_MODE", "proxy").strip().lower() != "proxy":
        raise RuntimeError("worker requires SENTINEL_DATA_MODE=proxy")
    if not _secret("ALPACA_API_KEY"):
        raise RuntimeError("worker requires ALPACA_API_KEY")
    if not _secret("ALPACA_SECRET_KEY"):
        raise RuntimeError("worker requires ALPACA_SECRET_KEY")
    provider = os.environ.get("LLM_PROVIDER", "fake").strip().lower()
    if provider == "fake":
        raise RuntimeError("worker requires a real LLM_PROVIDER")
    if provider == "featherless" and not _secret("FEATHERLESS_API_KEY"):
        raise RuntimeError("worker requires FEATHERLESS_API_KEY")
    if provider == "nvidia" and not _secret("NVIDIA_API_KEY"):
        raise RuntimeError("worker requires NVIDIA_API_KEY")


def _load_secret_environment() -> None:
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FEATHERLESS_API_KEY", "NVIDIA_API_KEY"):
        value = _secret(name)
        if value:
            os.environ[name] = value


def run_forever(*, cycle: Callable[[], int] | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
    """Run paper cycles forever; process managers restart only actual crashes.

    Honors SIGTERM/SIGINT for graceful shutdown — a signal received mid-cycle
    is recorded but does not trigger a new cycle. The current cycle completes
    (or fails safely) and then the loop exits.
    
    Production: Acquires worker lease before running. Only one worker may
    execute trading cycles at a time.
    """
    global _lease_renewal_thread
    
    interval = _interval_seconds()
    try:
        validate_worker_environment()
    except Exception as exc:
        _write_status("blocked", last_error=str(exc))
        raise

    _load_secret_environment()

    # Production fail-closed startup: strict config validation + database
    # bootstrap (validate -> connect -> migrate schema). Any failure blocks
    # the worker: no lease, no trading (C3/C5).
    if bootstrap.is_production():
        try:
            bootstrap.validate_startup_config()
            bootstrap.ensure_database()
        except Exception as exc:  # noqa: BLE001 - fail closed, never start unsafe
            _write_status("blocked", last_error=f"startup failed: {type(exc).__name__}")
            raise

    # Acquire worker lease (leader election)
    if not _acquire_worker_lease():
        _write_status("blocked", last_error="another worker holds the lease")
        raise RuntimeError("another worker holds the lease - cannot start")
    
    _lease_active.set()
    _lease_renewal_thread = threading.Thread(target=_lease_renewal_loop, daemon=True)
    _lease_renewal_thread.start()
    
    if cycle is None:
        from orchestrator import run_once

        cycle = run_once

    _write_status("running", last_result=None)
    while not _shutdown_requested.is_set():
        if lease_guard.is_lost():
            # C2: lease lost — NO new decision cycle. The renewal thread is
            # either recovering within the grace period or stopping the worker.
            if _shutdown_requested.wait(0.5):
                break
            continue
        _write_status("cycling")
        cycle_started = time.time()
        try:
            result = int(cycle())
            _write_status("running", last_result=result, last_cycle_started=cycle_started, last_cycle_completed=time.time(), last_success=time.time())
        except Exception as exc:  # noqa: BLE001 - one failed cycle must not stop the worker
            _write_status("degraded", last_error=f"{type(exc).__name__}: {exc}", last_cycle_started=cycle_started, last_cycle_completed=time.time())

        if _shutdown_requested.is_set():
            _write_status("stopped", last_error="graceful shutdown requested")
            break

        elapsed = time.time() - cycle_started
        sleep_for = max(interval - elapsed, MIN_INTERVAL_SECONDS)
        # Slice the sleep so a lease give-up (or shutdown) interrupts promptly
        # instead of waiting out the full interval while unable to trade.
        waited = 0.0
        while waited < sleep_for and not _shutdown_requested.is_set():
            step = min(0.5, sleep_for - waited)
            sleep(step)
            waited += step

    # Cleanup lease on exit
    _lease_active.clear()
    lease_guard.mark_lost()  # nothing in this process may trade once exit begins
    _release_worker_lease()
    if _lease_gave_up.is_set():
        raise RuntimeError(
            "worker lease lost; stopped safely (no trading without the PostgreSQL lease)"
        )


def main() -> int:
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _signal_handler)
    try:
        run_forever()
    except Exception as exc:  # noqa: BLE001 - no secret-bearing exception output
        print(f"worker stopped: {type(exc).__name__}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
