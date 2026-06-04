"""Tests for auto-stripping the Qwen-only `enable_thinking` extra_body param.

Providers like Cerebras/OpenAI reject `enable_thinking` with HTTP 400. The
provider should optimistically send it (when no_think is set), but on the first
such rejection drop it, retry once, and never send it again for that instance.

No real network: a fake OpenAI-style client records the kwargs of each call and
raises a 400 only while `enable_thinking` is present.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from providers.base import TranslateInput  # noqa: E402
from providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
    extract_translation_from_reasoning,
    is_enable_thinking_unsupported_error,
    strip_thinking_blocks,
)


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _UnsupportedParamError(Exception):
    """Mimics openai.BadRequestError shape (has .response.status_code)."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.response = _FakeResponse(status_code)
        self.status_code = status_code


class _Msg:
    def __init__(self, content, reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content


class _Choice:
    def __init__(self, content, reasoning_content=None, finish_reason=None):
        self.message = _Msg(content, reasoning_content)
        self.finish_reason = finish_reason


class _Usage:
    prompt_tokens = 5
    completion_tokens = 3


class _Resp:
    def __init__(self, content, reasoning_content=None, finish_reason=None):
        self.choices = [_Choice(content, reasoning_content, finish_reason)]
        self.usage = _Usage()


class _Delta:
    def __init__(self, content=None, reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content


class _StreamChoice:
    def __init__(self, content=None, reasoning_content=None):
        self.delta = _Delta(content, reasoning_content)


class _StreamChunk:
    def __init__(self, content=None, usage=None, reasoning_content=None):
        self.choices = (
            [_StreamChoice(content, reasoning_content)]
            if content is not None or reasoning_content is not None
            else []
        )
        self.usage = usage


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        extra = kwargs.get("extra_body") or {}
        if "enable_thinking" in extra:
            raise _UnsupportedParamError(
                "enable_thinking: property 'enable_thinking' is unsupported"
            )
        if kwargs.get("stream"):
            return iter(
                [
                    _StreamChunk(content="hello-"),
                    _StreamChunk(content="translation"),
                    _StreamChunk(usage=_Usage()),
                ]
            )
        return _Resp("hello-translation")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()


class _EmptyStreamCompletions:
    def __init__(self, fallback_content):
        self.calls = []
        self._fallback_content = fallback_content

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter([_StreamChunk(usage=_Usage())])
        return _Resp(self._fallback_content)


class _EmptyStreamChat:
    def __init__(self, fallback_content):
        self.completions = _EmptyStreamCompletions(fallback_content)


class _EmptyStreamClient:
    def __init__(self, fallback_content):
        self.chat = _EmptyStreamChat(fallback_content)


class _ReasoningOnlyCompletions:
    def __init__(self, reasoning, finish_reason=None):
        self.calls = []
        self._reasoning = reasoning
        self._finish_reason = finish_reason

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter([_StreamChunk(reasoning_content=self._reasoning)])
        return _Resp(
            "",
            reasoning_content=self._reasoning,
            finish_reason=self._finish_reason,
        )


class _ReasoningOnlyChat:
    def __init__(self, reasoning, finish_reason=None):
        self.completions = _ReasoningOnlyCompletions(reasoning, finish_reason)


class _ReasoningOnlyClient:
    def __init__(self, reasoning, finish_reason=None):
        self.chat = _ReasoningOnlyChat(reasoning, finish_reason)


class _ReasoningOnlyStreamCompletions:
    def __init__(self, reasoning, fallback_content):
        self.calls = []
        self._reasoning = reasoning
        self._fallback_content = fallback_content

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter([_StreamChunk(reasoning_content=self._reasoning)])
        return _Resp(self._fallback_content)


class _ReasoningOnlyStreamChat:
    def __init__(self, reasoning, fallback_content):
        self.completions = _ReasoningOnlyStreamCompletions(
            reasoning, fallback_content
        )


class _ReasoningOnlyStreamClient:
    def __init__(self, reasoning, fallback_content):
        self.chat = _ReasoningOnlyStreamChat(reasoning, fallback_content)


def _make_provider(no_think=True):
    p = OpenAICompatibleProvider(
        api_base="https://api.cerebras.ai/v1",
        api_key="x",
        model="gpt-oss-120b",
        no_think=no_think,
        streaming=False,
    )
    p._client = _FakeClient()
    return p


def test_detects_enable_thinking_error():
    err = _UnsupportedParamError(
        "enable_thinking: property 'enable_thinking' is unsupported"
    )
    assert is_enable_thinking_unsupported_error(err) is True


def test_unrelated_400_is_not_enable_thinking():
    err = _UnsupportedParamError("some other validation error")
    assert is_enable_thinking_unsupported_error(err) is False


def test_translate_retries_without_enable_thinking():
    p = _make_provider(no_think=True)
    result = p.translate(TranslateInput(text="hi"))
    assert result.translation == "hello-translation"
    calls = p._client.chat.completions.calls
    # First call carried enable_thinking (rejected), retry dropped it.
    assert len(calls) == 2
    assert calls[0].get("extra_body", {}).get("enable_thinking") is False
    assert "enable_thinking" not in (calls[1].get("extra_body") or {})
    # Flag is now sticky: subsequent calls never send it again.
    p.translate(TranslateInput(text="again"))
    assert len(p._client.chat.completions.calls) == 3
    assert "enable_thinking" not in (
        p._client.chat.completions.calls[2].get("extra_body") or {}
    )


def test_stream_translate_falls_back_when_stream_has_no_content():
    p = _make_provider(no_think=False)
    p._streaming = True
    p._client = _EmptyStreamClient("fallback translation")
    out = list(p.stream_translate(TranslateInput(text="hi")))
    assert out == ["fallback translation"]
    calls = p._client.chat.completions.calls
    assert len(calls) == 2
    assert calls[0].get("stream") is True
    assert "stream" not in calls[1]


def test_reasoning_translation_marker_is_extracted():
    reasoning = 'Translation: "英伟达的宝藏就是 CUDA 库。"'
    assert extract_translation_from_reasoning(reasoning) == "英伟达的宝藏就是 CUDA 库。"


def test_content_thinking_block_is_stripped():
    assert strip_thinking_blocks("<think>hidden</think>\n\n你好") == "你好"


def test_translate_strips_content_thinking_block():
    p = _make_provider(no_think=False)
    p._client = _EmptyStreamClient("<think>hidden</think>\n\n你好")
    result = p.translate(TranslateInput(text="hi"))
    assert result.translation == "你好"


def test_translate_recovers_reasoning_only_translation():
    p = _make_provider(no_think=False)
    p._client = _ReasoningOnlyClient('Translation: "你好，世界。"')
    result = p.translate(TranslateInput(text="hi"))
    assert result.translation == "你好，世界。"


def test_stream_translate_recovers_reasoning_only_translation():
    p = _make_provider(no_think=False)
    p._streaming = True
    p._client = _ReasoningOnlyClient('Translation: "你好，世界。"')
    out = list(p.stream_translate(TranslateInput(text="hi")))
    assert out == ["你好，世界。"]
    assert len(p._client.chat.completions.calls) == 1


def test_stream_translate_disables_reasoning_only_empty_stream():
    p = _make_provider(no_think=False)
    p._streaming = True
    p._client = _ReasoningOnlyStreamClient(
        "We need to translate this subtitle.", "fallback translation"
    )

    out = list(p.stream_translate(TranslateInput(text="hi")))

    assert out == ["fallback translation"]
    assert p._streaming is False
    assert len(p._client.chat.completions.calls) == 2
    assert p._client.chat.completions.calls[0].get("stream") is True
    assert "stream" not in p._client.chat.completions.calls[1]

    out = list(p.stream_translate(TranslateInput(text="again")))

    assert out == ["fallback translation"]
    assert len(p._client.chat.completions.calls) == 3
    assert "stream" not in p._client.chat.completions.calls[2]


def test_translate_empty_response_raises():
    p = _make_provider(no_think=False)
    p._client = _EmptyStreamClient("")
    with pytest.raises(RuntimeError, match="empty translation"):
        p.translate(TranslateInput(text="hi"))


def test_no_think_false_never_sends_enable_thinking():
    p = _make_provider(no_think=False)
    result = p.translate(TranslateInput(text="hi"))
    assert result.translation == "hello-translation"
    calls = p._client.chat.completions.calls
    assert len(calls) == 1
    assert "enable_thinking" not in (calls[0].get("extra_body") or {})


def test_stream_translate_retries_without_enable_thinking():
    p = _make_provider(no_think=True)
    p._streaming = True
    out = list(p.stream_translate(TranslateInput(text="hi")))
    assert out and out[-1] == "hello-translation"
    calls = p._client.chat.completions.calls
    assert len(calls) == 2
    assert calls[0].get("extra_body", {}).get("enable_thinking") is False
    assert "enable_thinking" not in (calls[1].get("extra_body") or {})


def test_translate_recovers_reasoning_marker_before_length_retry():
    p = _make_provider(no_think=False)
    p._client = _ReasoningOnlyClient(
        'Translation: "hello world"',
        finish_reason="length",
    )

    result = p.translate(TranslateInput(text="hi"))

    assert result.translation == "hello world"
    assert len(p._client.chat.completions.calls) == 1
