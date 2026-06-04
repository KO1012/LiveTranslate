from pathlib import Path

import yaml

from i18n import SUPPORTED_UI_LANGUAGES, get_lang, set_lang, t


ROOT = Path(__file__).resolve().parents[1]


def _load_locale(code: str) -> dict:
    return yaml.safe_load((ROOT / "i18n" / f"{code}.yaml").read_text("utf-8")) or {}


def test_supported_ui_locales_have_complete_keysets():
    base_keys = set(_load_locale("en"))

    for code, _name in SUPPORTED_UI_LANGUAGES:
        locale = _load_locale(code)
        assert set(locale) == base_keys


def test_supported_ui_locales_load_and_fallback_unknown_language():
    for code, _name in SUPPORTED_UI_LANGUAGES:
        set_lang(code)
        assert get_lang() == code
        assert t("settings") != "settings"

    set_lang("not-a-language")
    assert get_lang() == "en"
    assert t("settings") == "Settings"
