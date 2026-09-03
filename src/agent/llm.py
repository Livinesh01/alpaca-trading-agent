"""Minimal LLM provider abstraction.

`LLMProvider` is the seam between the agent loop and whatever model backend it
talks to. `generate()` is the only cross-provider call site; providers translate
their own client API into that one signature.
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from dotenv import load_dotenv

from errors import (
    LLMError,
    LLMExhaustedError,
    LLMInvalidResponse,
    LLMProviderError,
    LLMTimeoutError,
)

# Local dotenv loading is opt-in. Deployment workers must receive secrets from
# their process environment or secret manager, never an arbitrary workspace file.
if os.environ.get("SENTINEL_LOAD_DOTENV", "").strip().lower() in {"1", "true", "yes", "on"}:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


@dataclass(frozen=True)
class LLMResponse:
    """A single model reply. `text` is the full completion, `usage` optional."""

    text: str
    usage: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Anything that can take a prompt and return a completion."""

    def generate(self, prompt: str, **kwargs: Any) -> LLMResponse:
        """Complete `prompt`, returning the generated text plus optional usage.

        `kwargs` are provider-specific knobs (temperature, max_tokens, ...).
        Implementations must be synchronous and raise on transport/API errors.
        """
        ...


def generate(provider: LLMProvider, prompt: str, **kwargs: Any) -> LLMResponse:
    """Provider-independent completion: the one way the rest of the agent calls an LLM.

    Delegates straight to `provider.generate`. Kept as a function so the future
    addition of retries/logging lives in one place instead of every call site.
    """
    return provider.generate(prompt, **kwargs)


# --- Structured provider failure taxonomy ---
# LLMError, LLMTimeoutError, LLMProviderError, LLMInvalidResponse, LLMExhaustedError
# are defined in src/errors.py and imported above.


def _classify_exception(exc: Exception) -> tuple[str, bool]:
    """Classify an exception from a provider call.

    Returns (category, is_transient) where transient errors are safe to retry.
    """
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, LLMTimeoutError)):
        return "timeout", True
    if isinstance(exc, (ConnectionError, OSError)):
        return "connection", True
    message = str(exc).lower()
    transient_signals = ("rate limit", "429", "503", "502", "504", "too many requests", "temporarily", "overloaded", "retry")
    if any(signal in message for signal in transient_signals):
        return "transient", True
    if "timeout" in message:
        return "timeout", True
    return "provider", False


def generate_with_retry(
    provider: LLMProvider,
    prompt: str,
    *,
    timeout_seconds: float = 60.0,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    observability: Any = None,
    run_id: str | None = None,
    **kwargs: Any,
) -> LLMResponse:
    """Call a provider with bounded retries, exponential backoff, and structured failure.

    Only transient failures (timeouts, connection errors, rate limits) are retried.
    Persistent failures (validation errors, auth errors, malformed responses) fail
    immediately without retrying — they won't magically succeed on retry.

    On exhaustion, raises LLMExhaustedError with the full history of attempts.
    """
    last_exc: Exception | None = None
    attempts: list[dict[str, Any]] = []

    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                if observability:
                    observability.emit("llm_retry", run_id=run_id, attempt=attempt, delay_seconds=delay)
                time.sleep(delay)

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(provider.generate, prompt, **kwargs)
                try:
                    response = future.result(timeout=timeout_seconds)
                except TimeoutError:
                    # In Python 3.11+ concurrent.futures.TimeoutError IS TimeoutError,
                    # so this clause catches both the executor's own deadline and a
                    # TimeoutError raised by the provider callable. Distinguish them:
                    # future.done() is False only when the executor itself timed out
                    # (callable still running); if the callable raised TimeoutError the
                    # future is already done and we re-raise so it is classified/retested.
                    if not future.done():
                        raise LLMTimeoutError(
                            f"LLM provider timed out after {timeout_seconds}s on attempt {attempt + 1}"
                        )
                    raise  # provider-raised TimeoutError — let it propagate for retry classification

            if not isinstance(response, LLMResponse):
                raise LLMInvalidResponse(f"Provider returned {type(response).__name__}, expected LLMResponse")

            if observability:
                observability.emit("llm_success", run_id=run_id, attempt=attempt + 1, model=getattr(response, 'model', None))
            return response

        except (LLMInvalidResponse, LLMExhaustedError):
            raise  # Never retry validation errors
        except LLMError as exc:
            category, is_transient = _classify_exception(exc)
            attempts.append({"attempt": attempt + 1, "category": category, "transient": is_transient, "error": str(exc)})
            last_exc = exc
            if observability:
                observability.emit("llm_failure", run_id=run_id, attempt=attempt + 1, category=category, transient=is_transient)
            if not is_transient:
                raise LLMProviderError(f"Non-retryable LLM failure: {exc}") from exc
        except Exception as exc:
            category, is_transient = _classify_exception(exc)
            attempts.append({"attempt": attempt + 1, "category": category, "transient": is_transient, "error": str(exc)})
            last_exc = exc
            if observability:
                observability.emit("llm_failure", run_id=run_id, attempt=attempt + 1, category=category, transient=is_transient)
            if not is_transient:
                raise LLMProviderError(f"Non-retryable LLM failure: {exc}") from exc

    raise LLMExhaustedError(
        f"LLM provider exhausted all {max_retries + 1} attempts",
        details={"attempts": attempts, "last_error": str(last_exc)},
    )


