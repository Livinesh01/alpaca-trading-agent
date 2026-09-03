"""Deterministic risk checks for order requests.

No network — pure logic. An order request + account/portfolio state in,
allow/deny + reason out. The safety layer for the agent's autonomy: the LLM can
propose anything, but only orders passing every rule here ever reach Alpaca.
"""

from dataclasses import dataclass


@dataclass
class OrderRequest:
    """The ONLY order representation that exits the paper-trading process.

    Created exclusively by Python sizing in agent/decision_loop.py and consumed by
    the risk-guard proxy — the LLM never constructs one and never sets a quantity.
    """

    symbol: str
    side: str          # "buy" | "sell"
    qty: float | None = None
    notional: float | None = None
    order_type: str = "market"
    asset_class: str = "us_equity"   # "us_equity" | "crypto" | "option"
    idempotency_key: str | None = None


@dataclass
class AccountState:
    cash: float
    buying_power: float
    equity: float
    daily_pnl: float
    open_position_count: int
    orders_placed_this_run: int
    existing_position_notional: float = 0.0   # already held in this symbol


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    reason_code: str = "RISK_CHECK"

    @classmethod
    def allowed_decision(cls, reason: str = "Passed all risk checks.") -> "RiskDecision":
        return cls(allowed=True, reason=reason, reason_code="ALLOWED")

    @classmethod
    def rejected(cls, reason: str, reason_code: str) -> "RiskDecision":
        return cls(allowed=False, reason=reason, reason_code=reason_code)


def _coerce_float(value: object) -> float | None:
    """MCP sends qty/notional as strings (Alpaca API convention); normalize for math."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _estimated_order_notional(order: OrderRequest, last_price: float | None) -> float | None:
    if order.notional is not None:
        return _coerce_float(order.notional)
    if order.qty is not None and last_price is not None:
        qty = _coerce_float(order.qty)
        price = _coerce_float(last_price)
        if qty is None or price is None or price <= 0:
            return None
        return qty * price
    return None


def check_order(
    order: OrderRequest,
    account: AccountState,
    limits: dict,
    last_price: float | None = None,
) -> RiskDecision:
    """Run every rule in sequence, fail closed on the first violation."""

    # 1. asset class gates
    if order.asset_class == "crypto" and not limits.get("allow_crypto", False):
        return RiskDecision.rejected(
            "Crypto trading is disabled in strategy config.", "CRYPTO_DISABLED"
        )
    if order.asset_class == "option" and not limits.get("allow_options", False):
        return RiskDecision.rejected(
            "Options trading is disabled in strategy config.", "OPTIONS_DISABLED"
        )
    if order.side == "sell_short" and not limits.get("allow_short_selling", False):
        return RiskDecision.rejected(
            "Short selling is disabled in strategy config.", "SHORT_SELLING_DISABLED"
        )

    # 2. watchlist doubles as allow-list unless overridden
    allowed_symbols = limits.get("allowed_symbols") or limits.get("watchlist") or []
    if allowed_symbols and order.symbol.upper() not in [s.upper() for s in allowed_symbols]:
        return RiskDecision.rejected(
            f"{order.symbol} is not in the approved watchlist.", "SYMBOL_NOT_APPROVED"
        )

    # 3. per-run order cap
    max_orders = limits.get("max_orders_per_run")
    if max_orders is not None and account.orders_placed_this_run >= max_orders:
        return RiskDecision.rejected(
            f"Per-run order cap reached ({account.orders_placed_this_run}/{max_orders}).",
            "PER_RUN_ORDER_CAP_REACHED",
        )

    # 4. daily loss cap halts new orders when breached
    max_daily_loss = limits.get("max_daily_loss_usd")
    if max_daily_loss is not None and account.daily_pnl <= -abs(max_daily_loss):
        return RiskDecision.rejected(
            f"Daily loss cap breached (P&L {account.daily_pnl:.2f}, limit -{max_daily_loss}). "
            "Trading halted for the day.",
            "DAILY_LOSS_CAP_BREACHED",
        )

    # 5. open position cap (buys only)
    if order.side == "buy":
        max_positions = limits.get("max_open_positions")
        if max_positions is not None and account.open_position_count >= max_positions:
            return RiskDecision.rejected(
                f"Max open positions reached ({account.open_position_count}/{max_positions}).",
                "MAX_OPEN_POSITIONS_REACHED",
            )

    # 6. order notional cap
    order_notional = _estimated_order_notional(order, last_price)
    if order.side == "buy" and order_notional is None:
        return RiskDecision.rejected(
            "Cannot determine order notional for buy order (missing or invalid price).",
            "MISSING_PRICE",
        )
    max_order_notional = limits.get("max_order_notional_usd")
    if (
        max_order_notional is not None
        and order_notional is not None
        and order_notional > max_order_notional
    ):
        return RiskDecision.rejected(
            f"Order notional ${order_notional:.2f} exceeds per-order limit "
            f"${max_order_notional:.2f}.",
            "MAX_ORDER_NOTIONAL_EXCEEDED",
        )

    # 7. resulting position cap (existing + new, buys only)
    max_position_notional = limits.get("max_position_notional_usd")
    if order.side == "buy" and max_position_notional is not None and order_notional is not None:
        resulting = account.existing_position_notional + order_notional
        if resulting > max_position_notional:
            return RiskDecision.rejected(
                f"Resulting position ${resulting:.2f} would exceed position cap "
                f"${max_position_notional:.2f}.",
                "MAX_POSITION_EXCEEDED",
            )

    # 8. buying power check
    if (
        order.side == "buy"
        and order_notional is not None
        and order_notional > account.buying_power
    ):
        return RiskDecision.rejected(
            f"Order notional ${order_notional:.2f} exceeds available buying power "
            f"${account.buying_power:.2f}.",
            "INSUFFICIENT_BUYING_POWER",
        )

    return RiskDecision.allowed_decision()
