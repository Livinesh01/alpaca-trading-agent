"""Backtest service: execute the existing isolated backtest engine safely.

HYPOTHETICAL BACKTEST — the engine (backtest.BacktestEngine) uses a frozen
decision provider and a simulated executor. It never calls Alpaca order APIs,
never invokes the production executor or _FinalOrderGate for submission, never
submits orders, and never mutates live trading state. Reports are persisted
append-only and served read-only.
"""

from __future__ import annotations

import math
import statistics
import uuid
from pathlib import Path
from typing import Any

from api.services.records import append_record, read_records
from api.services.utils import iso_now

BACKTEST_PATH = Path(__file__).resolve().parents[3] / "journal" / "backtests.jsonl"


def run_backtest(
    *,
    symbol: str,
    bars: list[dict[str, Any]],
    limits: dict[str, Any],
    starting_capital: float = 10000.0,
    transaction_cost: float = 0.001,
    slippage: float = 0.0,
    provider_name: str = "frozen",
    observability: Any = None,
) -> dict[str, Any]:
    bars = _normalize_bars(bars)
    if not bars:
        raise ValueError("backtest requires at least one valid bar")
    from backtest import BacktestEngine, FrozenDecisionProvider

    if provider_name not in ("frozen",):
        raise ValueError("only the frozen (offline) decision provider is available for API backtests")
    provider = FrozenDecisionProvider()
    report = BacktestEngine(
        symbol=symbol,
        bars=bars,
        provider=provider,
        limits=limits,
        starting_capital=starting_capital,
        transaction_cost=transaction_cost,
        slippage=slippage,
        observability=observability,
    ).run()
    metrics = dict(report.metrics)
    returns = [trade.pnl / (trade.entry_price * trade.qty) for trade in report.trades]
    sharpe = (
        statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(252)
        if len(returns) >= 2 and statistics.stdev(returns) > 0
        else None
    )
    record = {
        "backtest_id": f"backtest-{uuid.uuid4().hex[:12]}",
        "symbol": str(symbol).upper(),
        "generated_at": iso_now(),
        "label": "HYPOTHETICAL BACKTEST",
        "read_only": True,
        "actual_production_execution": False,
        "config": report.config,
        "metrics": {
            "total_return": metrics.get("total_return"),
            "maximum_drawdown": metrics.get("maximum_drawdown"),
            "sharpe": sharpe,
            "win_rate": metrics.get("win_rate"),
            "trade_count": metrics.get("number_of_trades"),
            "rejected_decisions": metrics.get("rejected_decisions"),
            "starting_capital": metrics.get("starting_capital"),
            "ending_equity": metrics.get("ending_equity"),
            "absolute_pnl": metrics.get("absolute_pnl"),
            "transaction_costs": sum(float(trade.fees) for trade in report.trades),
            "slippage": metrics.get("slippage"),
            "buy_and_hold_return": report.baseline_comparison.get("buy_and_hold", {}).get("return"),
            "baseline_return": report.baseline_comparison.get("buy_and_hold", {}).get("return"),
            "turnover": metrics.get("turnover"),
            "exposure": metrics.get("exposure"),
        },
        "equity_curve": _sampled_equity(report),
        "trade_count": metrics.get("number_of_trades"),
    }
    append_record(BACKTEST_PATH, record)
    return record


def _sampled_equity(report: Any) -> list[dict[str, Any]]:
    """Light sampler over the report equity curve if present, else []."""
    curve = getattr(report, "equity_curve", None)
    if not isinstance(curve, list) or not curve:
        return []
    return [{"index": index, "equity": round(float(value), 2)} for index, value in enumerate(curve)]


def _normalize_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map short Alpaca bar keys (t/o/h/l/c/v) to the backtest engine's
    timestamp/open/high/low/close/volume shape."""
    normalized: list[dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        normalized.append(
            {
                "timestamp": bar.get("t") or bar.get("timestamp") or bar.get("time"),
                "open": bar.get("o", bar.get("open")),
                "high": bar.get("h", bar.get("high")),
                "low": bar.get("l", bar.get("low")),
                "close": bar.get("c", bar.get("close")),
                "volume": bar.get("v", bar.get("volume")),
            }
        )
    return normalized


def list_backtests(*, page: int = 1, page_size: int = 50, symbol: str | None = None) -> dict[str, Any]:
    records = read_records(BACKTEST_PATH)
    records.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
    if symbol:
        records = [item for item in records if str(item.get("symbol") or "").upper() == str(symbol).upper()]
    page = max(int(page), 1)
    page_size = max(min(int(page_size), 200), 1)
    start = (page - 1) * page_size
    return {
        "items": records[start : start + page_size],
        "available": True,
        "label": "HYPOTHETICAL BACKTEST",
        "reason": None,
        "pagination": {"page": page, "page_size": page_size, "total": len(records)},
    }


def get_backtest(backtest_id: str) -> dict[str, Any] | None:
    return next((item for item in read_records(BACKTEST_PATH) if item.get("backtest_id") == backtest_id), None)