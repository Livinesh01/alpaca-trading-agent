"""Tests for the minimal LLM provider abstraction (src/agent/llm.py)."""
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent import FakeLLMProvider, LLMResponse, generate
from agent.llm import FeatherlessLLMProvider, LLMProvider, NVIDIAProvider


def test_generate_returns_provider_text():
    provider = FakeLLMProvider(responses={"hello": "world"})
    assert generate(provider, "hello").text == "world"


def test_generate_forwards_kwargs():
    provider = FakeLLMProvider(responses={"hi": "yo"})
    generate(provider, "hi", temperature=0.2, max_tokens=64)
    assert provider.requests[-1]["kwargs"] == {"temperature": 0.2, "max_tokens": 64}


def test_generate_records_every_prompt_in_order():
    provider = FakeLLMProvider(responses={"a": "1", "b": "2"})
    generate(provider, "a")
    generate(provider, "b")
    assert [r["prompt"] for r in provider.requests] == ["a", "b"]


def test_generate_includes_default_usage():
    provider = FakeLLMProvider(responses={"x": "y"})
    response = generate(provider, "x")
    assert isinstance(response, LLMResponse)
    assert response.usage == {}


def test_generate_supports_populated_usage():
    provider = FakeLLMProvider(responses={"x": "y"})
    response = LLMResponse(text=provider.responses["x"], usage={"tokens": 10})
    assert response.usage == {"tokens": 10}


def test_fake_kwargs_do_not_mutate_caller_dict():
    provider = FakeLLMProvider(responses={"hi": "yo"})
    kwargs = {"temperature": 0.5}
    generate(provider, "hi", **kwargs)
    assert kwargs == {"temperature": 0.5}


def test_fake_unknown_prompt_raises():
    provider = FakeLLMProvider(responses={"known": "x"})
    with pytest.raises(KeyError, match="no response for prompt"):
        generate(provider, "unknown")


def test_fake_add_response_registers_new_prompt():
    provider = FakeLLMProvider()
    provider.add_response("a", "1")
    assert generate(provider, "a").text == "1"


def test_fake_fail_next_raises_once_then_works():
    provider = FakeLLMProvider(responses={"a": "1"})
    provider.fail_next = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        generate(provider, "a")
    assert generate(provider, "a").text == "1"  # fail_next is one-shot


def test_instantiate_protocol_for_isinstance_style_checks():
    llm_providers = {"fake": FakeLLMProvider()}
    assert all(isinstance(p, LLMProvider) for p in llm_providers.values())
