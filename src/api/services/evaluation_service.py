"""Evaluation service: existing isolated evaluation harness, read-only results.

HYPOTHETICAL EVALUATION RESULT — every candidate uses the frozen decision
provider (offline, deterministic), so no LLM call, no provider traffic, and no
order submission can happen. Nothing is auto-deployed; results require human
review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.services.records import append_record, read_records

EVALUATION_PATH = Path(__file__).resolve().parents[3] / "journal" / "evaluations.jsonl"


def run_evaluation(
    *,
    symbol: str,
    bars: list[dict[str, Any]],
    limits: dict[str, Any],
    starting_capital: float = 10000.0,
    transaction_cost: float = 0.001,
    slippage: float = 0.0,
    observability: Any = None,
) -> dict[str, Any]:
    from api.services.backtest_service import _normalize_bars
    from backtest import FrozenDecisionProvider

    bars = _normalize_bars(bars)
    if not bars:
        raise ValueError("evaluation requires at least one valid bar")
    from evaluation import CandidateConfig, EvaluationHarness

    symbol_upper = str(symbol).upper()
    candidates = [
        CandidateConfig(name="frozen-hold", provider_factory=lambda: FrozenDecisionProvider(actions={}, confidence=0.5), model="frozen", prompt_version="offline", strategy_id="strategy.yaml"),
        CandidateConfig(name="frozen-buy", provider_factory=lambda: FrozenDecisionProvider(actions={symbol_upper: "BUY"}, confidence=0.7), model="frozen", prompt_version="offline", strategy_id="strategy.yaml"),
        CandidateConfig(name="frozen-sell", provider_factory=lambda: FrozenDecisionProvider(actions={symbol_upper: "SELL"}, confidence=0.6), model="frozen", prompt_version="offline", strategy_id="strategy.yaml"),
    ]
    report = EvaluationHarness(
        symbol=symbol_upper,
        bars=bars,
        limits=limits,
        candidates=candidates,
        starting_capital=starting_capital,
        transaction_cost=transaction_cost,
        slippage=slippage,
        observability=observability,
    ).run()
    record = {
        "evaluation_id": report.evaluation_id,
        "dataset": report.dataset,
        "candidates": report.to_dict()["candidates"],
        "recommendation": report.recommendation,
        "generated_at": report.generated_at,
        "label": "HYPOTHETICAL EVALUATION RESULT",
        "human_review_required": True,
        "auto_deployed": False,
    }
    append_record(EVALUATION_PATH, record)
    return record


def list_evaluations(*, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    records = read_records(EVALUATION_PATH)
    records.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
    page = max(int(page), 1)
    page_size = max(min(int(page_size), 200), 1)
    start = (page - 1) * page_size
    return {
        "items": records[start : start + page_size],
        "available": True,
        "label": "HYPOTHETICAL EVALUATION RESULT",
        "reason": None,
        "pagination": {"page": page, "page_size": page_size, "total": len(records)},
    }


def get_evaluation(evaluation_id: str) -> dict[str, Any] | None:
    return next((item for item in read_records(EVALUATION_PATH) if item.get("evaluation_id") == evaluation_id), None)