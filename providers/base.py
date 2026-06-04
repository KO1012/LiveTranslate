from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


@dataclass
class TranslateInput:
    text: str
    source_language: str = "en"
    target_language: str = "zh"
    mode: str = "natural"
    context: list[tuple[str, str]] = field(default_factory=list)
    system_prompt: str | None = None


@dataclass
class TranslateResult:
    original: str
    translation: str
    explanation: str | None = None
    polished: str | None = None
    detected_language: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider:
    name = "base"

    def translate(self, input_data: TranslateInput) -> TranslateResult:
        raise NotImplementedError

    def stream_translate(
        self,
        input_data: TranslateInput,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> Iterable[str]:
        result = self.translate(input_data).translation
        if on_delta:
            on_delta(result)
        yield result

    @property
    def last_usage(self) -> tuple[int, int]:
        return 0, 0
