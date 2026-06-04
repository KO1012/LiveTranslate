"""Unit tests for translator pure logic: glossary, repetition, connection probe.

These tests avoid GUI, torch, and real network. They run fast and cover the
parts most prone to silent regressions.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from providers.base import LLMProvider, TranslateInput, TranslateResult  # noqa: E402
from providers.openai_compatible import make_openai_client  # noqa: E402
import translator as tr  # noqa: E402


# ── Glossary normalization ──────────────────────────────────────────────

def test_normalize_glossary_from_dict():
    out = tr.normalize_glossary({"Mikasa": "三笠", " Titan ": " 巨人 "})
    assert out == {"Mikasa": "三笠", "Titan": "巨人"}


def test_normalize_glossary_from_pairs():
    out = tr.normalize_glossary([["Mikasa", "三笠"], ["Eren", "艾伦"]])
    assert out == {"Mikasa": "三笠", "Eren": "艾伦"}


def test_normalize_glossary_drops_empty_entries():
    out = tr.normalize_glossary({"": "x", "y": "", "Ok": "好"})
    assert out == {"Ok": "好"}


def test_normalize_glossary_handles_none():
    assert tr.normalize_glossary(None) == {}


# ── Glossary matching (only inject terms present in the text) ────────────

def test_match_glossary_only_present_terms():
    glossary = {"Mikasa": "三笠", "Eren": "艾伦"}
    assert tr.match_glossary("Mikasa is here", glossary) == {"Mikasa": "三笠"}


def test_match_glossary_case_insensitive():
    assert tr.match_glossary("MIKASA runs", {"Mikasa": "三笠"}) == {"Mikasa": "三笠"}


def test_match_glossary_empty_text():
    assert tr.match_glossary("", {"Mikasa": "三笠"}) == {}


def test_render_glossary_hint_empty():
    assert tr.render_glossary_hint({}) == ""


def test_render_glossary_hint_format():
    hint = tr.render_glossary_hint({"Mikasa": "三笠"})
    assert "Mikasa => 三笠" in hint
    assert "Glossary" in hint


# ── Glossary injection into the system prompt ────────────────────────────

class _FakeProvider(LLMProvider):
    """Captures the TranslateInput it receives so we can assert on prompts."""

    name = "fake"

    def __init__(self, reply="译文"):
        self._reply = reply
        self.last_input: TranslateInput | None = None

    def translate(self, input_data: TranslateInput) -> TranslateResult:
        self.last_input = input_data
        return TranslateResult(original=input_data.text, translation=self._reply)

    @property
    def last_usage(self):
        return 0, 0


def _translator_with(provider, **kwargs):
    return tr.Translator(
        api_base="x",
        api_key="x",
        model="m",
        provider=provider,
        **kwargs,
    )


def test_glossary_injected_when_term_present():
    fake = _FakeProvider()
    t = _translator_with(fake, glossary={"Mikasa": "三笠"})
    t.translate("Mikasa fights", "en")
    assert "Mikasa => 三笠" in fake.last_input.text


def test_glossary_not_injected_when_term_absent():
    fake = _FakeProvider()
    t = _translator_with(fake, glossary={"Mikasa": "三笠"})
    t.translate("nobody here", "en")
    assert "Glossary" not in fake.last_input.text


def test_set_glossary_updates_behavior():
    fake = _FakeProvider()
    t = _translator_with(fake)
    t.translate("Eren runs", "en")
    assert "Glossary" not in fake.last_input.text
    t.set_glossary({"Eren": "艾伦"})
    t.translate("Eren runs", "en")
    assert "Eren => 艾伦" in fake.last_input.text


# ── Repetition detection ─────────────────────────────────────────────────

def test_context_does_not_change_system_prompt_prefix():
    fake = _FakeProvider()
    t = _translator_with(
        fake,
        system_prompt="Translate {source_lang} to {target_lang}. Context: {context}",
    )
    t.set_context_turns(4)
    t.translate("first fragment", "en")
    first_prompt = fake.last_input.system_prompt
    t.translate("second fragment", "en")
    assert fake.last_input.system_prompt == first_prompt
    assert fake.last_input.context == []
    assert "CURRENT SUBTITLE:\nsecond fragment" in fake.last_input.text
    assert "RECENT CONTEXT:" in fake.last_input.text
    assert "first fragment" in fake.last_input.text


def test_live_prompt_compacts_current_subtitle_tail():
    fake = _FakeProvider()
    t = _translator_with(fake)
    old = "Old context sentence " + ("x" * 90) + ". "
    middle = "Middle context sentence " + ("y" * 90) + ". "
    newest = "Newest subtitle sentence should remain."

    t.translate(old + middle + newest, "en")

    assert "CURRENT SUBTITLE:" in fake.last_input.text
    assert "Old context sentence" not in fake.last_input.text
    assert "Newest subtitle sentence should remain." in fake.last_input.text


def test_no_think_adds_low_latency_prompt_guard():
    fake = _FakeProvider()
    t = _translator_with(
        fake,
        no_think=True,
        system_prompt="Translate {source_lang} to {target_lang}.",
    )

    t.translate("hello", "en")

    assert "Realtime low-latency rule:" in fake.last_input.system_prompt
    assert "do not reason" in fake.last_input.system_prompt


def test_mojibake_system_prompt_falls_back():
    fake = _FakeProvider()
    t = _translator_with(fake, system_prompt="浣犳槸 锛 銆 歿 俓n")
    t.translate("hello", "en")
    assert "浣犳槸" not in (fake.last_input.system_prompt or "")


def test_repetition_detected():
    assert tr.Translator._check_repetition("abcdefgh" * 6) is True


def test_repetition_short_text_ok():
    assert tr.Translator._check_repetition("hello world") is False


# ── Empty API key must not crash client construction (regression) ────────

def test_make_openai_client_empty_key_does_not_raise():
    # Newer openai>=1.x raises on empty api_key; make_openai_client must
    # substitute a placeholder so local/no-auth servers still work.
    client = make_openai_client("http://localhost:1234/v1", "", proxy="none")
    assert client is not None


# ── test_connection: structured, never-raises probe ──────────────────────

def test_connection_missing_api_base():
    res = tr.test_connection({"model": "m", "provider": "openai-compatible"})
    assert res["ok"] is False
    assert "API Base" in res["message"]


def test_connection_missing_model():
    res = tr.test_connection({"api_base": "http://x/v1", "model": ""})
    assert res["ok"] is False
    assert "模型" in res["message"]


def test_connection_success_with_fake(monkeypatch):
    fake = _FakeProvider(reply="你好")

    def _fake_create_provider(provider_type, **kwargs):
        return fake

    monkeypatch.setattr(tr.Translator, "_create_provider", staticmethod(_fake_create_provider))
    res = tr.test_connection({"api_base": "http://x/v1", "model": "m"})
    assert res["ok"] is True
    assert res["detail"] == "你好"


def test_connection_provider_error_is_readable(monkeypatch):
    class _BoomProvider(LLMProvider):
        name = "boom"

        def translate(self, input_data):
            raise PermissionError("API Key 错误或没有访问权限")

        @property
        def last_usage(self):
            return 0, 0

    def _fake_create_provider(provider_type, **kwargs):
        return _BoomProvider()

    monkeypatch.setattr(tr.Translator, "_create_provider", staticmethod(_fake_create_provider))
    res = tr.test_connection({"api_base": "http://x/v1", "model": "m"})
    assert res["ok"] is False
    assert "API Key" in res["message"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
