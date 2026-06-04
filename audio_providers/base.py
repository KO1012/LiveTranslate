from __future__ import annotations

from abc import ABC, abstractmethod


class AudioCaptureProvider(ABC):
    sample_rate: int
    chunk_duration: float

    @abstractmethod
    def start(self):
        raise NotImplementedError

    @abstractmethod
    def stop(self):
        raise NotImplementedError

    @abstractmethod
    def get_audio(self, timeout=1.0):
        raise NotImplementedError

    def set_device(self, device_name):
        raise NotImplementedError

    def set_mic_device(self, device_name):
        raise NotImplementedError
