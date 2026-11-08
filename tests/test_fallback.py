"""Провайдерський фолбек: порядок ланцюжка, перемикання при збої, форма запиту.

Без мережі: OpenAI-сумісний клієнт ганяється на httpx.MockTransport, логіка
ланцюжка — на крихітних стабах.
"""

from __future__ import annotations

import json

import httpx
import pytest

from adwatch.llm import (
    FallbackChat,
    GroqChat,
    LLMError,
    OpenAICompatChat,
    build_chat,
)


class _Stub:
    def __init__(self, name: str, *, fail: bool = False, text: str = "{}") -> None:
        self.name, self.fail, self.text, self.calls = name, fail, text, 0

    def complete(self, messages: list[dict], json_mode: bool) -> tuple[str, dict]:
        self.calls += 1
        if self.fail:
            raise LLMError(f"{self.name} down")
        return self.text, {"prompt_tokens": 1, "completion_tokens": 1}


def test_first_working_provider_short_circuits() -> None:
    a, b = _Stub("a", text='{"ok":1}'), _Stub("b")
    text, _ = FallbackChat([("a", a), ("b", b)]).complete([], True)
    assert text == '{"ok":1}' and (a.calls, b.calls) == (1, 0)


def test_falls_through_then_raises_naming_each() -> None:
    a, b = _Stub("groq:GROQ_API_KEY", fail=True), _Stub("gemini:GEMINI_API_KEY", fail=True)
    with pytest.raises(LLMError) as e:
        FallbackChat([("groq:GROQ_API_KEY", a), ("gemini:GEMINI_API_KEY", b)]).complete([], True)
    assert a.calls == b.calls == 1
    assert "groq:GROQ_API_KEY" in str(e.value) and "gemini:GEMINI_API_KEY" in str(e.value)


def test_build_chat_order_and_dedup() -> None:
    env = {"GROQ_API_KEY": "gsk_p", "GEMINI_API_KEY": "AIza_a", "OPENAI_API_KEY": "sk-a",
           "XAI_API_KEY": "xai-a", "GEMINI_API_KEY_2": "AQ.1", "GEMINI_API_KEY_3": "AQ.2",
           "GROQ_API_KEY_2": "gsk_2"}
    names = [n for n, _ in build_chat("groq", env=env).members]
    assert names == ["groq:GROQ_API_KEY", "gemini:GEMINI_API_KEY", "openai:OPENAI_API_KEY",
                     "xai:XAI_API_KEY", "gemini:GEMINI_API_KEY_2", "gemini:GEMINI_API_KEY_3",
                     "groq:GROQ_API_KEY_2"]


def test_build_chat_single_is_bare_groq_and_empty_is_none() -> None:
    assert isinstance(build_chat("groq", env={"GROQ_API_KEY": "x"}), GroqChat)
    assert build_chat("groq", env={}) is None


def test_openai_compat_chat_shape() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"v":2}'}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    c = OpenAICompatChat("xai", "xai-key", http=httpx.Client(transport=httpx.MockTransport(handler)))
    text, usage = c.complete([{"role": "user", "content": "hi"}], json_mode=True)
    assert text == '{"v":2}' and usage["prompt_tokens"] == 5
    assert seen["url"] == "https://api.x.ai/v1/chat/completions"
    assert seen["auth"] == "Bearer xai-key"
    assert seen["body"]["model"] == "grok-2-latest"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in seen["body"]  # Groq-only, must not leak to others


def test_openai_compat_chat_4xx_raises_llmerror() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    c = OpenAICompatChat("openai", "sk-bad", http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "x"}], json_mode=False)
