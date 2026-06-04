from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import ASRProvider


@dataclass(frozen=True)
class ASRProviderConfig:
    engine_type: str
    device: str = "cuda"
    model_size: str = "medium"
    compute_type: str = "float16"
    language: str = "auto"
    hub: str = "ms"
    models_dir: Path | None = None
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_model: str = "whisper-1"
    timeout: float = 30.0


@dataclass(frozen=True)
class DeviceSpec:
    device: str
    device_index: int = 0
    pipeline_device: str = "cuda:0"
    compute_type: str = "float16"


def resolve_device(device: str, compute_type: str = "float16") -> DeviceSpec:
    normalized = device
    device_index = 0
    if device.startswith("cuda:"):
        part = device.split("(")[0].strip()
        device_index = int(part.split(":")[1])
        normalized = "cuda"
    pipeline_device = "cpu" if normalized == "cpu" else f"cuda:{device_index}"
    if normalized == "cpu" and compute_type == "float16":
        compute_type = "int8"
    return DeviceSpec(
        device=normalized,
        device_index=device_index,
        pipeline_device=pipeline_device,
        compute_type=compute_type,
    )


def create_asr_provider(config: ASRProviderConfig) -> ASRProvider:
    engine_type = config.engine_type
    device_spec = resolve_device(config.device, config.compute_type)

    if engine_type == "openai-audio":
        from asr_providers.openai_audio import OpenAIAudioASRProvider

        return OpenAIAudioASRProvider(
            base_url=config.api_base,
            api_key=config.api_key,
            model=config.api_model,
            timeout=config.timeout,
            language=config.language,
        )

    if engine_type == "assemblyai":
        from asr_providers.assemblyai import AssemblyAIASRProvider

        return AssemblyAIASRProvider(
            base_url=config.api_base or "https://api.assemblyai.com",
            api_key=config.api_key,
            model=config.api_model or "universal",
            timeout=config.timeout,
            language=config.language,
        )

    if engine_type == "assemblyai-streaming":
        from asr_providers.assemblyai_streaming import AssemblyAIStreamingASRProvider

        return AssemblyAIStreamingASRProvider(
            base_url=config.api_base or "wss://streaming.assemblyai.com/v3/ws",
            api_key=config.api_key,
            model=config.api_model or "u3-rt-pro",
            timeout=config.timeout,
            language=config.language,
        )

    if engine_type == "sensevoice":
        from asr_sensevoice import SenseVoiceEngine

        return SenseVoiceEngine(device=config.device, hub=config.hub)

    if engine_type in ("funasr-nano", "funasr-mlt-nano"):
        from asr_funasr_nano import FunASRNanoEngine

        return FunASRNanoEngine(
            device=config.device,
            hub=config.hub,
            engine_type=engine_type,
        )

    if engine_type == "anime-whisper":
        from asr_anime_whisper import AnimeWhisperEngine

        return AnimeWhisperEngine(device=device_spec.pipeline_device, hub=config.hub)

    from asr_engine import ASREngine

    download_root = None
    if config.models_dir is not None:
        download_root = str((config.models_dir / "huggingface" / "hub").resolve())
    return ASREngine(
        model_size=config.model_size,
        device=device_spec.device,
        device_index=device_spec.device_index,
        compute_type=device_spec.compute_type,
        language=config.language,
        download_root=download_root,
    )
