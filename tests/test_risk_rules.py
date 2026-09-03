import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_rules import AccountState, OrderRequest, check_order

LIMITS = {
    "max_position_notional_usd": 2000,
    "max_order_notional_usd": 1000,
    "max_open_positions": 5,
    "max_daily_loss_usd": 500,
    "max_orders_per_run": 3,
    "allowed_symbols": [],
    "watchlist": ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"],
    "allow_short_selling": False,
    "allow_options": False,
    "allow_crypto": False,
}


def base_account(**overrides):
    defaults = {
        "cash": 10000,
        "buying_power": 10000,
        "equity": 10000,
        "daily_pnl": 0,
        "open_position_count": 0,
        "orders_placed_this_run": 0,
        "existing_position_notional": 0,
    }
    defaults.update(overrides)
    return AccountState(**defaults)


def test_allows_reasonable_buy():
    order = OrderRequest(symbol="AAPL", side="buy", qty=1)
    decision = check_order(order, base_account(), LIMITS, last_price=200)
    assert decision.allowed


def test_rejects_symbol_outside_watchlist():
    order = OrderRequest(symbol="GME", side="buy", qty=1)
    decision = check_order(order, base_account(), LIMITS, last_price=20)
    assert not decision.allowed
    assert "watchlist" in decision.reason


def test_rejects_order_notional_over_limit():
    order = OrderRequest(symbol="NVDA", side="buy", qty=10)
    decision = check_order(order, base_account(), LIMITS, last_price=900)
    assert not decision.allowed
    assert "exceeds per-order limit" in decision.reason


def test_rejects_when_daily_loss_cap_breached():
    order = OrderRequest(symbol="AAPL", side="buy", qty=1)
    decision = check_order(order, base_account(daily_pnl=-600), LIMITS, last_price=200)
    assert not decision.allowed
    assert "Daily loss cap" in decision.reason


def test_rejects_when_max_open_positions_reached():
    order = OrderRequest(symbol="AAPL", side="buy", qty=1)
    decision = check_order(order, base_account(open_position_count=5), LIMITS, last_price=200)
    assert not decision.allowed
    assert "Max open positions" in decision.reason


def test_rejects_when_per_run_order_cap_reached():
    order = OrderRequest(symbol="AAPL", side="buy", qty=1)
    decision = check_order(
        order, base_account(orders_placed_this_run=3), LIMITS, last_price=200
    )
    assert not decision.allowed
    assert "Per-run order cap" in decision.reason


def test_rejects_short_selling_when_disabled():
    order = OrderRequest(symbol="AAPL", side="sell_short", qty=1)
    decision = check_order(order, base_account(), LIMITS, last_price=200)
    assert not decision.allowed
    assert "Short selling" in decision.reason


def test_rejects_insufficient_buying_power():
    order = OrderRequest(symbol="AAPL", side="buy", qty=4)
    decision = check_order(
        order, base_account(buying_power=100), LIMITS, last_price=200
    )
    assert not decision.allowed
    assert "buying power" in decision.reason


def test_rejects_resulting_position_over_cap():
    order = OrderRequest(symbol="AAPL", side="buy", qty=1)
    decision = check_order(
        order,
        base_account(existing_position_notional=1900),
        LIMITS,
        last_price=200,
    )
    assert not decision.allowed
    assert "position cap" in decision.reason
