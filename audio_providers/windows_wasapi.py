from __future__ import annotations

from audio_capture import AudioCapture, list_input_devices, list_output_devices

WindowsWasapiCaptureProvider = AudioCapture

__all__ = [
    "WindowsWasapiCaptureProvider",
    "list_input_devices",
    "list_output_devices",
]
