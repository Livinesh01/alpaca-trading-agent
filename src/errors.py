"""Internal error taxonomy for Sentinel.

Every error carries a stable, machine-readable `code` (safe to expose in API
responses and structured events) and a human-readable message. Messages must
never contain credentials — the LLM providers redact before raising, and the
API layer never forwards exception internals.

Categories (see also observability event names):

    CONFIGURATION_ERROR      invalid/unsafe configuration
    LIVE_TRADING_UNSUPPORTED live trading was requested; it is not implemented
    MARKET_DATA_ERROR        market data missing/malformed
    MARKET_DATA_STALE        market data older than the configured freshness cap
    LLM_TIMEOUT              LLM request exceeded its deadline
    LLM_PROVIDER_ERROR       transport/provider failure (message redacted)
    LLM_INVALID_RESPONSE     reply failed strict parsing / size limits
    DECISION_VALIDATION_ERROR structured decision failed schema validation
    RISK_REJECTION           risk_rules.check_order rejected the order
    KILL_SWITCH_ACTIVE       emergency stop engaged
    FINAL_GATE_REJECTION     FinalOrderGate refused immediate pre-execution checks
    DUPLICATE_EXECUTION      the same logical execution was attempted twice
    EXECUTION_ERROR          the executor/proxy failed
    MEMORY_ERROR             informational memory store failed (never fatal)
    INTERNAL_ERROR           anything unexpected
"""

from __future__ import annotations


class SentinelError(ValueError):
    """Base class for all classified Sentinel errors."""

    code = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details: dict | None = details

    def to_dict(self) -> dict[str, str | None]:
        """Safe representation for API responses / events (no internals)."""
        result: dict[str, str | None] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            result["details"] = str(self.details)
        return result


class ConfigurationError(SentinelError):
    code = "CONFIGURATION_ERROR"


class LiveTradingUnsupportedError(ConfigurationError):
    """Live trading was requested. It is not implemented and must never be
    enabled by configuration alone; the safe response is a loud refusal."""

    code = "LIVE_TRADING_UNSUPPORTED"


class MarketDataError(SentinelError):
    code = "MARKET_DATA_ERROR"


class MarketDataValidationError(MarketDataError):
    """Market data failed structural validation (missing fields, bad types, etc.)."""

    code = "MARKET_DATA_ERROR"


class MarketDataStaleError(MarketDataError):
    code = "MARKET_DATA_STALE"


class LLMError(SentinelError):
    """Base for all LLM-related errors (timeout, provider, validation, exhaustion)."""
    code = "LLM_ERROR"


class LLMTimeoutError(LLMError):
    code = "LLM_TIMEOUT"


class LLMProviderError(LLMError):
    code = "LLM_PROVIDER_ERROR"


class LLMInvalidResponse(LLMError):
    code = "LLM_INVALID_RESPONSE"


class LLMExhaustedError(LLMError):
    code = "LLM_EXHAUSTED"


class DecisionValidationError(SentinelError):
    code = "DECISION_VALIDATION_ERROR"


class RiskRejectionError(SentinelError):
    def __init__(self, message: str, *, reason_code: str = "RISK_REJECTED") -> None:
        super().__init__(message, code="RISK_REJECTION")
        self.reason_code = reason_code


class KillSwitchActiveError(SentinelError):
    code = "KILL_SWITCH_ACTIVE"


class FinalGateRejectionError(SentinelError):
    def __init__(self, message: str, *, reason_code: str = "FINAL_GATE_REJECTED") -> None:
        super().__init__(message, code="FINAL_GATE_REJECTION")
        self.reason_code = reason_code


class DuplicateExecutionError(SentinelError):
    code = "DUPLICATE_EXECUTION"


class ExecutionError(SentinelError):
    code = "EXECUTION_ERROR"


class MemoryStoreError(SentinelError):
    code = "MEMORY_ERROR"
