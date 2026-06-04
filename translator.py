import logging

from providers import OllamaProvider, OpenAICompatibleProvider, TranslateInput
from runtime_paths import resource_path

log = logging.getLogger("LiveTranslate.TL")

LANGUAGE_DISPLAY = {
    "en": "English",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "ru": "Russian",
    "pt": "Portuguese",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
    "ar": "Arabic",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ms": "Malay",
    "hi": "Hindi",
    "uk": "Ukrainian",
    "cs": "Czech",
    "ro": "Romanian",
    "el": "Greek",
    "hu": "Hungarian",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
    "he": "Hebrew",
}

_PROMPT_DIR = resource_path("prompts")
_MAX_CURRENT_SUBTITLE_CHARS = 240
_MAX_CONTEXT_SUBTITLE_CHARS = 180
_SENTENCE_ENDS = ".!?。！？"
_NO_THINK_SUBTITLE_GUARD = (
    "Realtime low-latency rule: do not reason, analyze, or explain. "
    "Return only the final translation, preferably one concise subtitle line "
    "and at most two short lines."
)


def _load_prompt_file(filename: str, fallback: str) -> str:
    try:
        path = _PROMPT_DIR / filename
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if _looks_mojibake(text):
                log.warning("Ignoring mojibake prompt file: %s", filename)
                return fallback
            return text
    except Exception as e:
        log.warning("Failed to load prompt %s: %s", filename, e)
    return fallback


def _looks_mojibake(text: str) -> bool:
    if not text:
        return False
    markers = ("浣犳槸", "锛", "銆", "歿", "俓n", "涓€", "瀛楀箷")
    return sum(text.count(marker) for marker in markers) >= 3


def compact_live_subtitle_text(
    text: str,
    max_chars: int = _MAX_CURRENT_SUBTITLE_CHARS,
) -> str:
    """Keep the newest readable subtitle units for low-latency live requests."""
    normalized = " ".join((text or "").split())
    if not normalized or len(normalized) <= max_chars:
        return normalized

    units = []
    start = 0
    for idx, char in enumerate(normalized):
        if char in _SENTENCE_ENDS:
            unit = normalized[start : idx + 1].strip()
            if unit:
                units.append(unit)
            start = idx + 1
    tail = normalized[start:].strip()
    if tail:
        units.append(tail)
    if not units:
        return normalized[-max_chars:].lstrip()

    selected = []
    total = 0
    for unit in reversed(units):
        sep = 1 if selected else 0
        if total + sep + len(unit) > max_chars:
            if not selected:
                return unit[-max_chars:].lstrip()
            break
        selected.append(unit)
        total += sep + len(unit)
    return " ".join(reversed(selected))


DEFAULT_PROMPT = _load_prompt_file(
    "subtitle_prompt.txt",
    (
        "You are a real-time subtitle translator. Translate {source_lang} into {target_lang}.\n"
        "Rules:\n"
        "- Output ONLY one single best translation, nothing else.\n"
        "- Never include alternatives, parenthetical options, annotations, or explanations.\n"
        "- Keep proper nouns, names, and brand names untranslated.\n"
        "- The user message contains one CURRENT SUBTITLE and may contain RECENT CONTEXT.\n"
        "- Translate the CURRENT SUBTITLE as a rolling live subtitle line.\n"
        "- Use RECENT CONTEXT only to resolve pronouns, missing subjects, causality, and ASR mistakes.\n"
        "- If the current subtitle is a fragment, infer only what the context clearly supports.\n"
        "- You may revise the meaning implied by earlier fragments when later context clarifies it.\n"
        "- Keep subtitles fluent, natural, and suitable for one-line scrolling display.\n"
        "- Prefer one concise subtitle update over a long paragraph.\n"
        "- Auto-correct likely ASR errors based on context and common sense.\n"
        "- Do not mention that you used context. Do not output labels."
    ),
)

EXPLAIN_PROMPT = _load_prompt_file("explain_prompt.txt", DEFAULT_PROMPT)
POLISH_PROMPT = _load_prompt_file("polish_prompt.txt", DEFAULT_PROMPT)

