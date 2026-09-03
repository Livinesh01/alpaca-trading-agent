"""Read-only historical backtesting with local simulated execution.

Backtesting never imports or invokes the production executor, MCP, or Alpaca.
It reuses deterministic signals, sizing, and risk rules, while all fills are
local hypothetical results.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.decision_loop import deterministic_quantity
from memory import (
    create_decision_id,
    create_execution_id,
    create_outcome_id,
    create_run_id,
)
from observability import Observability
from orchestrator import validate_trade_decision
from risk_rules import AccountState, OrderRequest, check_order
from signals import compute_technical_signals

HYPOTHETICAL_LABEL = "HYPOTHETICAL_BACKTEST_RESULT"


class BacktestDataError(ValueError):
    pass


def _price(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BacktestDataError("historical price is invalid") from exc
    if result <= 0:
        raise BacktestDataError("historical price must be greater than zero")
    return result


def _timestamp(bar: dict[str, Any]) -> str:
    value = bar.get("timestamp", bar.get("t"))
    if not isinstance(value, str) or not value.strip():
        raise BacktestDataError("each historical bar requires a timestamp")
    return value.strip()


def validate_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not bars:
        raise BacktestDataError("historical dataset is empty")
    checked: list[dict[str, Any]] = []
    previous: str | None = None
    for bar in bars:
        if not isinstance(bar, dict):
            raise BacktestDataError("historical bar must be an object")
        timestamp = _timestamp(bar)
        if previous is not None and timestamp <= previous:
            raise BacktestDataError("historical timestamps must be strictly increasing")
        close = _price(bar.get("close", bar.get("c")))
        high = _price(bar.get("high", bar.get("h")))
        low = _price(bar.get("low", bar.get("l")))
        if low > high or close > high or close < low:
            raise BacktestDataError("historical OHLC values are inconsistent")
        checked.append({**bar, "timestamp": timestamp, "c": close, "h": high, "l": low})
        previous = timestamp
    return checked


class DecisionProvider(Protocol):
    def decide(self, symbol: str, signals: dict[str, Any], account: AccountState, timestamp: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class FrozenDecisionProvider:
    """Reproducible fake decision provider for offline backtests."""

    actions: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.5

    def decide(self, symbol: str, signals: dict[str, Any], account: AccountState, timestamp: str) -> dict[str, Any]:
        action = self.actions.get(symbol.upper(), "HOLD")
        return {
            "symbol": symbol.upper(),
            "action": action,
            "confidence": self.confidence,
            "position_size": 0,
            "thesis": "frozen historical test decision",
            "entry_reason": "offline reproducible provider",
        }


@dataclass
class SimulatedPosition:
    qty: int = 0
    average_entry_price: float = 0.0


@dataclass
class SimulatedTrade:
    execution_id: str
    timestamp: str
    symbol: str
    side: str
    qty: int
    entry_price: float
    exit_price: float | None
    fees: float
    slippage: float
    pnl: float


class SimulatedExecutor:
    def __init__(self, starting_capital: float, transaction_cost: float = 0.0, slippage: float = 0.0) -> None:
        if starting_capital <= 0 or transaction_cost < 0 or slippage < 0:
            raise ValueError("capital must be positive and costs/slippage must be non-negative")
        self.cash = float(starting_capital)
        self.transaction_cost = float(transaction_cost)
        self.slippage = float(slippage)
        self.positions: dict[str, SimulatedPosition] = {}
        self.trades: list[SimulatedTrade] = []

    def submit(self, order: OrderRequest, price: float, timestamp: str) -> SimulatedTrade:
        symbol = order.symbol.upper()
        qty = int(order.qty or 0)
        if qty <= 0:
            raise ValueError("simulated order quantity must be positive")
        fill = price * (1 + self.slippage if order.side == "buy" else 1 - self.slippage)
        fees = fill * qty * self.transaction_cost
        position = self.positions.setdefault(symbol, SimulatedPosition())
        if order.side == "buy":
            total = fill * qty + fees
            if total > self.cash:
                raise ValueError("simulated buying power exceeded")
            self.cash -= total
            new_qty = position.qty + qty
            position.average_entry_price = ((position.average_entry_price * position.qty) + (fill * qty)) / new_qty
            position.qty = new_qty
            pnl = -fees
            exit_price = None
        elif order.side == "sell":
            if qty > position.qty:
                raise ValueError("simulated sell exceeds position")
            proceeds = fill * qty - fees
            self.cash += proceeds
            pnl = (fill - position.average_entry_price) * qty - fees
            position.qty -= qty
            if position.qty == 0:
                position.average_entry_price = 0.0
            exit_price = fill
        else:
            raise ValueError("simulator supports buy and sell only")
        trade = SimulatedTrade(create_execution_id(), timestamp, symbol, order.side, qty, fill, exit_price, fees, self.slippage, pnl)
        self.trades.append(trade)
        return trade

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(position.qty * prices.get(symbol, 0.0) for symbol, position in self.positions.items())


@dataclass
class BacktestReport:
    config: dict[str, Any]
    decisions: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]
    trades: list[SimulatedTrade]
    metrics: dict[str, Any]
    baseline_comparison: dict[str, Any]


class BacktestEngine:
    def __init__(self, *, symbol: str, bars: list[dict[str, Any]], provider: DecisionProvider, limits: dict[str, Any], starting_capital: float = 10000.0, transaction_cost: float = 0.001, slippage: float = 0.0, timeframe: str = "1Day", observability: Observability | None = None) -> None:
        self.symbol = symbol.upper()
        self.bars = validate_bars(bars)
        self.provider = provider
        self.limits = dict(limits)
        self.starting_capital = starting_capital
        self.transaction_cost = transaction_cost
        self.slippage = slippage
        self.timeframe = timeframe
        self.observability = observability

    def run(self) -> BacktestReport:
        run_id = create_run_id()
        if self.observability:
            self.observability.emit("backtest_started", run_id=run_id, symbol=self.symbol)
        executor = SimulatedExecutor(self.starting_capital, self.transaction_cost, self.slippage)
        decisions: list[dict[str, Any]] = []
        equity_curve = [self.starting_capital]
        rejected = 0
        for index, bar in enumerate(self.bars):
            timestamp = bar["timestamp"]
            history = self.bars[: index + 1]
            price = bar["c"]
            signals = compute_technical_signals(self.symbol, history, self.limits.get("signal_params", {}))
            position = executor.positions.get(self.symbol, SimulatedPosition())
            account = AccountState(
                cash=executor.cash,
                buying_power=executor.cash,
                equity=executor.equity({self.symbol: price}),
                daily_pnl=executor.equity({self.symbol: price}) - self.starting_capital,
                open_position_count=sum(item.qty > 0 for item in executor.positions.values()),
                orders_placed_this_run=len(executor.trades),
                existing_position_notional=position.qty * price,
            )
            decision_id = create_decision_id()
            raw_decision = self.provider.decide(self.symbol, signals, account, timestamp)
            decision = validate_trade_decision(raw_decision)
            decision_record = {"run_id": run_id, "decision_id": decision_id, "timestamp": timestamp, "symbol": self.symbol, "action": decision["action"], "confidence": decision["confidence"], "thesis": decision["thesis"], "entry_reason": decision["entry_reason"], "decision_price": price, "signals": signals, "account_context": account.__dict__.copy()}
            decisions.append(decision_record)
            if self.observability:
                self.observability.emit("backtest_decision", run_id=run_id, decision_id=decision_id, symbol=self.symbol, action=decision["action"])
            qty = deterministic_quantity(decision["action"], price, account, self.limits)
            if decision["action"] == "HOLD" or qty <= 0:
                equity_curve.append(executor.equity({self.symbol: price}))
                continue
            order = OrderRequest(self.symbol, decision["action"].lower(), qty, order_type="market")
            risk = check_order(order, account, self.limits, last_price=price)
            if not risk.allowed:
                rejected += 1
                if self.observability:
                    self.observability.emit("backtest_rejection", run_id=run_id, decision_id=decision_id, symbol=self.symbol)
                equity_curve.append(executor.equity({self.symbol: price}))
                continue
            executor.submit(order, price, timestamp)
            if self.observability:
                self.observability.emit("backtest_trade", run_id=run_id, decision_id=decision_id, execution_id=executor.trades[-1].execution_id, symbol=self.symbol)
            equity_curve.append(executor.equity({self.symbol: price}))
        outcomes = self._outcomes(decisions)
        metrics = self._metrics(decisions, executor, rejected, equity_curve, outcomes)
        config = {"dataset_hash": hashlib.sha256(json.dumps(self.bars, sort_keys=True).encode()).hexdigest(), "symbol": self.symbol, "timeframe": self.timeframe, "starting_capital": self.starting_capital, "transaction_cost": self.transaction_cost, "slippage": self.slippage, "provider": type(self.provider).__name__}
        baseline = {"buy_and_hold": self._buy_and_hold(), "deterministic_signal": "uses existing deterministic signals without LLM"}
        if self.observability:
            self.observability.emit("backtest_completed", run_id=run_id, symbol=self.symbol, trades=len(executor.trades))
        return BacktestReport(config, decisions, outcomes, executor.trades, metrics, baseline)

    def _outcomes(self, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        final_bar = self.bars[-1]
        outcomes: list[dict[str, Any]] = []
        for decision in decisions:
            decision_price = decision["decision_price"]
            future_price = final_bar["c"]
            action = decision["action"]
            result = None if decision["timestamp"] == final_bar["timestamp"] else (
                (future_price - decision_price) / decision_price
                if action == "BUY" else (decision_price - future_price) / decision_price
                if action == "SELL" else (future_price - decision_price) / decision_price
            )
            outcomes.append({"outcome_id": create_outcome_id(), "run_id": decision["run_id"], "decision_id": decision["decision_id"], "evaluation_timestamp": final_bar["timestamp"], "evaluation_price": future_price, "horizon": "through dataset end", "return": result, "label": HYPOTHETICAL_LABEL, "status": "evaluated" if result is not None else "insufficient_horizon"})
        return outcomes

    def _metrics(self, decisions: list[dict[str, Any]], executor: SimulatedExecutor, rejected: int, equity_curve: list[float], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        trades = executor.trades
        returns = [trade.pnl / (trade.entry_price * trade.qty) for trade in trades]
        peaks: list[float] = []
        drawdowns: list[float] = []
        peak = self.starting_capital
        for equity in equity_curve:
            peak = max(peak, equity)
            peaks.append(peak)
            drawdowns.append((peak - equity) / peak)
        ending = equity_curve[-1]
        return {"starting_capital": self.starting_capital, "ending_equity": ending, "total_return": (ending - self.starting_capital) / self.starting_capital, "absolute_pnl": ending - self.starting_capital, "number_of_trades": len(trades), "BUY_decisions": sum(item["action"] == "BUY" for item in decisions), "SELL_decisions": sum(item["action"] == "SELL" for item in decisions), "HOLD_decisions": sum(item["action"] == "HOLD" for item in decisions), "rejected_decisions": rejected, "rejected_orders": rejected, "exposure": sum(position.qty for position in executor.positions.values()) * self.bars[-1]["c"], "turnover": sum(trade.entry_price * trade.qty for trade in trades), "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else None, "average_trade_return": statistics.mean(returns) if returns else None, "median_trade_return": statistics.median(returns) if returns else None, "maximum_drawdown": max(drawdowns, default=0.0), "largest_loss": min((trade.pnl for trade in trades), default=0.0), "largest_gain": max((trade.pnl for trade in trades), default=0.0), "hypothetical_evaluated_decisions": sum(item["return"] is not None for item in outcomes), "transaction_cost": self.transaction_cost, "slippage": self.slippage}

    def _buy_and_hold(self) -> dict[str, Any]:
        first = self.bars[0]["c"]
        last = self.bars[-1]["c"]
        return {"label": HYPOTHETICAL_LABEL, "return": (last - first) / first, "start_price": first, "end_price": last}
