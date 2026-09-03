"""Read-only application service boundaries for the Sentinel API.

Every service adapts an existing authoritative component (signals, risk_rules,
memory, observability, backtest, evaluation, risk-guard proxy). No service
creates new trading logic, calls Alpaca directly, or exposes secrets.
"""