def _completion(text="ok", usage=None):
    """Shape of an OpenAI SDK chat completion, built without hitting the network."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=usage,
    )


def test_featherless_generates_successfully(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-test")
    monkeypatch.setenv("FEATHERLESS_MODEL", "featherless/local-1")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion("hello", usage={"total_tokens": 5})

    provider = FeatherlessLLMProvider(client=client)
    response = provider.generate("hi")

    assert response.text == "hello"
    assert response.usage == {"total_tokens": 5}
    call_kwargs = client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "featherless/local-1"
    assert call_kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_featherless_reads_key_and_model_from_env(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-env")
    monkeypatch.setenv("FEATHERLESS_MODEL", "env-model")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion()

    provider = FeatherlessLLMProvider(client=client)
    provider.generate("q", temperature=0.3, max_tokens=32)

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "env-model"
    assert kwargs["temperature"] == 0.3
    assert kwargs["max_tokens"] == 32


def test_featherless_explicit_config_beats_env(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-env")
    monkeypatch.setenv("FEATHERLESS_MODEL", "env-model")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion()

    provider = FeatherlessLLMProvider(
        api_key="sk-direct", model="direct-model", client=client
    )
    provider.generate("q")

    assert client.chat.completions.create.call_args.kwargs["model"] == "direct-model"


def test_featherless_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    monkeypatch.setenv("FEATHERLESS_MODEL", "m")
    with pytest.raises(ValueError, match="FEATHERLESS_API_KEY"):
        FeatherlessLLMProvider(client=mock.Mock())


def test_featherless_missing_or_blank_model_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-test")
    monkeypatch.delenv("FEATHERLESS_MODEL", raising=False)
    provider = FeatherlessLLMProvider(client=mock.Mock())
    assert provider.model == FeatherlessLLMProvider.DEFAULT_MODEL
    assert provider.model == "zai-org/GLM-5.3-Flash"

    monkeypatch.setenv("FEATHERLESS_MODEL", "   ")
    provider = FeatherlessLLMProvider(client=mock.Mock())
    assert provider.model == FeatherlessLLMProvider.DEFAULT_MODEL


def test_featherless_api_failure_raises(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-test")
    monkeypatch.setenv("FEATHERLESS_MODEL", "m")
    client = mock.Mock()
    client.chat.completions.create.side_effect = RuntimeError("upstream exploded")

    with pytest.raises(RuntimeError, match="Featherless API error"):
        FeatherlessLLMProvider(client=client).generate("q")


def test_featherless_redacts_api_key_from_failure(monkeypatch):
    secret = "sk-do-not-leak-12345"
    monkeypatch.setenv("FEATHERLESS_API_KEY", secret)
    monkeypatch.setenv("FEATHERLESS_MODEL", "m")
    client = mock.Mock()
    client.chat.completions.create.side_effect = RuntimeError(f"auth denied for key={secret}")

    with pytest.raises(RuntimeError) as excinfo:
        FeatherlessLLMProvider(client=client).generate("q")

    message = str(excinfo.value)
    assert secret not in message
    assert "[REDACTED]" in message


def test_featherless_usage_from_object_shape(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-test")
    monkeypatch.setenv("FEATHERLESS_MODEL", "m")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2)
    )

    response = FeatherlessLLMProvider(client=client).generate("q")
    assert response.usage == {"prompt_tokens": 1, "completion_tokens": 2}


def test_featherless_rejects_unsupported_kwargs(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-test")
    monkeypatch.setenv("FEATHERLESS_MODEL", "m")
    provider = FeatherlessLLMProvider(client=mock.Mock())
    with pytest.raises(TypeError, match="unsupported"):
        provider.generate("q", nonsense=True)


def test_featherless_conforms_to_protocol(monkeypatch):
    monkeypatch.setenv("FEATHERLESS_MODEL", "m")
    provider = FeatherlessLLMProvider(api_key="sk-test", client=mock.Mock())
    assert isinstance(provider, LLMProvider)


def test_nvidia_base_url_and_default_model_match_spec():
    assert NVIDIAProvider.DEFAULT_BASE_URL == "https://integrate.api.nvidia.com/v1"
    assert NVIDIAProvider.DEFAULT_MODEL == "deepseek-ai/deepseek-v4-pro-0813"


def test_nvidia_generates_successfully(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion("hello", usage={"total_tokens": 5})

    provider = NVIDIAProvider(client=client)
    response = provider.generate("hi")

    assert response.text == "hello"
    assert response.usage == {"total_tokens": 5}
    call_kwargs = client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == NVIDIAProvider.DEFAULT_MODEL
    assert call_kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert provider.base_url == NVIDIAProvider.DEFAULT_BASE_URL


def test_nvidia_reads_key_and_model_from_env(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-env")
    monkeypatch.setenv("NVIDIA_MODEL", "env-model")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion()

    provider = NVIDIAProvider(client=client)
    provider.generate("q", temperature=0.3, max_tokens=32)

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "env-model"
    assert kwargs["temperature"] == 0.3
    assert kwargs["max_tokens"] == 32


def test_nvidia_blank_env_model_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    monkeypatch.setenv("NVIDIA_MODEL", "   ")
    provider = NVIDIAProvider(client=mock.Mock())
    assert provider.model == NVIDIAProvider.DEFAULT_MODEL


def test_nvidia_explicit_model_override_beats_env(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-env")
    monkeypatch.setenv("NVIDIA_MODEL", "env-model")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion()

    provider = NVIDIAProvider(model="override-model", client=client)
    provider.generate("q")

    assert client.chat.completions.create.call_args.kwargs["model"] == "override-model"


def test_nvidia_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="NVIDIA_API_KEY"):
        NVIDIAProvider(client=mock.Mock())


def test_nvidia_api_failure_raises(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    client = mock.Mock()
    client.chat.completions.create.side_effect = RuntimeError("upstream exploded")

    with pytest.raises(RuntimeError, match="NVIDIA API error"):
        NVIDIAProvider(client=client).generate("q")


def test_nvidia_redacts_api_key_from_failure(monkeypatch):
    secret = "nv-secret-do-not-leak-999"
    monkeypatch.setenv("NVIDIA_API_KEY", secret)
    client = mock.Mock()
    client.chat.completions.create.side_effect = RuntimeError(f"auth denied for key={secret}")

    with pytest.raises(RuntimeError) as excinfo:
        NVIDIAProvider(client=client).generate("q")

    message = str(excinfo.value)
    assert secret not in message
    assert "[REDACTED]" in message


@pytest.mark.parametrize("effort", ["none", "high", "max"])
def test_nvidia_supports_reasoning_effort(monkeypatch, effort):
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion()

    NVIDIAProvider(client=client).generate("q", reasoning_effort=effort)
    assert client.chat.completions.create.call_args.kwargs["reasoning_effort"] == effort


def test_nvidia_rejects_unsupported_reasoning_effort(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    provider = NVIDIAProvider(client=mock.Mock())
    with pytest.raises(ValueError, match="reasoning_effort"):
        provider.generate("q", reasoning_effort="low")


def test_nvidia_usage_from_object_shape(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2)
    )

    response = NVIDIAProvider(client=client).generate("q")
    assert response.usage == {"prompt_tokens": 1, "completion_tokens": 2}


def test_nvidia_rejects_unsupported_kwargs(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    provider = NVIDIAProvider(client=mock.Mock())
    with pytest.raises(TypeError, match="unsupported"):
        provider.generate("q", nonsense=True)


def test_nvidia_conforms_to_protocol():
    provider = NVIDIAProvider(api_key="sk-test", client=mock.Mock())
    assert isinstance(provider, LLMProvider)


def test_nvidia_supports_extra_body(monkeypatch):
    """NVIDIA-specific extra_body (e.g., chat_template_kwargs) is forwarded."""
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion("ok")

    extra_body = {"chat_template_kwargs": {"thinking": False}}
    provider = NVIDIAProvider(client=client, extra_body=extra_body)
    provider.generate("q")

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"] == extra_body


def test_nvidia_supports_stream_parameter(monkeypatch):
    """stream parameter is forwarded to the API."""
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion("ok")

    provider = NVIDIAProvider(client=client)
    provider.generate("q", stream=True)

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["stream"] is True


def test_nvidia_default_parameters(monkeypatch):
    """NVIDIA-specific defaults (temperature, top_p, max_tokens) are applied."""
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion("ok")

    provider = NVIDIAProvider(client=client)
    provider.generate("q")

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["temperature"] == NVIDIAProvider.DEFAULT_TEMPERATURE
    assert kwargs["top_p"] == NVIDIAProvider.DEFAULT_TOP_P
    assert kwargs["max_tokens"] == NVIDIAProvider.DEFAULT_MAX_TOKENS
    assert kwargs["stream"] is False


def test_nvidia_seed_parameter(monkeypatch):
    """seed parameter is forwarded when set."""
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion("ok")

    provider = NVIDIAProvider(client=client, seed=42)
    provider.generate("q")

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["seed"] == 42


def test_nvidia_extra_body_via_generate_kwargs(monkeypatch):
    """extra_body can be passed via generate() kwargs."""
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-test")
    client = mock.Mock()
    client.chat.completions.create.return_value = _completion("ok")

    provider = NVIDIAProvider(client=client)
    extra_body = {"chat_template_kwargs": {"thinking": False}}
    provider.generate("q", extra_body=extra_body)

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"] == extra_body