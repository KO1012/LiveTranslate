from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_windows.ps1"

REQUIRED_PATHS = (
    "main.py",
    "config.yaml",
    "assets",
    "assets/startup-splash.png",
    "i18n",
    "prompts",
    "funasr_nano",
    "browser-extension",
    "docs",
    "screenshot",
    "providers",
    "asr_providers",
    "audio_providers",
    "selection_translate",
    "history",
    "exporters",
    "diagnostics.py",
    "logging_utils.py",
    "runtime_paths.py",
    "settings_utils.py",
    "scripts/dependency_check.py",
    "scripts/runtime_acceptance.py",
)

PACKAGED_RUNTIME_RESOURCES = (
    "config.yaml",
    "assets",
    "i18n",
    "prompts",
    "funasr_nano",
    "browser-extension",
    "docs",
    "screenshot",
)

REQUIRED_HIDDEN_IMPORTS = (
    "asr_engine",
    "asr_sensevoice",
    "asr_funasr_nano",
    "asr_anime_whisper",
    "asr_providers.openai_audio",
    "asr_providers.assemblyai",
    "asr_providers.assemblyai_streaming",
    "websocket",
)

FORBIDDEN_USER_DATA = (
    "user_settings.json",
    "data",
    "logs",
    "transcripts",
    "history.db",
)

SECRET_RE = re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,}|api[_ -]?key\s*[:=]\s*[^,\s]+)")


def check_required_paths():
    missing = [path for path in REQUIRED_PATHS if not (ROOT / path).exists()]
    if missing:
        raise AssertionError(f"Missing required project paths: {', '.join(missing)}")


def check_default_config_has_no_secrets():
    config = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8")) or {}
    translation = config.get("translation") or {}
    asr_api = config.get("asr_api") or {}
    local_api = config.get("local_api") or {}
    if translation.get("api_key"):
        raise AssertionError("config.yaml must not ship with translation.api_key")
    if asr_api.get("api_key"):
        raise AssertionError("config.yaml must not ship with asr_api.api_key")
    if local_api.get("token"):
        raise AssertionError("config.yaml must not ship with local_api.token")


def check_build_script_resources():
    text = BUILD_SCRIPT.read_text("utf-8")
    for resource in PACKAGED_RUNTIME_RESOURCES:
        expected = f'"--add-data", "{resource};'
        if expected not in text:
            raise AssertionError(f"Build script does not package {resource}")
    if '"--splash", "assets\\startup-splash.png"' not in text:
        raise AssertionError("Build script missing startup splash")
    for forbidden in FORBIDDEN_USER_DATA:
        if f'"--add-data", "{forbidden};' in text:
            raise AssertionError(f"Build script must not package user data: {forbidden}")
    for module in REQUIRED_HIDDEN_IMPORTS:
        if f'"--hidden-import", "{module}"' not in text:
            raise AssertionError(f"Build script missing hidden import: {module}")


def check_docs_have_no_obvious_secrets():
    for path in (ROOT / "docs").glob("*.md"):
        text = path.read_text("utf-8", errors="replace")
        matches = [m.group(0) for m in SECRET_RE.finditer(text)]
        real_matches = [m for m in matches if "your-" not in m and "your_" not in m]
        if real_matches:
            raise AssertionError(f"Possible secret in docs: {path.name}")


def check_manifest_json():
    manifest = json.loads((ROOT / "browser-extension" / "manifest.json").read_text("utf-8"))
    if manifest.get("manifest_version") != 3:
        raise AssertionError("Chrome extension must use Manifest V3")
    permissions = set(manifest.get("permissions") or [])
    if not {"contextMenus", "storage"}.issubset(permissions):
        raise AssertionError("Chrome extension permissions are incomplete")


def main():
    checks = [
        check_required_paths,
        check_default_config_has_no_secrets,
        check_build_script_resources,
        check_docs_have_no_obvious_secrets,
        check_manifest_json,
    ]
    for check in checks:
        check()
        print(f"OK {check.__name__}")
    print("package preflight passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"package preflight failed: {exc}", file=sys.stderr)
        sys.exit(1)
