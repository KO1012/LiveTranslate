from __future__ import annotations

import io

import numpy as np


def build_httpx_client(proxy: str = "none", timeout: float = 30.0):
    """Create a proxy-aware ``httpx.Client`` shared by the API TTS providers.

    ``proxy`` follows the project convention: ``"none"`` ignores environment
    proxies, ``"system"`` honours them (``trust_env``), any other value is a
    custom proxy URL.
    """
    import httpx

    client_kwargs: dict = {"timeout": httpx.Timeout(timeout, connect=5.0)}
    if proxy in ("none", "", None):
        client_kwargs["trust_env"] = False
    elif proxy != "system":
        client_kwargs["proxy"] = proxy
    return httpx.Client(**client_kwargs)


def decode_audio_bytes(data: bytes) -> tuple[np.ndarray, int] | None:
    """Decode encoded audio (MP3/WAV/OGG/...) into float32 PCM.

    Returns ``(samples, sample_rate)`` with ``samples`` shaped
    ``(n_samples, channels)``, or ``None`` if the buffer is empty/undecodable.
    """
    if not data:
        return None
    import soundfile as sf

    samples, sample_rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    if samples.size == 0:
        return None
    return samples, int(sample_rate)
