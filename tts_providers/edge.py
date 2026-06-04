from __future__ import annotations

import asyncio
import logging

import numpy as np

from .audio_utils import decode_audio_bytes

log = logging.getLogger("LiveTranslate.TTS.Edge")

# Map target-language codes to a sensible default Microsoft Edge neural voice.
# Edge TTS is free and needs no API key, which makes it the default backend for
# simultaneous interpretation.
DEFAULT_VOICES = {
    "zh": "zh-CN-XiaoxiaoNeural",
    "en": "en-US-AriaNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "es": "es-ES-ElviraNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "it": "it-IT-ElsaNeural",
    "nl": "nl-NL-ColetteNeural",
    "pl": "pl-PL-ZofiaNeural",
    "tr": "tr-TR-EmelNeural",
    "ar": "ar-SA-ZariyahNeural",
    "th": "th-TH-PremwadeeNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "id": "id-ID-GadisNeural",
    "ms": "ms-MY-YasminNeural",
    "hi": "hi-IN-SwaraNeural",
    "uk": "uk-UA-PolinaNeural",
    "cs": "cs-CZ-VlastaNeural",
    "ro": "ro-RO-AlinaNeural",
    "el": "el-GR-AthinaNeural",
    "hu": "hu-HU-NoemiNeural",
    "sv": "sv-SE-SofieNeural",
    "da": "da-DK-ChristelNeural",
    "fi": "fi-FI-NooraNeural",
    "no": "nb-NO-PernilleNeural",
    "he": "he-IL-HilaNeural",
}

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


class EdgeTTSProvider:
    """Free Microsoft Edge online neural TTS (no API key required)."""

    name = "edge"

    def __init__(
        self,
        voice: str = "",
        rate: str = "+0%",
        volume: str = "+0%",
        pitch: str = "+0Hz",
    ):
        self._voice = voice or ""
        self._rate = rate or "+0%"
        self._volume = volume or "+0%"
        self._pitch = pitch or "+0Hz"

    def _resolve_voice(self, language: str) -> str:
        if self._voice:
            return self._voice
        return DEFAULT_VOICES.get(language, DEFAULT_VOICE)

    def synthesize(self, text: str, language: str) -> tuple[np.ndarray, int] | None:
        text = (text or "").strip()
        if not text:
            return None
        voice = self._resolve_voice(language)
        try:
            data = asyncio.run(self._stream(text, voice))
        except Exception as exc:
            raise RuntimeError(f"Edge TTS synthesis failed: {exc}") from exc
        return decode_audio_bytes(data)

    async def _stream(self, text: str, voice: str) -> bytes:
        import edge_tts

        comm = edge_tts.Communicate(
            text,
            voice,
            rate=self._rate,
            volume=self._volume,
            pitch=self._pitch,
        )
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
        return bytes(buf)

    def close(self) -> None:
        pass