PROMPT_PRESETS = {
    "literal": (
        "You are a subtitle translator. Translate {source_lang} into {target_lang}.\n"
        "Keep the translation faithful to the source. Do not reason or explain.\n"
        "Output only the translation, preferably one concise subtitle line and at most two short lines."
    ),
    "natural": DEFAULT_PROMPT,
    "explain": EXPLAIN_PROMPT,
    "polish": POLISH_PROMPT,
    # Keep legacy preset keys so existing saved settings and UI keep working.
    "daily": DEFAULT_PROMPT,
    "esports": (
        "You are a real-time subtitle translator for esports/gaming live streams. "
        "Translate {source_lang} into {target_lang}.\n"
        "Rules:\n"
        "- Output ONLY one single best translation, nothing else.\n"
        "- Never include alternatives, parenthetical options, annotations, or explanations.\n"
        "- Keep player names (IGN), team names, game terms, and brand names untranslated.\n"
        "- Use energetic, concise language appropriate for competitive gaming commentary.\n"
        "- Auto-correct likely ASR errors based on context and common sense."
    ),
    "anime": (
        "You are a real-time subtitle translator for anime, movies, and TV shows. "
        "Translate {source_lang} into {target_lang}.\n"
        "Rules:\n"
        "- Output ONLY one single best translation, nothing else.\n"
        "- Never include alternatives, parenthetical options, annotations, or explanations.\n"
        "- Keep character names, place names, and cultural terms untranslated.\n"
        "- Use natural, expressive language that matches the tone and emotion of the dialogue.\n"
        "- Auto-correct likely ASR errors based on context and common sense."
    ),
}


def normalize_glossary(glossary) -> dict[str, str]:
    """Coerce arbitrary glossary input into a clean {term: translation} dict.

    Accepts a dict or a list of [term, translation] pairs. Entries with an empty
    term or empty translation are dropped. Both sides are stripped.
    """
    items = []
    if isinstance(glossary, dict):
        items = list(glossary.items())
    elif isinstance(glossary, (list, tuple)):
        for entry in glossary:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                items.append((entry[0], entry[1]))
            elif isinstance(entry, dict) and "term" in entry:
                items.append((entry.get("term"), entry.get("translation")))
    clean: dict[str, str] = {}
    for term, translation in items:
        term = str(term).strip() if term is not None else ""
        translation = str(translation).strip() if translation is not None else ""
        if term and translation:
            clean[term] = translation
    return clean


def match_glossary(text: str, glossary: dict[str, str]) -> dict[str, str]:
    """Return only the glossary entries whose term appears in `text`.

    Matching is case-insensitive so the injected hints stay small and relevant
    instead of dumping the whole glossary into every request.
    """
    if not text or not glossary:
        return {}
    lowered = text.lower()
    return {
        term: translation
        for term, translation in glossary.items()
        if term.lower() in lowered
    }


def render_glossary_hint(matched: dict[str, str]) -> str:
    """Render matched glossary entries as a prompt instruction block."""
    if not matched:
        return ""
    lines = "\n".join(f"- {term} => {translation}" for term, translation in matched.items())
    return (
        "Glossary (use these exact translations for the following terms):\n" + lines
    )


class RepetitionError(Exception):
    """Raised when model output contains repetition loops."""

    pass


def subtitle_error_label(exc: Exception) -> str:
    """Map a translation exception to a short, human-readable i18n key.

    The subtitle strip is small, so instead of dumping a raw exception like
    ``[error: Error code: 401 ...]`` we show a brief category label (the full
    technical reason is still logged). Exceptions arriving here are typically
    already normalized by ``providers.openai_compatible.readable_provider_error``
    (PermissionError / ConnectionError / TimeoutError / RuntimeError), so we
    classify on both the exception type and message text.

    Returns an i18n key; the caller passes it through ``t()``.
    """
    from i18n import t

    name = exc.__class__.__name__.lower()
    msg = str(exc).lower()

    if isinstance(exc, TimeoutError) or "timeout" in name or "超时" in str(exc):
        key = "error_tl_timeout"
    elif isinstance(exc, (ConnectionError,)) or "connection" in name or "连接" in str(exc):
        key = "error_tl_network"
    elif isinstance(exc, PermissionError) or "auth" in name or "permission" in name \
            or "api key" in msg or "401" in msg or "403" in msg:
        key = "error_tl_auth"
    elif "ratelimit" in name or "429" in msg or "额度" in str(exc) or "限流" in str(exc) \
            or "频繁" in str(exc):
        key = "error_tl_ratelimit"
    elif any(code in msg for code in ("500", "502", "503", "504")) \
            or "服务暂时不可用" in str(exc) or "服务端" in str(exc):
        key = "error_tl_server"
    else:
        key = "error_tl_generic"
    return t(key)


