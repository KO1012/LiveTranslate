from __future__ import annotations

import logging

import numpy as np

from providers.openai_compatible import make_openai_client, readable_provider_error

from .audio_utils import decode_audio_bytes

log = logging.getLogger("LiveTranslate.TTS.OpenAI")


class OpenAISpeechProvider:
    """Text-to-speech through an OpenAI-compatible /audio/speech endpoint.

    Works with OpenAI, and local servers that implement the same API
    (e.g. an OpenAI-compatible TTS gateway).
    """

    name = "openai-speech"

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "tts-1",
        voice: str = "alloy",
        response_format: str = "mp3",
        speed: float = 1.0,
        proxy: str = "none",
        timeout: float = 30.0,
    ):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "tts-1"
        self.voice = voice or "alloy"
        self.response_format = response_format or "mp3"
        self.speed = float(speed) if speed else 1.0
        self.proxy = proxy
        self.timeout = timeout
        self._client = make_openai_client(
            self.base_url, self.api_key, proxy=proxy, timeout=timeout
        )

    def synthesize(self, text: str, language: str) -> tuple[np.ndarray, int] | None:
        text = (text or "").strip()
        if not text:
            return None
        if self._client is None:
            self._client = make_openai_client(
                self.base_url, self.api_key, proxy=self.proxy, timeout=self.timeout
            )
        try:
            response = self._client.audio.speech.create(
                model=self.model,
                voice=self.voice,
                input=text,
                response_format=self.response_format,
                speed=self.speed,
            )
            data = response.read()
        except Exception as exc:
            raise RuntimeError(
                f"TTS API request failed: {readable_provider_error(exc)}"
            ) from exc
        return decode_audio_bytes(data)

    def close(self) -> None:
        self._client = None
