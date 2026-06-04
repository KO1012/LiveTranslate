from __future__ import annotations

from dataclasses import dataclass

from .base import TTSProvider


@dataclass(frozen=True)
class TTSProviderConfig:
    engine_type: str = "edge"
    # edge / openai-speech / elevenlabs shared
    voice: str = ""
    # edge prosody
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"
    # openai-speech / elevenlabs
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "tts-1"
    response_format: str = "mp3"
    speed: float = 1.0
    proxy: str = "none"
    timeout: float = 30.0
    # azure
    region: str = "eastus"


# Per-engine capability table. Drives both the editor dialog field visibility
# and the unified fetch dispatcher, so adding an engine is a single-entry edit.
# ``fetch`` is one of: "models" (text box for model ids), "voices" (voice list),
# or None (nothing to fetch / typed voice only).
ENGINE_CAPS = {
    "edge": {
        "needs_key": False,
        "fields": {"voice", "rate"},
        "fetch": "voices",
    },
    "openai-speech": {
        "needs_key": True,
        "fields": {"voice", "model", "api_base", "api_key", "proxy", "speed"},
        "fetch": "models",
    },
    "azure": {
        "needs_key": True,
        "fields": {"voice", "api_key", "region", "api_base", "proxy"},
        "fetch": "voices",
    },
    "elevenlabs": {
        "needs_key": True,
        "fields": {"voice", "model", "api_key", "api_base", "proxy", "speed"},
        "fetch": "voices",
    },
}


DEFAULT_PROFILE = {
    "name": "Edge",
    "engine": "edge",
    "voice": "",
    "rate": "+0%",
    "api_base": "https://api.openai.com/v1",
    "api_key": "",
    "model": "tts-1",
    "speed": 1.0,
    "proxy": "none",
    "region": "eastus",
}


def resolve_profile(tts_cfg: dict | None) -> dict:
    """Return the active TTS profile from a ``tts`` settings dict.

    Supports both the new ``profiles``/``active_profile`` layout and the legacy
    flat dict (pre-profiles), so older ``user_settings.json`` keeps working.
    """
    tts_cfg = tts_cfg or {}
    profiles = tts_cfg.get("profiles")
    if isinstance(profiles, list) and profiles:
        idx = tts_cfg.get("active_profile", 0)
        if not (0 <= idx < len(profiles)):
            idx = 0
        return dict(profiles[idx])
    # Legacy flat layout.
    return {
        "name": tts_cfg.get("name", "Edge"),
        "engine": tts_cfg.get("engine", "edge"),
        "voice": tts_cfg.get("voice", ""),
        "rate": tts_cfg.get("rate", "+0%"),
        "api_base": tts_cfg.get("api_base", "https://api.openai.com/v1"),
        "api_key": tts_cfg.get("api_key", ""),
        "model": tts_cfg.get("model", "tts-1"),
        "speed": tts_cfg.get("speed", 1.0),
        "proxy": tts_cfg.get("proxy", "none"),
        "region": tts_cfg.get("region", "eastus"),
    }


def build_provider_config(tts_cfg: dict | None) -> TTSProviderConfig:
    """Build a ``TTSProviderConfig`` from the active profile of a ``tts`` dict."""
    p = resolve_profile(tts_cfg)
    return TTSProviderConfig(
        engine_type=p.get("engine", "edge"),
        voice=p.get("voice", ""),
        rate=p.get("rate", "+0%"),
        api_base=p.get("api_base", "https://api.openai.com/v1"),
        api_key=p.get("api_key", ""),
        model=p.get("model", "tts-1"),
        speed=float(p.get("speed", 1.0)),
        proxy=p.get("proxy", "none"),
        region=p.get("region", "eastus"),
    )


