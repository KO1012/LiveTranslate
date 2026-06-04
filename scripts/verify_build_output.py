from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_OUTPUTS = (
    "LiveTranslate.exe",
    "_internal/config.yaml",
    "_internal/assets",
    "_internal/i18n",
    "_internal/prompts",
    "_internal/funasr_nano",
    "_internal/browser-extension",
    "_internal/docs",
    "_internal/screenshot",
)

FORBIDDEN_OUTPUTS = (
    "user_settings.json",
    "data",
    "logs",
    "transcripts",
    "_internal/user_settings.json",
    "_internal/data",
    "_internal/logs",
    "_internal/transcripts",
)


def verify_build_output(dist_dir: Path | None = None):
    dist_dir = dist_dir or (ROOT / "dist" / "LiveTranslate")
    if not dist_dir.exists():
        raise AssertionError(f"Build output not found: {dist_dir}")

    missing = [path for path in REQUIRED_OUTPUTS if not (dist_dir / path).exists()]
    if missing:
        raise AssertionError(f"Build output missing: {', '.join(missing)}")

    leaked = [path for path in FORBIDDEN_OUTPUTS if (dist_dir / path).exists()]
    if leaked:
        raise AssertionError(f"Build output contains user data: {', '.join(leaked)}")


def main():
    dist_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    verify_build_output(dist_dir)
    print("build output verification passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"build output verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
