import os
import json
import logging
from pathlib import Path

from runtime_paths import data_path

log = logging.getLogger("LiveTranslate.ModelManager")

DEFAULT_MODELS_DIR = data_path("models")
MODELS_DIR = DEFAULT_MODELS_DIR

ASR_MODEL_IDS = {
    "sensevoice": "iic/SenseVoiceSmall",
    "funasr-nano": "FunAudioLLM/Fun-ASR-Nano-2512",
    "funasr-mlt-nano": "FunAudioLLM/Fun-ASR-MLT-Nano-2512",
    "anime-whisper": "litagin/anime-whisper",
}

ASR_DISPLAY_NAMES = {
    "sensevoice": "SenseVoice Small",
    "funasr-nano": "Fun-ASR-Nano",
    "funasr-mlt-nano": "Fun-ASR-MLT-Nano",
    "whisper": "Whisper",
    "openai-audio": "OpenAI Audio API",
    "assemblyai": "AssemblyAI",
    "assemblyai-streaming": "AssemblyAI Streaming",
    "anime-whisper": "Anime-Whisper",
}

_MODEL_SIZE_BYTES = {
    "silero-vad": 2_000_000,
    "sensevoice": 940_000_000,
    "funasr-nano": 1_050_000_000,
    "funasr-mlt-nano": 1_050_000_000,
    "whisper-tiny": 78_000_000,
    "whisper-base": 148_000_000,
    "whisper-small": 488_000_000,
    "whisper-medium": 1_530_000_000,
    "whisper-large-v3": 3_100_000_000,
    "anime-whisper": 3_100_000_000,
}

_WHISPER_SIZES = ["tiny", "base", "small", "medium", "large-v3"]

_CACHE_MODELS = [
    ("SenseVoice Small", "iic/SenseVoiceSmall"),
    ("Fun-ASR-Nano", "FunAudioLLM/Fun-ASR-Nano-2512"),
    ("Fun-ASR-MLT-Nano", "FunAudioLLM/Fun-ASR-MLT-Nano-2512"),
    ("Anime-Whisper", "litagin/anime-whisper"),
]


def set_models_dir(path: str | Path | None):
    global MODELS_DIR
    if path:
        MODELS_DIR = Path(path).expanduser()
    else:
        MODELS_DIR = DEFAULT_MODELS_DIR
    _sync_cache_env()


def get_models_dir() -> Path:
    return MODELS_DIR


def _sync_cache_env():
    resolved = str(MODELS_DIR.resolve())
    os.environ["MODELSCOPE_CACHE"] = os.path.join(resolved, "modelscope")
    os.environ["HF_HOME"] = os.path.join(resolved, "huggingface")
    os.environ["TORCH_HOME"] = os.path.join(resolved, "torch")


def _load_saved_cache_path() -> str | None:
    settings_file = data_path("user_settings.json")
    try:
        if settings_file.exists():
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            cache_path = data.get("cache_path")
            if cache_path:
                return str(cache_path)
    except Exception as e:
        log.warning("Failed to read saved cache path: %s", e)
    return None


def apply_cache_env(cache_path: str | Path | None = None):
    """Point all model caches to ./models/."""
    set_models_dir(cache_path or _load_saved_cache_path())
    resolved = str(MODELS_DIR.resolve())
    log.info(f"Cache env set: {resolved}")


def is_silero_cached() -> bool:
    torch_hub = MODELS_DIR / "torch" / "hub"
    return any(torch_hub.glob("snakers4_silero-vad*")) if torch_hub.exists() else False


def _ms_model_path(org, name):
    """Return the first existing ModelScope cache path, or the default."""
    for sub in (
        MODELS_DIR / "modelscope" / org / name,
        MODELS_DIR / "modelscope" / "hub" / "models" / org / name,
    ):
        if sub.exists():
            return sub
    return MODELS_DIR / "modelscope" / org / name


def is_asr_cached(engine_type, model_size="medium", hub="ms") -> bool:
    # API-based engines never need a local model download.
    if engine_type in ("openai-audio", "assemblyai"):
        return True
    if engine_type in ("sensevoice", "funasr-nano", "funasr-mlt-nano"):
        model_id = ASR_MODEL_IDS[engine_type]
        org, name = model_id.split("/")
        # Accept cache from either hub to avoid redundant downloads
        if _ms_model_path(org, name).exists():
            return True
        if (MODELS_DIR / "huggingface" / "hub" / f"models--{org}--{name}").exists():
            return True
        return False
    if engine_type == "anime-whisper":
        # HF-only (not published to ModelScope). Check that snapshots dir actually
        # contains weight files; an .incomplete blob means a prior run aborted mid-download.
        model_id = ASR_MODEL_IDS[engine_type]
        org, name = model_id.split("/")
        snap_root = (
            MODELS_DIR / "huggingface" / "hub" / f"models--{org}--{name}" / "snapshots"
        )
        if not snap_root.exists():
            return False
        for snap in snap_root.iterdir():
            if not snap.is_dir():
                continue
            has_weights = any(
                (snap / fn).exists()
                for fn in ("model.safetensors", "pytorch_model.bin")
            )
            has_config = (snap / "config.json").exists()
            if has_weights and has_config:
                return True
        return False
    elif engine_type == "whisper":
        return (
            MODELS_DIR
            / "huggingface"
            / "hub"
            / f"models--Systran--faster-whisper-{model_size}"
        ).exists()
    return True


