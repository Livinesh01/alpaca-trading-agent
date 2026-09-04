"""Non-authoritative structured observability for the trading agent."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Self

DEFAULT_OBSERVABILITY_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "journal", "observability"))
_SECRET_KEY = re.compile(r"(api.?key|secret|token|password|authorization|credential)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(?:Bearer\s+)?[A-Za-z0-9_\-]{24,}")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    return value


class Observability:
    """Best-effort structured events, counters, latency, health, and alerts."""

    def __init__(self, directory: str | os.PathLike[str] = DEFAULT_OBSERVABILITY_DIR) -> None:
        self.directory = Path(directory)
        self.events_path = self.directory / "events.jsonl"
        self.health_path = self.directory / "health.json"
        self._lock = threading.Lock()
        self.counters: Counter[str] = Counter()
        self.latencies: dict[str, list[float]] = {}
        self.health: dict[str, Any] = {
            "last_successful_run": None,
            "last_failed_run": None,
            "last_llm_failure": None,
            "last_market_data_failure": None,
            "last_order_failure": None,
            "kill_switch_enabled": os.environ.get("TRADING_KILL_SWITCH", "").strip().lower() in {"1", "true", "yes", "on"},
            "backend": os.environ.get("AGENT_BACKEND", "decision_loop"),
            "provider": os.environ.get("LLM_PROVIDER"),
            "dry_run": False,
        }

    def emit(self, event_type: str, *, run_id: str | None = None, decision_id: str | None = None, execution_id: str | None = None, outcome_id: str | None = None, **fields: Any) -> None:
        event = {"timestamp": time.time(), "event_type": event_type, "run_id": run_id, "decision_id": decision_id, "execution_id": execution_id, "outcome_id": outcome_id, **fields}
        redacted = _redact(event)
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.events_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(redacted, sort_keys=True, separators=(",", ":")) + "\n")
            except Exception as exc:  # noqa: BLE001 - observability cannot stop execution
                del exc
            self.counters[event_type] += 1
            if event_type.endswith("failure"):
                key = {"llm_failure": "last_llm_failure", "market_data_failure": "last_market_data_failure", "order_failed": "last_order_failure"}.get(event_type)
                if key:
                    self.health[key] = event["timestamp"]
            if event_type == "run_completed":
                self.health["last_successful_run"] = event["timestamp"]
            elif event_type == "run_failed":
                self.health["last_failed_run"] = event["timestamp"]
            self._save_health()

    def observe(self, stage: str) -> _Timer:
        return _Timer(self, stage)

    def _save_health(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.health_path.write_text(json.dumps(_redact(self.health), sort_keys=True), encoding="utf-8")
        except Exception:  # noqa: BLE001 — health is informational only
            return

    def record_latency(self, stage: str, seconds: float) -> None:
        with self._lock:
            self.latencies.setdefault(stage, []).append(max(float(seconds), 0.0))
        self.emit("latency_recorded", stage=stage, duration_seconds=seconds)

    def _alerts_unlocked(self, *, failure_threshold: int = 3) -> list[str]:
        """Compute current alerts without acquiring the lock.

        Callers must already hold ``self._lock`` or otherwise synchronize access to
        the shared counters/health dicts. Kept separate from the public ``alerts()``
        so that ``snapshot()`` (which already holds the lock) can compute alert state
        in the same critical section instead of re-entering a non-reentrant lock.
        """
        alerts: list[str] = []
        for event_type, label in (("llm_failure", "repeated LLM failures"), ("market_data_failure", "repeated market-data failures"), ("order_failed", "repeated order failures")):
            if self.counters[event_type] >= failure_threshold:
                alerts.append(label)
        if self.health["kill_switch_enabled"]:
            alerts.append("kill switch enabled")
        return alerts

    def alerts(self, *, failure_threshold: int = 3) -> list[str]:
        with self._lock:
            return self._alerts_unlocked(failure_threshold=failure_threshold)

    def snapshot(self) -> dict[str, Any]:
        """Single consistent point-in-time snapshot of all observable state.

        Everything is read under one critical section so counters, health, alerts,
        and latency samples never observe each other mid-update. Alert computation
        is delegated to ``_alerts_unlocked`` (NOT the lock-acquiring ``alerts()``)
        to avoid re-entering the non-reentrant ``_lock`` — the previous code deadlocked
        here because ``snapshot`` held the lock while calling ``alerts`` which tried to
        reacquire it.
        """
        with self._lock:
            return {
                "health": dict(self.health),
                "counters": dict(self.counters),
                "alerts": self._alerts_unlocked(),
                "latency_samples": {key: len(value) for key, value in self.latencies.items()},
            }

    @classmethod
    def read_status(cls, directory: str | os.PathLike[str] = DEFAULT_OBSERVABILITY_DIR) -> dict[str, Any]:
        path = Path(directory) / "health.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, TypeError, json.JSONDecodeError):
            return {}


class _Timer:
    def __init__(self, owner: Observability, stage: str) -> None:
        self.owner = owner
        self.stage = stage
        self.started = time.monotonic()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.owner.record_latency(self.stage, time.monotonic() - self.started)
