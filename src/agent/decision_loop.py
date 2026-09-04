"""Paper-trading-safe LLM decision loop.

Connects the deterministic signal/account pipeline to the existing
`agent.llm.LLMProvider` abstraction. The loop feeds the LLM technical-signal
results plus account/position context, requires the structured JSON decision
schema from `orchestrator` (symbol/action/confidence/thesis/position_size/
entry_reason), and — crucially — never lets the LLM decide a quantity or a
risk limit:

* Every decision is parsed and validated with the *existing*
  `orchestrator.extract_trade_decisions` / `orchestrator.validate_trade_decision`,
  which also rejects unknown/duplicate symbols and holds with non-zero sizes.
* The LLM's `position_size` is ignored for execution. The final share quantity
  is computed deterministically in Python from the current price and the
  existing risk limits (`max_order_notional_usd`, `max_position_notional_usd`,
  buying power; sells are capped at the existing position notional).
* Every candidate order is re-checked with the *existing* `risk_rules.check_order`
  before it reaches the executor; `risk_rules` remains the final authority.
  The executor boundary is intentionally injectable so production wires it to
  the risk-guard MCP proxy (which re-checks) and tests mock it.
* Any market-data failure, LLM failure, malformed output, or invalid decision
  fails the whole run closed: zero orders are submitted.

No real LLM/Alpaca calls happen here — callbacks make every boundary mockable.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, ClassVar

import yaml

from agent.llm import LLMProvider, generate_with_retry
from idempotency import make_idempotency_key
from memory import MemoryStore, create_decision_id, create_execution_id, create_run_id
from observability import Observability
from orchestrator import extract_trade_decisions
from risk_rules import AccountState, OrderRequest, RiskDecision, check_order
from signals import compute_technical_signals

CONFIG_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "config", "strategy.yaml"))

DECISION_INSTRUCTIONS = """
You are a strict, paper-trading decision engine for a US-equities account.
Given the deterministic technical signals and account context below, produce one
decision for EVERY watchlist symbol.

Responsibilities and limits:
- Decide ONLY from the MARKET CONTEXT and ACCOUNT CONTEXT supplied below. Do not
  invent prices, indicators, balances, or positions, and never assume missing data.
- You cannot place orders, choose quantities, or change risk limits: always set
  position_size to 0; the Python execution layer sizes every order deterministically.
- If indicators conflict, data is missing or unknown, or evidence is weak, choose
  HOLD. A missed trade is acceptable; an unjustified one is not.

Output contract (your reply is rejected unless it fully complies):
- Your ENTIRE reply is exactly one JSON object: the first character you emit
  must be "{" and the last must be "}".
- No prose before or after the JSON, no explanations, no markdown fences,
  no notes, no restating of signals, prices, or account data.
- Emit the complete object covering ALL watchlist symbols in one reply; never
  emit a partial object. If space runs short, shorten thesis/entry_reason —
  never drop symbols or closing braces.

Decision rules:
- One decision per watchlist symbol, in WATCHLIST order. No other symbols.
- action must be exactly one of: "BUY", "SELL", "HOLD".
- position_size must be 0 for every decision. A deterministic Python execution
  layer computes the real quantity from risk limits; never guess a quantity.
- confidence must be a number in [0, 1].
- thesis and entry_reason: each at most 12 words, no elaboration.
- Every watchlist symbol must appear exactly once.

