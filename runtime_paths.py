from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parent


def data_root() -> Path:
    override = os.environ.get("LIVETRANSLATE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata).expanduser().resolve() / "LiveTranslate"
        return Path.home() / "AppData" / "Roaming" / "LiveTranslate"
    return resource_root()


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)


def data_path(*parts: str) -> Path:
    return data_root().joinpath(*parts)


def migrate_legacy_data_root():
    """Move old exe-adjacent runtime data to the stable per-user data folder."""
    if os.environ.get("LIVETRANSLATE_DATA_DIR") or not getattr(sys, "frozen", False):
        return
    old_root = Path(sys.executable).resolve().parent
    new_root = data_root()
    if old_root == new_root:
        return

    for name in ("user_settings.json", "data", "logs", "transcripts", "models"):
        src = old_root / name
        dst = new_root / name
        if not src.exists() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        except OSError:
            pass
