from __future__ import annotations

import json
import logging
import time
from typing import Callable, Iterable, Optional

import httpx

from .base import LLMProvider, TranslateInput, TranslateResult

log = logging.getLogger("LiveTranslate.Provider.Ollama")


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434/api/chat",
        model: str = "qwen2.5:7b",
        temperature: float = 0.3,
        streaming: bool = True,
        timeout: int = 60,
        extra_body: dict | None = None,
        **_ignored,
    ):
        self._base_url = base_url or "http://localhost:11434/api/chat"
        self._model = model
        self._temperature = temperature
        self._streaming = streaming
        self._timeout = timeout
        self._extra_body = dict(extra_body) if extra_body else {}
        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0))
        log.info("OllamaProvider initialized: base=%s model=%s", self._base_url, model)

    @property
    def model(self) -> str:
        return self._model

    @property
    def last_usage(self) -> tuple[int, int]:
        return self._last_prompt_tokens, self._last_completion_tokens

    def set_timeout(self, timeout: int):
        self._timeout = timeout
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0))

    def _messages(self, input_data: TranslateInput):
        messages = []
        if input_data.system_prompt:
            messages.append({"role": "system", "content": input_data.system_prompt})
        for src, tgt in input_data.context:
            messages.append({"role": "user", "content": src})
            messages.append({"role": "assistant", "content": tgt})
        messages.append({"role": "user", "content": input_data.text})
        return messages

    def _payload(self, input_data: TranslateInput, stream: bool):
        payload = {
            "model": self._model,
            "messages": self._messages(input_data),
            "stream": stream,
            "options": {
                "temperature": self._temperature,
            },
        }
        payload.update(self._extra_body)
        return payload

    def translate(self, input_data: TranslateInput) -> TranslateResult:
        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        try:
            resp = self._client.post(self._base_url, json=self._payload(input_data, False))
            resp.raise_for_status()
        except httpx.ConnectError as e:
            raise ConnectionError("Ollama 未启动或无法连接") from e
        except httpx.TimeoutException as e:
            raise TimeoutError("Ollama 响应超时") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Ollama 请求失败：HTTP {e.response.status_code}") from e

        data = resp.json()
        message = data.get("message") or {}
        result = (message.get("content") or data.get("response") or "").strip()
        self._last_prompt_tokens = int(data.get("prompt_eval_count") or 0)
        self._last_completion_tokens = int(data.get("eval_count") or 0)
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
        deadline = time.monotonic() + self._timeout
        chunks = []
        try:
            with self._client.stream(
                "POST", self._base_url, json=self._payload(input_data, True)
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if time.monotonic() > deadline:
                        raise TimeoutError("Ollama 响应超时")
                    if not line:
                        continue
                    data = json.loads(line)
                    message = data.get("message") or {}
                    delta = message.get("content") or data.get("response") or ""
                    if delta:
                        chunks.append(delta)
                        current = "".join(chunks)
                        if on_delta:
                            on_delta(current)
                        yield current
                    if data.get("done"):
                        self._last_prompt_tokens = int(data.get("prompt_eval_count") or 0)
                        self._last_completion_tokens = int(data.get("eval_count") or 0)
                        break
        except httpx.ConnectError as e:
            raise ConnectionError("Ollama 未启动或无法连接") from e
        except httpx.TimeoutException as e:
            raise TimeoutError("Ollama 响应超时") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Ollama 请求失败：HTTP {e.response.status_code}") from e