class Translator:
    """Translation facade used by the app; LLM calls go through a provider."""

    def __init__(
        self,
        api_base,
        api_key,
        model,
        target_language="zh",
        max_tokens=256,
        temperature=0.3,
        streaming=True,
        system_prompt=None,
        proxy="none",
        no_system_role=False,
        no_think=False,
        json_response=False,
        timeout=10,
        overrides=None,
        extra_body=None,
        mode="natural",
        provider_type="openai-compatible",
        provider=None,
        glossary=None,
    ):
        self._provider_type = provider_type or "openai-compatible"
        self._provider = provider or self._create_provider(
            provider_type=self._provider_type,
            api_base=api_base,
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            streaming=streaming,
            proxy=proxy,
            no_system_role=no_system_role,
            no_think=no_think,
            json_response=json_response,
            timeout=timeout,
            overrides=overrides,
            extra_body=extra_body,
        )
        self._target_language = target_language
        self._system_prompt_template = system_prompt or PROMPT_PRESETS.get(
            mode, DEFAULT_PROMPT
        )
        if _looks_mojibake(self._system_prompt_template):
            log.warning("Ignoring mojibake system prompt; using built-in subtitle prompt")
            self._system_prompt_template = DEFAULT_PROMPT
        self._mode = mode
        self._no_think = no_think
        self._glossary = normalize_glossary(glossary)
        self._context_turns = 0
        self._history = []
        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        self._init_kwargs = {
            "api_base": api_base,
            "api_key": api_key,
            "model": model,
            "target_language": target_language,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "streaming": streaming,
            "system_prompt": system_prompt,
            "proxy": proxy,
            "no_system_role": no_system_role,
            "no_think": no_think,
            "json_response": json_response,
            "timeout": timeout,
            "overrides": overrides,
            "extra_body": extra_body,
            "mode": mode,
            "provider_type": provider_type,
            "glossary": glossary,
        }

    @staticmethod
    def _create_provider(provider_type: str, **kwargs):
        if provider_type == "ollama":
            return OllamaProvider(
                base_url=kwargs["api_base"] or "http://localhost:11434/api/chat",
                model=kwargs["model"],
                temperature=kwargs["temperature"],
                streaming=kwargs["streaming"],
                timeout=kwargs["timeout"],
                extra_body=kwargs.get("extra_body"),
            )
        return OpenAICompatibleProvider(
            api_base=kwargs["api_base"],
            api_key=kwargs["api_key"],
            model=kwargs["model"],
            max_tokens=kwargs["max_tokens"],
            temperature=kwargs["temperature"],
            streaming=kwargs["streaming"],
            proxy=kwargs["proxy"],
            no_system_role=kwargs["no_system_role"],
            no_think=kwargs["no_think"],
            json_response=kwargs["json_response"],
            timeout=kwargs["timeout"],
            overrides=kwargs.get("overrides"),
            extra_body=kwargs.get("extra_body"),
        )

    @property
    def last_usage(self):
        """(prompt_tokens, completion_tokens) from last translate call."""
        return self._last_prompt_tokens, self._last_completion_tokens

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "name", self._provider.__class__.__name__)

    @property
    def model(self) -> str | None:
        return getattr(self._provider, "model", self._init_kwargs.get("model"))

    @property
    def mode(self) -> str:
        return self._mode

    def set_target_language(self, target_language: str):
        self._target_language = target_language
        self._init_kwargs["target_language"] = target_language

    def set_timeout(self, timeout: int):
        if hasattr(self._provider, "set_timeout"):
            self._provider.set_timeout(timeout)
        self._init_kwargs["timeout"] = timeout

    def set_context_turns(self, n: int):
        self._context_turns = n
        if n == 0:
            self._history.clear()

    def set_glossary(self, glossary):
        self._glossary = normalize_glossary(glossary)
        self._init_kwargs["glossary"] = glossary

    def clear_history(self):
        self._history.clear()

    def with_target_language(self, target_language: str) -> "Translator":
        kwargs = dict(self._init_kwargs)
        kwargs["target_language"] = target_language
        t = Translator(**kwargs)
        t.set_context_turns(0)
        return t

    def _build_system_prompt(self, source_lang, text: str = ""):
        src = LANGUAGE_DISPLAY.get(source_lang, source_lang)
        tgt = LANGUAGE_DISPLAY.get(self._target_language, self._target_language)
        try:
            prompt = self._system_prompt_template.format(
                source_lang=src,
                target_lang=tgt,
                context="",
            )
        except (KeyError, IndexError, ValueError) as e:
            log.warning("Bad prompt template, falling back to default: %s", e)
            prompt = DEFAULT_PROMPT.format(
                source_lang=src,
                target_lang=tgt,
                context="",
            )
        if self._no_think and "Realtime low-latency rule:" not in prompt:
            prompt = f"{prompt.rstrip()}\n\n{_NO_THINK_SUBTITLE_GUARD}"
        return prompt

    def _build_input(self, text: str, source_language: str) -> TranslateInput:
        context = self._history[-self._context_turns :] if self._context_turns > 0 else []
        current_text = compact_live_subtitle_text(text)
        hint = render_glossary_hint(match_glossary(current_text, self._glossary))
        sections = []
        if hint:
            sections.append(hint)
        sections.append(f"CURRENT SUBTITLE:\n{current_text}")
        if context:
            ctx = "\n".join(
                f"- source: {compact_live_subtitle_text(src_text, _MAX_CONTEXT_SUBTITLE_CHARS)}\n"
                f"  translation: {compact_live_subtitle_text(translated_text, _MAX_CONTEXT_SUBTITLE_CHARS)}"
                for src_text, translated_text in context
            )
            sections.append(f"RECENT CONTEXT:\n{ctx}")
        input_text = "\n\n".join(sections)
        return TranslateInput(
            text=input_text,
            source_language=source_language,
            target_language=self._target_language,
            mode=self._mode,
            context=[],
            system_prompt=self._build_system_prompt(source_language, text),
        )

    def _append_history(self, text, result):
        if self._context_turns > 0 and result:
            self._history.append((text, result))
            max_keep = self._context_turns + 2
            if len(self._history) > max_keep:
                self._history = self._history[-self._context_turns :]

    def translate(self, text: str, source_language: str = "en"):
        input_data = self._build_input(text, source_language)
        result = self._provider.translate(input_data).translation
        self._last_prompt_tokens, self._last_completion_tokens = self._provider.last_usage
        if self._check_repetition(result):
            raise RepetitionError(result)
        self._append_history(text, result)
        return result

    def translate_iter(self, text: str, source_language: str = "en"):
        """Yield accumulated partial text, then final result."""
        input_data = self._build_input(text, source_language)
        result = None
        for partial in self._provider.stream_translate(input_data):
            result = partial
            yield partial
        result = result or ""
        self._last_prompt_tokens, self._last_completion_tokens = self._provider.last_usage
        if self._check_repetition(result):
            raise RepetitionError(result)
        self._append_history(text, result)

    @staticmethod
    def _check_repetition(text: str) -> bool:
        """Detect repetition loops in model output."""
        if not text or len(text) < 40:
            return False
        for plen in range(8, len(text) // 2 + 1):
            if text[plen : plen * 2] == text[:plen]:
                return True
        return False


def test_connection(model_config: dict, timeout: int = 10) -> dict:
    """Probe a model configuration with a tiny translation request.

    Returns {"ok": bool, "message": str, "detail": str}. Never raises; provider
    errors are converted to readable messages via the provider layer's existing
    error classification (PermissionError / TimeoutError / ConnectionError / ...).
    """
    model_config = model_config or {}
    api_base = (model_config.get("api_base") or "").strip()
    model = (model_config.get("model") or "").strip()
    provider_type = model_config.get("provider", "openai-compatible")

    if provider_type != "ollama" and not api_base:
        return {"ok": False, "message": "缺少 API Base URL", "detail": ""}
    if not model:
        return {"ok": False, "message": "缺少模型名称", "detail": ""}

    try:
        translator = Translator(
            api_base=api_base,
            api_key=model_config.get("api_key", ""),
            model=model,
            provider_type=provider_type,
            target_language="zh",
            max_tokens=16,
            temperature=0.0,
            streaming=False,
            proxy=model_config.get("proxy", "none"),
            no_system_role=model_config.get("no_system_role", False),
            no_think=model_config.get("no_think", True),
            json_response=False,
            timeout=timeout,
            overrides=model_config.get("overrides"),
            extra_body=model_config.get("extra_body"),
            mode="literal",
        )
        result = translator.translate("hello", "en")
    except Exception as exc:  # noqa: BLE001 - surface readable message to UI
        return {"ok": False, "message": str(exc), "detail": exc.__class__.__name__}

    text = (result or "").strip()
    if not text:
        return {"ok": False, "message": "连接成功但模型返回空内容", "detail": ""}
    return {"ok": True, "message": "连接成功", "detail": text[:80]}
