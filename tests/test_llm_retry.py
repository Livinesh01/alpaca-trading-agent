"""Reliability tests for the bounded-retry LLM helper (`generate_with_retry`).

These tests lock in the Step 3 — LLM Reliability guarantees without touching real
network: transient failures retry with backoff, persistent failures fail fast,
exhaustion raises LLMExhaustedError, and the helper still fails closed.
"""
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent.llm import LLMResponse, generate_with_retry
from errors import LLMExhaustedError, LLMInvalidResponse, LLMProviderError


class CountingProvider:
    """LLMProvider stub: raises on the first N calls, then returns text."""

    def __init__(self, *, fail_times=None, fail_exc=None, success_text="ok", usage=None):
        self.fail_times = list(fail_times or [])
        self.fail_exc = fail_exc
        self.success_text = success_text
        self._usage = usage or {}
        self.calls = []

    def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        if self.fail_times and self.fail_times.pop(0):
            raise self.fail_exc
        return LLMResponse(text=self.success_text, usage=dict(self._usage))


def test_retry_succeeds_after_transient_failure():
    prov = CountingProvider(fail_times=[True, False], fail_exc=TimeoutError("transient"))
    resp = generate_with_retry(prov, "hi", max_retries=3, base_delay=0.0, max_delay=0.01)
    assert resp.text == "ok"
    assert len(prov.calls) == 2  # first failed, second succeeded


def test_retry_exhaustion_raises_llm_exhausted_error():
    prov = CountingProvider(fail_times=[True] * 10, fail_exc=TimeoutError("boom"))
    with pytest.raises(LLMExhaustedError):
        generate_with_retry(prov, "hi", max_retries=2, base_delay=0.0, max_delay=0.01)
    # 1 initial attempt + 2 retries == 3 total
    assert len(prov.calls) == 3


def test_non_transient_failure_does_not_retry():
    # A plain RuntimeError with no transient signal classifies as non-transient.
    prov = CountingProvider(fail_times=[True], fail_exc=RuntimeError("bad request"))
    with pytest.raises(LLMProviderError):
        generate_with_retry(prov, "hi", max_retries=5, base_delay=0.0, max_delay=0.01)
    assert len(prov.calls) == 1


def test_max_retries_zero_means_one_attempt():
    prov = CountingProvider(fail_times=[True], fail_exc=TimeoutError("boom"))
    with pytest.raises(LLMExhaustedError):
        generate_with_retry(prov, "hi", max_retries=0, base_delay=0.0, max_delay=0.01)
    assert len(prov.calls) == 1


def test_invalid_response_type_fails_without_retry():
    class BadProvider:
        def generate(self, prompt, **kwargs):
            return SimpleNamespace(text="not an LLMResponse")

    with pytest.raises(LLMInvalidResponse):
        generate_with_retry(BadProvider(), "hi", max_retries=3, base_delay=0.0, max_delay=0.01)


def test_observability_emits_retry_and_success_events():
    observer = mock.Mock()
    observer.emit = mock.Mock()
    prov = CountingProvider(fail_times=[True, False], fail_exc=TimeoutError("transient"))
    resp = generate_with_retry(
        prov, "hi", max_retries=3, base_delay=0.0, max_delay=0.01, observability=observer
    )
    assert resp.text == "ok"
    event_types = [
        c.args[0] if c.args else c.kwargs.get("event")
        for c in observer.emit.call_args_list
    ]
    assert "llm_retry" in event_types
    assert "llm_success" in event_types


def test_observability_emits_failure_event_on_exhaustion():
    observer = mock.Mock()
    observer.emit = mock.Mock()
    prov = CountingProvider(fail_times=[True] * 5, fail_exc=TimeoutError("boom"))
    with pytest.raises(LLMExhaustedError):
        generate_with_retry(
            prov, "hi", max_retries=1, base_delay=0.0, max_delay=0.01, observability=observer
        )
    event_types = [c.args[0] if c.args else c.kwargs.get("event") for c in observer.emit.call_args_list]
    assert "llm_failure" in event_types


def test_non_transient_error_does_not_leak_secret_from_prompt():
    # generate_with_retry must never echo the prompt or kwargs into the wrapped
    # LLMProviderError (those can carry market data / sensitive context).
    class PromptProvider:
        def generate(self, prompt, **kwargs):
            raise RuntimeError("upstream rejected the request")

    with pytest.raises(LLMProviderError) as excinfo:
        generate_with_retry(PromptProvider(), "SECRET_MARKET_CONTEXT", max_retries=0, base_delay=0.0)
    message = str(excinfo.value)
    assert "SECRET_MARKET_CONTEXT" not in message


def test_run_id_is_propagated_to_observability():
    observer = mock.Mock()
    observer.emit = mock.Mock()
    prov = CountingProvider()
    generate_with_retry(
        prov, "hi", max_retries=0, base_delay=0.0, observability=observer, run_id="run-xyz"
    )
    assert all(
        c.kwargs.get("run_id") == "run-xyz" for c in observer.emit.call_args_list
    )