def get_missing_models(engine, model_size, hub, vad_mode="silero") -> list:
    missing = []
    if vad_mode == "silero" and not is_silero_cached():
        missing.append(
            {
                "name": "Silero VAD",
                "type": "silero-vad",
                "estimated_bytes": _MODEL_SIZE_BYTES["silero-vad"],
            }
        )
    if not is_asr_cached(engine, model_size, hub):
        key = engine if engine != "whisper" else f"whisper-{model_size}"
        display = ASR_DISPLAY_NAMES.get(engine, engine)
        if engine == "whisper":
            display = f"Whisper {model_size}"
        missing.append(
            {
                "name": display,
                "type": key,
                "estimated_bytes": _MODEL_SIZE_BYTES.get(key, 0),
            }
        )
    return missing


def detect_hardware() -> dict:
    """Best-effort probe of local hardware for model recommendations.

    Returns ``{"gpus": [{"index", "name", "vram_gb"}], "has_cuda": bool,
    "ram_gb": float, "cpu_count": int}``. Every probe is wrapped so a missing
    dependency or driver never raises — fields just degrade to empty/zero.
    """
    info = {"gpus": [], "has_cuda": False, "ram_gb": 0.0, "cpu_count": 0}
    try:
        info["cpu_count"] = os.cpu_count() or 0
    except Exception:  # noqa: BLE001
        pass
    try:
        import psutil

        info["ram_gb"] = round(psutil.virtual_memory().total / (1024**3), 1)
    except Exception as e:  # noqa: BLE001
        log.debug("RAM probe failed: %s", e)
    try:
        import torch

        if torch.cuda.is_available():
            info["has_cuda"] = True
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                info["gpus"].append(
                    {
                        "index": i,
                        "name": props.name,
                        "vram_gb": round(props.total_memory / (1024**3), 1),
                    }
                )
    except Exception as e:  # noqa: BLE001
        log.debug("CUDA probe failed: %s", e)
    return info


# Minimum VRAM (GB) a local Whisper size needs to load comfortably in float16.
# Used to warn when a user picks a size their GPU likely cannot hold.
WHISPER_VRAM_NEED_GB = {
    "tiny": 1.0,
    "base": 1.5,
    "small": 2.5,
    "medium": 5.0,
    "large-v3": 10.0,
}


def recommend_asr(hw: dict | None = None) -> dict:
    """Recommend an ASR setup from detected hardware.

    Returns a dict with:
      - ``mode``: ``"local"`` or ``"cloud"`` (cloud when no GPU and low RAM).
      - ``engine``: a concrete local engine type (always ``"sensevoice"`` —
        light, fast, multilingual; safe default across tiers).
      - ``whisper_size``: suggested faster-whisper size *if* the user prefers
        Whisper, scaled to the available VRAM.
      - ``device`` / ``device_label``: ``"cpu"`` or ``"cuda:N"`` (+ GPU name).
      - ``vram_gb``: VRAM of the chosen GPU (0 when CPU-only).
      - ``reason_key``: i18n key explaining the recommendation.
    """
    if hw is None:
        hw = detect_hardware()
    gpus = hw.get("gpus") or []
    if gpus:
        best = max(gpus, key=lambda g: g.get("vram_gb", 0))
        vram = best.get("vram_gb", 0.0)
        device = f"cuda:{best['index']}"
        device_label = f"cuda:{best['index']} ({best['name']})"
        # Thresholds mirror WHISPER_VRAM_NEED_GB so the recommended size never
        # contradicts the VRAM-fit warning (e.g. recommending large-v3 on a
        # card that the warning then flags as too small).
        if vram >= 10:
            size, reason = "large-v3", "hw_reason_gpu_high"
        elif vram >= 5:
            size, reason = "medium", "hw_reason_gpu_mid"
        else:
            size, reason = "small", "hw_reason_gpu_low"
        return {
            "mode": "local",
            "engine": "sensevoice",
            "whisper_size": size,
            "device": device,
            "device_label": device_label,
            "vram_gb": vram,
            "reason_key": reason,
        }
    # No CUDA GPU — fall back to CPU, or cloud on low-RAM machines.
    ram = hw.get("ram_gb", 0.0)
    if ram >= 8:
        return {
            "mode": "local",
            "engine": "sensevoice",
            "whisper_size": "base",
            "device": "cpu",
            "device_label": "cpu",
            "vram_gb": 0.0,
            "reason_key": "hw_reason_cpu",
        }
    return {
        "mode": "cloud",
        "engine": "sensevoice",
        "whisper_size": "tiny",
        "device": "cpu",
        "device_label": "cpu",
        "vram_gb": 0.0,
        "reason_key": "hw_reason_lowend",
    }


