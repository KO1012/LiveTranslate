from __future__ import annotations

import logging

import numpy as np

from .audio_utils import build_httpx_client, decode_audio_bytes

log = logging.getLogger("LiveTranslate.TTS.ElevenLabs")

_DEFAULT_BASE = "https://api.elevenlabs.io"
# "Rachel" — a stock multilingual voice present on every account.
DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"
DEFAULT_MODEL = "eleven_multilingual_v2"
# 44.1kHz mono mp3 decodes cleanly via soundfile.
_OUTPUT_FORMAT = "mp3_44100_128"


def _base(api_base: str) -> str:
    base = (api_base or "").strip().rstrip("/")
    return base if base.startswith("http") else _DEFAULT_BASE


def _voice_id(voice: str) -> str:
    """Extract the voice id from a stored value.

    The fetch helper offers voices as ``"Name | voice_id"`` for readability;
    accept either that form or a bare id.
    """
    voice = (voice or "").strip()
    if "|" in voice:
        return voice.rsplit("|", 1)[-1].strip()
    return voice


class ElevenLabsTTSProvider:
    """ElevenLabs text-to-speech (REST ``/v1/text-to-speech/{voice_id}``)."""

    name = "elevenlabs"

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        voice: str = "",
        model: str = "",
        speed: float = 1.0,
        proxy: str = "none",
        timeout: float = 30.0,
    ):
        self.api_key = api_key or ""
        self.api_base = api_base or ""
        self.voice = voice or DEFAULT_VOICE
        self.model = model or DEFAULT_MODEL
        self.speed = float(speed) if speed else 1.0
        self.proxy = proxy
        self.timeout = timeout

    def synthesize(self, text: str, language: str) -> tuple[np.ndarray, int] | None:
        text = (text or "").strip()
        if not text:
            return None
        if not self.api_key:
            raise RuntimeError("ElevenLabs TTS requires an API key")
        voice_id = _voice_id(self.voice) or DEFAULT_VOICE
        url = (
            f"{_base(self.api_base)}/v1/text-to-speech/{voice_id}"
            f"?output_format={_OUTPUT_FORMAT}"
        )
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        payload: dict = {"text": text, "model_id": self.model or DEFAULT_MODEL}
        if self.speed and self.speed != 1.0:
            payload["voice_settings"] = {"speed": self.speed}
        try:
            with build_httpx_client(self.proxy, self.timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.content
        except Exception as exc:
            raise RuntimeError(f"ElevenLabs TTS request failed: {exc}") from exc
        return decode_audio_bytes(data)

    def close(self) -> None:
        pass


def list_elevenlabs_voices(
    api_key: str, api_base: str = "", proxy: str = "none", timeout: float = 20.0
) -> list[str]:
    """Fetch available voices as ``"Name | voice_id"`` strings.

    The display keeps the human name while preserving the id the API needs;
    the dialog stores whatever the user picks as the profile ``voice``.
    """
    url = f"{_base(api_base)}/v1/voices"
    headers = {"xi-api-key": api_key or ""}
    with build_httpx_client(proxy, timeout) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    rows = data.get("voices", []) if isinstance(data, dict) else []
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        vid = row.get("voice_id")
        if not vid:
            continue
        label = row.get("name") or vid
        out.append(f"{label} | {vid}")
    return out


def list_elevenlabs_models(
    api_key: str, api_base: str = "", proxy: str = "none", timeout: float = 20.0
) -> list[str]:
    """Fetch model ids that can do text-to-speech."""
    url = f"{_base(api_base)}/v1/models"
    headers = {"xi-api-key": api_key or ""}
    with build_httpx_client(proxy, timeout) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    rows = data if isinstance(data, list) else data.get("models", [])
    out: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mid = row.get("model_id") or row.get("id")
        if not mid:
            continue
        if row.get("can_do_text_to_speech") is False:
            continue
        out.append(str(mid))
    return sorted(dict.fromkeys(out), key=str.lower)
