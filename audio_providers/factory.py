from __future__ import annotations

import platform

from .macos_placeholder import MacOSAudioCaptureProviderPlaceholder


def create_audio_capture(device=None, sample_rate=16000, chunk_duration=0.5):
    system = platform.system().lower()
    if system == "darwin":
        return MacOSAudioCaptureProviderPlaceholder(
            device=device,
            sample_rate=sample_rate,
            chunk_duration=chunk_duration,
        )
    if system == "windows":
        try:
            from .windows_wasapi import WindowsWasapiCaptureProvider
        except ImportError as e:
            raise RuntimeError(
                "Windows 音频捕获依赖未安装，请先安装 requirements.txt 中的 PyAudioWPatch"
            ) from e

        return WindowsWasapiCaptureProvider(
            device=device,
            sample_rate=sample_rate,
            chunk_duration=chunk_duration,
        )
    raise NotImplementedError(f"Unsupported audio capture platform: {platform.system()}")


def list_output_devices():
    if platform.system().lower() == "windows":
        try:
            from .windows_wasapi import list_output_devices as _list_output_devices
        except ImportError:
            return []

        return _list_output_devices()
    return []


def list_input_devices():
    if platform.system().lower() == "windows":
        try:
            from .windows_wasapi import list_input_devices as _list_input_devices
        except ImportError:
            return []

        return _list_input_devices()
    return []
