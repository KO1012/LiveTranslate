"""TTS engine for simultaneous interpretation.

Runs synthesis + playback on a dedicated background thread so the live
pipeline never blocks. Translated sentences are pushed via ``speak()``; the
worker synthesizes audio (through a pluggable ``TTSProvider``) and streams the
PCM to the system output device with pyaudiowpatch.

Design notes for "同声传译" (simultaneous interpretation):
- A bounded queue keeps latency low. When speech outpaces playback, the
  oldest pending items are dropped so the spoken output stays close to live.
- Playback runs in small frames so ``stop()``/``clear()`` interrupt promptly.
"""

from __future__ import annotations

import logging
import queue
import threading

import numpy as np

from tts_providers import TTSProviderConfig, create_tts_provider

log = logging.getLogger("LiveTranslate.TTS")

_PLAYBACK_FRAMES = 2048  # samples per write; small enough to interrupt fast


class TTSEngine:
    """Threaded text-to-speech playback queue."""

    def __init__(self, config: TTSProviderConfig, volume: float = 1.0, max_queue: int = 8):
        self._config = config
        self._volume = max(0.0, min(1.0, volume))
        self._output_device = None  # device name; None = system default

        self._provider = create_tts_provider(config)
        self._provider_lock = threading.Lock()

        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._worker: threading.Thread | None = None
        self._running = False
        self._interrupt = threading.Event()  # signals current playback to stop

        self._pa = None
        self._stream = None
        self._stream_rate = None
        self._stream_channels = None

    # ── lifecycle ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._interrupt.clear()
        self._worker = threading.Thread(
            target=self._run, name="TTSWorker", daemon=True
        )
        self._worker.start()
        log.info("TTS engine started")

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._interrupt.set()
        self.clear()
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=5)
            self._worker = None
        self._close_stream()
        try:
            with self._provider_lock:
                self._provider.close()
        except Exception:
            pass
        log.info("TTS engine stopped")

    # ── configuration ──

    def set_config(self, config: TTSProviderConfig):
        """Swap the synthesis backend (e.g. engine/voice change)."""
        with self._provider_lock:
            try:
                self._provider.close()
            except Exception:
                pass
            self._config = config
            self._provider = create_tts_provider(config)
        log.info(f"TTS provider switched: {config.engine_type}")

    def set_volume(self, volume: float):
        self._volume = max(0.0, min(1.0, float(volume)))

    def set_output_device(self, device_name: str | None):
        """Set output device by name (None = system default). Forces a stream rebuild."""
        if device_name != self._output_device:
            self._output_device = device_name
            self._close_stream()

    # ── queue control ──

    def speak(self, text: str, language: str):
        """Enqueue text for spoken playback. Drops oldest item if the queue is full."""
        if not self._running:
            return
        text = (text or "").strip()
        if not text:
            return
        try:
            self._queue.put_nowait((text, language))
        except queue.Full:
            try:
                dropped = self._queue.get_nowait()
                if dropped is not None:
                    log.debug(f"TTS queue full, dropped: {dropped[0][:40]}")
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait((text, language))
            except queue.Full:
                log.warning("TTS queue still full after drop, skipping")

    def clear(self):
        """Drop all pending items and interrupt current playback."""
        self._interrupt.set()
        while True:
            try:
                item = self._queue.get_nowait()
                if item is None:
                    # Preserve the stop sentinel.
                    self._queue.put_nowait(None)
                    break
            except queue.Empty:
                break

    # ── worker ──

    def _run(self):
        while self._running:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            # A fresh item means any pending interrupt is consumed.
            self._interrupt.clear()
            text, language = item
            try:
                self._synthesize_and_play(text, language)
            except Exception as e:
                log.warning(f"TTS playback error: {e}")

    def _synthesize_and_play(self, text: str, language: str):
        with self._provider_lock:
            provider = self._provider
        result = provider.synthesize(text, language)
        if result is None:
            return
        samples, sample_rate = result
        if self._interrupt.is_set() or not self._running:
            return
        self._play(samples, sample_rate)

    # ── playback (pyaudiowpatch) ──

    def _ensure_stream(self, sample_rate: int, channels: int):
        import pyaudiowpatch as pyaudio

        if (
            self._stream is not None
            and self._stream_rate == sample_rate
            and self._stream_channels == channels
        ):
            return
        self._close_stream()
        if self._pa is None:
            self._pa = pyaudio.PyAudio()
        output_index = self._resolve_output_index()
        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=sample_rate,
            output=True,
            output_device_index=output_index,
        )
        self._stream_rate = sample_rate
        self._stream_channels = channels

    def _resolve_output_index(self):
        if not self._output_device or self._pa is None:
            return None
        try:
            for i in range(self._pa.get_device_count()):
                dev = self._pa.get_device_info_by_index(i)
                if (
                    dev.get("maxOutputChannels", 0) > 0
                    and dev.get("name") == self._output_device
                ):
                    return i
        except Exception:
            pass
        return None

    def _play(self, samples: np.ndarray, sample_rate: int):
        channels = samples.shape[1] if samples.ndim > 1 else 1
        if self._volume != 1.0:
            samples = samples * self._volume
        samples = np.clip(samples, -1.0, 1.0).astype(np.float32)
        try:
            self._ensure_stream(sample_rate, channels)
        except Exception as e:
            log.warning(f"TTS output device open failed: {e}")
            return
        total = samples.shape[0]
        pos = 0
        while pos < total and not self._interrupt.is_set() and self._running:
            frame = samples[pos : pos + _PLAYBACK_FRAMES]
            try:
                self._stream.write(frame.tobytes())
            except Exception as e:
                log.warning(f"TTS stream write failed: {e}")
                self._close_stream()
                return
            pos += _PLAYBACK_FRAMES

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._stream_rate = None
        self._stream_channels = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
