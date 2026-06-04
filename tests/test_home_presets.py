"""Unit tests for home_presets pure logic (no GUI / torch / network).

Covers is_local_asr_engine(), which the home tab relies on to decide whether
to show the read-only local-model sentinel instead of a cloud preset.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import home_presets as hp  # noqa: E402


@pytest.mark.parametrize(
    "engine",
    ["whisper", "sensevoice", "funasr-nano", "funasr-mlt-nano", "anime-whisper"],
)
def test_local_engines_are_local(engine):
    assert hp.is_local_asr_engine(engine) is True


@pytest.mark.parametrize(
    "engine",
    ["openai-audio", "assemblyai", "assemblyai-streaming"],
)
def test_cloud_engines_are_not_local(engine):
    assert hp.is_local_asr_engine(engine) is False


@pytest.mark.parametrize("engine", ["", None, "unknown-engine", "OPENAI-AUDIO"])
def test_unknown_or_empty_not_local(engine):
    # Empty/None/unknown must not be treated as local (falls back to cloud
    # presets). Cloud engine names are not local regardless of case.
    assert hp.is_local_asr_engine(engine) is False


def test_local_engines_match_cloud_presets_are_disjoint():
    # No ASR_PRESETS entry should use a local engine type (the home presets are
    # cloud-only; local engines are handled via the sentinel).
    preset_engines = {p["engine"] for p in hp.ASR_PRESETS}
    assert preset_engines.isdisjoint(hp.LOCAL_ASR_ENGINES)


def test_case_insensitive_and_whitespace():
    assert hp.is_local_asr_engine("  SenseVoice  ") is True
