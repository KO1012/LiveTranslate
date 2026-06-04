import locale

import yaml

from runtime_paths import resource_path

_strings: dict = {}
_fallback_strings: dict = {}
_lang = "en"
_dir = resource_path("i18n")


# UI languages supported by the settings/control-panel interface.
SUPPORTED_UI_LANGUAGES = [
    ("en", "English"),
    ("zh", "中文"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("fr", "Français"),
    ("es", "Español"),
    ("pt", "Português"),
]
SUPPORTED_UI_LANG_CODES = {code for code, _name in SUPPORTED_UI_LANGUAGES}


def _detect_system_lang() -> str:
    """Return the best supported UI language for the system locale."""
    try:
        lang_code = (locale.getlocale()[0] or "").split("_", 1)[0].lower()
        if lang_code in SUPPORTED_UI_LANG_CODES:
            return lang_code
    except Exception:
        pass
    return "en"


def _load_yaml(lang: str) -> dict:
    f = _dir / f"{lang}.yaml"
    if not f.exists():
        return {}
    return yaml.safe_load(f.read_text("utf-8")) or {}


def set_lang(lang: str):
    global _lang, _strings, _fallback_strings
    normalized = (lang or "en").lower()
    if normalized not in SUPPORTED_UI_LANG_CODES:
        normalized = "en"
    _fallback_strings = _load_yaml("en")
    _strings = {**_fallback_strings, **_load_yaml(normalized)}
    _lang = normalized


def get_lang() -> str:
    return _lang


def t(key: str) -> str:
    return _strings.get(key, key)


# Detect system language on import.
set_lang(_detect_system_lang())


# Shared translation/subtitle language list: (code, native_name).
LANGUAGES = [
    ("auto", None),  # display name comes from t("asr_lang_auto")
    ("ja", "日本語"),
    ("en", "English"),
    ("zh", "中文"),
    ("ko", "한국어"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("ru", "Русский"),
    ("pt", "Português"),
    ("it", "Italiano"),
    ("nl", "Nederlands"),
    ("pl", "Polski"),
    ("tr", "Türkçe"),
    ("ar", "العربية"),
    ("th", "ไทย"),
    ("vi", "Tiếng Việt"),
    ("id", "Bahasa Indonesia"),
    ("ms", "Bahasa Melayu"),
    ("hi", "हिन्दी"),
    ("uk", "Українська"),
    ("cs", "Čeština"),
    ("ro", "Română"),
    ("el", "Ελληνικά"),
    ("hu", "Magyar"),
    ("sv", "Svenska"),
    ("da", "Dansk"),
    ("fi", "Suomi"),
    ("no", "Norsk"),
    ("he", "עברית"),
]


# Common languages shown directly in tray menu (no submenu).
COMMON_LANG_CODES = {"auto", "ja", "en", "zh", "ko", "fr", "de", "es", "pt", "ru"}
