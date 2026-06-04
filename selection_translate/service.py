from __future__ import annotations

import logging
import re
import threading

from translator import PROMPT_PRESETS, Translator

log = logging.getLogger("LiveTranslate.Selection")


class SelectionTranslateService:
    def __init__(
        self,
        get_model_config,
        get_target_language,
        get_timeout,
        history_store=None,
        get_history_enabled=None,
    ):
        self._get_model_config = get_model_config
        self._get_target_language = get_target_language
        self._get_timeout = get_timeout
        self._history = history_store
        self._get_history_enabled = get_history_enabled or (lambda: True)
        self._lock = threading.Lock()

    def translate(
        self,
        text: str,
        source: str = "local-api",
        url: str | None = None,
        mode: str = "explain",
        source_language: str = "auto",
    ) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("没有读取到选中文本")

        mode = mode if mode in PROMPT_PRESETS else "explain"
        model_config = self._get_model_config() or {}
        if not model_config:
            raise RuntimeError("未配置翻译模型")

        target_language = self._get_target_language() or "zh"
        timeout = self._get_timeout() or 10

        translator = Translator(
            api_base=model_config.get("api_base", ""),
            api_key=model_config.get("api_key", ""),
            model=model_config.get("model", ""),
            provider_type=model_config.get("provider", "openai-compatible"),
            target_language=target_language,
            max_tokens=model_config.get("overrides", {}).get("max_tokens", 512),
            temperature=model_config.get("overrides", {}).get("temperature", 0.3),
            streaming=False,
            system_prompt=PROMPT_PRESETS[mode],
            proxy=model_config.get("proxy", "none"),
            no_system_role=model_config.get("no_system_role", False),
            no_think=model_config.get("no_think", True),
            json_response=False,
            timeout=timeout,
            overrides=model_config.get("overrides"),
            extra_body=model_config.get("extra_body"),
            mode=mode,
        )

        with self._lock:
            translated = translator.translate(text, source_language)

        parsed = self._parse_structured_response(translated)

        result = {
            "original": text,
            "translation": parsed["translation"],
            "explanation": parsed["explanation"],
            "polished": parsed["polished"],
            "raw": translated,
            "source": source,
            "url": url,
            "mode": mode,
            "source_language": source_language,
            "target_language": target_language,
            "provider": translator.provider_name,
            "model": translator.model,
        }

        if self._history and self._get_history_enabled():
            try:
                self._history.add_translation_history(
                    source_type=source or "selection",
                    original_text=text,
                    translated_text=parsed["translation"],
                    target_language=target_language,
                    mode=mode,
                    explanation=parsed["explanation"],
                    polished_text=parsed["polished"],
                    source_language=source_language,
                    provider=translator.provider_name,
                    model=translator.model,
                )
            except Exception as e:
                log.warning("Selection history write failed: %s", e)
        return result

    @staticmethod
    def _parse_structured_response(text: str) -> dict:
        labels = {
            "translation": r"(?:自然翻译|翻译|译文)",
            "explanation": r"(?:解释|说明)",
            "polished": r"(?:更自然/更适合复制的表达|更自然的表达|润色|改写)",
        }

        values = {"translation": text.strip(), "explanation": None, "polished": None}
        matches = []
        for key, pattern in labels.items():
            for match in re.finditer(rf"^\s*{pattern}\s*[:：]\s*", text, re.MULTILINE):
                matches.append((match.start(), match.end(), key))
        if not matches:
            return values

        matches.sort(key=lambda item: item[0])
        parsed = {"translation": None, "explanation": None, "polished": None}
        for idx, (_start, end, key) in enumerate(matches):
            next_start = matches[idx + 1][0] if idx + 1 < len(matches) else len(text)
            value = text[end:next_start].strip()
            if value:
                parsed[key] = value
        return {
            "translation": parsed["translation"] or text.strip(),
            "explanation": parsed["explanation"],
            "polished": parsed["polished"],
        }