class FakeLLMProvider:
    """Deterministic in-memory provider for tests.

    Usage:

        provider = FakeLLMProvider(responses={"hi": "hello"})
        generate(provider, "hi").text  # -> "hello"
        provider.requests                # -> [{"prompt": "hi", "kwargs": {}}]

    Unmapped prompts raise so tests fail loudly on unexpected inputs.
    """

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses: dict[str, str] = dict(responses or {})
        self.requests: list[dict[str, Any]] = []
        self.fail_next: BaseException | None = None

    def add_response(self, prompt: str, text: str) -> None:
        self.responses[prompt] = text

    def generate(self, prompt: str, **kwargs: Any) -> LLMResponse:
        self.requests.append({"prompt": prompt, "kwargs": dict(kwargs)})
        if self.fail_next is not None:
            exc = self.fail_next
            self.fail_next = None
            raise exc
        if prompt not in self.responses:
            raise KeyError(f"FakeLLMProvider: no response for prompt {prompt!r}")
        return LLMResponse(text=self.responses[prompt])


_MISSING_KEY_HINT = (
    "FEATHERLESS_API_KEY is not set. Add FEATHERLESS_API_KEY to your environment "
    "or .env (never hard-code or commit it)."
)
_MISSING_MODEL_HINT = (
    "FEATHERLESS_MODEL is not set or is empty. Set FEATHERLESS_MODEL in your "
    "environment or .env, e.g. FEATHERLESS_MODEL=<model-id>."
)
_REDACTED = "[REDACTED]"


class FeatherlessLLMProvider:
    """OpenAI-compatible provider for Featherless AI.

    Speaks Featherless' OpenAI-compatible API via the `openai` SDK, so no
    Featherless-specific SDK or extra dependency is needed. Reads
    FEATHERLESS_API_KEY and FEATHERLESS_MODEL from the environment (dotenv
    loads the project `.env`, which never overrides real env vars). An API key
    never appears in any raised exception.
    """

    DEFAULT_BASE_URL = "https://api.featherless.ai/v1"
    _CHAT_PARAMS = (
        "temperature",
        "max_tokens",
        "top_p",
        "seed",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
    )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self.api_key: str | None = api_key if api_key is not None else os.environ.get("FEATHERLESS_API_KEY")
        if not self.api_key:
            raise ValueError(_MISSING_KEY_HINT)
        self.api_key = str(self.api_key)

        resolved_model = model if model is not None else os.environ.get("FEATHERLESS_MODEL", "")
        resolved_model = str(resolved_model or "").strip()
        if not resolved_model:
            raise ValueError(_MISSING_MODEL_HINT)
        self.model = resolved_model
        self.base_url = base_url

        if client is not None:
            self._client = client
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "openai package is required for FeatherlessLLMProvider. "
                    "Install it (pip install openai) or inject a compatible client."
                ) from exc
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def generate(self, prompt: str, **kwargs: Any) -> LLMResponse:
        unknown = set(kwargs).difference(self._CHAT_PARAMS)
        if unknown:
            raise TypeError(f"unsupported generate() kwargs: {sorted(unknown)}")

        params: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        for name in self._CHAT_PARAMS:
            if name in kwargs:
                params[name] = kwargs[name]

        try:
            completion = self._client.chat.completions.create(**params)
        except Exception as exc:  # mapped to a redacted RuntimeError below
            raise RuntimeError(f"Featherless API error: {self._redact(str(exc))}") from exc

        text = ""
        if getattr(completion, "choices", None):
            message = completion.choices[0].message
            text = getattr(message, "content", None) or ""
        return LLMResponse(text=text, usage=self._usage_dict(getattr(completion, "usage", None)))

    def _usage_dict(self, usage: Any) -> dict[str, int]:
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):  # pydantic CompletionUsage (real SDK)
            return usage.model_dump()
        if isinstance(usage, dict):
            return dict(usage)
        return {k: v for k, v in vars(usage).items()}

    def _redact(self, message: str) -> str:
        if self.api_key:
            message = message.replace(self.api_key, _REDACTED)
        return message


