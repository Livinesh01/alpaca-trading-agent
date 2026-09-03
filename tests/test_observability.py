import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from observability import Observability


def test_structured_event_is_bounded_redacted_and_correlated(tmp_path):
    observer = Observability(tmp_path)
    observer.emit(
        "order_submitted",
        run_id="run-1",
        decision_id="decision-1",
        execution_id="execution-1",
        api_key="never-write-this",
        summary="short result",
    )
    event = json.loads(observer.events_path.read_text(encoding="utf-8"))
    assert event["run_id"] == "run-1"
    assert event["decision_id"] == "decision-1"
    assert event["execution_id"] == "execution-1"
    assert event["api_key"] == "[REDACTED]"
    assert "never-write-this" not in observer.events_path.read_text(encoding="utf-8")


def test_counters_latency_health_and_repeated_failure_alerts(tmp_path):
    observer = Observability(tmp_path)
    with observer.observe("llm"):
        pass
    for _ in range(3):
        observer.emit("llm_failure", run_id="run-1")
    observer.emit("run_completed", run_id="run-1")
    assert observer.counters["llm_failure"] == 3
    assert observer.snapshot()["latency_samples"]["llm"] == 1
    assert observer.health["last_successful_run"] is not None
    assert "repeated LLM failures" in observer.alerts()


def test_health_exposes_kill_switch_without_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_KILL_SWITCH", "true")
    observer = Observability(tmp_path)
    observer.emit("run_failed", run_id="run-2", authorization="Bearer secret-value")
    status = json.loads(observer.health_path.read_text(encoding="utf-8"))
    assert status["kill_switch_enabled"] is True
    assert "secret-value" not in observer.health_path.read_text(encoding="utf-8")


def test_observability_write_failure_is_isolated(tmp_path, monkeypatch):
    observer = Observability(tmp_path)
    monkeypatch.setattr(type(observer.events_path), "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    observer.emit("run_completed", run_id="run-3")
    assert observer.counters["run_completed"] == 1
    assert observer.health["last_successful_run"] is not None


def test_status_read_is_safe_when_missing_or_corrupt(tmp_path):
    assert Observability.read_status(tmp_path) == {}
    (tmp_path / "health.json").write_text("broken", encoding="utf-8")
    assert Observability.read_status(tmp_path) == {}


def test_status_command_does_not_run_workflow(monkeypatch, tmp_path, capsys):
    import observability
    import orchestrator

    monkeypatch.setattr(observability, "DEFAULT_OBSERVABILITY_DIR", str(tmp_path))
    monkeypatch.setattr(orchestrator, "run_once", lambda **kwargs: (_ for _ in ()).throw(AssertionError("workflow called")))
    assert orchestrator.main(["--status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert "backend" in status and "kill_switch_enabled" in status
