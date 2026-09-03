"""Informational decision memory and deterministic outcome evaluation.

This module is deliberately outside the execution authority chain. It stores
append-only JSONL records, retrieves concise historical context, and evaluates
hypothetical decision outcomes. It never calls an executor or broker.
"""

from __future__ import annotations

import json
import os
import statistics
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MEMORY_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "journal", "memory"))
HISTORICAL_MARKER = "HISTORICAL / INFORMATIONAL ONLY"
HYPOTHETICAL_LABEL = "HYPOTHETICAL_DECISION_OUTCOME"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _jsonl_append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _assert_no_secrets(value: Any, path: str = "record") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("api_key", "secret", "token", "password", "credential")):
                raise ValueError(f"secret-like field cannot be persisted: {path}.{key}")
            _assert_no_secrets(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_secrets(item, f"{path}[{index}]")


def _valid_price(value: Any) -> float:
    price = float(value)
    if price <= 0:
        raise ValueError("price must be greater than zero")
    return price


def _parse_timestamp(value: str) -> datetime:
    timestamp = value.strip()
    if timestamp.endswith("Z"):
        timestamp = f"{timestamp[:-1]}+00:00"
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def create_run_id() -> str:
    return _new_id("run")


def create_decision_id() -> str:
    return _new_id("decision")


def create_execution_id() -> str:
    return _new_id("execution")


def create_outcome_id() -> str:
    return _new_id("outcome")


class MemoryStore:
    """Append-only JSONL store for decisions, outcomes, and execution facts."""

    def __init__(self, directory: str | os.PathLike[str] = DEFAULT_MEMORY_DIR) -> None:
        self.directory = Path(directory)
        self.decisions_path = self.directory / "decisions.jsonl"
        self.outcomes_path = self.directory / "outcomes.jsonl"
        self.executions_path = self.directory / "executions.jsonl"

    def save_decision(self, record: dict[str, Any]) -> None:
        required = {"run_id", "decision_id", "timestamp", "symbol", "action", "confidence", "thesis", "entry_reason"}
        missing = required.difference(record)
        if missing:
            raise ValueError(f"decision missing fields: {sorted(missing)}")
        _assert_no_secrets(record)
        _jsonl_append(self.decisions_path, dict(record))

    def save_outcome(self, record: dict[str, Any]) -> bool:
        decision_id = str(record.get("decision_id", "")).strip()
        if not decision_id:
            raise ValueError("outcome requires decision_id")
        _assert_no_secrets(record)
        existing = self.outcomes()
        if any(str(item.get("decision_id")) == decision_id and item.get("horizon") == record.get("horizon") for item in existing):
            return False
        _jsonl_append(self.outcomes_path, dict(record))
        return True

    def save_execution(self, record: dict[str, Any]) -> None:
        if not record.get("decision_id"):
            raise ValueError("execution requires decision_id")
        _assert_no_secrets(record)
        _jsonl_append(self.executions_path, dict(record))

    def decisions(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.decisions_path)

    def outcomes(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.outcomes_path)

    def executions(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.executions_path)

    def historical(self, symbol: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        wanted = symbol.upper() if symbol else None
        outcomes = {str(item.get("decision_id")): item for item in self.outcomes()}
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for decision in reversed(self.decisions()):
            decision_id = str(decision.get("decision_id", ""))
            if not decision_id or decision_id in seen:
                continue
            if wanted and str(decision.get("symbol", "")).upper() != wanted:
                continue
            seen.add(decision_id)
            item = {
                "decision_id": decision_id,
                "timestamp": decision.get("timestamp"),
                "symbol": decision.get("symbol"),
                "action": decision.get("action"),
                "confidence": decision.get("confidence"),
            }
            outcome = outcomes.get(decision_id)
            if outcome:
                item["outcome"] = {
                    "evaluation_timestamp": outcome.get("evaluation_timestamp"),
                    "horizon": outcome.get("horizon"),
                    "return": outcome.get("return"),
                    "label": outcome.get("label"),
                }
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def historical_context(self, symbol: str, limit: int = 3) -> str:
        records = self.historical(symbol, limit)
        return f"{HISTORICAL_MARKER}\n{json.dumps(records, sort_keys=True)}"


def evaluate_decision(
    decision: dict[str, Any],
    evaluation_price: Any,
    *,
    evaluation_timestamp: str | None = None,
    horizon: str = "1 trading day",
    horizon_seconds: int | None = None,
) -> dict[str, Any]:
    """Evaluate a decision without changing the original decision record."""
    decision_price = _valid_price(decision.get("decision_price"))
    future_price = _valid_price(evaluation_price)
    evaluation_time = evaluation_timestamp or _timestamp()
    if horizon_seconds is not None:
        if horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be greater than zero")
        decision_time = _parse_timestamp(str(decision["timestamp"]))
        if (_parse_timestamp(evaluation_time) - decision_time).total_seconds() < horizon_seconds:
            return {
                "decision_id": decision["decision_id"],
                "evaluation_timestamp": evaluation_time,
                "evaluation_price": future_price,
                "horizon": horizon,
                "return": None,
                "label": HYPOTHETICAL_LABEL,
                "status": "insufficient_horizon",
                "action": decision.get("action"),
            }
    action = decision.get("action")
    if action == "BUY":
        result = (future_price - decision_price) / decision_price
    elif action == "SELL":
        result = (decision_price - future_price) / decision_price
    elif action == "HOLD":
        result = (future_price - decision_price) / decision_price
    else:
        raise ValueError("action must be BUY, SELL, or HOLD")
    return {
        "outcome_id": create_outcome_id(),
        "decision_id": decision["decision_id"],
        "run_id": decision.get("run_id"),
        "evaluation_timestamp": evaluation_time,
        "evaluation_price": future_price,
        "horizon": horizon,
        "return": result,
        "label": HYPOTHETICAL_LABEL,
        "action": action,
    }


def missing_outcome(decision_id: str, *, horizon: str = "1 trading day", reason: str = "future price unavailable") -> dict[str, Any]:
    return {
        "outcome_id": create_outcome_id(),
        "decision_id": decision_id,
        "evaluation_timestamp": _timestamp(),
        "horizon": horizon,
        "return": None,
        "label": HYPOTHETICAL_LABEL,
        "status": "missing",
        "reason": reason,
    }


def metrics(decisions: Iterable[dict[str, Any]], outcomes: Iterable[dict[str, Any]], executions: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    decisions_list = list(decisions)
    outcomes_list = list(outcomes)
    executions_list = list(executions)
    evaluated = [float(item["return"]) for item in outcomes_list if isinstance(item.get("return"), (int, float))]
    actions = {action: sum(item.get("action") == action for item in decisions_list) for action in ("BUY", "SELL", "HOLD")}
    returns = evaluated or []
    accepted = sum(bool(item.get("accepted")) for item in decisions_list)
    rejected = sum(item.get("accepted") is False for item in decisions_list)
    if not accepted and not rejected:
        accepted = sum(bool(item.get("risk_allowed")) for item in executions_list)
        rejected = sum(item.get("risk_allowed") is False for item in executions_list)
    return {
        "total_decisions": len(decisions_list),
        "BUY_decisions": actions["BUY"],
        "SELL_decisions": actions["SELL"],
        "HOLD_decisions": actions["HOLD"],
        "accepted_decisions": accepted,
        "rejected_decisions": rejected,
        "executed_orders": sum(bool(item.get("submitted")) for item in executions_list),
        "hypothetical_evaluated_decisions": len(evaluated),
        "average_hypothetical_return": statistics.mean(returns) if returns else None,
        "median_hypothetical_return": statistics.median(returns) if returns else None,
        "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else None,
        "missing_evaluations": len(outcomes_list) - len(evaluated),
    }
