from __future__ import annotations

import logging
from xml.sax.saxutils import escape

import numpy as np

from .audio_utils import build_httpx_client, decode_audio_bytes

log = logging.getLogger("LiveTranslate.TTS.Azure")

# region -> default neural voice, keyed by target-language code.
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
}
DEFAULT_VOICE = "en-US-AriaNeural"

# Endpoint-friendly: 24kHz mono mp3 decodes cleanly via soundfile.
_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"


def _host(region: str, api_base: str) -> str:
    """Resolve the TTS host. A full ``api_base`` URL wins over ``region``."""
    base = (api_base or "").strip().rstrip("/")
    if base.startswith("http"):
        return base
    region = (region or "eastus").strip()
    return f"https://{region}.tts.speech.microsoft.com"


class AzureTTSProvider:
    """Microsoft Azure Cognitive Services neural TTS (REST).

    ``region`` selects the datacentre (e.g. ``eastus``); ``api_key`` is the
    Speech resource key. ``api_base`` may override the host for sovereign
    clouds (e.g. ``https://<region>.tts.speech.azure.cn``).
    """

    name = "azure"

    def __init__(
        self,
        api_key: str = "",
        region: str = "eastus",
        api_base: str = "",
        voice: str = "",
        proxy: str = "none",
        timeout: float = 30.0,
    ):
        self.api_key = api_key or ""
        self.region = region or "eastus"
        self.api_base = api_base or ""
        self.voice = voice or ""
        self.proxy = proxy
        self.timeout = timeout

    def _resolve_voice(self, language: str) -> str:
        if self.voice:
            return self.voice
        return DEFAULT_VOICES.get(language, DEFAULT_VOICE)

    def synthesize(self, text: str, language: str) -> tuple[np.ndarray, int] | None:
        text = (text or "").strip()
        if not text:
            return None
        if not self.api_key:
            raise RuntimeError("Azure TTS requires an API key")
        voice = self._resolve_voice(language)
        locale = "-".join(voice.split("-")[:2]) if "-" in voice else "en-US"
        ssml = (
            f"<speak version='1.0' xml:lang='{locale}'>"
            f"<voice xml:lang='{locale}' name='{voice}'>{escape(text)}</voice>"
            f"</speak>"
        )
        url = f"{_host(self.region, self.api_base)}/cognitiveservices/v1"
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": _OUTPUT_FORMAT,
        }
        try:
            with build_httpx_client(self.proxy, self.timeout) as client:
                resp = client.post(url, headers=headers, content=ssml.encode("utf-8"))
                resp.raise_for_status()
                data = resp.content
        except Exception as exc:
            raise RuntimeError(f"Azure TTS request failed: {exc}") from exc
        return decode_audio_bytes(data)

    def close(self) -> None:
        pass


def list_azure_voices(
    api_key: str,
    region: str = "eastus",
    api_base: str = "",
    locale_prefix: str = "",
    proxy: str = "none",
    timeout: float = 20.0,
) -> list[str]:
    """Fetch Azure neural voice short-names via the voices/list endpoint."""
    url = f"{_host(region, api_base)}/cognitiveservices/voices/list"
    headers = {"Ocp-Apim-Subscription-Key": api_key or ""}
    with build_httpx_client(proxy, timeout) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        rows = resp.json()
    prefix = (locale_prefix or "").lower()
    names: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        short = row.get("ShortName")
        if not short:
            continue
        if prefix and not str(row.get("Locale", "")).lower().startswith(prefix):
            continue
        names.append(short)
    return sorted(dict.fromkeys(names), key=str.lower)