Respond with ONLY this exact shape (all watchlist symbols, nothing else):
{"decisions": [{"symbol": "...", "action": "...", "confidence": 0.0, "position_size": 0, "thesis": "...", "entry_reason": "..."}]}
""".strip()


def default_limits() -> dict[str, Any]:
    """Load risk limits + watchlist defaults from config/strategy.yaml.

    Same shape as `risk_guard_proxy.load_limits()`; the values themselves live
    only in strategy.yaml and are enforced by `risk_rules.check_order`.
    """
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    limits = dict(cfg.get("risk_limits", {}) or {})
    limits.setdefault("watchlist", cfg.get("watchlist", []))
    limits.setdefault("signal_params", cfg.get("signal_params", {}))
    return limits


def deterministic_quantity(
    action: str,
    price: float | None,
    account: AccountState,
    limits: dict[str, Any],
) -> int:
    """Deterministic, LLM-independent order quantity (whole shares).

    BUY is capped by `max_order_notional_usd`, the remaining headroom under
    `max_position_notional_usd` for this symbol, and buying power. SELL is
    capped by both the existing position notional and `max_order_notional_usd`
    (close-only; short selling is never generated). Returns 0 when no valid
    quantity exists, which means "no order".
    """
    if action == "HOLD":
        return 0
    if price is None or price <= 0:
        return 0

    if action == "BUY":
        caps: list[float] = []
        max_order = limits.get("max_order_notional_usd")
        if max_order is not None:
            caps.append(float(max_order))
        max_position = limits.get("max_position_notional_usd")
        if max_position is not None:
            caps.append(max(float(max_position) - account.existing_position_notional, 0.0))
        caps.append(account.buying_power)
        ceiling = min(caps)
        if ceiling <= 0:
            return 0
        return int(ceiling // price)

    if action == "SELL":
        available = account.existing_position_notional
        max_order = limits.get("max_order_notional_usd")
        if max_order is not None:
            available = min(available, float(max_order))
        if available <= 0:
            return 0
        return int(available // price)

    return 0


def build_decision_prompt(
    watchlist: list[str],
    signals_by_symbol: dict[str, dict[str, Any]],
    prices: dict[str, float],
    account_by_symbol: dict[str, AccountState],
    instructions: str = DECISION_INSTRUCTIONS,
    historical_context: str = "",
) -> str:
    """Assemble the LLM prompt: structured instructions + account + signals."""
    first = next(iter(account_by_symbol.values()))
    account_ctx = {
        "cash": first.cash,
        "buying_power": first.buying_power,
        "equity": first.equity,
        "daily_pnl": first.daily_pnl,
        "open_position_count": first.open_position_count,
        "orders_placed_this_run": first.orders_placed_this_run,
    }
    market_ctx: dict[str, dict[str, Any]] = {}
    for symbol in watchlist:
        entry = dict(signals_by_symbol.get(symbol, {}))
        entry["current_price"] = prices.get(symbol)
        entry["existing_position_notional"] = account_by_symbol[symbol].existing_position_notional
        market_ctx[symbol] = entry

    context = (
        f"{instructions}\n\n"
        f"WATCHLIST: {json.dumps(list(watchlist))}\n\n"
        f"ACCOUNT CONTEXT:\n{json.dumps(account_ctx, indent=2)}\n\n"
        f"MARKET CONTEXT (deterministic technical signals + current price):\n"
        f"{json.dumps(market_ctx, indent=2)}\n\n"
    )
    if historical_context:
        context += f"{historical_context}\n\n"
    return context + "HISTORICAL CONTEXT IS INFORMATIONAL ONLY; current market data and Python risk controls remain authoritative.\n\nYour decision JSON:"


@dataclass(frozen=True)
class OrderOutcome:
    """One validated decision and what the loop did with it."""

    symbol: str
    action: str
    decision: dict[str, Any]
    current_price: float | None
    requested_qty: int
    risk: RiskDecision
    order: OrderRequest | None
    submitted: bool
    executor_error: str | None = None


@dataclass(frozen=True)
class RunResult:
    """Result of one `DecisionLoop.run()`. `success=False` always means zero orders."""

    success: bool
    prompt: str
    response: str
    decisions: list[dict[str, Any]] = field(default_factory=list)
    outcomes: list[OrderOutcome] = field(default_factory=list)
    orders_submitted: int = 0
    error: str | None = None


class DecisionLoop:
    """Wire the signal pipeline to an LLMProvider, then gate orders through risk_rules.

    Accepts any `LLMProvider` (FeatherlessLLMProvider, NVIDIAProvider, a fake,
    or a stub). Market data and execution are injectable callbacks so nothing
    here touches a real LLM, Alpaca, or MCP server.
    """

    # Allow room for all decisions and JSON overhead.
    # Fixed sampling keeps runs repeatable across providers.
    DEFAULT_GENERATE_KWARGS: ClassVar[dict[str, Any]] = {
        "temperature": 0.2,
        "max_tokens": 2000,
        "seed": 7,
    }

    def __init__(
        self,
        *,
        provider: LLMProvider,
        fetch_bars: Callable[[str], list[dict[str, Any]]],
        fetch_price: Callable[[str], float],
        fetch_account: Callable[[str], AccountState],
        executor: Any,
        limits: dict[str, Any] | None = None,
        watchlist: list[str] | None = None,
        generate_kwargs: dict[str, Any] | None = None,
        memory_store: MemoryStore | None = None,
        run_id: str | None = None,
        observability: Observability | None = None,
    ) -> None:
        self.provider = provider
        self.fetch_bars = fetch_bars
        self.fetch_price = fetch_price
        self.fetch_account = fetch_account
        self.executor = executor
        self.limits = default_limits() if limits is None else dict(limits)
        self.watchlist = [
            str(s).upper() for s in (watchlist if watchlist is not None else self.limits.get("watchlist", []))
        ]
        self.signal_params = dict(self.limits.get("signal_params", {}) or {})
        self.generate_kwargs = dict(self.DEFAULT_GENERATE_KWARGS)
        self.generate_kwargs.update(generate_kwargs or {})
        self.memory_store = memory_store
        self.run_id = run_id or create_run_id()
        self.observability = observability

    def _submit(self, order: OrderRequest) -> Any:
        """Call the executor boundary (object with `.submit` or a plain callable)."""
        if hasattr(self.executor, "submit"):
            return self.executor.submit(order)
        return self.executor(order)

    def run(self) -> RunResult:
        """One full decision cycle. Any failure aborts with zero orders submitted."""
        account_by_symbol: dict[str, AccountState] = {}
        signals_by_symbol: dict[str, dict[str, Any]] = {}
        prices: dict[str, float] = {}

        for symbol in self.watchlist:
            try:
                if self.observability:
                    with self.observability.observe("market_data"):
                        account_by_symbol[symbol] = self.fetch_account(symbol)
                        bars = self.fetch_bars(symbol)
                        signals_by_symbol[symbol] = compute_technical_signals(symbol, bars, self.signal_params)
                        prices[symbol] = float(self.fetch_price(symbol))
                    self.observability.emit(
                        "market_data_received",
                        run_id=self.run_id,
                        symbol=symbol,
                        price=prices[symbol],
                    )
                else:
                    account_by_symbol[symbol] = self.fetch_account(symbol)
                    bars = self.fetch_bars(symbol)
                    signals_by_symbol[symbol] = compute_technical_signals(symbol, bars, self.signal_params)
                    prices[symbol] = float(self.fetch_price(symbol))
            except Exception as exc:  # noqa: BLE001 — fail closed on any data problem
                if self.observability:
                    self.observability.emit("market_data_rejected", run_id=self.run_id, symbol=symbol)
                return RunResult(False, "", "", error=f"market data failure for {symbol}: {exc}")

        historical_context = ""
        if self.memory_store is not None:
            try:
                records = {
                    symbol: self.memory_store.historical(symbol, limit=3)
                    for symbol in self.watchlist
                }
                historical_context = (
                    "HISTORICAL / INFORMATIONAL ONLY\n"
                    f"{json.dumps(records, sort_keys=True)}"
                )
            except Exception:  # noqa: BLE001 — memory is informational only
                historical_context = ""
        prompt = build_decision_prompt(
            self.watchlist,
            signals_by_symbol,
            prices,
            account_by_symbol,
            historical_context=historical_context,
        )

        try:
            timeout = float(self.generate_kwargs.get("timeout_seconds", 60.0))
            max_retries = int(self.generate_kwargs.get("max_retries", 3))
            clean_kwargs = {
                k: v
                for k, v in self.generate_kwargs.items()
                if k not in ("timeout_seconds", "max_retries")
            }
            if self.observability:
                self.observability.emit("llm_request", run_id=self.run_id, provider=type(self.provider).__name__)
                with self.observability.observe("llm"):
                    response = generate_with_retry(
                        self.provider,
                        prompt,
                        timeout_seconds=timeout,
                        max_retries=max_retries,
                        observability=self.observability,
                        run_id=self.run_id,
                        **clean_kwargs,
                    )
            else:
                response = generate_with_retry(
                    self.provider,
                    prompt,
                    timeout_seconds=timeout,
                    max_retries=max_retries,
                    run_id=self.run_id,
                    **clean_kwargs,
                )
            response_text = str(getattr(response, "text", "") or "")
            if self.observability:
                self.observability.emit("llm_success", run_id=self.run_id, response_length=len(response_text))
        except Exception as exc:  # noqa: BLE001 — provider must never reach broker on error
            if self.observability:
                self.observability.emit("llm_failure", run_id=self.run_id)
            return RunResult(False, prompt, "", error=f"LLM call failed: {exc}")
        if not response_text.strip():
            return RunResult(False, prompt, response_text, error="LLM returned an empty response")

        try:
            decisions = extract_trade_decisions(response_text, expected_symbols=set(self.watchlist))
        except (ValueError, TypeError) as exc:
            return RunResult(False, prompt, response_text, error=f"decisions invalid: {exc}")

        decision_ids = {
            str(decision["symbol"]).upper(): create_decision_id()
            for decision in decisions
        }
        if self.memory_store is not None:
            provider_model = str(getattr(self.provider, "model", "unknown"))
            provider_name = type(self.provider).__name__
            for decision in decisions:
                symbol = str(decision["symbol"]).upper()
                decision_id = decision_ids[symbol]
                try:
                    self.memory_store.save_decision(
                        {
                            "run_id": self.run_id,
                            "decision_id": decision_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "symbol": symbol,
                            "action": decision["action"],
                            "confidence": decision["confidence"],
                            "thesis": decision["thesis"],
                            "entry_reason": decision["entry_reason"],
                            "deterministic_signals": dict(signals_by_symbol[symbol]),
                            "decision_price": prices[symbol],
                            "account_context": asdict(account_by_symbol[symbol]),
                            "provider": provider_name,
                            "model": provider_model,
                            "config_version": "strategy.yaml",
                        }
                    )
                except Exception as exc:  # noqa: BLE001 — memory is informational only
                    del exc
                if self.observability:
                    self.observability.emit(
                        "decision_created",
                        run_id=self.run_id,
                        decision_id=decision_id,
                        symbol=symbol,
                        action=decision["action"],
                        confidence=decision["confidence"],
                    )

        outcomes: list[OrderOutcome] = []
        submitted_count = 0
        for decision in decisions:
            symbol = str(decision["symbol"]).upper()
            action = decision["action"]
            decision_id = decision_ids[symbol]
            # The LLM's position_size is deliberately ignored; quantity is computed here.
            account = replace(account_by_symbol[symbol], orders_placed_this_run=submitted_count)
            price = prices.get(symbol)
            qty = deterministic_quantity(action, price, account, self.limits)

            if action == "HOLD" or qty <= 0:
                risk = RiskDecision(True, "No order generated (HOLD or zero deterministic quantity).")
                order = None
            else:
                order = OrderRequest(
                    symbol=symbol,
                    side=action.lower(),
                    qty=qty,
                    order_type="market",
                    asset_class="us_equity",
                    idempotency_key=make_idempotency_key(
                        symbol=symbol,
                        side=action.lower(),
                        qty=qty,
                        run_id=self.run_id,
                        decision_id=decision_id,
                    ),
                )
                risk = check_order(order, account, self.limits, last_price=price)

            submitted = False
            executor_error = None
            execution_id = create_execution_id()
            if order is not None and risk.allowed:
                if self.observability:
                    self.observability.emit(
                        "final_gate",
                        run_id=self.run_id,
                        decision_id=decision_id,
                        execution_id=execution_id,
                        symbol=symbol,
                        side=order.side,
                        qty=order.qty,
                    )
                try:
                    self._submit(order)
                    submitted = True
                    submitted_count += 1
                    if self.observability:
                        self.observability.emit(
                            "execution_success",
                            run_id=self.run_id,
                            decision_id=decision_id,
                            execution_id=execution_id,
                            symbol=symbol,
                            side=order.side,
                            qty=order.qty,
                        )
                except Exception as exc:  # noqa: BLE001 — record, never crash mid-run
                    executor_error = str(exc)
                    if self.observability:
                        self.observability.emit(
                            "execution_failure",
                            run_id=self.run_id,
                            decision_id=decision_id,
                            execution_id=execution_id,
                            symbol=symbol,
                            error=str(exc),
                        )
            elif order is not None and not risk.allowed:
                if self.observability:
                    self.observability.emit(
                        "risk_rejection",
                        run_id=self.run_id,
                        decision_id=decision_id,
                        execution_id=execution_id,
                        symbol=symbol,
                        reason_code=risk.reason_code,
                    )

            if self.observability:
                self.observability.emit(
                    "risk_check" if risk.allowed else "risk_rejected",
                    run_id=self.run_id,
                    decision_id=decision_id,
                    execution_id=execution_id,
                    symbol=symbol,
                    reason_code=risk.reason_code,
                )

            outcomes.append(
                OrderOutcome(
                    symbol=symbol,
                    action=action,
                    decision=dict(decision),
                    current_price=price,
                    requested_qty=qty,
                    risk=risk,
                    order=order,
                    submitted=submitted,
                    executor_error=executor_error,
                )
            )
            if self.memory_store is not None:
                try:
                    self.memory_store.save_execution(
                        {
                            "decision_id": decision_ids[symbol],
                            "execution_id": execution_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "symbol": symbol,
                            "risk_allowed": risk.allowed,
                            "risk_reason": risk.reason,
                            "requested_qty": qty,
                            "submitted": submitted,
                            "executor_error": executor_error,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 — memory is informational only
                    del exc

        return RunResult(
            success=True,
            prompt=prompt,
            response=response_text,
            decisions=decisions,
            outcomes=outcomes,
            orders_submitted=submitted_count,
        )