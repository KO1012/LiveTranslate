from __future__ import annotations

from typing import Protocol

import numpy as np


class ASRProvider(Protocol):
    language: str | None

    def set_language(self, language: str):
        ...

    def to_device(self, device: str):
        ...

    def unload(self):
        ...

    def transcribe(self, audio: np.ndarray, word_timestamps: bool = False) -> dict | None:
        ...
