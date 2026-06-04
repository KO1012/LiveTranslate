from __future__ import annotations

import io
import logging
import wave

import numpy as np
from openai import OpenAI

from providers.openai_compatible import readable_provider_error
from translator import LANGUAGE_DISPLAY

log = logging.getLogger("LiveTranslate.OpenAIAudioASR")

LANGUAGE_NAMES = {**LANGUAGE_DISPLAY, "auto": "auto"}


class OpenAIAudioASRProvider:
    """Speech-to-text through an OpenAI-compatible audio transcription API."""

    name = "openai-audio"

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "whisper-1",
        timeout: float = 30.0,
        language: str = "auto",
    ):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or "EMPTY"
        self.model = model or "whisper-1"
        self.timeout = timeout
        self.language = language if language != "auto" else None
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
        )

    def set_language(self, language: str):
        old = self.language
        self.language = language if language != "auto" else None
        log.info("OpenAI Audio ASR language: %s -> %s", old, self.language)

    def to_device(self, device: str):
        return True

    def unload(self):
        self._client = None

    def transcribe(self, audio: np.ndarray, word_timestamps: bool = False) -> dict | None:
        """Transcribe a 16 kHz mono float32 segment through the audio API."""
        if self._client is None:
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
            )

        wav_file = self._to_wav_file(audio)
        kwargs = {
            "file": wav_file,
            "model": self.model,
            "response_format": "json",
        }
        if self.language:
            kwargs["language"] = self.language

        try:
            response = self._client.audio.transcriptions.create(**kwargs)
        except Exception as exc:
            raise RuntimeError(f"ASR API request failed: {readable_provider_error(exc)}") from exc

        text = self._extract_text(response).strip()
        if not text:
            return None

        detected_lang = self.language or getattr(response, "language", None) or "auto"
        return {
            "text": text,
            "language": detected_lang,
            "language_name": LANGUAGE_NAMES.get(detected_lang, detected_lang),
        }

    @staticmethod
    def _to_wav_file(audio: np.ndarray) -> io.BytesIO:
        clipped = np.clip(audio, -1.0, 1.0)
        pcm = (clipped * 32767).astype(np.int16)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm.tobytes())
        buffer.seek(0)
        buffer.name = "audio.wav"
        return buffer

    @staticmethod
    def _extract_text(response) -> str:
        if isinstance(response, dict):
            return str(response.get("text") or "")
        return str(getattr(response, "text", "") or "")
