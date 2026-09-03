"""Market-data source boundary for read-only Sentinel services.

This adapter is the ONLY way API services obtain account, position, order, and
market data. In production it tunnels through the existing risk-guard MCP proxy
(the same authoritative Alpaca paper path the orchestrator uses) and exposes
READ tools only — order submission is never reachable from here. If Alpaca
access is missing or unreachable the data source fails explicitly
(MarketDataUnavailable); it never fabricates values.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Protocol

DEFAULT_MAX_AGE_SECONDS = 120


class MarketDataUnavailable(RuntimeError):
    """Explicit data-source unavailability; never a silently fabricated value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MarketDataSource(Protocol):
    """Read-only data contract consumed by every API service."""

    def get_account(self) -> dict[str, Any]: ...

    def get_positions(self) -> list[dict[str, Any]]: ...

    def get_orders(self) -> list[dict[str, Any]]: ...

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Day",
        days: int = 180,
        limit: int = 500,
    ) -> list[dict[str, Any]]: ...

    def get_latest_trade(self, symbol: str) -> dict[str, Any]: ...


class NoDataSource:
    """Explicit unavailable source: every read raises an explicit reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _raise(self, *_args: Any, **_kwargs: Any) -> Any:
        raise MarketDataUnavailable(self.reason)

    get_account = _raise
    get_positions = _raise
    get_orders = _raise
    get_bars = _raise
    get_latest_trade = _raise


class ProxyMarketDataSource:
    """READ-ONLY adapter over the risk-guard proxy MCP session.

    Sessions provide `.call(tool, arguments)` — in production this is the
    orchestrator's `_RiskGuardProxySession` (a stdio MCP client to
    risk_guard_proxy.py); tests inject a fake. Only read tools are invoked.
    """

    def __init__(self, session: Any, limits: dict[str, Any] | None = None) -> None:
        self._session = session
        self._limits = dict(limits or {})

    @staticmethod
    def _unwrap(text: str) -> Any:
        import risk_guard_proxy as rgp

        return rgp._unwrap_payload(rgp._to_json_or_none(text))

    def get_account(self) -> dict[str, Any]:
        payload = self._unwrap(self._session.call("get_account_info", {}))
        return payload if isinstance(payload, dict) else {}

    def get_positions(self) -> list[dict[str, Any]]:
        payload = self._unwrap(self._session.call("get_all_positions", {}))
        return payload if isinstance(payload, list) else []

    def get_orders(self) -> list[dict[str, Any]]:
        payload = self._unwrap(self._session.call("get_orders", {}))
        return payload if isinstance(payload, list) else []

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Day",
        days: int = 180,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        import risk_guard_proxy as rgp

        payload = {
            "symbols": str(symbol).upper(),
            "timeframe": timeframe,
            "days": int(days),
            "limit": int(limit),
            "sort": "asc",
        }
        raw = self._session.call(rgp.STOCK_BARS_TOOL, payload)
        bars = rgp._extract_stock_bars(self._unwrap(raw), symbol)
        if not bars:
            raise MarketDataUnavailable(f"no bar data returned for {str(symbol).upper()}")
        return bars

    def get_latest_trade(self, symbol: str) -> dict[str, Any]:
        import risk_guard_proxy as rgp

        payload = self._unwrap(self._session.call(rgp.LATEST_TRADE_TOOL, {"symbols": str(symbol).upper()}))
        if not isinstance(payload, dict):
            raise MarketDataUnavailable(f"latest trade unavailable for {str(symbol).upper()}")
        return payload


_shared_lock = threading.Lock()
_shared_session: Any = None


def _credentials_configured() -> bool:
    return bool(
        (os.environ.get("ALPACA_API_KEY") or "").strip()
        and (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    )


def get_data_source() -> MarketDataSource:
    """Build the configured data source.

    SENTINEL_DATA_MODE=proxy (default) uses the risk-guard proxy when Alpaca
    credentials are configured; SENTINEL_DATA_MODE=offline forces the explicit
    unavailable source. Never fabricates account/position/order values.
    """
    mode = os.environ.get("SENTINEL_DATA_MODE", "proxy").strip().lower()
    if mode != "proxy":
        return NoDataSource("data source disabled by SENTINEL_DATA_MODE")
    if not _credentials_configured():
        return NoDataSource("Alpaca credentials are not configured; account/positions/orders/market data unavailable")
    global _shared_session
    with _shared_lock:
        if _shared_session is None:
            from orchestrator import _RiskGuardProxySession

            session = _RiskGuardProxySession()
            try:
                session.start()
            except Exception as exc:  # explicit, non-secret failure
                raise MarketDataUnavailable(f"risk-guard proxy unavailable: {type(exc).__name__}") from exc
            _shared_session = session
    return ProxyMarketDataSource(_shared_session)


def close_data_source() -> None:
    """Best-effort teardown of the shared proxy session (never raises)."""
    global _shared_session
    with _shared_lock:
        if _shared_session is not None:
            try:
                _shared_session.close()
            finally:
                _shared_session = None