def get_local_model_path(engine_type, hub="ms"):
    """Return local snapshot path if model is cached, else None.

    Checks the preferred hub first, then falls back to the other hub.
    """
    if engine_type not in ASR_MODEL_IDS:
        return None
    model_id = ASR_MODEL_IDS[engine_type]
    org, name = model_id.split("/")

    def _try_ms():
        local = _ms_model_path(org, name)
        return str(local) if local.exists() else None

    def _try_hf():
        snap_dir = (
            MODELS_DIR / "huggingface" / "hub" / f"models--{org}--{name}" / "snapshots"
        )
        if not snap_dir.exists():
            return None
        snaps = sorted(p for p in snap_dir.iterdir() if p.is_dir())
        if not snaps:
            return None
        # Prefer a snapshot that actually has a config.json — an empty or
        # partially-downloaded snapshot dir (e.g. an aborted download) would
        # otherwise be handed to the ASR loader and fail with a cryptic error
        # instead of being treated as "not cached → re-download".
        for snap in reversed(snaps):
            if (snap / "config.json").exists():
                return str(snap)
        return str(snaps[-1])

    if hub == "ms":
        return _try_ms() or _try_hf()
    else:
        return _try_hf() or _try_ms()


def download_silero():
    import torch

    log.info("Downloading Silero VAD...")
    model, _ = torch.hub.load(
        repo_or_dir="snakers4/silero-vad",
        model="silero_vad",
        trust_repo=True,
    )
    del model
    log.info("Silero VAD downloaded")


def download_asr(engine, model_size="medium", hub="ms"):
    resolved = str(MODELS_DIR.resolve())
    ms_cache = os.path.join(resolved, "modelscope")
    hf_cache = os.path.join(resolved, "huggingface", "hub")
    if engine in ("sensevoice", "funasr-nano", "funasr-mlt-nano"):
        model_id = ASR_MODEL_IDS[engine]
        if hub == "ms":
            from modelscope import snapshot_download

            log.info(f"Downloading {model_id} from ModelScope...")
            snapshot_download(model_id=model_id, cache_dir=ms_cache)
        else:
            from huggingface_hub import snapshot_download

            log.info(f"Downloading {model_id} from HuggingFace...")
            snapshot_download(repo_id=model_id, cache_dir=hf_cache)
    elif engine == "anime-whisper":
        # HF-only, ignore hub setting
        from huggingface_hub import snapshot_download

        model_id = ASR_MODEL_IDS[engine]
        log.info(f"Downloading {model_id} from HuggingFace...")
        snapshot_download(repo_id=model_id, cache_dir=hf_cache)
    elif engine == "whisper":
        from huggingface_hub import snapshot_download

        model_id = f"Systran/faster-whisper-{model_size}"
        log.info(f"Downloading {model_id} from HuggingFace...")
        snapshot_download(repo_id=model_id, cache_dir=hf_cache)
    log.info(f"ASR model downloaded: {engine}")


def dir_size(path) -> int:
    total = 0
    try:
        for f in Path(path).rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except (OSError, PermissionError):
        pass
    return total


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024**2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024**3:
        return f"{size_bytes / (1024**2):.1f} MB"
    else:
        return f"{size_bytes / (1024**3):.2f} GB"


def get_cache_entries():
    """Scan ./models/ for cached models."""
    entries = []
    hf_base = MODELS_DIR / "huggingface" / "hub"
    torch_base = MODELS_DIR / "torch" / "hub"

    for name, model_id in _CACHE_MODELS:
        org, model = model_id.split("/")
        ms_path = _ms_model_path(org, model)
        hf_path = hf_base / f"models--{org}--{model}"
        if ms_path.exists():
            entries.append((f"{name} (ModelScope)", ms_path))
        if hf_path.exists():
            entries.append((f"{name} (HuggingFace)", hf_path))

    for size in _WHISPER_SIZES:
        hf_path = hf_base / f"models--Systran--faster-whisper-{size}"
        if hf_path.exists():
            entries.append((f"Whisper {size}", hf_path))

    if torch_base.exists():
        for d in sorted(torch_base.glob("snakers4_silero-vad*")):
            if d.is_dir():
                entries.append(("Silero VAD", d))
                break

    return entries
