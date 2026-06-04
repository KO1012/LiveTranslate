from __future__ import annotations

import json
import platform
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from logging_utils import redact_secret_data, redact_secret_text
from model_manager import get_models_dir
from runtime_paths import data_root, resource_root


def create_diagnostic_bundle(
    output_path: str | Path,
    project_root: str | Path | None = None,
    resource_base: str | Path | None = None,
    max_logs: int = 3,
) -> Path:
    data_base = Path(project_root) if project_root is not None else data_root()
    resources = Path(resource_base) if resource_base is not None else (
        data_base if project_root is not None else resource_root()
    )
    bundle_path = Path(output_path)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_json(zf, "diagnostics/summary.json", _build_summary(data_base, resources))
        _write_redacted_yaml(zf, resources / "config.yaml", "diagnostics/config.redacted.yaml")
        _write_redacted_json(
            zf,
            data_base / "user_settings.json",
            "diagnostics/user_settings.redacted.json",
        )
        _write_recent_logs(zf, data_base / "logs", max_logs=max_logs)

    return bundle_path


def _build_summary(data_base: Path, resources: Path) -> dict[str, Any]:
    paths = {
        "config": resources / "config.yaml",
        "user_settings": data_base / "user_settings.json",
        "history_db": data_base / "data" / "history.db",
        "browser_extension": resources / "browser-extension" / "manifest.json",
        "logs": data_base / "logs",
        "prompts": resources / "prompts",
        "models": get_models_dir(),
        "dist": data_base / "dist" / "LiveTranslate",
    }
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "audio_provider": _audio_provider_name(),
        "packaged": bool(getattr(sys, "frozen", False)),
        "data_root": str(data_base),
        "resource_root": str(resources),
        "paths": {
            name: {"path": str(path), "exists": path.exists()}
            for name, path in paths.items()
        },
    }


def _audio_provider_name() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "WindowsWasapiCaptureProvider"
    if system == "darwin":
        return "MacOSAudioCaptureProviderPlaceholder"
    return f"unsupported:{platform.system()}"


def _write_json(zf: zipfile.ZipFile, arcname: str, data: Any):
    zf.writestr(
        arcname,
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _write_redacted_yaml(zf: zipfile.ZipFile, path: Path, arcname: str):
    if not path.exists():
        return
    try:
        data = yaml.safe_load(path.read_text("utf-8"))
        redacted = _redact_data(data)
        content = yaml.safe_dump(redacted, allow_unicode=True, sort_keys=False)
    except Exception:
        content = redact_secret_text(path.read_text("utf-8", errors="replace"))
    zf.writestr(arcname, content.encode("utf-8"))


def _write_redacted_json(zf: zipfile.ZipFile, path: Path, arcname: str):
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text("utf-8"))
        _write_json(zf, arcname, _redact_data(data))
    except Exception:
        content = redact_secret_text(path.read_text("utf-8", errors="replace"))
        zf.writestr(arcname, content.encode("utf-8"))


def _write_recent_logs(zf: zipfile.ZipFile, log_dir: Path, max_logs: int):
    if not log_dir.exists():
        return
    logs = sorted(
        (p for p in log_dir.glob("*.log") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_logs]
    for path in logs:
        text = path.read_text("utf-8", errors="replace")
        zf.writestr(
            f"diagnostics/logs/{path.name}",
            redact_secret_text(text).encode("utf-8"),
        )


def _redact_data(value: Any) -> Any:
    return redact_secret_data(value)
