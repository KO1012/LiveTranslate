from __future__ import annotations

from typing import Protocol

import numpy as np


class TTSProvider(Protocol):
    """Text-to-speech backend.

    A provider turns a piece of text into decoded PCM audio. Implementations
    return float32 samples shaped (n_samples, channels) together with the
    sample rate, so the player can stream them to the output device directly.
    """

    name: str

    def synthesize(self, text: str, language: str) -> tuple[np.ndarray, int] | None:
        """Render ``text`` (in ``language``) to audio.

        Returns ``(samples, sample_rate)`` where ``samples`` is float32 with
        shape ``(n, channels)``, or ``None`` when nothing was produced.
        """
        ...

    def close(self) -> None:
        ...