def create_tts_provider(config: TTSProviderConfig) -> TTSProvider:
    engine_type = config.engine_type or "edge"

    if engine_type == "openai-speech":
        from .openai_speech import OpenAISpeechProvider

        return OpenAISpeechProvider(
            base_url=config.api_base,
            api_key=config.api_key,
            model=config.model,
            voice=config.voice or "alloy",
            response_format=config.response_format,
            speed=config.speed,
            proxy=config.proxy,
            timeout=config.timeout,
        )

    if engine_type == "azure":
        from .azure import AzureTTSProvider

        return AzureTTSProvider(
            api_key=config.api_key,
            region=config.region,
            api_base=config.api_base if "speech" in (config.api_base or "") else "",
            voice=config.voice,
            proxy=config.proxy,
            timeout=config.timeout,
        )

    if engine_type == "elevenlabs":
        from .elevenlabs import ElevenLabsTTSProvider

        return ElevenLabsTTSProvider(
            api_key=config.api_key,
            api_base=config.api_base if "elevenlabs" in (config.api_base or "") else "",
            voice=config.voice,
            model=config.model,
            speed=config.speed,
            proxy=config.proxy,
            timeout=config.timeout,
        )

    # Default: free Edge neural TTS.
    from .edge import EdgeTTSProvider

    return EdgeTTSProvider(
        voice=config.voice,
        rate=config.rate,
        volume=config.volume,
        pitch=config.pitch,
    )


def list_edge_voices(locale_prefix: str = "") -> list[str]:
    """Return Edge TTS voice short-names, optionally filtered by locale prefix.

    ``locale_prefix`` is matched case-insensitively against the voice locale
    (e.g. "zh" matches zh-CN/zh-HK/zh-TW). Empty returns all voices.
    """
    import asyncio

    import edge_tts

    voices = asyncio.run(edge_tts.list_voices())
    prefix = (locale_prefix or "").lower()
    names: list[str] = []
    for v in voices:
        short = v.get("ShortName")
        if not short:
            continue
        if prefix and not str(v.get("Locale", "")).lower().startswith(prefix):
            continue
        names.append(short)
    return sorted(dict.fromkeys(names), key=str.lower)


def list_openai_speech_models(
    api_base: str, api_key: str, proxy: str = "none", timeout: float = 20.0
) -> list[str]:
    """Fetch model IDs from an OpenAI-compatible ``/models`` endpoint."""
    import httpx

    base = (api_base or "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client_kwargs = {"timeout": httpx.Timeout(timeout, connect=5.0)}
    if proxy in ("none", "", None):
        client_kwargs["trust_env"] = False
    elif proxy != "system":
        client_kwargs["proxy"] = proxy
    with httpx.Client(**client_kwargs) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    rows = data.get("data", []) if isinstance(data, dict) else []
    models: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            model_id = row.get("id") or row.get("name")
        else:
            model_id = str(row)
        if model_id:
            models.append(str(model_id))
    return sorted(dict.fromkeys(models), key=str.lower)


def fetch_tts_items(
    engine: str,
    *,
    api_base: str = "",
    api_key: str = "",
    region: str = "eastus",
    proxy: str = "none",
    locale_prefix: str = "",
) -> tuple[str, list[str]]:
    """Fetch selectable values for an engine's editable combo.

    Returns ``(kind, items)`` where ``kind`` is ``"models"`` or ``"voices"``
    so the caller knows which combo (model vs voice) to populate. Centralising
    the per-engine dispatch here keeps the editor dialog engine-agnostic — add
    a new engine in ``ENGINE_CAPS`` + here, the UI follows automatically.
    """
    engine = engine or "edge"
    kind = ENGINE_CAPS.get(engine, {}).get("fetch")
    if engine == "openai-speech":
        return "models", list_openai_speech_models(api_base, api_key, proxy)
    if engine == "azure":
        items = list_azure_voices(
            api_key, region=region, api_base=api_base,
            locale_prefix=locale_prefix, proxy=proxy,
        )
        return "voices", items
    if engine == "elevenlabs":
        # Voices are the primary selector; models are a secondary fetch.
        from .elevenlabs import list_elevenlabs_voices

        return "voices", list_elevenlabs_voices(api_key, api_base, proxy)
    # edge (and any voice-listing fallback)
    items = list_edge_voices(locale_prefix)
    if not items:
        items = list_edge_voices("")
    return kind or "voices", items


def list_azure_voices(*args, **kwargs) -> list[str]:
    """Proxy to :func:`tts_providers.azure.list_azure_voices` (lazy import)."""
    from .azure import list_azure_voices as _impl

    return _impl(*args, **kwargs)