_NVIDIA_MISSING_KEY_HINT = (
    "NVIDIA_API_KEY is not set. Add NVIDIA_API_KEY to your environment "
    "or .env (never hard-code or commit it)."
)


class NVIDIAProvider:
    """OpenAI-compatible provider for NVIDIA NIM (DeepSeek on NVIDIA's API).

    Speaks NVIDIA's OpenAI-compatible API via the `openai` SDK, so no
    NVIDIA-specific SDK or extra dependency is needed. Reads NVIDIA_API_KEY
    and NVIDIA_MODEL from the environment (dotenv loads the project `.env`,
    which never overrides real env vars); NVIDIA_MODEL defaults to
    `NVIDIAProvider.DEFAULT_MODEL`. An API key never appears in any raised
    exception.
    """

    DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
    DEFAULT_MODEL = "deepseek-ai/deepseek-v4-pro-0813"
    _CHAT_PARAMS = (
        "temperature",
        "max_tokens",
        "top_p",
        "seed",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
    )
    _REASONING_EFFORTS = frozenset({"none", "high", "max"})

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self.api_key: str | None = api_key if api_key is not None else os.environ.get("NVIDIA_API_KEY")
        if not self.api_key:
            raise ValueError(_NVIDIA_MISSING_KEY_HINT)
        self.api_key = str(self.api_key)

        if model is not None:
            self.model = str(model)
        else:
            env_model = os.environ.get("NVIDIA_MODEL", "").strip()
            self.model = env_model or self.DEFAULT_MODEL
        self.base_url = base_url

        if client is not None:
            self._client = client
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "openai package is required for NVIDIAProvider. "
                    "Install it (pip install openai) or inject a compatible client."
                ) from exc
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def generate(self, prompt: str, **kwargs: Any) -> LLMResponse:
        unknown = set(kwargs).difference(self._CHAT_PARAMS).difference({"reasoning_effort"})
        if unknown:
            raise TypeError(f"unsupported generate() kwargs: {sorted(unknown)}")

        if "reasoning_effort" in kwargs and kwargs["reasoning_effort"] not in self._REASONING_EFFORTS:
            raise ValueError(
                "unsupported reasoning_effort "
                f"{kwargs['reasoning_effort']!r}; expected one of "
                f"{sorted(self._REASONING_EFFORTS)}"
            )

        params: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        for name in (*self._CHAT_PARAMS, "reasoning_effort"):
            if name in kwargs:
                params[name] = kwargs[name]

        try:
            completion = self._client.chat.completions.create(**params)
        except Exception as exc:  # mapped to a redacted RuntimeError below
            raise RuntimeError(f"NVIDIA API error: {self._redact(str(exc))}") from exc

        text = ""
        if getattr(completion, "choices", None):
            message = completion.choices[0].message
            text = getattr(message, "content", None) or ""
        return LLMResponse(text=text, usage=self._usage_dict(getattr(completion, "usage", None)))

    def _usage_dict(self, usage: Any) -> dict[str, int]:
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):  # pydantic CompletionUsage (real SDK)
            return usage.model_dump()
        if isinstance(usage, dict):
            return dict(usage)
        return {k: v for k, v in vars(usage).items()}

    def _redact(self, message: str) -> str:
        if self.api_key:
            message = message.replace(self.api_key, _REDACTED)
        return message