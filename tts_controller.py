"""High-level TTS (simultaneous interpretation) controller.

Wraps the threaded :class:`~tts_engine.TTSEngine` plus provider configuration
and owns all the "speak the translation aloud" *policy* that used to live
inline in ``main.py``:

- enable/disable lifecycle and live settings application (profile/voice swap,
  volume, output device);
- streaming sentence chunking so speech starts sentence-by-sentence while a
  translation is still streaming, instead of waiting for the whole result —
  this is what keeps simultaneous-interpretation latency low.

The live pipeline talks to this controller; the controller talks to the engine.
Keeping it here stops ``main.py`` from growing yet another subsystem inline
(see CLAUDE.md hard constraint: don't pile features into main.py).
"""

from __future__ import annotations

import logging

from tts_engine import TTSEngine
from tts_providers import build_provider_config, resolve_profile

log = logging.getLogger("LiveTranslate.TTS")

# Sentence-final punctuation used to chunk a streaming translation for TTS.
_SENTENCE_ENDS_CJK = "。！？…"


def extract_tts_sentences(remainder: str) -> tuple[str, str]:
    """Split completed sentence(s) off the front of a streaming translation.

    ``remainder`` is the not-yet-spoken tail of the accumulating translation.
    Returns ``(to_speak, new_remainder)``; ``to_speak`` is empty when no
    sentence boundary has arrived yet so the caller keeps buffering. This lets
    TTS start speaking sentence-by-sentence during streaming instead of waiting
    for the whole translation, cutting simultaneous-interpretation latency.
    Latin '.' only counts when followed by whitespace and not part of a
    decimal/abbreviation, to avoid choppy mid-number splits.
    """
    last_end = -1
    n = len(remainder)
    for i, ch in enumerate(remainder):
        if ch in _SENTENCE_ENDS_CJK or ch in "!?":
            last_end = i
        elif ch == ".":
            prev_digit = i > 0 and remainder[i - 1].isdigit()
            next_space = i + 1 < n and remainder[i + 1] in " \t\n"
            if next_space and not prev_digit:
                last_end = i
    if last_end < 0:
        return "", remainder
    to_speak = remainder[: last_end + 1].strip()
    new_remainder = remainder[last_end + 1 :]
    if not any(c.isalnum() for c in to_speak):
        # Only punctuation so far; keep buffering until real content arrives.
        return "", remainder
    return to_speak, new_remainder


class TTSStream:
    """Per-translation streaming state for simultaneous interpretation.

    Feed it the streamed partial translations via :meth:`feed`; it speaks each
    completed sentence as it arrives and the trailing remainder on
    :meth:`finish`. Created by :meth:`TTSController.begin_stream` so the caller
    never touches the engine directly.
    """

    def __init__(self, engine: TTSEngine, language: str, streaming: bool):
        self._engine = engine
        self._language = language
        # When False (source == target), nothing is chunked mid-stream; the
        # whole result is spoken once on finish(), matching legacy behaviour.
        self._streaming = streaming
        self._spoken_len = 0
        self._remainder = ""

    def feed(self, partial: str) -> None:
        """Consume a streamed partial translation, speaking any complete sentence."""
        if not self._streaming or len(partial) <= self._spoken_len:
            return
        self._remainder += partial[self._spoken_len :]
        self._spoken_len = len(partial)
        to_speak, self._remainder = extract_tts_sentences(self._remainder)
        if to_speak:
            self._engine.speak(to_speak, self._language)

    def finish(self, translated: str) -> None:
        """Speak whatever is left once the stream completes."""
        if not translated:
            return
        if self._streaming:
            # Speak only the trailing remainder not yet queued during streaming.
            leftover = self._remainder.strip()
            if leftover and any(c.isalnum() for c in leftover):
                self._engine.speak(leftover, self._language)
        else:
            self._engine.speak(translated, self._language)


class TTSController:
    """Owns the TTS engine + enable state and applies live setting changes.

    A thin policy layer over :class:`~tts_engine.TTSEngine`: the engine handles
    threaded synthesis/playback, this class decides *when* to start/stop, how to
    react to setting changes, and how to drive streaming simultaneous
    interpretation.
    """

    def __init__(self, tts_cfg: dict | None = None):
        tts_cfg = dict(tts_cfg or {})
        self._enabled = bool(tts_cfg.get("enabled", False))
        self._settings = dict(tts_cfg)
        self._engine = TTSEngine(
            build_provider_config(tts_cfg),
            volume=float(tts_cfg.get("volume", 1.0)),
        )
        self._engine.set_output_device(tts_cfg.get("output_device") or None)
        if self._enabled:
            self._engine.start()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def apply_settings(self, tts_cfg: dict) -> None:
        """Apply TTS settings: profile/voice swap, volume, device, enable toggle."""
        if not isinstance(tts_cfg, dict):
            return
        if tts_cfg == self._settings:
            return  # nothing changed; avoid rebuilding the synthesis backend
        prev = self._settings
        self._settings = dict(tts_cfg)

        # Rebuild the provider only when the active profile changed.
        if resolve_profile(prev) != resolve_profile(tts_cfg):
            self._engine.set_config(build_provider_config(tts_cfg))
        self._engine.set_volume(float(tts_cfg.get("volume", 1.0)))
        self._engine.set_output_device(tts_cfg.get("output_device") or None)
        enabled = bool(tts_cfg.get("enabled", False))
        if enabled and not self._enabled:
            self._engine.start()
        elif not enabled and self._enabled:
            self._engine.clear()
            self._engine.stop()
        self._enabled = enabled

    def set_enabled(self, enabled: bool) -> None:
        """Immediately start/stop simultaneous interpretation (TTS).

        Used by the one-click overlay button and tray toggle so the engine
        reacts instantly, without waiting for the panel's debounced auto-save.
        """
        cfg = dict(self._settings)
        cfg["enabled"] = bool(enabled)
        self.apply_settings(cfg)

    def begin_stream(self, source_lang: str, target_lang: str) -> "TTSStream | None":
        """Start a per-translation speech stream, or ``None`` when disabled.

        Sentence-by-sentence chunking only kicks in when translating across
        languages; if source == target the whole result is spoken on finish.
        """
        if not self._enabled:
            return None
        return TTSStream(
            self._engine, target_lang, streaming=source_lang != target_lang
        )

    def clear(self) -> None:
        """Drop queued speech (e.g. on pause or target-language switch)."""
        if self._enabled:
            self._engine.clear()

    def stop(self) -> None:
        """Stop the engine on shutdown."""
        if self._enabled:
            self._engine.stop()
