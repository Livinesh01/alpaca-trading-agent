import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from agent.llm import FeatherlessLLMProvider, LLMProvider, NVIDIAProvider

SMOKE_TEST_PROMPT = "Respond with OK."


def _build_provider() -> LLMProvider:
    name = os.environ.get("LLM_PROVIDER", "featherless").strip().lower()
    if name == "featherless":
        return FeatherlessLLMProvider()
    if name == "nvidia":
        return NVIDIAProvider()
    raise ValueError(f"Unsupported LLM_PROVIDER {name!r}; expected 'featherless' or 'nvidia'.")


def main() -> int:
    try:
        response = _build_provider().generate(SMOKE_TEST_PROMPT)
    except Exception as exc:  # noqa: BLE001 — provider errors never contain credentials
        print(f"LLM smoke test failed: {exc}", file=sys.stderr)
        return 1
    print(response.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())