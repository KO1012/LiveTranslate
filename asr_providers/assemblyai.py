from __future__ import annotations

import io
import time
import wave

import httpx
import numpy as np

from translator import LANGUAGE_DISPLAY

LANGUAGE_NAMES = {**LANGUAGE_DISPLAY, "auto": "auto"}


class AssemblyAIASRProvider:
    """Speech-to-text through AssemblyAI's REST transcription API."""

    name = "assemblyai"

    def __init__(
        self,
        base_url: str = "https://api.assemblyai.com",
        api_key: str = "",
        model: str = "universal",
        timeout: float = 60.0,
        language: str = "auto",
    ):
        self.base_url = (base_url or "https://api.assemblyai.com").rstrip("/")
        self.api_key = api_key
        self.model = model or "universal"
        self.timeout = timeout
        self.language = language if language != "auto" else None
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"authorization": self.api_key},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    def set_language(self, language: str):
        self.language = language if language != "auto" else None

    def to_device(self, device: str):
        return True

    def unload(self):
        if self._client is not None:
            self._client.close()
        self._client = None

    def transcribe(self, audio: np.ndarray, word_timestamps: bool = False) -> dict | None:
        if not self.api_key:
            raise RuntimeError("AssemblyAI API Key is empty")
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers={"authorization": self.api_key},
                timeout=httpx.Timeout(self.timeout, connect=10.0),
            )

        wav_bytes = self._to_wav_bytes(audio)
        upload = self._client.post(
            "/v2/upload",
            content=wav_bytes,
            headers={"content-type": "application/octet-stream"},
        )
        upload.raise_for_status()
        upload_url = upload.json().get("upload_url")
        if not upload_url:
            raise RuntimeError("AssemblyAI upload did not return upload_url")

        payload = {
            "audio_url": upload_url,
            "speech_model": self.model,
            "format_text": True,
        }
        if self.language:
            payload["language_code"] = self.language

        submitted = self._client.post("/v2/transcript", json=payload)
        submitted.raise_for_status()
        transcript_id = submitted.json().get("id")
        if not transcript_id:
            raise RuntimeError("AssemblyAI transcript request did not return id")

        deadline = time.monotonic() + self.timeout
        last = {}
        while time.monotonic() < deadline:
            response = self._client.get(f"/v2/transcript/{transcript_id}")
            response.raise_for_status()
            last = response.json()
            status = last.get("status")
            if status == "completed":
                text = str(last.get("text") or "").strip()
                if not text:
                    return None
                detected = self.language or last.get("language_code") or "auto"
                return {
                    "text": text,
                    "language": detected,
                    "language_name": LANGUAGE_NAMES.get(detected, detected),
                }
            if status == "error":
                raise RuntimeError(last.get("error") or "AssemblyAI transcription failed")
            time.sleep(0.8)

        raise TimeoutError("AssemblyAI transcription timed out")

    @staticmethod
    def _to_wav_bytes(audio: np.ndarray) -> bytes:
        clipped = np.clip(audio, -1.0, 1.0)
        pcm = (clipped * 32767).astype(np.int16)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm.tobytes())
        return buffer.getvalue()
