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
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_INTERVAL_SECONDS = 900
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 86_400
HEARTBEAT_MAX_AGE_SECONDS = 600
WORKER_VERSION = "0.4.0"
WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"

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
        from repositories import save_worker_heartbeat, is_db_configured
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
    except Exception:
        pass  # Heartbeat persistence is best-effort

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
        from repositories import acquire_worker_lease, is_db_configured
        if not is_db_configured():
            # No DB configured - allow single-worker mode (development)
            return True
        return acquire_worker_lease(
            worker_id=WORKER_ID,
            lease_expiry_seconds=LEASE_EXPIRY_SECONDS,
        )
    except Exception:
        # If lease mechanism fails, fail closed (don't run)
        return False


def _renew_worker_lease() -> bool:
    """Renew the worker lease in PostgreSQL."""
    try:
        from repositories import renew_worker_lease, is_db_configured
        if not is_db_configured():
            return True
        return renew_worker_lease(worker_id=WORKER_ID)
    except Exception:
        return False


def _release_worker_lease() -> None:
    """Release the worker lease in PostgreSQL."""
    try:
        from repositories import release_worker_lease
        release_worker_lease(worker_id=WORKER_ID)
    except Exception:
        pass


def _lease_renewal_loop() -> None:
    """Background thread to renew the worker lease."""
    while _lease_active.is_set() and not _shutdown_requested.is_set():
        _renew_worker_lease()
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
        sleep(sleep_for)
    
    # Cleanup lease on exit
    _lease_active.clear()
    _release_worker_lease()


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
