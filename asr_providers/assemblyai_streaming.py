from __future__ import annotations

import json
import threading
import urllib.parse

import numpy as np
import websocket

from translator import LANGUAGE_DISPLAY

LANGUAGE_NAMES = {**LANGUAGE_DISPLAY, "auto": "auto"}


class AssemblyAIStreamingASRProvider:
    """Speech-to-text through AssemblyAI's v3 Streaming WebSocket API."""

    name = "assemblyai-streaming"

    # AssemblyAI's streaming endpoint only accepts these speech_model values.
    # Anything else (e.g. an OpenAI model name accidentally carried over from
    # another ASR preset) is rejected per-segment and would silently produce
    # no subtitles, so we validate up front and fall back to a sane default.
    VALID_SPEECH_MODELS = (
        "universal-streaming-english",
        "universal-streaming-multilingual",
        "whisper-rt",
        "alpha-english",
        "u3-rt-pro",
        "u3-rt-pro-beta-1",
        "u3-rt-agent",
    )
    DEFAULT_SPEECH_MODEL = "u3-rt-pro"

    def __init__(
        self,
        base_url: str = "wss://streaming.assemblyai.com/v3/ws",
        api_key: str = "",
        model: str = "u3-rt-pro",
        timeout: float = 30.0,
        language: str = "auto",
    ):
        self.base_url = (base_url or "wss://streaming.assemblyai.com/v3/ws").strip()
        self.api_key = api_key
        self.model = self._normalize_model(model)
        self.timeout = timeout
        self.language = language if language != "auto" else None
        self._closed = False
        self._lock = threading.Lock()

    @classmethod
    def _normalize_model(cls, model: str) -> str:
        """Return a valid AssemblyAI speech_model, falling back on bad input."""
        candidate = (model or "").strip()
        if candidate in cls.VALID_SPEECH_MODELS:
            return candidate
        import logging
        logging.getLogger("LiveTranslate.ASR").warning(
            "Invalid AssemblyAI speech_model %r; falling back to %r. Valid: %s",
            candidate or "(empty)",
            cls.DEFAULT_SPEECH_MODEL,
            ", ".join(cls.VALID_SPEECH_MODELS),
        )
        return cls.DEFAULT_SPEECH_MODEL

    def set_language(self, language: str):
        self.language = language if language != "auto" else None

    def to_device(self, device: str):
        return True

    def unload(self):
        self._closed = True

    def transcribe(self, audio: np.ndarray, word_timestamps: bool = False) -> dict | None:
        if self._closed:
            return None
        if not self.api_key:
            raise RuntimeError("AssemblyAI Streaming API Key is empty")
        pcm = self._float32_to_pcm16(audio)
        if not pcm:
            return None

        with self._lock:
            ws = websocket.create_connection(
                self._stream_url(),
                header=[f"Authorization: {self.api_key}"],
                timeout=self.timeout,
            )
            try:
                self._send_audio(ws, pcm)
                ws.send(json.dumps({"type": "ForceEndpoint"}))
                text = self._read_final_turn(ws)
                ws.send(json.dumps({"type": "Terminate"}))
            finally:
                ws.close()

        text = (text or "").strip()
        if not text:
            return None
        detected = self.language or "auto"
        return {
            "text": text,
            "language": detected,
            "language_name": LANGUAGE_NAMES.get(detected, detected),
        }

    def _stream_url(self) -> str:
        parsed = urllib.parse.urlparse(self.base_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        params.setdefault("sample_rate", "16000")
        params.setdefault("encoding", "pcm_s16le")
        params.setdefault("format_turns", "true")
        params["speech_model"] = self.model
        query = urllib.parse.urlencode(params)
        return urllib.parse.urlunparse(parsed._replace(query=query))

    @staticmethod
    def _float32_to_pcm16(audio: np.ndarray) -> bytes:
        if audio is None or len(audio) == 0:
            return b""
        clipped = np.clip(audio, -1.0, 1.0)
        return (clipped * 32767).astype("<i2").tobytes()

    @staticmethod
    def _send_audio(ws, pcm: bytes):
        # AssemblyAI recommends 50 ms to 1000 ms chunks. At 16 kHz 16-bit mono,
        # 100 ms is 3200 bytes.
        chunk_size = 3200
        for offset in range(0, len(pcm), chunk_size):
            ws.send_binary(pcm[offset : offset + chunk_size])

    def _read_final_turn(self, ws) -> str:
        best_text = ""
        while True:
            raw = ws.recv()
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = data.get("type") or data.get("message_type")
            if msg_type == "Error":
                raise RuntimeError(data.get("error") or data.get("message") or raw)
            if data.get("transcript"):
                best_text = str(data["transcript"])
            if data.get("end_of_turn") or data.get("turn_is_formatted"):
                return best_text
