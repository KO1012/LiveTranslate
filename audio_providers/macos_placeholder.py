from __future__ import annotations

from .base import AudioCaptureProvider

MACOS_AUDIO_MESSAGE = (
    "macOS 音频捕获功能暂未开放，后续将通过 ScreenCaptureKit/CoreAudio 支持。"
)


class MacOSAudioCaptureProviderPlaceholder(AudioCaptureProvider):
    def __init__(self, device=None, sample_rate=16000, chunk_duration=0.5):
        self.sample_rate = sample_rate
        self.chunk_duration = chunk_duration
        self._device_name = device
        self._mic_device_name = None

    def start(self):
        raise NotImplementedError(MACOS_AUDIO_MESSAGE)

    def stop(self):
        return None

    def get_audio(self, timeout=1.0):
        return None

    def set_device(self, device_name):
        self._device_name = device_name

    def set_mic_device(self, device_name):
        self._mic_device_name = device_name
