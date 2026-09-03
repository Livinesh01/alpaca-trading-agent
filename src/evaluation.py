"""Offline candidate evaluation above the isolated historical backtester."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backtest import BacktestEngine, BacktestReport, DecisionProvider
from observability import Observability

HYPOTHETICAL_LABEL = "HYPOTHETICAL_EVALUATION_RESULT"


@dataclass(frozen=True)
class CandidateConfig:
    name: str
    provider_factory: Callable[[], DecisionProvider]
    model: str = "unknown"
    prompt_version: str = "unknown"
    temperature: float = 0.0
    seed: int | None = 7
    strategy_id: str = "strategy.yaml"


@dataclass(frozen=True)
class EvaluationCriteria:
    return_weight: float = 0.5
    drawdown_weight: float = 0.3
    safety_weight: float = 0.2

    def __post_init__(self) -> None:
        weights = (self.return_weight, self.drawdown_weight, self.safety_weight)
        if any(weight < 0 for weight in weights) or sum(weights) <= 0:
            raise ValueError("evaluation weights must be non-negative and not all zero")


@dataclass
class CandidateResult:
    candidate: dict[str, Any]
    report: BacktestReport | None
    decision_metrics: dict[str, Any]
    safety_metrics: dict[str, Any]
    reliability_metrics: dict[str, Any]
    scorecard: dict[str, Any]
    failure: str | None = None


@dataclass
class EvaluationReport:
    evaluation_id: str
    dataset: dict[str, Any]
    candidates: list[CandidateResult]
    recommendation: str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "dataset": self.dataset,
            "candidates": [
                {
                    "candidate": item.candidate,
                    "decision_metrics": item.decision_metrics,
                    "trading_metrics": item.report.metrics if item.report else {},
                    "safety_metrics": item.safety_metrics,
                    "reliability_metrics": item.reliability_metrics,
                    "scorecard": item.scorecard,
                    "baseline_comparison": item.report.baseline_comparison if item.report else {},
                    "failure": item.failure,
                }
                for item in self.candidates
            ],
            "recommendation": self.recommendation,
            "label": HYPOTHETICAL_LABEL,
            "actual_production_execution": False,
            "generated_at": self.generated_at,
        }


class EvaluationHarness:
    """Run comparable candidates against identical historical inputs."""

    def __init__(self, *, symbol: str, bars: list[dict[str, Any]], limits: dict[str, Any], candidates: list[CandidateConfig], starting_capital: float = 10000.0, transaction_cost: float = 0.001, slippage: float = 0.0, dataset_id: str | None = None, observability: Observability | None = None, criteria: EvaluationCriteria | None = None) -> None:
        self.symbol = symbol.upper()
        self.bars = list(bars)
        self.limits = dict(limits)
        self.candidates = list(candidates)
        self.starting_capital = starting_capital
        self.transaction_cost = transaction_cost
        self.slippage = slippage
        self.dataset_id = dataset_id or hashlib.sha256(json.dumps(self.bars, sort_keys=True).encode()).hexdigest()
        self.observability = observability
        self.criteria = criteria or EvaluationCriteria()

    def run(self) -> EvaluationReport:
        evaluation_id = f"evaluation-{hashlib.sha256((self.dataset_id + self.symbol).encode()).hexdigest()[:16]}"
        if self.observability:
            self.observability.emit("evaluation_started", evaluation_id=evaluation_id, symbol=self.symbol)
        results: list[CandidateResult] = []
        for candidate in self.candidates:
            metadata = {"name": candidate.name, "model": candidate.model, "prompt_version": candidate.prompt_version, "temperature": candidate.temperature, "seed": candidate.seed, "strategy_id": candidate.strategy_id}
            if self.observability:
                self.observability.emit("candidate_started", evaluation_id=evaluation_id, candidate=candidate.name)
            try:
                report = BacktestEngine(symbol=self.symbol, bars=list(self.bars), provider=candidate.provider_factory(), limits=self.limits, starting_capital=self.starting_capital, transaction_cost=self.transaction_cost, slippage=self.slippage, observability=self.observability).run()
                result = CandidateResult(metadata, report, self._decision_metrics(report), self._safety_metrics(report), {"provider_success": True, "failure_rate": 0.0, "samples": len(report.decisions), "failures": 0}, {})
                if self.observability:
                    self.observability.emit("candidate_completed", evaluation_id=evaluation_id, candidate=candidate.name)
            except Exception as exc:  # noqa: BLE001 — isolate a candidate failure
                result = CandidateResult(metadata, None, {}, {"invalid_or_failed": 1}, {"provider_success": False, "failure_rate": 1.0, "samples": 0, "failures": 1}, {}, str(exc))
                if self.observability:
                    self.observability.emit("evaluation_failure", evaluation_id=evaluation_id, candidate=candidate.name)
            results.append(result)
        for result in results:
            result.scorecard = self._scorecard(result)
        recommendation = self._recommendation(results)
        if self.observability:
            self.observability.emit("evaluation_completed", evaluation_id=evaluation_id, candidate_count=len(results))
        dataset = {"dataset_id": self.dataset_id, "symbol": self.symbol, "starting_capital": self.starting_capital, "transaction_cost": self.transaction_cost, "slippage": self.slippage, "label": HYPOTHETICAL_LABEL}
        return EvaluationReport(evaluation_id, dataset, results, recommendation)

    def _scorecard(self, result: CandidateResult) -> dict[str, Any]:
        if result.report is None:
            return {"composite_score": None, "weights": self.criteria.__dict__.copy(), "status": "failed"}
        trading = result.report.metrics
        decision_count = max(len(result.report.decisions), 1)
        components = {"return": float(trading.get("total_return", 0.0)), "drawdown": -float(trading.get("maximum_drawdown", 0.0)), "safety": 1.0 - (trading.get("rejected_decisions", 0) / decision_count)}
        score = sum((self.criteria.return_weight, self.criteria.drawdown_weight, self.criteria.safety_weight)[index] * components[name] for index, name in enumerate(("return", "drawdown", "safety")))
        return {"composite_score": score, "weights": self.criteria.__dict__.copy(), "components": components, "status": "observed historical comparison only"}

    @staticmethod
    def _decision_metrics(report: BacktestReport) -> dict[str, Any]:
        decisions = report.decisions
        confidence = [float(item["confidence"]) for item in decisions]
        buckets: dict[str, list[float]] = {f"{start:.1f}-{start + 0.1:.1f}": [] for start in (i / 10 for i in range(5, 10))}
        outcomes = {item["decision_id"]: item for item in report.outcomes}
        for item in decisions:
            bucket = min(int(float(item["confidence"]) * 10), 9)
            key = f"{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}"
            outcome = outcomes.get(item["decision_id"], {}).get("return")
            if outcome is not None:
                buckets.setdefault(key, []).append(float(outcome))
        return {"BUY": sum(item["action"] == "BUY" for item in decisions), "SELL": sum(item["action"] == "SELL" for item in decisions), "HOLD": sum(item["action"] == "HOLD" for item in decisions), "valid_decisions": len(decisions), "invalid_decisions": 0, "average_confidence": statistics.mean(confidence) if confidence else None, "median_confidence": statistics.median(confidence) if confidence else None, "confidence_buckets": {key: {"count": len(values), "outcome_rate": sum(value > 0 for value in values) / len(values) if values else None} for key, values in buckets.items()}}

    @staticmethod
    def _safety_metrics(report: BacktestReport) -> dict[str, Any]:
        return {"risk_approved_decisions": report.metrics["number_of_trades"], "risk_rejected_decisions": report.metrics["rejected_decisions"], "final_gate_rejections": 0, "zero_quantity_attempts": 0, "kill_switch_interactions": 0, "stale_data_rejections": 0, "insufficient_data_rejections": sum(item["signals"].get("insufficient_data", False) for item in report.decisions)}

    @staticmethod
    def _recommendation(results: list[CandidateResult]) -> str:
        valid = [item for item in results if item.report is not None]
        if not valid:
            return "NO RECOMMENDATION: all candidates failed; HUMAN REVIEW REQUIRED"
        best = max(valid, key=lambda item: item.scorecard.get("composite_score", float("-inf")))
        return f"Candidate {best.candidate['name']} ranked highest under configured historical criteria; HUMAN REVIEW REQUIRED; no deployment performed"
