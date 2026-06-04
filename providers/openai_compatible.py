from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Iterable, Optional

import httpx
from openai import OpenAI

from .base import LLMProvider, TranslateInput, TranslateResult
from logging_utils import mask_secret

log = logging.getLogger("LiveTranslate.Provider.OpenAI")

_REASONING_TRANSLATION_PATTERNS = (
    re.compile(
        r"(?:final\s+translation|translation|translated\s+text|最终译文|译文|翻译)\s*[:：]\s*[\"“](.+?)[\"”]",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:final\s+translation|translation|translated\s+text|最终译文|译文|翻译)\s*[:：]\s*(.+?)(?:\n{2,}|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"^\s*<think>.*?(?:\n\s*\n|\Z)", re.IGNORECASE | re.DOTALL)

_OVERRIDE_KEYS = (
    "temperature",
    "top_p",
    "max_tokens",
    "frequency_penalty",
    "presence_penalty",
    "seed",
)


def make_openai_client(
    api_base: str, api_key: str, proxy: str = "none", timeout=None
) -> OpenAI:
    # Local/compatible servers (LM Studio, Ollama, vLLM, etc.) often need no key.
    # Newer openai>=1.x raises OpenAIError on an empty api_key, so fall back to a
    # placeholder; servers that don't require auth ignore the value.
    kwargs = {"base_url": api_base, "api_key": api_key or "not-needed"}
    if timeout is not None:
        kwargs["timeout"] = httpx.Timeout(timeout, connect=5.0)
    if proxy == "system":
        pass
    elif proxy in ("none", "", None):
        kwargs["http_client"] = httpx.Client(trust_env=False)
    else:
        kwargs["http_client"] = httpx.Client(proxy=proxy)
    return OpenAI(**kwargs)


def is_enable_thinking_unsupported_error(exc: Exception) -> bool:
    """Detect a 400 rejecting the ``enable_thinking`` extra_body parameter.

    Only Qwen-style endpoints understand ``enable_thinking``; OpenAI, Cerebras,
    Groq and most others reject the whole request with HTTP 400 when it is
    present (e.g. Cerebras: "enable_thinking: property 'enable_thinking' is
    unsupported", code "wrong_api_format"). We use this to auto-strip the param
    and retry once, then remember not to send it again for that provider.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    if status not in (400, 422):
        return False
    return "enable_thinking" in str(exc).lower()


def readable_provider_error(exc: Exception) -> Exception:
    name = exc.__class__.__name__.lower()
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    msg = str(exc)

    if "timeout" in name:
        return TimeoutError("翻译请求超时，请检查网络或调大超时时间")
    if "connection" in name or "connect" in name:
        return ConnectionError("无法连接到 API Base URL，请检查地址、网络或代理设置")
    if status in (401, 403) or "authentication" in name or "permission" in name:
        return PermissionError("API Key 错误或没有访问权限")
    if status == 404 or "notfound" in name:
        return RuntimeError("模型不存在或 API 路径不正确")
    if status == 429 or "ratelimit" in name:
        return RuntimeError("请求过于频繁或额度不足，请稍后重试")
    if status and status >= 500:
        return RuntimeError(f"API 服务暂时不可用：HTTP {status}")
    if "json" in name:
        return RuntimeError("API 返回内容不是有效 JSON")
    return RuntimeError(f"翻译请求失败：{msg}")

def extract_translation_from_reasoning(reasoning: str | None) -> str:
    """Recover a final translation from reasoning-only compatible API output."""
    text = (reasoning or "").strip()
    if not text:
        return ""
    for pattern in _REASONING_TRANSLATION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        result = match.group(1).strip()
        result = result.strip("\"'“”‘’` \t\r\n")
        if result:
            return result
    return ""


def strip_thinking_blocks(text: str | None) -> str:
    """Remove model-visible thinking blocks from compatible API content."""
    result = (text or "").strip()
    if not result:
        return ""
    result = _THINK_BLOCK_RE.sub("", result).strip()
    result = _THINK_OPEN_RE.sub("", result).strip()
    return result


class OpenAICompatibleProvider(LLMProvider):
    name = "openai-compatible"

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        max_tokens: int = 256,
        temperature: float = 0.3,
        streaming: bool = True,
        proxy: str = "none",
        no_system_role: bool = False,
        no_think: bool = False,
        json_response: bool = False,
        timeout: int = 10,
        overrides: dict | None = None,
        extra_body: dict | None = None,
    ):
        self._client = make_openai_client(api_base, api_key, proxy, timeout=timeout)
        self._api_base = api_base
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._streaming = streaming
        self._proxy = proxy
        self._no_system_role = no_system_role
        self._no_think = no_think
        # Some providers (OpenAI, Cerebras, Groq, ...) reject the Qwen-specific
        # extra_body `enable_thinking` param with HTTP 400. We optimistically
        # send it when no_think is set, but flip this flag and stop sending it
        # after the first such rejection (then auto-retry without it).
        self._enable_thinking_supported = True
        self._json_response = json_response
        self._timeout = timeout
        self._overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        self._extra_body = dict(extra_body) if extra_body else {}
        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        log.info(
            "OpenAICompatibleProvider initialized: base=%s model=%s key=%s",
            api_base,
            model,
            mask_secret(api_key),
        )

    @property
    def last_usage(self) -> tuple[int, int]:
        return self._last_prompt_tokens, self._last_completion_tokens

    @property
    def model(self) -> str:
        return self._model

    def set_timeout(self, timeout: int):
        self._timeout = timeout
        self._client = self._client.copy(timeout=timeout)

    def _build_messages(self, input_data: TranslateInput):
        system_prompt = input_data.system_prompt or ""
        if self._no_system_role:
            return [{"role": "user", "content": f"{system_prompt}\n{input_data.text}"}]

        messages = [{"role": "system", "content": system_prompt}]
        for src, tgt in input_data.context:
            messages.append({"role": "user", "content": src})
            messages.append({"role": "assistant", "content": tgt})
        messages.append({"role": "user", "content": input_data.text})
        return messages

    def _build_request_kwargs(
        self,
        input_data: TranslateInput,
        stream=False,
        max_tokens_override: int | None = None,
        timeout_override: int | None = None,
    ):
        kwargs = dict(
            model=self._model,
            messages=self._build_messages(input_data),
            max_tokens=max_tokens_override or self._max_tokens,
            temperature=self._temperature,
        )
        for k in _OVERRIDE_KEYS:
            if k in self._overrides:
                kwargs[k] = self._overrides[k]
        extra_body = {}
        if self._no_think and self._enable_thinking_supported:
            extra_body["enable_thinking"] = False
        if self._extra_body:
            extra_body.update(self._extra_body)
        if extra_body:
            kwargs["extra_body"] = extra_body
        if self._json_response:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "translation",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"t": {"type": "string"}},
                        "required": ["t"],
                        "additionalProperties": False,
                    },
                },
            }
        if stream:
            kwargs["stream"] = True
        if timeout_override is not None:
            kwargs["timeout"] = timeout_override
        return kwargs

    def _extract_json_translation(self, raw: str) -> str:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "t" in data:
                return data["t"]
        except (json.JSONDecodeError, TypeError):
            pass
        return raw

    def translate(
        self,
        input_data: TranslateInput,
        max_tokens_override: int | None = None,
        timeout_override: int | None = None,
    ) -> TranslateResult:
        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        try:
            resp = self._client.chat.completions.create(
                **self._build_request_kwargs(
                    input_data,
                    stream=False,
                    max_tokens_override=max_tokens_override,
                    timeout_override=timeout_override,
                )
            )
        except Exception as e:
            # Provider rejected the Qwen-only `enable_thinking` param: drop it
            # for good and retry once so translation isn't lost.
            if self._no_think and self._enable_thinking_supported and (
                is_enable_thinking_unsupported_error(e)
            ):
                self._enable_thinking_supported = False
                log.info(
                    "Provider rejected enable_thinking; disabling it and retrying "
                    "(base=%s model=%s)",
                    self._api_base,
                    self._model,
                )
                try:
                    resp = self._client.chat.completions.create(
                        **self._build_request_kwargs(
                            input_data,
                            stream=False,
                            max_tokens_override=max_tokens_override,
                            timeout_override=timeout_override,
                        )
                    )
                except Exception as e2:
                    raise readable_provider_error(e2) from e2
            else:
                raise readable_provider_error(e) from e
        if resp.usage:
            self._last_prompt_tokens = resp.usage.prompt_tokens or 0
            self._last_completion_tokens = resp.usage.completion_tokens or 0
        content = ""
        reasoning = ""
        finish_reason = None
        if getattr(resp, "choices", None):
            choice = resp.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None) or ""
            reasoning = (
                getattr(message, "reasoning_content", None)
                or getattr(message, "reasoning", None)
                or ""
            )
        result = strip_thinking_blocks(content)
        if not result:
            result = extract_translation_from_reasoning(reasoning)
            if result:
                log.warning(
                    "API returned empty message.content; recovered translation "
                    "from reasoning_content (base=%s model=%s)",
                    self._api_base,
                    self._model,
                )
        if (
            not result
            and reasoning
            and finish_reason == "length"
            and max_tokens_override is None
        ):
            boosted_max_tokens = max(self._max_tokens * 2, 512)
            log.warning(
                "API used all completion tokens for reasoning; retrying with "
                "max_tokens=%s (base=%s model=%s)",
                boosted_max_tokens,
                self._api_base,
                self._model,
            )
            return self.translate(
                input_data,
                max_tokens_override=boosted_max_tokens,
                timeout_override=timeout_override or max(self._timeout * 4, 20),
            )
        if self._json_response:
            result = self._extract_json_translation(result)
        if not result:
            raise RuntimeError("API returned an empty translation")
        return TranslateResult(
            original=input_data.text,
            translation=result,
            model=self._model,
            input_tokens=self._last_prompt_tokens,
            output_tokens=self._last_completion_tokens,
        )

    def stream_translate(
        self,
        input_data: TranslateInput,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> Iterable[str]:
        if not self._streaming:
            result = self.translate(input_data).translation
            if on_delta:
                on_delta(result)
            yield result
            return

        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0

        def _open_stream():
            base_kwargs = self._build_request_kwargs(input_data, stream=True)
            try:
                return self._client.chat.completions.create(
                    **base_kwargs,
                    stream_options={"include_usage": True},
                )
            except TypeError:
                return self._client.chat.completions.create(**base_kwargs)

        try:
            try:
                stream = _open_stream()
            except Exception as e:
                # Provider rejected the Qwen-only `enable_thinking` param: drop
                # it for good and retry once so translation isn't lost.
                if self._no_think and self._enable_thinking_supported and (
                    is_enable_thinking_unsupported_error(e)
                ):
                    self._enable_thinking_supported = False
                    log.info(
                        "Provider rejected enable_thinking; disabling it and "
                        "retrying (base=%s model=%s)",
                        self._api_base,
                        self._model,
                    )
                    stream = _open_stream()
                else:
                    raise
        except Exception as e:
            raise readable_provider_error(e) from e

        deadline = time.monotonic() + self._timeout
        chunks = []
        reasoning_chunks = []

        def _recover_stream_reasoning(reason: str) -> str:
            reasoning = "".join(reasoning_chunks)
            result = extract_translation_from_reasoning(reasoning)
            if result:
                log.warning(
                    "Streaming translation recovered from reasoning_content "
                    "(%s, base=%s model=%s)",
                    reason,
                    self._api_base,
                    self._model,
                )
                if on_delta:
                    on_delta(result)
            return result

        def _fallback_non_streaming(reason: str, disable_streaming: bool = False):
            if disable_streaming:
                self._streaming = False
            log.warning(
                "Streaming translation produced no usable content; retrying "
                "without streaming (%s, base=%s model=%s)",
                reason,
                self._api_base,
                self._model,
            )
            result = self.translate(
                input_data,
                max_tokens_override=max(self._max_tokens * 2, 512),
                timeout_override=max(self._timeout * 4, 20),
            ).translation
            if on_delta:
                on_delta(result)
            return result

        try:
            for chunk in stream:
                if time.monotonic() > deadline:
                    stream.close()
                    raise TimeoutError(
                        f"Translation exceeded {self._timeout}s total timeout"
                    )
                if hasattr(chunk, "usage") and chunk.usage:
                    self._last_prompt_tokens = chunk.usage.prompt_tokens or 0
                    self._last_completion_tokens = chunk.usage.completion_tokens or 0
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    reasoning = (
                        getattr(delta, "reasoning_content", None)
                        or getattr(delta, "reasoning", None)
                    )
                    if reasoning:
                        reasoning_chunks.append(reasoning)
                    if content:
                        chunks.append(content)
                        current = strip_thinking_blocks("".join(chunks))
                        if not self._json_response:
                            if current and on_delta:
                                on_delta(current)
                            if current:
                                yield current
        except Exception as e:
            if not chunks:
                recovered = _recover_stream_reasoning(e.__class__.__name__)
                if recovered:
                    yield recovered
                    return
                yield _fallback_non_streaming(
                    e.__class__.__name__,
                    disable_streaming=bool(reasoning_chunks),
                )
                return
            raise readable_provider_error(e) from e
        result = strip_thinking_blocks("".join(chunks))
        if not result:
            recovered = _recover_stream_reasoning("empty stream")
            if recovered:
                yield recovered
                return
            yield _fallback_non_streaming(
                "reasoning-only stream" if reasoning_chunks else "empty stream",
                disable_streaming=bool(reasoning_chunks),
            )
            return
        if self._json_response:
            result = self._extract_json_translation(result)
            if on_delta:
                on_delta(result)
            yield result
