"""Decision service: immutable historical decisions, filters, and replay.

Historical decision records are append-only and never mutated. The replay is a
read-only structured timeline that keeps LLM authority and Python authority
explicitly separate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from api.services.utils import num, parse_timestamp
from memory import MemoryStore

CANONICAL_FIELDS = ("symbol", "action", "confidence", "position_size", "thesis", "entry_reason")


def _matches(record: dict[str, Any], **filters: Any) -> bool:
    symbol = filters.get("symbol")
    if symbol and str(record.get("symbol") or "").upper() != str(symbol).upper():
        return False
    action = filters.get("action")
    if action and str(record.get("action") or "").upper() != str(action).upper():
        return False
    run_id = filters.get("run_id")
    if run_id and str(record.get("run_id") or "") != str(run_id):
        return False
    decision_id = filters.get("decision_id")
    if decision_id and str(record.get("decision_id") or "") != str(decision_id):
        return False
    confidence = num(record.get("confidence"))
    if confidence is not None:
        confidence_min = filters.get("confidence_min")
        if confidence_min is not None and confidence < float(confidence_min):
            return False
        confidence_max = filters.get("confidence_max")
        if confidence_max is not None and confidence > float(confidence_max):
            return False
    record_time = parse_timestamp(record.get("timestamp"))
    if record_time is not None:
        date_from = filters.get("date_from")
        if date_from and record_time < _as_utc(date_from):
            return False
        date_to = filters.get("date_to")
        if date_to and record_time > _as_utc(date_to):
            return False
    return True


def _as_utc(value: str) -> datetime:
    parsed = parse_timestamp(value)
    return parsed or datetime.now(timezone.utc)


def _use_postgres() -> bool:
    from api.services.history_source import use_postgres_history

    return use_postgres_history()


def _pg_unavailable(exc: Exception, page: int, page_size: int) -> dict[str, Any]:
    from api.services.history_source import unavailable

    return unavailable(
        f"decision history unavailable (PostgreSQL): {type(exc).__name__}",
        page=page,
        page_size=page_size,
    )


def list_decisions(store: MemoryStore, *, page: int = 1, page_size: int = 50, **filters: Any) -> dict[str, Any]:
    if _use_postgres():
        # C4: production reads authoritative decision history from PostgreSQL.
        try:
            from repositories import list_decisions as pg_list_decisions

            return pg_list_decisions(page=page, page_size=page_size, **filters)
        except Exception as exc:  # noqa: BLE001 — explicit unavailability, never fabricated
            return _pg_unavailable(exc, page, page_size)
    items = sorted(store.decisions(), key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    filtered = [item for item in items if _matches(item, **filters)]
    page = max(int(page), 1)
    page_size = max(min(int(page_size), 200), 1)
    start = (page - 1) * page_size
    page_items = filtered[start : start + page_size]
    return {
        "items": page_items,
        "available": True,
        "reason": None,
        "pagination": {"page": page, "page_size": page_size, "total": len(filtered)},
    }


def _pg_replay(decision_id: str) -> dict[str, Any] | None:
    """Build the replay timeline from PostgreSQL records (production)."""
    from repositories import (
        get_decision,
        get_execution_for_decision,
        get_risk_event_for_decision,
    )

    record = get_decision(decision_id)
    if record is None:
        return None
    execution = get_execution_for_decision(decision_id)
    risk_event = get_risk_event_for_decision(decision_id)
    timestamp = record.get("timestamp")

    def _stage(name: str, *, status: str, authority: str = "SYSTEM", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        stage = {"stage": name, "status": status, "authority": authority, "timestamp": timestamp}
        if metadata:
            stage["metadata"] = metadata
        return stage

    decision = {
        key: record.get(key)
        for key in (*CANONICAL_FIELDS, "decision_id", "run_id", "timestamp", "model", "provider", "decision_price")
    }
    stages = [
        _stage("RUN", status="recorded", metadata={"run_id": record.get("run_id")}),
        _stage(
            "MARKET DATA",
            status="recorded" if record.get("decision_price") is not None else "unavailable",
            metadata={"decision_price": record.get("decision_price")},
        ),
        _stage(
            "LLM DECISION",
            status="recorded",
            authority="LLM",
            metadata={"action": record.get("action"), "confidence": record.get("confidence")},
        ),
        _stage(
            "RISK CHECK",
            status="allowed" if (risk_event or {}).get("allowed") else ("rejected" if risk_event else "unavailable"),
            authority="PYTHON",
            metadata={"reason": (risk_event or {}).get("reason"), "reason_code": (risk_event or {}).get("reason_code")},
        ),
        _stage(
            "EXECUTION",
            status="recorded" if execution else "not_recorded",
            metadata={
                "execution_id": (execution or {}).get("execution_id"),
                "status": (execution or {}).get("status"),
                "client_order_id": (execution or {}).get("client_order_id"),
            },
        ),
    ]
    return {
        "decision": decision,
        "stages": stages,
        "llm_authority": {
            "authority": "LLM",
            "action": record.get("action"),
            "confidence": record.get("confidence"),
            "thesis": record.get("thesis"),
            "entry_reason": record.get("entry_reason"),
        },
        "python_authority": {
            "authority": "PYTHON",
            "deterministic_quantity": (execution or {}).get("qty"),
            "risk_allowed": (risk_event or {}).get("allowed"),
            "risk_reason": (risk_event or {}).get("reason"),
            "decision_price": record.get("decision_price"),
        },
        "execution": execution,
        "outcome": None,
        "read_only": True,
        "label": "IMMUTABLE HISTORICAL RECORD",
    }


def replay_decision(store: MemoryStore, decision_id: str) -> dict[str, Any] | None:
    if _use_postgres():
        # C4: production replays authoritative PostgreSQL records.
        return _pg_replay(decision_id)
    record = next((item for item in store.decisions() if item.get("decision_id") == decision_id), None)
    if record is None:
        return None
    execution = next((item for item in store.executions() if item.get("decision_id") == decision_id), None)
    outcome = next((item for item in store.outcomes() if item.get("decision_id") == decision_id), None)

    llm_authority = {
        "authority": "LLM",
        "action": record.get("action"),
        "confidence": record.get("confidence"),
        "thesis": record.get("thesis"),
        "entry_reason": record.get("entry_reason"),
    }
    execution_qty = num(execution.get("qty")) if execution else None
    python_authority = {
        "authority": "PYTHON",
        "position_size_parsed": record.get("position_size", 0),
        "deterministic_quantity": int(execution_qty) if execution_qty is not None else None,
        "risk_allowed": bool(execution.get("risk_allowed")) if execution else None,
        "risk_reason": (execution.get("risk_reason") if execution else None) or (record.get("risk_reason")),
        "final_gate": bool(execution.get("final_gate")) if execution else None,
        "decision_price": num(record.get("decision_price")),
    }

    decision = {
        key: record.get(key)
        for key in (*CANONICAL_FIELDS, "decision_id", "run_id", "timestamp", "model", "provider", "decision_price")
    }

    def _stage(name: str, *, status: str, authority: str = "SYSTEM", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        stage = {"stage": name, "status": status, "authority": authority, "timestamp": record.get("timestamp")}
        if metadata:
            stage["metadata"] = metadata
        return stage

    signals = record.get("signals") or record.get("deterministic_signals") or {}
    stages = [
        _stage("RUN", status="recorded", metadata={"run_id": record.get("run_id")}),
        _stage(
            "MARKET DATA",
            status="recorded" if record.get("decision_price") else "unavailable",
            metadata={"decision_price": record.get("decision_price"), "market_data_timestamp": record.get("market_data_timestamp")},
        ),
        _stage(
            "TECHNICAL SIGNALS",
            status="recorded" if signals else "unavailable",
            metadata={"trend": signals.get("trend"), "momentum_state": signals.get("momentum_state"), "volatility_state": signals.get("volatility_state")},
        ),
        _stage(
            "LLM DECISION",
            status="recorded",
            authority="LLM",
            metadata={"action": record.get("action"), "confidence": record.get("confidence")},
        ),
        _stage(
            "SCHEMA VALIDATION",
            status="recorded" if record.get("validated", True) else "failed",
            authority="PYTHON",
            metadata={"schema": "canonical 6-field decision"},
        ),
        _stage(
            "PYTHON SIZING",
            status="recorded" if python_authority["deterministic_quantity"] is not None else "unavailable",
            authority="PYTHON",
            metadata={"quantity": python_authority["deterministic_quantity"]},
        ),
        _stage(
            "RISK CHECK",
            status="allowed" if python_authority["risk_allowed"] else ("rejected" if python_authority["risk_allowed"] is False else "unavailable"),
            authority="PYTHON",
            metadata={"reason": python_authority["risk_reason"]},
        ),
        _stage(
            "FINAL GATE",
            status="passed" if python_authority["final_gate"] else ("rejected" if python_authority["final_gate"] is False else "unavailable"),
            authority="PYTHON",
        ),
        _stage(
            "EXECUTION",
            status="recorded" if execution else "not_recorded",
            authority="SYSTEM",
            metadata={"execution_id": execution.get("execution_id") if execution else None, "submitted": execution.get("submitted") if execution else None, "final_gate": execution.get("final_gate") if execution else None},
        ),
        _stage(
            "OUTCOME",
            status="evaluated" if outcome and outcome.get("return") is not None else "pending",
            authority="SYSTEM",
            metadata={"outcome_id": outcome.get("outcome_id") if outcome else None, "return": outcome.get("return") if outcome else None},
        ),
    ]
    return {
        "decision": decision,
        "stages": stages,
        "llm_authority": llm_authority,
        "python_authority": python_authority,
        "execution": execution or None,
        "outcome": outcome or None,
        "read_only": True,
        "label": "IMMUTABLE HISTORICAL RECORD",
    }