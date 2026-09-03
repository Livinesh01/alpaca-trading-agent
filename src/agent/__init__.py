"""Agent LLM abstraction: provider interface, generate(), and a fake for tests.

Re-exports the core API from `agent.llm`; consumers use
`from agent import LLMProvider, generate`.
"""
from agent.llm import FakeLLMProvider, LLMProvider, LLMResponse, generate

__all__ = ["FakeLLMProvider", "LLMProvider", "LLMResponse", "generate"]