"""Centralized, validated configuration for Sentinel.

All configuration flows through here. Environment-based, strongly typed, with
safe defaults and explicit validation. Dangerous configurations are rejected
at import time — never silently defaulted.

Critical invariant: LIVE trading can NEVER become enabled accidentally.
PAPER_TRADING=true is the only safe state. Anything else fails closed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from errors import ConfigurationError, LiveTradingUnsupportedError


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int | None = None, min_value: int | None = None, max_value: int | None = None) -> int | None:
    value = _env(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer, got: {value!r}") from exc
    if min_value is not None and parsed < min_value:
        raise ConfigurationError(f"{name} must be >= {min_value}, got {parsed}")
    if max_value is not None and parsed > max_value:
        raise ConfigurationError(f"{name} must be <= {max_value}, got {parsed}")
    return parsed


def _env_float(name: str, default: float | None = None, min_value: float | None = None, max_value: float | None = None) -> float | None:
    value = _env(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number, got: {value!r}") from exc
    if min_value is not None and parsed < min_value:
        raise ConfigurationError(f"{name} must be >= {min_value}, got {parsed}")
    if max_value is not None and parsed > max_value:
        raise ConfigurationError(f"{name} must be <= {max_value}, got {parsed}")
    return parsed


@dataclass(frozen=True)
class SentinelConfig:
    """Immutable, validated configuration snapshot.

    Construct via SentinelConfig.load() — never directly, so validation
    always runs before any component can observe an unsafe value.
    """

    paper_trading: bool = True
    environment: str = "development"
    kill_switch: bool = False
    backend: str = "decision_loop"
    llm_provider: str = "fake"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_max_tokens: int = 1024
    data_mode: str = "offline"
    market_data_max_age_seconds: float = 120.0
    api_auth_mode: str = "disabled"
    api_cors_origins: tuple[str, ...] = ()
    rate_limit_per_minute: int = 120
    max_position_notional_usd: float = 10_000.0
    max_order_notional_usd: float = 5_000.0
    max_open_positions: int = 5
    max_daily_loss_usd: float = 1_000.0
    max_orders_per_run: int = 3
    allow_short_selling: bool = False
    allow_options: bool = False
    allow_crypto: bool = False
    watchlist: tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "SPY", "TSLA")

    @classmethod
    def load(cls) -> SentinelConfig:
        """Build and validate configuration from environment variables.

        Raises ConfigurationError (or LiveTradingUnsupportedError) for any
        invalid or dangerous combination. Never returns a config that could
        silently enable live trading.
        """
        trading_mode = (_env("TRADING_MODE") or "").lower()
        if trading_mode and trading_mode != "paper":
            raise LiveTradingUnsupportedError("TRADING_MODE must be 'paper'; live trading is unsupported")
        paper_trading = _env_bool("PAPER_TRADING", default=trading_mode == "paper")
        kill_switch = _env_bool("TRADING_KILL_SWITCH", default=False)

        environment = (_env("APP_ENV") or _env("SENTINEL_ENVIRONMENT", "development") or "development").lower()
        if environment not in {"development", "paper", "production"}:
            raise ConfigurationError(
                "SENTINEL_ENVIRONMENT must be 'development', 'paper', or 'production'"
            )

        if not paper_trading:
            raise LiveTradingUnsupportedError(
                "LIVE trading is not implemented and cannot be enabled. "
                "PAPER_TRADING=true is the only supported mode."
            )

        if environment == "production":
            raise ConfigurationError(
                "production mode is disabled until a genuine OIDC/OAuth2 identity provider "
                "and independently verified paper execution path are configured"
            )

        backend = _env("AGENT_BACKEND", "decision_loop") or "decision_loop"
        if backend not in {"decision_loop", "cline"}:
            raise ConfigurationError(
                f"AGENT_BACKEND must be 'decision_loop' or 'cline', got: {backend!r}"
            )

        llm_provider = _env("LLM_PROVIDER", "fake") or "fake"
        if llm_provider not in {"fake", "featherless", "nvidia"}:
            raise ConfigurationError(
                f"LLM_PROVIDER must be 'fake', 'featherless', or 'nvidia', got: {llm_provider!r}"
            )

        llm_timeout_seconds = _env_float("LLM_TIMEOUT_SECONDS", default=60.0, min_value=1.0, max_value=600.0)
        llm_max_retries = _env_int("LLM_MAX_RETRIES", default=3, min_value=0, max_value=10)
        llm_max_tokens = _env_int("LLM_MAX_TOKENS", default=1024, min_value=64, max_value=32_768)

        data_mode = _env("SENTINEL_DATA_MODE", "offline") or "offline"
        if data_mode not in {"proxy", "offline"}:
            raise ConfigurationError(
                f"SENTINEL_DATA_MODE must be 'proxy' or 'offline', got: {data_mode!r}"
            )

        api_auth_mode = _env("API_AUTH_MODE", "disabled") or "disabled"
        if api_auth_mode not in {"disabled", "development"}:
            raise ConfigurationError(
                f"API_AUTH_MODE must be 'disabled' or 'development', got: {api_auth_mode!r}"
            )
        if environment == "paper" and api_auth_mode != "development":
            raise ConfigurationError("paper mode requires explicitly configured authentication")

        cors_raw = _env("API_CORS_ORIGINS", "") or ""
        api_cors_origins = tuple(origin.strip() for origin in cors_raw.split(",") if origin.strip())

        rate_limit_per_minute = _env_int("API_RATE_LIMIT_PER_MINUTE", default=120, min_value=1, max_value=10_000)

        market_data_max_age_seconds = _env_float(
            "MARKET_DATA_MAX_AGE_SECONDS", default=120.0, min_value=1.0, max_value=3600.0
        )

        max_position_notional_usd = _env_float("MAX_POSITION_NOTIONAL_USD", default=10_000.0, min_value=0.0)
        max_order_notional_usd = _env_float("MAX_ORDER_NOTIONAL_USD", default=5_000.0, min_value=0.0)
        max_open_positions = _env_int("MAX_OPEN_POSITIONS", default=5, min_value=0, max_value=1000)
        max_daily_loss_usd = _env_float("MAX_DAILY_LOSS_USD", default=1_000.0, min_value=0.0)
        max_orders_per_run = _env_int("MAX_ORDERS_PER_RUN", default=3, min_value=0, max_value=1000)
        allow_short_selling = _env_bool("ALLOW_SHORT_SELLING", default=False)
        allow_options = _env_bool("ALLOW_OPTIONS", default=False)
        allow_crypto = _env_bool("ALLOW_CRYPTO", default=False)

        watchlist_raw = _env("WATCHLIST", "AAPL,MSFT,NVDA,SPY,TSLA") or ""
        watchlist = tuple(s.strip().upper() for s in watchlist_raw.split(",") if s.strip())
        if not watchlist:
            raise ConfigurationError("WATCHLIST must contain at least one symbol")

        if llm_provider == "featherless" and not _env("FEATHERLESS_API_KEY"):
            raise ConfigurationError("LLM_PROVIDER=featherless requires FEATHERLESS_API_KEY")
        elif llm_provider == "nvidia" and not _env("NVIDIA_API_KEY"):
            raise ConfigurationError("LLM_PROVIDER=nvidia requires NVIDIA_API_KEY")

        return cls(
            paper_trading=paper_trading,
            environment=environment,
            kill_switch=kill_switch,
            backend=backend,
            llm_provider=llm_provider,
            llm_timeout_seconds=llm_timeout_seconds,
            llm_max_retries=llm_max_retries,
            llm_max_tokens=llm_max_tokens,
            data_mode=data_mode,
            api_auth_mode=api_auth_mode,
            api_cors_origins=api_cors_origins,
            rate_limit_per_minute=rate_limit_per_minute,
            market_data_max_age_seconds=market_data_max_age_seconds,
            max_position_notional_usd=max_position_notional_usd,
            max_order_notional_usd=max_order_notional_usd,
            max_open_positions=max_open_positions,
            max_daily_loss_usd=max_daily_loss_usd,
            max_orders_per_run=max_orders_per_run,
            allow_short_selling=allow_short_selling,
            allow_options=allow_options,
            allow_crypto=allow_crypto,
            watchlist=watchlist,
        )

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "paper_trading": self.paper_trading, "environment": self.environment,
            "kill_switch": self.kill_switch, "backend": self.backend,
            "llm_provider": self.llm_provider, "llm_timeout_seconds": self.llm_timeout_seconds,
            "llm_max_retries": self.llm_max_retries, "data_mode": self.data_mode,
            "api_auth_mode": self.api_auth_mode, "rate_limit_per_minute": self.rate_limit_per_minute,
            "market_data_max_age_seconds": self.market_data_max_age_seconds,
            "max_position_notional_usd": self.max_position_notional_usd,
            "max_order_notional_usd": self.max_order_notional_usd,
            "max_open_positions": self.max_open_positions, "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_orders_per_run": self.max_orders_per_run, "allow_short_selling": self.allow_short_selling,
            "allow_options": self.allow_options, "allow_crypto": self.allow_crypto, "watchlist": list(self.watchlist),
        }
