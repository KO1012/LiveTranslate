from __future__ import annotations

import argparse
import importlib.util
import sys


DEPENDENCIES = (
    ("PyQt6", "PyQt6", True),
    ("psutil", "psutil", True),
    ("PyYAML", "yaml", True),
    ("numpy", "numpy", True),
    ("httpx", "httpx", True),
    ("openai", "openai", True),
    ("PyAudioWPatch", "pyaudiowpatch", True),
    ("faster-whisper", "faster_whisper", True),
    ("torch", "torch", True),
    ("torchaudio", "torchaudio", True),
    ("modelscope", "modelscope", True),
    ("huggingface_hub", "huggingface_hub", True),
    ("omegaconf", "omegaconf", True),
    ("kaldiio", "kaldiio", True),
    ("torch-complex", "torch_complex", True),
    ("soundfile", "soundfile", True),
    ("librosa", "librosa", True),
    ("jaconv", "jaconv", True),
    ("jamo", "jamo", True),
    ("hydra-core", "hydra", True),
    ("sentencepiece", "sentencepiece", True),
    ("tiktoken", "tiktoken", True),
    ("transformers", "transformers", True),
    ("funasr", "funasr", True),
)


def check_dependencies() -> list[tuple[str, str, bool]]:
    missing = []
    for package, module, required in DEPENDENCIES:
        if importlib.util.find_spec(module) is None:
            missing.append((package, module, required))
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Check LiveTranslate runtime dependencies")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any required runtime dependency is missing",
    )
    args = parser.parse_args()

    missing = check_dependencies()
    if not missing:
        print("dependency check passed")
        return 0

    print("Missing dependencies:")
    for package, module, required in missing:
        label = "required" if required else "optional"
        print(f"  - {package} (module: {module}, {label})")

    print("\nInstall the normal runtime environment first:")
    print("  .\\install.bat")
    print("or manually:")
    print("  python -m pip install -r requirements.txt")
    print("  python -m pip install funasr --no-deps")

    if args.strict and any(required for _package, _module, required in missing):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
