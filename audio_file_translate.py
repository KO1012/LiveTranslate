"""Transcribe an uploaded audio file and translate it into a target language.

This module is intentionally UI-agnostic so it can be unit-tested and reused.
It decodes an arbitrary audio file (wav/mp3/flac/ogg/...) into the 16 kHz mono
float32 format every ASR provider in this project expects, splits long audio
into manageable chunks, runs the provided ASR engine on each chunk, and then
translates the recognised text with the provided translator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("LiveTranslate.AudioFile")

TARGET_SAMPLE_RATE = 16000
# Audio is sent to ASR in windows of this many seconds. Cloud transcription
# endpoints and local models both behave better on bounded segments, and it
# lets us report progress / translate incrementally for long recordings.
DEFAULT_CHUNK_SECONDS = 30.0

# File extensions we advertise in the picker. soundfile (libsndfile) handles
# the first group natively; the rest are attempted via librosa/audioread.
SUPPORTED_EXTENSIONS = (
    ".wav",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".mp3",
    ".m4a",
    ".aac",
    ".wma",
    ".aiff",
    ".aif",
)


class AudioDecodeError(RuntimeError):
    """Raised when an uploaded file cannot be decoded to PCM samples."""


@dataclass
class TranslatedSegment:
    index: int
    start_seconds: float
    end_seconds: float
    original: str
    translation: str = ""
    language: str = "auto"


@dataclass
class AudioTranslationResult:
    source_language: str = "auto"
    target_language: str = "zh"
    segments: list[TranslatedSegment] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def full_original(self) -> str:
        return "\n".join(s.original for s in self.segments if s.original).strip()

    @property
    def full_translation(self) -> str:
        return "\n".join(s.translation for s in self.segments if s.translation).strip()


def _to_mono(samples: np.ndarray) -> np.ndarray:
    """Downmix an (n, channels) or (n,) array to a 1-D float32 mono signal."""
    arr = np.asarray(samples, dtype=np.float32)
    if arr.ndim == 2:
        if arr.shape[1] == 1:
            arr = arr[:, 0]
        else:
            arr = arr.mean(axis=1)
    return np.ascontiguousarray(arr, dtype=np.float32)


def _resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample a 1-D signal with linear interpolation (no extra deps)."""
    if src_rate == dst_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    duration = samples.shape[0] / float(src_rate)
    dst_count = max(1, int(round(duration * dst_rate)))
    src_idx = np.linspace(0.0, samples.shape[0] - 1, num=dst_count)
    resampled = np.interp(src_idx, np.arange(samples.shape[0]), samples)
    return resampled.astype(np.float32)


def decode_to_16k_mono(path: str | Path) -> np.ndarray:
    """Decode an audio file into a 16 kHz mono float32 array in [-1, 1].

    Tries soundfile (libsndfile) first, then falls back to librosa for codecs
    libsndfile cannot read (e.g. some m4a/aac files).
    """
    path = Path(path)
    if not path.exists():
        raise AudioDecodeError(f"文件不存在: {path}")

    samples: np.ndarray | None = None
    sample_rate: int | None = None
    errors: list[str] = []

    try:
        import soundfile as sf

        data, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
        samples = data
    except Exception as exc:  # noqa: BLE001 - report and try the fallback
        errors.append(f"soundfile: {exc}")
        log.debug("soundfile decode failed for %s: %s", path, exc)

    if samples is None:
        try:
            import librosa

            data, sample_rate = librosa.load(str(path), sr=None, mono=False)
            # librosa returns (channels, n) for multi-channel; transpose to (n, channels)
            if data.ndim == 2:
                data = data.T
            samples = data
        except Exception as exc:  # noqa: BLE001
            errors.append(f"librosa: {exc}")
            log.debug("librosa decode failed for %s: %s", path, exc)

    if samples is None or sample_rate is None:
        raise AudioDecodeError(
            "无法解码音频文件，请确认格式受支持。\n" + "\n".join(errors)
        )

    mono = _to_mono(samples)
    if mono.size == 0:
        raise AudioDecodeError("音频文件为空或没有可用的音频数据。")
    mono = _resample_linear(mono, int(sample_rate), TARGET_SAMPLE_RATE)
    # Guard against clipping/NaNs from odd source material.
    mono = np.nan_to_num(mono, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.clip(mono, -1.0, 1.0)


def _iter_chunks(audio: np.ndarray, chunk_seconds: float):
    """Yield (start_seconds, end_seconds, samples) windows over the signal."""
    chunk_size = max(1, int(chunk_seconds * TARGET_SAMPLE_RATE))
    total = audio.shape[0]
    if total <= chunk_size:
        yield 0.0, total / TARGET_SAMPLE_RATE, audio
        return
    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)
        yield (
            start / TARGET_SAMPLE_RATE,
            end / TARGET_SAMPLE_RATE,
            audio[start:end],
        )


def transcribe_and_translate(
    path: str | Path,
    asr,
    translator,
    target_language: str = "zh",
    source_language: str = "auto",
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    progress=None,
    should_cancel=None,
) -> AudioTranslationResult:
    """Decode ``path``, transcribe with ``asr`` and translate with ``translator``.

    Args:
        path: audio file to process.
        asr: object exposing ``transcribe(np.ndarray) -> dict | None`` and,
            optionally, ``language`` / ``set_language``.
        translator: object exposing ``translate(text, source_language) -> str``.
        target_language: language code the translation should be produced in.
        source_language: ASR/source language hint ("auto" to detect).
        chunk_seconds: window length fed to the ASR engine.
        progress: optional callable(done_chunks, total_chunks, stage_text).
        should_cancel: optional callable() -> bool checked between chunks.

    Returns:
        AudioTranslationResult with per-chunk original + translated text.
    """
    audio = decode_to_16k_mono(path)
    duration = audio.shape[0] / TARGET_SAMPLE_RATE
    chunks = list(_iter_chunks(audio, chunk_seconds))
    total = len(chunks)

    result = AudioTranslationResult(
        source_language=source_language,
        target_language=target_language,
        duration_seconds=duration,
    )

    detected_language: str | None = None
    seg_index = 0
    for i, (start, end, segment) in enumerate(chunks):
        if should_cancel and should_cancel():
            log.info("Audio file translation cancelled by user")
            break
        if progress:
            progress(i, total, "transcribe")
        try:
            asr_result = asr.transcribe(segment)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"语音识别失败: {exc}") from exc
        if not asr_result:
            continue
        original = (asr_result.get("text") or "").strip()
        if not original:
            continue
        lang = asr_result.get("language") or source_language or "auto"
        if detected_language is None and lang and lang != "auto":
            detected_language = lang

        translation = ""
        if progress:
            progress(i, total, "translate")
        try:
            translation = translator.translate(
                original, lang if lang and lang != "auto" else "auto"
            ).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("Translation failed for a chunk: %s", exc)
            translation = f"[翻译失败] {exc}"

        seg_index += 1
        result.segments.append(
            TranslatedSegment(
                index=seg_index,
                start_seconds=start,
                end_seconds=end,
                original=original,
                translation=translation,
                language=lang,
            )
        )

    if detected_language:
        result.source_language = detected_language
    if progress:
        progress(total, total, "done")
    return result
