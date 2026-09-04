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


def is_production_env() -> bool:
    """True only when APP_ENV is explicitly 'production'."""
    return (_env("APP_ENV") or "development").strip().lower() == "production"


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
    trading_enabled: bool = True
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
    db_url: str = ""
    jwt_signing_secret: str = ""
    max_position_notional_usd: float = 10_000.0
    max_order_notional_usd: float = 5_000.0
    max_open_positions: int = 5
    max_daily_loss_usd: float = 1_000.0
    max_orders_per_run: int = 3
    allow_short_selling: bool = False
    allow_options: bool = False
    allow_crypto: bool = False
    watchlist: tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "SPY", "TSLA")
    alpaca_paper: bool = True

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
        trading_enabled = _env_bool("TRADING_ENABLED", default=True)
        kill_switch = _env_bool("TRADING_KILL_SWITCH", default=False)
        if not trading_enabled:
            kill_switch = True

        environment = (_env("APP_ENV") or _env("SENTINEL_ENVIRONMENT", "development") or "development").lower()
        if environment not in {"development", "paper", "production"}:
            raise ConfigurationError(
                "APP_ENV must be 'development', 'paper', or 'production'"
            )

        if not paper_trading:
            raise LiveTradingUnsupportedError(
                "LIVE trading is not implemented and cannot be enabled. "
                "PAPER_TRADING=true is the only supported mode."
            )

        alpaca_paper = _env_bool("ALPACA_PAPER", default=True)
        if not alpaca_paper:
            raise LiveTradingUnsupportedError(
                "ALPACA_PAPER must be true (or unset, which defaults to true). "
                "Live Alpaca execution is never supported."
            )
        if not _env("ALPACA_PAPER_TRADE", "true").strip().lower() in {"1", "true", "yes", "on"}:
            raise ConfigurationError("ALPACA_PAPER_TRADE must be true for paper trading")

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
        if environment == "production":
            if api_auth_mode != "production":
                raise ConfigurationError(
                    "APP_ENV=production requires API_AUTH_MODE=production (JWT/OIDC). "
                    "Development-only auth is not safe for production."
                )
        elif api_auth_mode not in {"disabled", "development"}:
            raise ConfigurationError(
                f"API_AUTH_MODE must be 'disabled', 'development', or 'production', got: {api_auth_mode!r}"
            )
        if environment == "paper" and api_auth_mode != "development":
            raise ConfigurationError("paper mode requires explicitly configured authentication")

        cors_raw = _env("API_CORS_ORIGINS", "") or ""
        api_cors_origins = tuple(origin.strip() for origin in cors_raw.split(",") if origin.strip())

        rate_limit_per_minute = _env_int("API_RATE_LIMIT_PER_MINUTE", default=120, min_value=1, max_value=10_000)

        market_data_max_age_seconds = _env_float(
            "MARKET_DATA_MAX_AGE_SECONDS", default=120.0, min_value=1.0, max_value=3600.0
        )

        db_url = _env("DATABASE_URL", "") or ""
        jwt_signing_secret = _env("JWT_SIGNING_SECRET", "") or ""

        if llm_provider == "featherless" and not _env("FEATHERLESS_API_KEY"):
            raise ConfigurationError("LLM_PROVIDER=featherless requires FEATHERLESS_API_KEY")
        elif llm_provider == "nvidia" and not _env("NVIDIA_API_KEY"):
            raise ConfigurationError("LLM_PROVIDER=nvidia requires NVIDIA_API_KEY")

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

        return cls(
            paper_trading=paper_trading,
            environment=environment,
            kill_switch=kill_switch,
            trading_enabled=trading_enabled,
            backend=backend,
            llm_provider=llm_provider,
            llm_timeout_seconds=llm_timeout_seconds,
            llm_max_retries=llm_max_retries,
            llm_max_tokens=llm_max_tokens,
            data_mode=data_mode,
            api_auth_mode=api_auth_mode,
            api_cors_origins=api_cors_origins,
            rate_limit_per_minute=rate_limit_per_minute,
            db_url=db_url,
            jwt_signing_secret=jwt_signing_secret,
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
            alpaca_paper=alpaca_paper,
        )

    @staticmethod
    def validate_production_config() -> SentinelConfig:
        """Validate production configuration. Fails closed on any missing/invalid value.

        Production requires (fails closed when any is missing or unsafe):
        - APP_ENV=production
        - TRADING_MODE=paper and PAPER_TRADING=true
        - TRADING_ENABLED=true (kill switch must not be active)
        - ALPACA_PAPER=true and ALPACA_PAPER_TRADE=true
        - ALPACA_API_KEY and ALPACA_SECRET_KEY (paper credentials)
        - DATABASE_URL (PostgreSQL)
        - API_AUTH_MODE=production
        - JWT_SIGNING_SECRET (>= 32 chars)
        - A real LLM_PROVIDER with the matching API key
        - SENTINEL_DATA_MODE=proxy (real market data)
        - SENTINEL_LOAD_DOTENV must NOT be enabled (no .env in production)
        """
        cfg = SentinelConfig.load()

        if cfg.environment != "production":
            raise ConfigurationError(
                f"validate_production_config requires APP_ENV=production, got {cfg.environment!r}"
            )

        if not cfg.paper_trading:
            raise LiveTradingUnsupportedError("Production requires PAPER_TRADING=true")
        if not cfg.alpaca_paper:
            raise LiveTradingUnsupportedError("Production requires ALPACA_PAPER=true")

        if not cfg.trading_enabled:
            raise ConfigurationError(
                "Production requires TRADING_ENABLED=true (kill switch must not be active)"
            )

        if not cfg.db_url:
            raise ConfigurationError("Production requires DATABASE_URL (PostgreSQL connection string)")
        if not cfg.db_url.startswith(("postgresql://", "postgres://")):
            raise ConfigurationError("DATABASE_URL must be a PostgreSQL connection string")

        if cfg.api_auth_mode != "production":
            raise ConfigurationError("Production requires API_AUTH_MODE=production")
        if not cfg.jwt_signing_secret:
            raise ConfigurationError("Production requires JWT_SIGNING_SECRET")
        if len(cfg.jwt_signing_secret) < 32:
            raise ConfigurationError("JWT_SIGNING_SECRET must be at least 32 characters")

        if not _env("ALPACA_API_KEY", ""):
            raise ConfigurationError("Production requires ALPACA_API_KEY (Alpaca paper credential)")
        if not _env("ALPACA_SECRET_KEY", ""):
            raise ConfigurationError("Production requires ALPACA_SECRET_KEY (Alpaca paper credential)")

        if cfg.llm_provider == "fake":
            raise ConfigurationError("Production requires a real LLM_PROVIDER (featherless or nvidia)")
        if cfg.llm_provider == "featherless" and not _env("FEATHERLESS_API_KEY", ""):
            raise ConfigurationError("Production requires FEATHERLESS_API_KEY for LLM_PROVIDER=featherless")
        if cfg.llm_provider == "nvidia" and not _env("NVIDIA_API_KEY", ""):
            raise ConfigurationError("Production requires NVIDIA_API_KEY for LLM_PROVIDER=nvidia")

        if cfg.data_mode != "proxy":
            raise ConfigurationError("Production requires SENTINEL_DATA_MODE=proxy (real paper market data)")

        if _env_bool("SENTINEL_LOAD_DOTENV", default=False):
            raise ConfigurationError("Production must not enable SENTINEL_LOAD_DOTENV (.env files are forbidden)")

        return cfg

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "paper_trading": self.paper_trading, "environment": self.environment,
            "kill_switch": self.kill_switch, "trading_enabled": self.trading_enabled,
            "backend": self.backend,
            "llm_provider": self.llm_provider, "llm_timeout_seconds": self.llm_timeout_seconds,
            "llm_max_retries": self.llm_max_retries, "data_mode": self.data_mode,
            "api_auth_mode": self.api_auth_mode, "rate_limit_per_minute": self.rate_limit_per_minute,
            "market_data_max_age_seconds": self.market_data_max_age_seconds,
            "max_position_notional_usd": self.max_position_notional_usd,
            "max_order_notional_usd": self.max_order_notional_usd,
            "max_open_positions": self.max_open_positions, "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_orders_per_run": self.max_orders_per_run, "allow_short_selling": self.allow_short_selling,
            "allow_options": self.allow_options, "allow_crypto": self.allow_crypto, "watchlist": list(self.watchlist),
            "alpaca_paper": self.alpaca_paper,
            "database_configured": bool(self.db_url), "auth_mode": self.api_auth_mode,
        }
