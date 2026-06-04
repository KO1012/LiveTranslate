"""
LiveTranslate - Phase 0 Prototype
Real-time audio translation using WASAPI loopback + faster-whisper + LLM.
"""

import sys
import signal
import logging
import threading
import queue
import tempfile
from concurrent.futures import ThreadPoolExecutor
import yaml
import time
import numpy as np
from datetime import datetime

from runtime_paths import data_path, migrate_legacy_data_root, resource_path

migrate_legacy_data_root()

from model_manager import (
    apply_cache_env,
    get_missing_models,
    is_asr_cached,
    is_silero_cached,
    ASR_DISPLAY_NAMES,
    get_models_dir,
    set_models_dir,
)

# Set cache env BEFORE importing torch so TORCH_HOME is respected
apply_cache_env()

import os

# torch must be imported before PyQt6 to avoid DLL conflicts on Windows
import torch  # noqa: F401

from audio_providers import create_audio_capture
from vad_processor import VADProcessor
from asr_providers import ASRProviderConfig, create_asr_provider
from translator import Translator, RepetitionError, subtitle_error_label
from tts_controller import TTSController
from transcript_writer import TranscriptWriter
from history import HistoryStore
from logging_utils import add_secret_redaction
from selection_translate import LocalAPIServer, SelectionTranslateService
from selection_translate.clipboard import copy_selected_text
from selection_translate.hotkey import GlobalHotkeyManager
from selection_translate.panel import SelectionResultBridge, SelectionResultDialog

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QDialog, QMessageBox
from PyQt6.QtGui import QAction, QActionGroup, QIcon, QPixmap, QPainter, QColor, QFont
from PyQt6.QtCore import QTimer, Qt

from subtitle_overlay import SubtitleOverlay
from subtitle_window import SubtitleWindow
from log_window import LogWindow
from splash import StartupSplash
from control_panel import (
    ControlPanel,
    SETTINGS_FILE,
    _load_saved_settings,
    _save_settings,
)
from dialogs import (
    SetupWizardDialog,
    ModelDownloadDialog,
    _ModelLoadDialog,
)
from i18n import t, set_lang, LANGUAGES, COMMON_LANG_CODES


LIVE_TRANSLATION_MAX_TOKENS = 160
LIVE_MERGE_WINDOW_SECONDS = 6.0
LIVE_MERGE_MAX_CHARS = 220
LIVE_MERGE_MAX_SENTENCES = 2
_LIVE_SENTENCE_ENDS = ".!?。！？"


_LANG_ALIASES = {
    "english": "en",
    "chinese": "zh",
    "mandarin": "zh",
    "japanese": "ja",
    "korean": "ko",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "russian": "ru",
    "portuguese": "pt",
    "italian": "it",
}


def _normalize_lang_code(lang: str | None) -> str:
    code = (lang or "").strip().lower().replace("_", "-")
    if not code:
        return ""
    code = _LANG_ALIASES.get(code, code)
    if "-" in code:
        code = code.split("-", 1)[0]
    if code in ("cmn", "yue", "zho"):
        return "zh"
    return code


def _same_language(source_lang: str | None, target_lang: str | None) -> bool:
    source = _normalize_lang_code(source_lang)
    target = _normalize_lang_code(target_lang)
    if source in ("", "auto", "unknown") or target in ("", "auto", "unknown"):
        return False
    return source == target


def _live_translation_max_tokens(value) -> int:
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        tokens = 256
    return max(64, min(tokens, LIVE_TRANSLATION_MAX_TOKENS))


def _live_sentence_count(text: str) -> int:
    return sum(1 for char in (text or "") if char in _LIVE_SENTENCE_ENDS)


def _looks_like_live_tail_fragment(text: str) -> bool:
    normalized = " ".join((text or "").split())
    return bool(normalized) and len(normalized) <= 24 and not normalized.endswith(
        tuple(_LIVE_SENTENCE_ENDS)
    )


def _preferred_overlay_geometry() -> tuple[int, int, int, int]:
    screen = QApplication.primaryScreen()
    geo = screen.availableGeometry()
    width = min(860, max(560, int(geo.width() * 0.52)))
    height = 220
    x = geo.left() + (geo.width() - width) // 2
    y = geo.bottom() - height - 72
    return x, y, width, height


def _should_migrate_overlay_geometry(saved: dict) -> bool:
    if not saved or saved.get("overlay_watch_layout_v3"):
        return False
    try:
        width = int(saved.get("overlay_w") or 0)
        height = int(saved.get("overlay_h") or 0)
    except (TypeError, ValueError):
        return False
    return 0 < width <= 700 and height >= 300


def _should_skip_translation(text: str, source_lang: str | None, target_lang: str | None) -> bool:
    """Skip only when the recognized text itself looks like the target language."""
    target = _normalize_lang_code(target_lang)
    if target in ("", "auto", "unknown"):
        return False
    inferred = _infer_text_language(text, None)
    if inferred in ("", "auto", "unknown"):
        return False
    skip = inferred == target
    if not skip and _same_language(source_lang, target_lang):
        log.info(
            "ASR language matched target, but text looks different; translating "
            "(source=%s target=%s inferred=%s)",
            source_lang,
            target_lang,
            inferred,
        )
    return skip


def _infer_text_language(text: str, detected_lang: str | None = None) -> str:
    detected = _normalize_lang_code(detected_lang)
    if not text:
        return detected
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ascii_letters = sum(1 for c in text if ("a" <= c.lower() <= "z"))
    kana = sum(1 for c in text if "\u3040" <= c <= "\u30ff")
    hangul = sum(1 for c in text if "\uac00" <= c <= "\ud7af")
    if ascii_letters >= max(8, cjk * 3) and detected in ("", "auto", "unknown", "zh", "ja", "ko"):
        return "en"
    if cjk >= max(4, ascii_letters):
        return "zh"
    if kana >= 3:
        return "ja"
    if hangul >= 3:
        return "ko"
    return detected


def _ends_like_complete_sentence(text: str) -> bool:
    return bool((text or "").rstrip().endswith((".", "?", "!", "。", "？", "！", "…")))


def run_self_check() -> int:
    """Non-GUI packaged/runtime check used by build scripts."""
    checks = [
        ("config", resource_path("config.yaml").exists()),
        ("i18n", resource_path("i18n").exists()),
        ("prompts", resource_path("prompts").exists()),
        ("browser-extension", resource_path("browser-extension").exists()),
        ("docs", resource_path("docs").exists()),
    ]
    missing = [name for name, ok in checks if not ok]
    if missing:
        print(f"self-check failed: missing resources: {', '.join(missing)}")
        return 1

    try:
        config = load_config()
        if config.get("translation", {}).get("api_key"):
            print("self-check failed: default translation.api_key is not empty")
            return 1
        if config.get("asr_api", {}).get("api_key"):
            print("self-check failed: default asr_api.api_key is not empty")
            return 1
        if config.get("local_api", {}).get("token"):
            print("self-check failed: default local_api.token is not empty")
            return 1

        from diagnostics import create_diagnostic_bundle
        from exporters import export_subtitles
        from history import HistoryStore
        import zipfile

        with tempfile.TemporaryDirectory():
            store = HistoryStore(data_path("self_check", "history.db"))
            session_id = store.start_session("self-check")
            msg_id = store.add_subtitle_original(
                1,
                "hello",
                "en",
                "zh",
                start_time=0,
            )
            store.update_subtitle_translation(1, "你好", end_time=1)
            assert msg_id
            if not store.get_session_subtitles(session_id):
                print("self-check failed: subtitle history read returned no rows")
                return 1
            if export_subtitles([], "both", "srt").strip():
                print("self-check failed: empty subtitle export returned content")
                return 1
            bundle = create_diagnostic_bundle(data_path("self_check", "diag.zip"))
            if not bundle.exists():
                print("self-check failed: diagnostic bundle was not created")
                return 1
            with zipfile.ZipFile(bundle) as zf:
                names = set(zf.namelist())
            if "diagnostics/config.redacted.yaml" not in names:
                print("self-check failed: diagnostic bundle is missing config")
                return 1
    except Exception as exc:
        print(f"self-check failed: {exc}")
        return 1

    print("self-check passed")
    return 0


def setup_logging():
    log_dir = data_path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"livetrans_{datetime.now():%Y%m%d_%H%M%S}.log"

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    add_secret_redaction(file_handler)
    add_secret_redaction(console_handler)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler.setFormatter(fmt)
    console_handler.setFormatter(fmt)

    logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, console_handler])

    for noisy in (
        "httpcore",
        "httpx",
        "openai",
        "filelock",
        "huggingface_hub",
        "funasr",
        "modelscope",
        "onnxruntime",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.info(f"Log file: {log_file}")

    # FunASR/ModelScope spam the root logger; suppress after our own init log
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("LiveTranslate").setLevel(logging.DEBUG)

    _logger = logging.getLogger("LiveTranslate")

    def _excepthook(exc_type, exc_value, exc_tb):
        _logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        _logger.critical(
            f"Uncaught exception in thread {args.thread}",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_excepthook

    return _logger


log = logging.getLogger("LiveTranslate")


def _close_boot_splash():
    """Close PyInstaller's boot splash when running from a packaged build."""
    try:
        import pyi_splash
    except (ImportError, RuntimeError):
        return
    try:
        pyi_splash.close()
    except RuntimeError:
        pass


def create_app_icon() -> QIcon:
    icon_path = resource_path("assets", "app-icon.png")
    if icon_path.exists():
        return QIcon(str(icon_path))

    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(60, 130, 240))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    p.setPen(QColor(255, 255, 255))
    p.setFont(QFont("Consolas", 28, QFont.Weight.Bold))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "LT")
    p.end()
    return QIcon(pix)


def load_config():
    config_path = resource_path("config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _evaluate_setup_status(settings: dict) -> tuple[bool, bool]:
    """Decide whether ASR and LLM are ready for the welcome card.

    Returns (asr_ready, llm_ready). The welcome card shows when either is
    False. Local engines (sensevoice, whisper, ...) and local LLM endpoints
    (Ollama, LM Studio, any 127.0.0.1 base) are treated as ready without
    requiring an API key, since they don't need one.
    """
    settings = settings or {}

    asr_engine = settings.get("asr_engine", "openai-audio")
    asr_api = settings.get("asr_api") or {}
    if asr_engine in ("openai-audio", "assemblyai", "assemblyai-streaming"):
        asr_ready = bool((asr_api.get("api_key") or "").strip())
    else:
        # Local model engines don't need an API key.
        asr_ready = True

    models = settings.get("models") or []
    active = settings.get("active_model", 0)
    if 0 <= active < len(models):
        m = models[active]
        provider = (m.get("provider") or "openai-compatible").strip()
        api_base = (m.get("api_base") or "").lower()
        is_local = (
            provider == "ollama"
            or "127.0.0.1" in api_base
            or "localhost" in api_base
        )
        has_model = bool((m.get("model") or "").strip())
        has_key = bool((m.get("api_key") or "").strip())
        llm_ready = has_model and (is_local or has_key)
    else:
        llm_ready = False

    return asr_ready, llm_ready


class LiveTranslateApp:
    def __init__(self, config):
        self._config = config
        self._running = False
        self._paused = False
        self._asr_ready = False  # True when ASR model is loaded

        self._audio = create_audio_capture(
            device=config["audio"].get("device"),
            sample_rate=config["audio"]["sample_rate"],
            chunk_duration=config["audio"]["chunk_duration"],
        )
        self._vad = VADProcessor(
            sample_rate=config["audio"]["sample_rate"],
            threshold=config["asr"]["vad_threshold"],
            min_speech_duration=config["asr"]["min_speech_duration"],
            max_speech_duration=config["asr"]["max_speech_duration"],
            chunk_duration=config["audio"]["chunk_duration"],
        )
        self._asr_type = None
        self._asr = None
        self._asr_device = config["asr"]["device"]
        self._whisper_model_size = config["asr"]["model_size"]
        self._asr_lock = threading.Lock()
        self._asr_load_thread = None
        self._asr_load_error = None
        self._asr_load_generation = 0
        self._asr_loading_type = None
        self._asr_status_text = ""
        self._last_asr_error_text = ""
        self._last_asr_error_time = 0.0
        self._vad_lock = threading.Lock()
        self._target_language = config["translation"]["target_language"]
        self._translator = Translator(
            api_base=config["translation"]["api_base"],
            api_key=config["translation"]["api_key"],
            model=config["translation"]["model"],
            provider_type=config["translation"].get("provider", "openai-compatible"),
            target_language=self._target_language,
            max_tokens=_live_translation_max_tokens(config["translation"]["max_tokens"]),
            temperature=config["translation"]["temperature"],
            streaming=config["translation"]["streaming"],
            system_prompt=config["translation"].get("system_prompt"),
            no_think=config["translation"].get("no_think", True),
            mode=config["translation"].get("mode", "natural"),
        )
        self._translator.set_context_turns(config["translation"].get("context_window", 10))
        # Cache of per-language auxiliary translators for the subtitle window's
        # extra target languages. Rebuilding a Translator per segment also
        # rebuilds its httpx connection pool, so we cache and only invalidate
        # when the active model changes.
        self._aux_translators = {}
        self._aux_translators_lock = threading.Lock()
        self._overlay = None
        self._subwin = None
        self._panel = None
        self._capture_thread = None
        self._asr_thread = None
        self._asr_queue = queue.Queue(maxsize=16)
        self._tl_executor = ThreadPoolExecutor(max_workers=8)

        self._transcript = TranscriptWriter(data_path("transcripts"))
        self._history = HistoryStore(data_path("data", "history.db"))
        self._history_enabled = config.get("privacy", {}).get("history_enabled", True)
        self._active_model_config = {
            "name": config["translation"]["model"],
            "provider": config["translation"].get("provider", "openai-compatible"),
            "api_base": config["translation"]["api_base"],
            "api_key": config["translation"]["api_key"],
            "model": config["translation"]["model"],
            "streaming": config["translation"].get("streaming", True),
        }
        self._selection_service = SelectionTranslateService(
            get_model_config=self._get_active_model_config,
            get_target_language=lambda: self._target_language,
            get_timeout=self._get_timeout_setting,
            history_store=self._history,
            get_history_enabled=lambda: self._history_enabled,
        )
        self._selection_dialog = SelectionResultDialog()
        self._selection_bridge = SelectionResultBridge()
        self._selection_bridge.result_ready.connect(self._selection_dialog.show_result)
        self._selection_bridge.error_ready.connect(self._selection_dialog.show_error)
        hotkey_config = config.get("hotkey", {})
        self._hotkey_enabled = hotkey_config.get("enabled", True)
        self._hotkey = GlobalHotkeyManager(hotkey_config.get("shortcut", "Ctrl+Shift+T"))
        self._hotkey.triggered.connect(self._on_selection_hotkey)
        # Guards against re-entrancy: copy_selected_text() spins a nested Qt
        # event loop (wait_ms), so a second hotkey press while the first is
        # still reading/restoring the clipboard would interleave the two
        # clear/copy/restore sequences and corrupt the saved clipboard.
        self._selection_busy = False
        self._hotkey.failed.connect(self._selection_dialog.show_error)
        if self._hotkey_enabled:
            self._hotkey.start()
        local_api_config = config.get("local_api", {})
        self._local_api = LocalAPIServer(
            translate_callback=self._handle_selection_api_request,
            host=local_api_config.get("host", "127.0.0.1"),
            port=local_api_config.get("port", 17891),
            token=local_api_config.get("token", ""),
        )
        try:
            self._local_api.start()
        except OSError as e:
            log.warning(f"Local API failed to start: {e}")

        # Memory diagnostic state
        import psutil
        self._mem_proc = psutil.Process(os.getpid())
        self._mem_baseline_mb = self._mem_proc.memory_info().rss / 1024 / 1024
        self._mem_last_mb = self._mem_baseline_mb
        self._mem_seg_count = 0
        self._mem_periodic_timer = None
        # Memory ceiling: warn once when RSS exceeds threshold (FunASR has a
        # known C-side leak ~5MB/seg that GC can't reclaim; user restarts when needed)
        self._mem_threshold_mb = 4096
        self._mem_warned = False
        self._mem_warning_callback = None

        self._asr_count = 0
        self._translate_count = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._input_price = 0.0
        self._output_price = 0.0
        self._msg_id = 0
        self._last_original = ""
        self._last_msg_id = 0
        self._last_msg_time = 0.0
        self._last_source_lang = ""
        self._translation_versions = {}
        self._recognized_texts = {}

        # Incremental ASR state
        self._incremental_enabled = True
        self._interim_interval = 2.0
        self._interim_pending = ""
        self._interim_active = False
        self._last_interim_samples = 0
        self._last_interim_check_time = 0.0
        self._interim_committed_tail = ""

        # TTS (simultaneous interpretation) — speaks translated text aloud.
        self._tts = TTSController(config.get("tts", {}))

    def set_overlay(self, overlay: SubtitleOverlay):
        self._overlay = overlay

    def set_subtitle_window(self, subwin: SubtitleWindow):
        self._subwin = subwin

    def set_panel(self, panel: ControlPanel):
        self._panel = panel
        panel.settings_changed.connect(self._on_settings_changed)
        panel.model_changed.connect(self._on_model_changed)
        panel.models_list_changed.connect(self._on_models_list_changed)

    @property
    def is_running(self) -> bool:
        return self._running

    def _on_models_list_changed(self, models: list, active_idx: int):
        if self._overlay:
            self._overlay.set_models(models, active_idx)

    def _on_settings_changed(self, settings):
        self._vad.update_settings(settings)
        if "style" in settings and self._overlay:
            self._overlay.apply_style(settings["style"])
        if self._overlay and self._panel is not None:
            # Re-evaluate welcome card visibility on every settings change so
            # it disappears the moment the user finishes filling API keys.
            self._overlay.update_welcome_card(
                *_evaluate_setup_status(self._panel.get_settings())
            )
        if "asr_language" in settings and self._asr:
            self._asr.set_language(settings["asr_language"])
        # ASR compute device change: try in-place migration first
        new_device = settings.get("asr_device")
        if new_device and new_device != self._asr_device:
            old_device = self._asr_device
            self._asr_device = new_device
            if self._asr is not None and hasattr(self._asr, "to_device"):
                result = self._asr.to_device(new_device)
                if result is not False:
                    log.info(f"ASR device migrated: {old_device} -> {new_device}")
                    if self._overlay:
                        display_name = ASR_DISPLAY_NAMES.get(
                            self._asr_type, self._asr_type
                        )
                        self._overlay.update_asr_device(
                            f"{display_name} [{new_device}]"
                        )
                    import gc

                    gc.collect()
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                else:
                    self._asr_type = None  # ctranslate2: force reload
            else:
                self._asr_type = None  # no engine loaded: force reload
        new_whisper_size = settings.get("whisper_model_size")
        if new_whisper_size and new_whisper_size != self._whisper_model_size:
            self._whisper_model_size = new_whisper_size
            if self._asr_type == "whisper":
                self._asr_type = None
        if "asr_api" in settings and self._asr_type in (
            "openai-audio",
            "assemblyai",
            "assemblyai-streaming",
        ):
            self._asr_type = None
        if "asr_engine" in settings:
            self._switch_asr_engine(
                settings["asr_engine"],
                background=not self._running and self._asr is None,
            )
        if "audio_device" in settings:
            old_device = self._audio._device_name
            self._audio.set_device(settings["audio_device"])
            if old_device != settings.get("audio_device"):
                self._vad.flush()
                self._vad._reset()
                if self._overlay:
                    self._overlay.update_monitor(0.0, 0.0)
        if "mic_device" in settings:
            self._audio.set_mic_device(settings["mic_device"])
        if "incremental_asr" in settings:
            self._incremental_enabled = settings["incremental_asr"]
        if "interim_interval" in settings:
            self._interim_interval = settings["interim_interval"]
        if "target_language" in settings:
            self._target_language = settings["target_language"]
            if self._translator:
                self._translator.set_target_language(self._target_language)
            with self._aux_translators_lock:
                self._aux_translators.clear()
            if self._overlay:
                self._overlay.set_target_language(self._target_language)
        if "timeout" in settings and self._translator:
            self._translator.set_timeout(settings["timeout"])
        if "glossary" in settings and self._translator:
            self._translator.set_glossary(settings["glossary"])
            # Invalidate cached per-language translators so they pick up the
            # new glossary on next use.
            with self._aux_translators_lock:
                self._aux_translators.clear()
        if "auto_save_transcript" in settings:
            self._transcript.set_enabled(settings["auto_save_transcript"])
        if "hotkey" in settings:
            self._apply_hotkey_settings(settings["hotkey"])
        if "privacy" in settings:
            self._history_enabled = settings["privacy"].get("history_enabled", True)
        if "local_api" in settings:
            self._local_api.set_token(settings["local_api"].get("token", ""))
        if "tts" in settings:
            self._tts.apply_settings(settings["tts"])

    def set_tts_enabled(self, enabled: bool):
        """Immediately start/stop simultaneous interpretation (TTS).

        Used by the one-click overlay button and tray toggle so the engine
        reacts instantly, without waiting for the panel's debounced auto-save.
        """
        self._tts.set_enabled(enabled)

    def _on_target_language_changed(self, lang: str):
        self._target_language = lang
        log.info(f"Target language: {lang}")
        if self._translator:
            self._translator.set_target_language(lang)
        # Drop queued speech in the previous language.
        self._tts.clear()
        if self._panel:
            settings = self._panel.get_settings()
            settings["target_language"] = lang
            # Keep the panel's in-memory state in sync, otherwise a later
            # auto-save from the control panel would overwrite user_settings.json
            # with the stale target language and silently revert this change.
            self._panel._current_settings["target_language"] = lang
            from control_panel import _save_settings

            _save_settings(settings)

    def _on_model_changed(self, model_config: dict):
        log.info(
            f"Switching translator: {model_config['name']} ({model_config['model']})"
        )
        self._active_model_config = dict(model_config)
        prompt = None
        if self._panel:
            prompt = self._panel.get_settings().get("system_prompt")
        if not prompt:
            prompt = self._config["translation"].get("system_prompt")
        mode = self._config["translation"].get("mode", "natural")
        if self._panel:
            mode = self._panel.get_settings().get("translation_mode", mode)
        timeout = 10
        if self._panel:
            timeout = self._panel.get_settings().get("timeout", 10)
        glossary = None
        if self._panel:
            glossary = self._panel.get_settings().get("glossary")
        self._translator = Translator(
            api_base=model_config["api_base"],
            api_key=model_config.get("api_key", ""),
            model=model_config["model"],
            provider_type=model_config.get("provider", "openai-compatible"),
            target_language=self._target_language,
            max_tokens=_live_translation_max_tokens(self._config["translation"]["max_tokens"]),
            temperature=self._config["translation"]["temperature"],
            streaming=model_config.get("streaming", True),
            system_prompt=prompt,
            proxy=model_config.get("proxy", "none"),
            no_system_role=model_config.get("no_system_role", False),
            no_think=model_config.get("no_think", True),
            json_response=model_config.get("json_response", False),
            timeout=timeout,
            overrides=model_config.get("overrides"),
            extra_body=model_config.get("extra_body"),
            mode=mode,
            glossary=glossary,
        )
        self._translator.set_context_turns(
            model_config.get(
                "context_turns",
                self._config.get("translation", {}).get("context_window", 10),
            )
        )
        # Drop cached per-language translators built from the previous model.
        with self._aux_translators_lock:
            self._aux_translators.clear()
        self._input_price = model_config.get("input_price", 0)
        self._output_price = model_config.get("output_price", 0)

    def _get_active_model_config(self):
        if self._panel:
            active = self._panel.get_active_model()
            if active:
                return dict(active)
        return dict(self._active_model_config)

    def _get_timeout_setting(self):
        if self._panel:
            return self._panel.get_settings().get("timeout", 10)
        return self._config.get("translation", {}).get("timeout", 10)

    def _handle_selection_api_request(self, payload: dict) -> dict:
        return self._selection_service.translate(
            text=payload.get("text", ""),
            source=payload.get("source", "local-api"),
            url=payload.get("url"),
            mode=payload.get("mode", "explain"),
            source_language=payload.get("source_language", "auto"),
        )

    def _apply_hotkey_settings(self, hotkey_settings: dict):
        enabled = bool(hotkey_settings.get("enabled", True))
        shortcut = hotkey_settings.get("shortcut", "Ctrl+Shift+T")
        if not enabled:
            self._hotkey.stop()
            self._hotkey_enabled = False
            return
        try:
            self._hotkey.set_hotkey(shortcut)
            if not self._hotkey_enabled:
                self._hotkey.start()
            self._hotkey_enabled = True
        except ValueError as e:
            self._selection_dialog.show_error(str(e))

    def _on_selection_hotkey(self):
        # Ignore re-entrant triggers while a previous selection is still being
        # captured (copy_selected_text spins a nested event loop).
        if self._selection_busy:
            log.debug("Selection hotkey ignored: previous capture still in progress")
            return
        self._selection_busy = True
        try:
            text, error = copy_selected_text()
        finally:
            self._selection_busy = False
        if error:
            self._selection_dialog.show_error(error)
            return
        self._selection_dialog.show_loading(text)
        try:
            future = self._tl_executor.submit(
                self._selection_service.translate,
                text=text,
                source="global-hotkey",
                mode="explain",
                source_language="auto",
            )
            future.add_done_callback(self._on_selection_future_done)
        except RuntimeError as e:
            self._selection_dialog.show_error(f"翻译任务启动失败：{e}")

    def _on_selection_future_done(self, future):
        try:
            self._selection_bridge.result_ready.emit(future.result())
        except Exception as e:
            self._selection_bridge.error_ready.emit(str(e))

    def _asr_load_context(self, engine_type: str):
        device = self._asr_device
        hub = "ms"
        if self._panel:
            hub = self._panel.get_settings().get("hub", "ms")

        model_size = self._config["asr"]["model_size"]
        asr_api_config = self._config.get("asr_api", {})
        if self._panel:
            panel_settings = self._panel.get_settings()
            model_size = panel_settings.get("whisper_model_size", model_size)
            asr_api_config = panel_settings.get("asr_api", asr_api_config)

        display_name = ASR_DISPLAY_NAMES.get(engine_type, engine_type)
        if engine_type == "whisper":
            display_name = f"Whisper {model_size}"
        return device, hub, model_size, asr_api_config, display_name

    def _start_asr_load(
        self,
        *,
        engine_type: str,
        device: str,
        model_size: str,
        hub: str,
        asr_api_config: dict,
        display_name: str,
        old_engine,
    ):
        self._asr_load_generation += 1
        generation = self._asr_load_generation
        self._asr_load_error = None
        self._asr_loading_type = engine_type

        def _load():
            nonlocal old_engine
            loaded_asr = None
            try:
                if old_engine is not None:
                    log.info(
                        f"Releasing old ASR engine: {old_engine.__class__.__name__}"
                    )
                    if hasattr(old_engine, "unload"):
                        old_engine.unload()
                    old_engine = None
                loaded_asr = create_asr_provider(
                    ASRProviderConfig(
                        engine_type=engine_type,
                        device=device,
                        model_size=model_size,
                        compute_type=self._config["asr"]["compute_type"],
                        language=self._config["asr"]["language"],
                        hub=hub,
                        models_dir=get_models_dir(),
                        api_base=asr_api_config.get(
                            "api_base", "https://api.openai.com/v1"
                        ),
                        api_key=asr_api_config.get("api_key", ""),
                        api_model=asr_api_config.get("model", "whisper-1"),
                        timeout=asr_api_config.get("timeout", 30),
                    )
                )
                if self._panel:
                    asr_lang = self._panel.get_settings().get("asr_language", "auto")
                    loaded_asr.set_language(asr_lang)
            except Exception as e:
                with self._asr_lock:
                    if generation == self._asr_load_generation:
                        self._asr_load_error = str(e)
                        self._asr_loading_type = None
                        self._asr_type = None
                        self._asr_ready = False
                        self._notify_asr_error(e)
                log.error(f"Failed to load ASR engine: {e}", exc_info=True)
                return

            stale = False
            with self._asr_lock:
                if generation != self._asr_load_generation:
                    stale = True
                else:
                    self._asr = loaded_asr
                    self._asr_type = engine_type
                    self._asr_ready = True
                    self._asr_load_error = None
                    self._asr_loading_type = None

            if stale:
                if loaded_asr is not None and hasattr(loaded_asr, "unload"):
                    loaded_asr.unload()
                return

            if self._overlay:
                self._set_asr_status(f"{display_name} [{device}]")
            log.info(f"ASR engine ready: {engine_type} on {device}")

        thread = threading.Thread(target=_load, daemon=True)
        self._asr_load_thread = thread
        thread.start()
        return thread

    def _wait_for_asr_load(self, display_name: str, parent):
        thread = self._asr_load_thread
        if thread is None:
            return

        dlg = _ModelLoadDialog(
            t("loading_model").format(name=display_name), parent=parent
        )

        poll_timer = QTimer()

        def _check():
            if not thread.is_alive():
                poll_timer.stop()
                dlg.accept()

        poll_timer.setInterval(100)
        poll_timer.timeout.connect(_check)
        poll_timer.start()

        if thread.is_alive():
            dlg.exec()
        poll_timer.stop()

        if self._asr_load_error:
            QMessageBox.warning(
                parent,
                t("error_title"),
                t("error_load_asr").format(error=self._asr_load_error),
            )

    def _switch_asr_engine(self, engine_type: str, background: bool = False):
        if engine_type == self._asr_type and self._asr_ready:
            return

        device, hub, model_size, asr_api_config, display_name = self._asr_load_context(
            engine_type
        )
        parent = (
            self._panel if self._panel and self._panel.isVisible() else self._overlay
        )

        if self._asr_load_thread and self._asr_load_thread.is_alive():
            if self._asr_loading_type == engine_type:
                if background:
                    return
                self._wait_for_asr_load(display_name, parent)
                return
            self._asr_load_generation += 1

        log.info(f"Switching ASR engine: {self._asr_type} -> {engine_type}")
        self._asr_ready = False
        # Reset interim state
        self._interim_active = False
        self._interim_pending = ""
        self._last_interim_samples = 0
        self._last_interim_check_time = 0.0
        self._interim_committed_tail = ""
        # Flush and reset VAD to stop accumulating audio during engine switch
        self._vad.flush()
        self._vad._reset()

        cached = is_asr_cached(engine_type, model_size, hub)

        if not cached:
            missing = get_missing_models(engine_type, model_size, hub)
            missing = [m for m in missing if m["type"] != "silero-vad"]
            if missing:
                if background:
                    log.info(f"ASR model missing, skip background load: {engine_type}")
                    return
                dlg = ModelDownloadDialog(missing, hub=hub, parent=parent)
                if dlg.exec() != QDialog.DialogCode.Accepted:
                    log.info(f"Download cancelled/failed: {engine_type}")
                    # Restore readiness if old engine is still available
                    if self._asr is not None:
                        self._asr_ready = True
                    return

        with self._asr_lock:
            old_engine = self._asr
            self._asr = None

        if self._overlay:
            self._overlay.update_asr_device(
                f"{t('loading_model').format(name=display_name)} [{device}]"
            )
        self._start_asr_load(
            engine_type=engine_type,
            device=device,
            model_size=model_size,
            hub=hub,
            asr_api_config=asr_api_config,
            display_name=display_name,
            old_engine=old_engine,
        )
        if not background:
            self._wait_for_asr_load(display_name, parent)

    @staticmethod
    def _short_error_text(error) -> str:
        text = str(error or "").strip().splitlines()[0] if error is not None else ""
        if not text:
            text = error.__class__.__name__ if error is not None else "unknown"
        return text[:157] + "..." if len(text) > 160 else text

    def _set_asr_status(self, status: str):
        self._asr_status_text = status
        self._last_asr_error_text = ""
        if self._overlay:
            self._overlay.update_asr_device(status)

    def _notify_asr_error(self, error):
        detail = self._short_error_text(error)
        status = t("error_asr_status").format(error=detail)
        now = time.time()
        if status == self._last_asr_error_text and now - self._last_asr_error_time < 5.0:
            return
        self._last_asr_error_text = status
        self._last_asr_error_time = now
        if self._overlay:
            self._overlay.update_asr_device(status)

    def _restore_asr_status_after_error(self):
        if not self._last_asr_error_text:
            return
        self._last_asr_error_text = ""
        if self._asr_status_text and self._overlay:
            self._overlay.update_asr_device(self._asr_status_text)

    def _mem_snapshot(self) -> dict:
        rss_mb = self._mem_proc.memory_info().rss / 1024 / 1024
        gpu_alloc_mb = 0.0
        gpu_reserved_mb = 0.0
        try:
            if torch.cuda.is_available():
                gpu_alloc_mb = torch.cuda.memory_allocated() / 1024 / 1024
                gpu_reserved_mb = torch.cuda.memory_reserved() / 1024 / 1024
        except Exception:
            pass
        msgs = len(self._overlay._messages) if self._overlay else 0
        vad_buf = len(self._vad._speech_buffer)
        return {
            "rss": rss_mb,
            "gpu_alloc": gpu_alloc_mb,
            "gpu_reserved": gpu_reserved_mb,
            "msgs": msgs,
            "vad_buf": vad_buf,
        }

    def _log_mem_after_segment(self):
        self._mem_seg_count += 1
        snap = self._mem_snapshot()
        delta = snap["rss"] - self._mem_last_mb
        total_delta = snap["rss"] - self._mem_baseline_mb
        self._mem_last_mb = snap["rss"]
        log.info(
            f"MEM[seg#{self._mem_seg_count}] RSS={snap['rss']:.1f}MB "
            f"(Δ{delta:+.2f} since last, {total_delta:+.1f} since start) "
            f"GPU(alloc/reserved)={snap['gpu_alloc']:.0f}/{snap['gpu_reserved']:.0f}MB "
            f"msgs={snap['msgs']} vad_buf={snap['vad_buf']}"
        )
        self._check_memory_threshold(snap["rss"])

    def _check_memory_threshold(self, rss_mb: float):
        if self._mem_warned or rss_mb < self._mem_threshold_mb:
            return
        self._mem_warned = True
        log.warning(
            f"Memory ceiling reached: RSS={rss_mb:.0f}MB "
            f"(threshold {self._mem_threshold_mb}MB). "
            f"Recommend restarting LiveTranslate to free C-side allocator caches."
        )
        if self._mem_warning_callback is not None:
            try:
                self._mem_warning_callback(rss_mb)
            except Exception as e:
                log.warning(f"Memory warning callback failed: {e}")

    def set_memory_warning_callback(self, callback):
        self._mem_warning_callback = callback

    def _log_mem_periodic(self):
        snap = self._mem_snapshot()
        total_delta = snap["rss"] - self._mem_baseline_mb
        log.info(
            f"MEM[tick] RSS={snap['rss']:.1f}MB ({total_delta:+.1f} since start) "
            f"GPU(alloc/reserved)={snap['gpu_alloc']:.0f}/{snap['gpu_reserved']:.0f}MB "
            f"msgs={snap['msgs']} segs={self._mem_seg_count} "
            f"asr_count={self._asr_count} tl_count={self._translate_count}"
        )
        self._check_memory_threshold(snap["rss"])

    def _compute_cost(self):
        if self._input_price > 0 or self._output_price > 0:
            return (self._total_prompt_tokens * self._input_price +
                    self._total_completion_tokens * self._output_price) / 1_000_000
        return 0.0

    def _history_provider_model(self):
        if not self._translator:
            return None, None
        return self._translator.provider_name, self._translator.model

    def _record_subtitle_original(self, msg_id, original_text, source_lang):
        if not self._history_enabled:
            return
        try:
            provider, model = self._history_provider_model()
            self._history.add_subtitle_original(
                msg_id=msg_id,
                original_text=original_text,
                source_language=source_lang,
                target_language=self._target_language,
                start_time=time.time(),
                provider=provider,
                model=model,
            )
        except Exception as e:
            log.warning(f"History original write failed: {e}")

    def _record_subtitle_translation(self, msg_id, translated_text):
        if not self._history_enabled:
            return
        try:
            provider, model = self._history_provider_model()
            self._history.update_subtitle_translation(
                msg_id=msg_id,
                translated_text=translated_text,
                end_time=time.time(),
                provider=provider,
                model=model,
            )
        except Exception as e:
            log.warning(f"History translation write failed: {e}")

    def _record_subtitle_original_update(self, msg_id, original_text, source_lang):
        if not self._history_enabled:
            return
        try:
            self._history.update_subtitle_original(
                msg_id=msg_id,
                original_text=original_text,
                source_language=source_lang,
            )
        except Exception as e:
            log.warning(f"History original update failed: {e}")

    def _next_translation_version(self, msg_id: int) -> int:
        version = self._translation_versions.get(msg_id, 0) + 1
        self._translation_versions[msg_id] = version
        return version

    def _translation_is_current(self, msg_id: int, version: int) -> bool:
        return self._translation_versions.get(msg_id) == version

    def _submit_translation(self, msg_id, text, source_lang, extra_langs=None):
        version = self._next_translation_version(msg_id)
        self._tl_executor.submit(
            self._translate_async, msg_id, text, source_lang, extra_langs, version
        )

    def _translate_async(self, msg_id, text, source_lang, extra_langs=None, version=0):
        """Translate text and update UI with streaming display."""
        try:
            tl_start = time.perf_counter()
            translated = None
            # Simultaneous interpretation: speak completed sentences as they
            # stream in, instead of waiting for the whole translation.
            tts_stream = self._tts.begin_stream(source_lang, self._target_language)
            for partial in self._translator.translate_iter(text, source_lang):
                if version and not self._translation_is_current(msg_id, version):
                    log.debug("Dropping stale translation stream: msg=%s v=%s", msg_id, version)
                    return
                translated = partial
                if self._overlay:
                    self._overlay.update_streaming(msg_id, partial)
                if tts_stream is not None:
                    tts_stream.feed(partial)
            if version and not self._translation_is_current(msg_id, version):
                log.debug("Dropping stale translation result: msg=%s v=%s", msg_id, version)
                return
            if not translated:
                raise RuntimeError("API returned an empty translation")
            tl_ms = (time.perf_counter() - tl_start) * 1000
            self._translate_count += 1
            pt, ct = self._translator.last_usage
            self._total_prompt_tokens += pt
            self._total_completion_tokens += ct
            cost = self._compute_cost()
            log.info(f"Translate ({tl_ms:.0f}ms): {translated}")
            if translated:
                self._transcript.write_translation(msg_id, translated)
            else:
                self._transcript.finalize_no_translation(msg_id)
            self._record_subtitle_translation(msg_id, translated)
            if self._overlay:
                self._overlay.update_translation(msg_id, translated, tl_ms)
                self._overlay.update_stats(
                    self._asr_count,
                    self._translate_count,
                    self._total_prompt_tokens,
                    self._total_completion_tokens,
                    cost,
                )
            if translated and tts_stream is not None:
                tts_stream.finish(translated)
            if self._subwin and self._subwin.isVisible() and translated:
                tl_dict = {self._target_language: translated}
                if extra_langs:
                    self._translate_extra_langs(text, source_lang, extra_langs, tl_dict)
                self._subwin.update_text(text, tl_dict)
        except RepetitionError:
            log.warning("Repetition loop detected, model may not support structured output well")
            self._transcript.finalize_no_translation(msg_id)
            self._record_subtitle_translation(msg_id, None)
            if self._overlay:
                self._overlay.update_translation(
                    msg_id, f"[{t('error_repetition')}]", 0
                )
        except Exception as e:
            import openai
            if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError,
                              openai.AuthenticationError, openai.APIStatusError,
                              TimeoutError, ConnectionError)):
                log.warning(f"Translate error: {e}")
            else:
                log.error(f"Translate error: {e}", exc_info=True)
            self._transcript.finalize_no_translation(msg_id)
            self._record_subtitle_translation(msg_id, None)
            if self._overlay:
                self._overlay.update_translation(
                    msg_id, f"[{subtitle_error_label(e)}]", 0
                )

    def _get_aux_translator(self, lang: str):
        """Return a cached auxiliary Translator targeting ``lang``.

        Building a Translator also builds an httpx connection pool, so we cache
        one per language and reuse it across segments. The cache is cleared in
        ``_on_model_changed`` when the active model is swapped.
        """
        with self._aux_translators_lock:
            translator = self._aux_translators.get(lang)
            if translator is None:
                translator = self._translator.with_target_language(lang)
                self._aux_translators[lang] = translator
            return translator

    def _translate_extra_langs(self, text, source_lang, extra_langs, tl_dict):
        """Translate into additional languages for subtitle window (parallel).

        Uses a dedicated short-lived executor instead of ``self._tl_executor``.
        This method always runs *inside* a ``self._tl_executor`` worker and then
        blocks on its children; submitting those children back into the same
        pool can deadlock — when every worker is busy running a parent task that
        is waiting on children, no worker is left to run the children, so they
        never complete. A separate pool guarantees the children can be scheduled.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _do_translate(lang):
            translator = self._get_aux_translator(lang)
            return lang, translator.translate(text, source_lang)

        with ThreadPoolExecutor(
            max_workers=max(1, len(extra_langs)), thread_name_prefix="tl-extra"
        ) as pool:
            futures = [pool.submit(_do_translate, lang) for lang in extra_langs]
            for future in as_completed(futures):
                try:
                    lang, result = future.result()
                    tl_dict[lang] = result
                    log.info(f"Extra translate [{lang}]: {result}")
                except Exception as e:
                    import openai
                    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError,
                                      openai.AuthenticationError, openai.APIStatusError,
                                      TimeoutError, ConnectionError)):
                        log.warning(f"Extra translate error: {e}")
                    else:
                        log.error(f"Extra translate error: {e}", exc_info=True)

    def _translate_subwin_only(self, text, source_lang, extra_langs):
        """Translate only for subtitle window when primary target == source language."""
        tl_dict = {self._target_language: text}  # same language, use original
        self._translate_extra_langs(text, source_lang, extra_langs, tl_dict)
        if self._subwin and self._subwin.isVisible():
            self._subwin.update_text(text, tl_dict)

    def _should_merge_fragment(self, text: str, source_lang: str) -> bool:
        if not self._last_msg_id or self._last_msg_id not in self._recognized_texts:
            return False
        if not _same_language(self._last_source_lang, source_lang):
            return False
        if time.time() - self._last_msg_time > LIVE_MERGE_WINDOW_SECONDS:
            return False
        prev = self._recognized_texts.get(self._last_msg_id, "")
        merged = f"{prev} {text}".strip()
        if not prev or len(merged) > LIVE_MERGE_MAX_CHARS:
            return False
        if _live_sentence_count(prev) >= LIVE_MERGE_MAX_SENTENCES:
            return False
        if prev.rstrip().endswith(tuple(_LIVE_SENTENCE_ENDS)) and not (
            _looks_like_live_tail_fragment(text)
        ):
            return False
        return True

    def _handle_recognized_text(self, original_text, source_lang, asr_ms, timestamp):
        """Add or revise a live subtitle, then translate unless source == target."""
        inferred_lang = _infer_text_language(original_text, source_lang)
        if inferred_lang and inferred_lang != _normalize_lang_code(source_lang):
            log.info("Language corrected by text heuristic: %s -> %s", source_lang, inferred_lang)
            source_lang = inferred_lang
        target_lang = self._target_language
        extra_langs = set()
        if self._subwin and self._subwin.isVisible():
            subwin_langs = self._subwin.get_target_languages()
            extra_langs = subwin_langs - {target_lang, source_lang}

        if self._should_merge_fragment(original_text, source_lang):
            msg_id = self._last_msg_id
            merged = f"{self._recognized_texts[msg_id]} {original_text}".strip()
            self._recognized_texts[msg_id] = merged
            self._last_original = merged
            self._last_msg_time = time.time()
            log.info("Merged ASR fragment into msg %s: %s", msg_id, merged)
            if self._overlay:
                self._overlay.update_original(msg_id, merged, source_lang)
            self._transcript.update_original(msg_id, merged)
            self._record_subtitle_original_update(msg_id, merged, source_lang)
        else:
            self._asr_count += 1
            self._msg_id += 1
            msg_id = self._msg_id
            self._recognized_texts[msg_id] = original_text
            self._last_original = original_text
            self._last_msg_id = msg_id
            self._last_msg_time = time.time()
            self._last_source_lang = source_lang
            if self._overlay:
                self._overlay.add_message(
                    msg_id, timestamp, original_text, source_lang, asr_ms
                )
            self._transcript.write_original(msg_id, timestamp, original_text)
            self._record_subtitle_original(msg_id, original_text, source_lang)

        if _should_skip_translation(self._recognized_texts[msg_id], source_lang, target_lang):
            log.info("Same language (%s -> %s), no translation", source_lang, target_lang)
            self._transcript.finalize_no_translation(msg_id)
            self._record_subtitle_translation(msg_id, "")
            if self._overlay:
                self._overlay.update_translation(msg_id, "", 0)
                self._overlay.update_stats(
                    self._asr_count,
                    self._translate_count,
                    self._total_prompt_tokens,
                    self._total_completion_tokens,
                    self._compute_cost(),
                )
            if self._subwin and self._subwin.isVisible():
                if extra_langs:
                    try:
                        self._tl_executor.submit(
                            self._translate_subwin_only,
                            self._recognized_texts[msg_id],
                            source_lang,
                            extra_langs,
                        )
                    except RuntimeError:
                        pass
                else:
                    self._subwin.update_text(
                        self._recognized_texts[msg_id], {target_lang: self._recognized_texts[msg_id]}
                    )
            return

        try:
            self._submit_translation(
                msg_id, self._recognized_texts[msg_id], source_lang, extra_langs or None
            )
        except RuntimeError:
            log.warning("Translation executor shut down, skipping")

    def start(self):
        if self._running:
            return
        n = len(self._subwin.get_target_languages()) if self._subwin else 1
        old_executor = self._tl_executor
        old_queue = self._asr_queue
        new_executor = ThreadPoolExecutor(max_workers=max(8, n + 1))
        new_queue = queue.Queue(maxsize=16)
        audio_started = False
        try:
            self._audio.start()
            audio_started = True
            self._tl_executor = new_executor
            self._asr_queue = new_queue
            self._running = True
            self._paused = False
            self._capture_thread = threading.Thread(
                target=self._capture_loop, daemon=True
            )
            self._asr_thread = threading.Thread(
                target=self._asr_loop, daemon=True
            )
            self._capture_thread.start()
            self._asr_thread.start()
        except Exception:
            self._running = False
            self._paused = False
            if audio_started:
                try:
                    self._audio.stop()
                except Exception:
                    pass
            for thread in (self._capture_thread, self._asr_thread):
                if thread and thread.is_alive():
                    thread.join(timeout=1)
            self._capture_thread = None
            self._asr_thread = None
            self._tl_executor = old_executor
            self._asr_queue = old_queue
            new_executor.shutdown(wait=False)
            raise
        if old_executor is not new_executor:
            old_executor.shutdown(wait=False)
        # Periodic memory snapshot every 30s
        if self._mem_periodic_timer is None:
            self._mem_periodic_timer = QTimer()
            self._mem_periodic_timer.timeout.connect(self._log_mem_periodic)
            self._mem_periodic_timer.start(30000)
        snap = self._mem_snapshot()
        log.info(
            f"MEM[start] RSS={snap['rss']:.1f}MB "
            f"GPU(alloc/reserved)={snap['gpu_alloc']:.0f}/{snap['gpu_reserved']:.0f}MB "
            f"(baseline for delta tracking)"
        )
        log.info("Pipeline started (capture + ASR threads)")

    def stop(self):
        self._running = False
        self._audio.stop()
        if self._capture_thread:
            self._capture_thread.join(timeout=3)
            self._capture_thread = None
        self._asr_queue.put(None)
        if self._asr_thread:
            self._asr_thread.join(timeout=10)
            if self._asr_thread.is_alive():
                log.warning("ASR thread still running after timeout, proceeding with cleanup")
            self._asr_thread = None
        # Flush remaining VAD buffer after pipeline threads are done
        if self._interim_active:
            remaining = self._vad.force_flush()
            if remaining is not None and self._asr_ready:
                self._process_interim_final(remaining)
        else:
            remaining = self._vad.flush()
            if remaining is not None and self._asr_ready:
                self._process_segment(remaining)
        self._interim_active = False
        self._interim_pending = ""
        self._last_interim_samples = 0
        self._last_interim_check_time = 0.0
        self._interim_committed_tail = ""
        self._tl_executor.shutdown(wait=False)
        self._transcript.close()
        self._hotkey.stop()
        self._local_api.stop()
        self._history.end_session()
        self._tts.stop()
        if self._mem_periodic_timer is not None:
            try:
                self._mem_periodic_timer.stop()
            except Exception:
                pass
            self._mem_periodic_timer = None
        snap = self._mem_snapshot()
        total_delta = snap["rss"] - self._mem_baseline_mb
        log.info(
            f"MEM[stop] RSS={snap['rss']:.1f}MB ({total_delta:+.1f} since start) "
            f"GPU(alloc/reserved)={snap['gpu_alloc']:.0f}/{snap['gpu_reserved']:.0f}MB "
            f"segs={self._mem_seg_count}"
        )
        log.info("Pipeline stopped")

    def pause(self):
        self._paused = True
        self._interim_active = False
        self._interim_pending = ""
        self._last_interim_samples = 0
        self._last_interim_check_time = 0.0
        self._interim_committed_tail = ""
        self._tts.clear()
        if self._overlay:
            self._overlay.update_monitor(0.0, 0.0)
        log.info("Pipeline paused")

    def resume(self):
        self._paused = False
        log.info("Pipeline resumed")

    def _process_segment(self, speech_segment):
        """Run ASR + translation on a speech segment. Called from ASR thread and stop()."""
        seg_len = len(speech_segment) / 16000
        log.info(f"Speech segment: {seg_len:.1f}s")

        asr_start = time.perf_counter()
        with self._asr_lock:
            if not self._asr_ready or self._asr is None:
                if not self._asr_loading_type:
                    self._notify_asr_error(t("error_asr_not_ready"))
                return
            try:
                result = self._asr.transcribe(speech_segment)
            except Exception as e:
                log.error(f"ASR error: {e}", exc_info=True)
                self._notify_asr_error(e)
                return
        self._restore_asr_status_after_error()
        asr_ms = (time.perf_counter() - asr_start) * 1000
        if asr_ms > 10000:
            log.warning(f"ASR took {asr_ms:.0f}ms, possible hang")
        if result is None:
            return

        original_text = result["text"].strip()
        # Skip empty or punctuation-only ASR results
        if not original_text or not any(c.isalnum() for c in original_text):
            log.debug(
                f"ASR returned empty/punctuation-only, skipping: '{result['text']}'"
            )
            return

        # Skip suspiciously short text from long segments (likely noise)
        alnum_chars = sum(1 for c in original_text if c.isalnum())
        if seg_len >= 2.0 and alnum_chars <= 3:
            log.debug(
                f"Noise filter: {seg_len:.1f}s segment produced only '{original_text}', skipping"
            )
            return

        source_lang = result["language"]
        asr_lang_setting = self._panel.get_settings().get("asr_language", "auto") if self._panel else "auto"
        if asr_lang_setting != "auto" and source_lang != asr_lang_setting:
            log.info(
                f"Language filter: expected '{asr_lang_setting}' but got '{source_lang}', "
                f"discarding: {original_text[:60]}"
            )
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        log.info(f"ASR [{source_lang}] ({asr_ms:.0f}ms): {original_text}")
        self._handle_recognized_text(original_text, source_lang, asr_ms, timestamp)
        self._log_mem_after_segment()

    # ── Incremental ASR ──

    _pysbd_cache = {}  # lang -> pysbd.Segmenter

    @staticmethod
    def _get_segmenter(lang: str):
        import pysbd
        if lang not in LiveTranslateApp._pysbd_cache:
            pysbd_lang = lang if lang in pysbd.languages.LANGUAGE_CODES else "en"
            LiveTranslateApp._pysbd_cache[lang] = pysbd.Segmenter(
                language=pysbd_lang, clean=False
            )
        return LiveTranslateApp._pysbd_cache[lang]

    def _split_sentences(self, text: str, lang: str = "en") -> list[str]:
        """Split text into sentences using pysbd, with comma fallback for long text."""
        seg = self._get_segmenter(lang)
        parts = [p for p in seg.segment(text) if p.strip()]
        if len(parts) > 1:
            return parts

        # Comma fallback for long unsplit text — split at last balanced comma
        # CJK 「、」at 25 chars; all commas at 60 chars (long sentence, reduce latency)
        min_len = 25 if any(c == '、' for c in text) else 60
        if len(text) > min_len:
            for i in range(len(text) - 8, 5, -1):
                if text[i] in ',，;；、':
                    before = text[:i + 1].strip()
                    after = text[i + 1:].strip()
                    if before and after and len(before) > 15 and len(after) > 3:
                        return [before, after]

        return parts

    @staticmethod
    def _is_short_utterance(text: str) -> bool:
        """Check if text has ≤8 alphanumeric chars (likely noise/filler/fragment)."""
        alnum = sum(1 for c in text if c.isalnum())
        return alnum <= 8

    def _strip_committed_overlap(self, text: str) -> str:
        """Remove text that overlaps with previously committed content."""
        if not self._interim_committed_tail:
            return text
        tail = self._interim_committed_tail.lower().rstrip()
        text_lower = text.lower()
        # Check if text starts with a suffix of the committed tail
        max_check = min(len(tail), len(text_lower))
        for overlap_len in range(max_check, 2, -1):
            if text_lower[:overlap_len] == tail[-overlap_len:]:
                stripped = text[overlap_len:].strip()
                if stripped:
                    log.debug(f"Stripped echo overlap ({overlap_len} chars): '{text[:overlap_len]}...'")
                    return stripped
                return ""
        return text

    def _do_interim_asr(self) -> bool:
        """Run ASR on current VAD buffer, output complete sentences, trim consumed audio.
        Returns True if any sentences were committed."""
        with self._vad_lock:
            peek = self._vad.peek_buffer()
        if peek is None:
            return False
        audio, duration = peek

        # Don't bother with very short buffers
        if duration < 1.5:
            return False

        use_word_ts = self._asr_type == "whisper"

        asr_start = time.perf_counter()
        with self._asr_lock:
            if not self._asr_ready or self._asr is None:
                if not self._asr_loading_type:
                    self._notify_asr_error(t("error_asr_not_ready"))
                return False
            try:
                result = self._asr.transcribe(audio, word_timestamps=use_word_ts) if use_word_ts else self._asr.transcribe(audio)
            except Exception as e:
                log.error(f"Interim ASR error: {e}", exc_info=True)
                self._notify_asr_error(e)
                return False
        self._restore_asr_status_after_error()
        asr_ms = (time.perf_counter() - asr_start) * 1000

        if result is None:
            return False

        full_text = result["text"].strip()
        if not full_text or not any(c.isalnum() for c in full_text):
            return False

        # Strip echo from previous commit's overlap
        full_text = self._strip_committed_overlap(full_text)
        if not full_text:
            return False

        split_start = time.perf_counter()
        sentences = self._split_sentences(full_text, result["language"])
        split_ms = (time.perf_counter() - split_start) * 1000
        if len(sentences) <= 1:
            return False
        log.debug(f"Interim split [{result['language']}] ({split_ms:.1f}ms): {len(sentences)} parts -> {sentences}")

        # All but last are complete; last is still being spoken
        complete = sentences[:-1]

        committed_text = ""
        for sent in complete:
            committed_text += sent

        if not committed_text.strip():
            return False

        # Determine trim point
        total_samples = len(audio)
        if use_word_ts and result.get("words"):
            words = result["words"]
            committed_lower = committed_text.lower().rstrip()
            char_pos = 0
            last_word_end = 0.0
            for w in words:
                word_text = w["word"].strip()
                idx = committed_lower.find(word_text.lower(), char_pos)
                if idx >= 0:
                    char_pos = idx + len(word_text)
                    last_word_end = w["end"]
                if char_pos >= len(committed_lower):
                    break
            trim_samples = int(last_word_end * 16000)
        else:
            # Proportional trim with safety margin to reduce echo
            ratio = len(committed_text) / max(len(full_text), 1)
            margin = int(0.3 * 16000)  # 0.3s extra trim to avoid re-recognition
            trim_samples = int(ratio * total_samples) + margin
            # Don't over-trim: keep at least 0.5s for the remaining sentence
            max_trim = total_samples - int(0.5 * 16000)
            trim_samples = min(trim_samples, max(max_trim, 0))
            # Minimum trim to prevent re-recognition loops
            min_trim = int(0.3 * 16000)
            if trim_samples < min_trim and trim_samples > 0:
                trim_samples = min(min_trim, total_samples // 2)

        # Output committed sentences
        actually_committed = False
        for sent in complete:
            text = sent.strip()
            if not text:
                continue
            if self._is_short_utterance(text):
                self._interim_pending += text
                log.debug(f"Interim short utterance buffered: '{text}', pending='{self._interim_pending}'")
                continue

            if self._interim_pending:
                text = self._interim_pending + text
                self._interim_pending = ""

            self._process_segment_text(text, result["language"], asr_ms)
            actually_committed = True

        if not actually_committed:
            return False

        if trim_samples > 0:
            with self._vad_lock:
                self._vad.trim_front(trim_samples)

        # Track committed text tail for echo dedup
        self._interim_committed_tail = committed_text[-50:] if len(committed_text) > 50 else committed_text

        self._interim_active = True
        log.info(f"Interim ASR: committed {len(complete)} sentence(s), trimmed {trim_samples / 16000:.2f}s")
        return True

    def _process_segment_text(self, text: str, source_lang: str, asr_ms: float = 0):
        """Output a text result (from interim or final) — similar to _process_segment but skips ASR."""
        original_text = text.strip()
        if not original_text or not any(c.isalnum() for c in original_text):
            return

        asr_lang_setting = self._panel.get_settings().get("asr_language", "auto") if self._panel else "auto"
        if asr_lang_setting != "auto" and source_lang != asr_lang_setting:
            log.info(f"Language filter: expected '{asr_lang_setting}' but got '{source_lang}', discarding: {original_text[:60]}")
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        log.info(f"ASR [{source_lang}] ({asr_ms:.0f}ms, interim): {original_text}")
        self._handle_recognized_text(original_text, source_lang, asr_ms, timestamp)
        self._log_mem_after_segment()

    def _process_interim_final(self, speech_segment):
        """Handle VAD flush after interim outputs were already made."""
        seg_len = len(speech_segment) / 16000
        log.info(f"Interim final segment: {seg_len:.1f}s")

        asr_start = time.perf_counter()
        with self._asr_lock:
            if not self._asr_ready or self._asr is None:
                if not self._asr_loading_type:
                    self._notify_asr_error(t("error_asr_not_ready"))
                return
            try:
                result = self._asr.transcribe(speech_segment)
            except Exception as e:
                log.error(f"Interim final ASR error: {e}", exc_info=True)
                self._notify_asr_error(e)
                return
        self._restore_asr_status_after_error()
        asr_ms = (time.perf_counter() - asr_start) * 1000

        if result is None:
            # Flush any remaining pending
            if self._interim_pending:
                text = self._interim_pending
                self._interim_pending = ""
                lang = self._panel.get_settings().get("asr_language", "auto") if self._panel else "auto"
                if lang == "auto":
                    lang = "unknown"
                self._process_segment_text(text, lang)
            return

        original_text = result["text"].strip()

        # Strip echo from previous commit's overlap
        original_text = self._strip_committed_overlap(original_text)

        # Prepend any remaining pending short utterances
        if self._interim_pending:
            original_text = self._interim_pending + original_text
            self._interim_pending = ""

        if not original_text or not any(c.isalnum() for c in original_text):
            return

        # Apply noise filter like _process_segment
        alnum_chars = sum(1 for c in original_text if c.isalnum())
        if seg_len >= 2.0 and alnum_chars <= 3:
            log.debug(f"Noise filter: {seg_len:.1f}s segment produced only '{original_text}', skipping")
            return

        self._process_segment_text(original_text, result["language"], asr_ms)

    def _capture_loop(self):
        silence_chunk = np.zeros(
            int(
                self._config["audio"]["sample_rate"]
                * self._config["audio"]["chunk_duration"]
            ),
            dtype=np.float32,
        )
        while self._running:
            item = self._audio.get_audio(timeout=1.0)
            if item is None:
                if self._vad._is_speaking and not self._paused:
                    n = self._vad._get_effective_silence_limit() + 1
                    for _ in range(n):
                        with self._vad_lock:
                            seg = self._vad.process_chunk(silence_chunk)
                        if seg is not None and self._asr_ready:
                            self._enqueue_asr("vad_flush", seg)
                            break
                continue

            chunk, mic_rms = item

            if self._paused:
                continue

            rms = float(np.sqrt(np.mean(chunk**2)))

            if self._overlay:
                self._overlay.update_monitor(rms, self._vad.last_confidence, mic_rms)

            with self._vad_lock:
                speech_segment = self._vad.process_chunk(chunk)

            if speech_segment is None:
                # Still accumulating — check for interim ASR
                if (self._incremental_enabled and self._asr_ready
                        and self._vad._is_speaking):
                    buf_samples = self._vad._speech_samples
                    total_dur = buf_samples / 16000
                    elapsed = (buf_samples - self._last_interim_samples) / 16000
                    now = time.perf_counter()
                    cooldown = now - self._last_interim_check_time
                    if total_dur >= self._interim_interval and elapsed >= self._interim_interval and cooldown >= 1.0:
                        self._last_interim_check_time = now
                        self._enqueue_asr("interim", None)
                continue

            if not self._asr_ready:
                log.debug("ASR not ready, dropping segment")
                if not self._asr_loading_type:
                    self._notify_asr_error(t("error_asr_not_ready"))
                continue

            self._enqueue_asr("vad_flush", speech_segment)

    def _enqueue_asr(self, seg_type: str, segment):
        try:
            self._asr_queue.put_nowait((seg_type, segment))
        except queue.Full:
            try:
                dropped = self._asr_queue.get_nowait()
                log.warning(f"ASR queue full, dropped {dropped[0]} segment")
            except queue.Empty:
                pass
            try:
                self._asr_queue.put_nowait((seg_type, segment))
            except queue.Full:
                log.warning("ASR queue still full after drop, skipping segment")

    def _asr_loop(self):
        while self._running:
            try:
                item = self._asr_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is None:
                break

            seg_type, segment = item

            if seg_type == "vad_flush":
                if self._interim_active:
                    self._process_interim_final(segment)
                else:
                    self._process_segment(segment)
                self._interim_active = False
                self._interim_pending = ""
                self._last_interim_samples = 0
                self._last_interim_check_time = 0.0
                self._interim_committed_tail = ""
            elif seg_type == "interim":
                self._drain_interim_duplicates()
                committed = self._do_interim_asr()
                if committed:
                    self._last_interim_samples = self._vad._speech_samples

    def _drain_interim_duplicates(self):
        while True:
            try:
                item = self._asr_queue.get_nowait()
            except queue.Empty:
                break
            if item is None or item[0] != "interim":
                self._asr_queue.put(item)
                break


def main():
    setup_logging()
    log.info("LiveTranslate starting...")
    config = load_config()
    saved = _load_saved_settings()

    # Log actual effective config
    _asr_eng = (saved or {}).get(
        "asr_engine", config["asr"].get("engine", "openai-audio")
    )
    _active_idx = (saved or {}).get("active_model", 0)
    _models = (saved or {}).get("models", [])
    if 0 <= _active_idx < len(_models):
        _m = _models[_active_idx]
        _model_info = f"{_m.get('name', '?')} ({_m.get('model', '?')})"
    else:
        _model_info = f"{config['translation']['model']} (default)"
    log.info(f"Config loaded: ASR={_asr_eng}, Translator={_model_info}")

    # Apply UI language before creating any widgets
    if saved and saved.get("ui_lang"):
        set_lang(saved["ui_lang"])

    os.environ["QT_LOGGING_RULES"] = "qt.text.font.db=false"
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    _app_icon = create_app_icon()
    app.setWindowIcon(_app_icon)

    # First launch → guided setup wizard → download any models the chosen
    # configuration needs (e.g. SenseVoice for local ASR) → main window.
    if not SETTINGS_FILE.exists():
        _close_boot_splash()
        wizard = SetupWizardDialog()
        if wizard.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
        saved = _load_saved_settings()
        log.info("Setup wizard completed")

        # The wizard may have chosen local SenseVoice; fetch it (and Silero if
        # the VAD mode needs it) before the main UI comes up.
        set_models_dir(saved.get("cache_path") or None)
        missing = get_missing_models(
            saved.get("asr_engine", config["asr"].get("engine", "openai-audio")),
            config["asr"]["model_size"],
            saved.get("hub", "ms"),
            saved.get("vad_mode", "energy"),
        )
        if missing:
            log.info(f"Downloading initial models: {[m['name'] for m in missing]}")
            _close_boot_splash()
            dlg = ModelDownloadDialog(missing, hub=saved.get("hub", "ms"))
            if dlg.exec() != QDialog.DialogCode.Accepted:
                sys.exit(0)

    # Non-first launch but models missing → download dialog
    else:
        if saved.get("vad_mode") == "silero" and not is_silero_cached():
            saved["vad_mode"] = "energy"
            _save_settings(saved)
            log.info("Silero VAD is not cached; switched VAD mode to energy")
        missing = get_missing_models(
            saved.get("asr_engine", config["asr"].get("engine", "openai-audio")),
            config["asr"]["model_size"],
            saved.get("hub", "ms"),
            saved.get("vad_mode", "energy"),
        )
        if missing:
            log.info(f"Missing models: {[m['name'] for m in missing]}")
            _close_boot_splash()
            dlg = ModelDownloadDialog(missing, hub=saved.get("hub", "ms"))
            if dlg.exec() != QDialog.DialogCode.Accepted:
                sys.exit(0)

    log_window = LogWindow()
    log_handler = log_window.get_handler()
    logging.getLogger().addHandler(log_handler)

    # Branded splash during the (potentially several-second) heavy UI + engine
    # construction, so cold start doesn't look frozen.
    splash = StartupSplash()
    splash.show()
    splash.status(t("loading_please_wait"))
    app.processEvents()
    _close_boot_splash()

    panel = ControlPanel(config, saved_settings=saved)

    overlay = SubtitleOverlay(config["subtitle"])
    migrated_overlay_geometry = False
    if saved:
        ox = saved.get("overlay_x")
        oy = saved.get("overlay_y")
        ow = saved.get("overlay_w")
        oh = saved.get("overlay_h")
        if _should_migrate_overlay_geometry(saved):
            x, y, width, height = _preferred_overlay_geometry()
            overlay.setGeometry(x, y, width, height)
            saved.update(
                {
                    "overlay_x": x,
                    "overlay_y": y,
                    "overlay_w": width,
                    "overlay_h": height,
                }
            )
            saved["overlay_watch_layout_v2"] = True
            saved["overlay_watch_layout_v3"] = True
            migrated_overlay_geometry = True
        elif ox is not None and oy is not None:
            if SubtitleWindow._is_pos_visible(ox, oy):
                overlay.move(ox, oy)
            else:
                screen = QApplication.primaryScreen()
                geo = screen.availableGeometry()
                overlay.move(geo.right() - overlay.width() - 20, geo.bottom() - overlay.height() - 60)
        if ow and oh and not migrated_overlay_geometry:
            overlay.resize(ow, oh)
    if migrated_overlay_geometry:
        _save_settings(saved)
    overlay.show()
    splash.finish(overlay)

    # Subtitle window
    subwin_cfg = (saved or {}).get("subtitle_mode")
    subwin = SubtitleWindow(subwin_cfg)
    subwin_was_enabled = (subwin_cfg or {}).get("enabled", False)

    live_trans = LiveTranslateApp(config)
    live_trans.set_overlay(overlay)
    live_trans.set_subtitle_window(subwin)
    live_trans.set_panel(panel)

    def _deferred_init():
        panel._apply_settings()
        s = panel.get_settings()
        models = s.get("models", [])
        active_idx = s.get("active_model", 0)
        overlay.set_models(models, active_idx)
        target = s.get("target_language", "zh")
        overlay.set_target_language(target)
        asr_lang = s.get("asr_language", "auto")
        overlay.set_source_language(asr_lang)
        style = s.get("style")
        if style:
            overlay.apply_style(style)
        active_model = panel.get_active_model()
        if active_model:
            live_trans._on_model_changed(active_model)
        overlay.update_welcome_card(*_evaluate_setup_status(s))

    QTimer.singleShot(100, _deferred_init)

    tray = QSystemTrayIcon()
    tray.setToolTip(t("tray_tooltip"))
    tray.setIcon(_app_icon)

    menu = QMenu()

    # --- Pause / Resume toggle ---
    pause_action = QAction(t("tray_resume"))
    _is_running = [False]  # mutable for closure

    def _set_pipeline_ui_running(running: bool):
        overlay.set_running(running)
        _is_running[0] = running
        pause_action.setText(t("tray_pause") if running else t("tray_resume"))

    def on_start():
        try:
            live_trans.start()
            _set_pipeline_ui_running(True)
        except NotImplementedError as e:
            _set_pipeline_ui_running(False)
            QMessageBox.warning(panel, "LiveTranslate", str(e))
            log.warning(f"Start not supported: {e}")
        except Exception as e:
            _set_pipeline_ui_running(False)
            QMessageBox.warning(panel, "LiveTranslate", str(e))
            log.error(f"Start error: {e}", exc_info=True)

    def on_pause():
        if not live_trans.is_running:
            _set_pipeline_ui_running(False)
            return
        live_trans.pause()
        _set_pipeline_ui_running(False)

    def on_resume():
        if live_trans.is_running:
            live_trans.resume()
            _set_pipeline_ui_running(True)
        else:
            on_start()

    def on_toggle_pause():
        if _is_running[0]:
            on_pause()
        else:
            on_resume()

    pause_action.triggered.connect(on_toggle_pause)
    menu.addAction(pause_action)
    menu.addSeparator()

    # --- Show/hide overlay ---
    overlay_toggle_action = QAction(t("tray_hide_overlay"))

    _hide_notified = [False]

    def on_toggle_overlay():
        if overlay.isVisible():
            overlay.hide()
            overlay_toggle_action.setText(t("tray_show_overlay"))
            if not _hide_notified[0]:
                _hide_notified[0] = True
                tray.showMessage(
                    "LiveTranslate",
                    t("hide_tray_hint"),
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
        else:
            overlay.show()
            overlay.raise_()
            overlay_toggle_action.setText(t("tray_hide_overlay"))

    overlay_toggle_action.triggered.connect(on_toggle_overlay)
    menu.addAction(overlay_toggle_action)

    # --- Subtitle window toggle ---
    def _save_overlay_pos():
        settings = panel.get_settings()
        pos = overlay.pos()
        size = overlay.size()
        settings["overlay_x"] = pos.x()
        settings["overlay_y"] = pos.y()
        settings["overlay_w"] = size.width()
        settings["overlay_h"] = size.height()
        settings["overlay_watch_layout_v2"] = True
        settings["overlay_watch_layout_v3"] = True
        panel._current_settings.update({
            "overlay_x": pos.x(), "overlay_y": pos.y(),
            "overlay_w": size.width(), "overlay_h": size.height(),
            "overlay_watch_layout_v2": True,
            "overlay_watch_layout_v3": True,
        })
        _save_settings(settings)

    overlay.position_changed.connect(_save_overlay_pos)

    def _save_overlay_opacity(pct):
        # Persist the overlay opacity edited from the bottom slider. Update the
        # panel's in-memory style too, otherwise the panel's next auto-save
        # (whole-settings overwrite) would silently roll the opacity back.
        settings = panel.get_settings()
        style = dict(settings.get("style", {}))
        style["window_opacity"] = int(pct)
        settings["style"] = style
        panel._current_settings.setdefault("style", {})["window_opacity"] = int(pct)
        if hasattr(panel, "_window_opacity"):
            panel._window_opacity.blockSignals(True)
            panel._window_opacity.setValue(int(pct))
            panel._window_opacity.blockSignals(False)
        _save_settings(settings)

    overlay.opacity_changed.connect(_save_overlay_opacity)

    subwin_toggle_action = QAction(t("subwin_show"), checkable=True)

    def _save_subwin_state():
        settings = panel.get_settings()
        sm = settings.get("subtitle_mode") or {}
        sm["enabled"] = subwin.isVisible()
        pos = subwin.pos()
        sm["window_x"] = pos.x()
        sm["window_y"] = pos.y()
        settings["subtitle_mode"] = sm
        panel._current_settings["subtitle_mode"] = sm
        # Keep the panel's subtitle settings widget in sync, otherwise a later
        # style change in the panel would emit a stale enabled/position and
        # silently revert the window's visibility/position.
        if getattr(panel, "_subtitle_widget", None) is not None:
            panel._subtitle_widget.sync_external_state(
                {"enabled": sm["enabled"], "window_x": sm["window_x"], "window_y": sm["window_y"]}
            )
        _save_settings(settings)

    _subwin_notified = [False]

    def on_toggle_subwin(checked):
        if checked:
            subwin.show()
            subwin.raise_()
            if not _subwin_notified[0]:
                _subwin_notified[0] = True
                tray.showMessage(
                    "LiveTranslate",
                    t("subwin_drag_hint"),
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
        else:
            subwin.hide()
        overlay.set_subtitle_checked(checked)
        _save_subwin_state()

    subwin_toggle_action.toggled.connect(on_toggle_subwin)
    subwin.position_changed.connect(_save_subwin_state)

    # Sync when subtitle window is manually closed (e.g. Alt+F4)
    def _on_subwin_closed():
        subwin_toggle_action.blockSignals(True)
        subwin_toggle_action.setChecked(False)
        subwin_toggle_action.blockSignals(False)
        overlay.set_subtitle_checked(False)
        _save_subwin_state()

    subwin.window_closed.connect(_on_subwin_closed)

    # Restore subtitle window visibility from saved state
    if subwin_was_enabled:
        subwin_toggle_action.setChecked(True)

    menu.addAction(subwin_toggle_action)

    # Connect overlay subtitle button
    def _on_overlay_subtitle_toggle():
        subwin_toggle_action.setChecked(not subwin_toggle_action.isChecked())

    overlay.subtitle_toggled.connect(_on_overlay_subtitle_toggle)

    # Connect panel subtitle settings changes
    def _on_panel_subtitle_changed(s):
        subwin.apply_settings(s)

    panel.subtitle_settings_changed.connect(_on_panel_subtitle_changed)

    def _on_reset_positions():
        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry()
        subwin.move(100, 100)
        _save_subwin_state()
        ow, oh = overlay.width(), overlay.height()
        overlay.move(geo.right() - ow - 50, geo.bottom() - oh - 100)
        _save_overlay_pos()

    panel.reset_positions.connect(_on_reset_positions)

    menu.addSeparator()

    # --- Show log / panel ---
    log_action = QAction(t("tray_show_log"))
    panel_action = QAction(t("tray_show_panel"))

    def on_toggle_log():
        if log_window.isVisible():
            log_window.hide()
        else:
            log_window.show()
            log_window.raise_()

    def on_toggle_panel():
        if panel.isVisible():
            panel.hide()
        else:
            panel.show()
            panel.raise_()

    log_action.triggered.connect(on_toggle_log)
    panel_action.triggered.connect(on_toggle_panel)
    menu.addAction(panel_action)
    menu.addAction(log_action)
    menu.addSeparator()

    # --- TTS (simultaneous interpretation) one-click toggle ---
    # Speaking the translation aloud is a headline feature, so it gets a
    # top-level checkable tray item plus an overlay toolbar button. The three
    # entry points (overlay button / tray item / settings checkbox) stay in
    # sync through a single applier guarded against re-entrant signal loops.
    tts_action = QAction(t("tray_tts"), checkable=True)
    _tts_initial = bool(panel.get_settings().get("tts", {}).get("enabled", False))
    _syncing_tts = [False]

    def _sync_tts_ui(enabled: bool):
        if _syncing_tts[0]:
            return
        _syncing_tts[0] = True
        try:
            tts_action.setChecked(enabled)
            overlay.set_tts_active(enabled)
            panel.set_tts_enabled(enabled)
        finally:
            _syncing_tts[0] = False

    def _apply_tts_toggle(enabled: bool):
        """Apply to the running engine + persist, then mirror to all controls."""
        if _syncing_tts[0]:
            return  # change originated from _sync_tts_ui; nothing more to do
        live_trans.set_tts_enabled(enabled)
        _sync_tts_ui(enabled)

    tts_action.toggled.connect(lambda v: _apply_tts_toggle(v))
    overlay.tts_toggled.connect(lambda v: _apply_tts_toggle(v))
    # Settings checkbox already persists + applies via the settings channel;
    # just mirror its state onto the overlay button and tray item.
    panel.tts_enabled_changed.connect(lambda v: _sync_tts_ui(v))

    tts_action.setChecked(_tts_initial)
    overlay.set_tts_active(_tts_initial)

    menu.addAction(tts_action)
    menu.addSeparator()

    # --- Overlay submenu (click-through, topmost, auto-scroll, taskbar) ---
    overlay_menu = QMenu(t("tray_menu_overlay"))

    ct_action = QAction(t("click_through"), checkable=True)
    topmost_action = QAction(t("top_most"), checkable=True)
    topmost_action.setChecked(True)
    autoscroll_action = QAction(t("auto_scroll"), checkable=True)
    autoscroll_action.setChecked(True)
    taskbar_action = QAction(t("taskbar"), checkable=True)
    taskbar_action.setChecked(True)

    # Tray → overlay sync
    ct_action.toggled.connect(lambda v: overlay._handle._ct_check.setChecked(v))
    topmost_action.toggled.connect(
        lambda v: overlay._handle._topmost_check.setChecked(v)
    )
    autoscroll_action.toggled.connect(
        lambda v: overlay._handle._auto_scroll.setChecked(v)
    )
    taskbar_action.toggled.connect(
        lambda v: overlay._handle._taskbar_check.setChecked(v)
    )

    # Overlay → tray sync
    overlay._handle.click_through_toggled.connect(lambda v: ct_action.setChecked(v))
    overlay._handle.topmost_toggled.connect(lambda v: topmost_action.setChecked(v))
    overlay._handle.auto_scroll_toggled.connect(
        lambda v: autoscroll_action.setChecked(v)
    )
    overlay._handle.taskbar_toggled.connect(lambda v: taskbar_action.setChecked(v))

    overlay_menu.addAction(ct_action)
    overlay_menu.addAction(topmost_action)
    overlay_menu.addAction(autoscroll_action)
    overlay_menu.addAction(taskbar_action)
    menu.addMenu(overlay_menu)

    # --- Model submenu ---
    model_menu = QMenu(t("tray_menu_model"))
    model_action_group = QActionGroup(model_menu)
    model_action_group.setExclusive(True)

    def _rebuild_model_menu():
        for a in model_action_group.actions():
            model_action_group.removeAction(a)
        model_menu.clear()
        settings = panel.get_settings()
        models = settings.get("models", [])
        active = settings.get("active_model", 0)
        for i, m in enumerate(models):
            name = m.get("name", m.get("model", "?"))
            action = QAction(name, checkable=True)
            if i == active:
                action.setChecked(True)
            model_action_group.addAction(action)
            action.triggered.connect(lambda checked, idx=i: _on_tray_model_switch(idx))
            model_menu.addAction(action)

    def _on_tray_model_switch(index):
        models = panel.get_settings().get("models", [])
        if 0 <= index < len(models):
            from control_panel import _save_settings

            settings = panel.get_settings()
            settings["active_model"] = index
            panel._current_settings["active_model"] = index
            _save_settings(settings)
            panel._refresh_model_list()
            live_trans._on_model_changed(models[index])
            overlay.set_models(models, index)

    def on_overlay_model_switch(index):
        models = panel.get_settings().get("models", [])
        if 0 <= index < len(models):
            from control_panel import _save_settings

            settings = panel.get_settings()
            settings["active_model"] = index
            panel._current_settings["active_model"] = index
            _save_settings(settings)
            panel._refresh_model_list()
            live_trans._on_model_changed(models[index])
        _rebuild_model_menu()

    model_menu.aboutToShow.connect(_rebuild_model_menu)
    menu.addMenu(model_menu)

    # --- Target language submenu ---
    lang_menu = QMenu(t("tray_menu_target_lang"))
    lang_action_group = QActionGroup(lang_menu)
    lang_action_group.setExclusive(True)
    _lang_actions = {}
    lang_more_menu = QMenu(t("tray_more_langs"))

    for code, native in LANGUAGES:
        if code == "auto":
            continue
        action = QAction(f"{code} - {native}", checkable=True)
        lang_action_group.addAction(action)
        action.triggered.connect(lambda checked, lc=code: _on_tray_lang_switch(lc))
        if code in COMMON_LANG_CODES:
            lang_menu.addAction(action)
        else:
            lang_more_menu.addAction(action)
        _lang_actions[code] = action

    lang_menu.addMenu(lang_more_menu)

    current_target = panel.get_settings().get("target_language", "zh")
    if current_target in _lang_actions:
        _lang_actions[current_target].setChecked(True)

    def _on_tray_lang_switch(lang_code):
        overlay.set_target_language(lang_code)
        live_trans._on_target_language_changed(lang_code)
        from control_panel import _save_settings

        settings = panel.get_settings()
        settings["target_language"] = lang_code
        panel._current_settings["target_language"] = lang_code
        _save_settings(settings)

    # Overlay → tray lang sync
    def _on_overlay_lang_changed(lang_code):
        if lang_code in _lang_actions:
            _lang_actions[lang_code].setChecked(True)

    overlay.target_language_changed.connect(_on_overlay_lang_changed)

    menu.addMenu(lang_menu)

    # --- ASR language hint submenu ---
    asr_lang_menu = QMenu(t("tray_menu_asr_lang"))
    asr_lang_action_group = QActionGroup(asr_lang_menu)
    asr_lang_action_group.setExclusive(True)
    _asr_lang_actions = {}
    asr_more_menu = QMenu(t("tray_more_langs"))

    for code, native in LANGUAGES:
        label = t("asr_lang_auto") if code == "auto" else native
        action = QAction(f"{code} - {label}", checkable=True)
        asr_lang_action_group.addAction(action)
        action.triggered.connect(lambda checked, c=code: _on_tray_asr_lang(c))
        if code in COMMON_LANG_CODES:
            asr_lang_menu.addAction(action)
        else:
            asr_more_menu.addAction(action)
        _asr_lang_actions[code] = action

    asr_lang_menu.addMenu(asr_more_menu)

    current_asr_lang = panel.get_settings().get("asr_language", "auto")
    if current_asr_lang in _asr_lang_actions:
        _asr_lang_actions[current_asr_lang].setChecked(True)

    def _on_tray_asr_lang(code):
        from control_panel import _save_settings

        if live_trans._asr:
            live_trans._asr.set_language(code)
        settings = panel.get_settings()
        settings["asr_language"] = code
        panel._current_settings["asr_language"] = code
        _save_settings(settings)
        # Sync control panel combo
        idx = panel._asr_lang.findData(code)
        if idx >= 0:
            panel._asr_lang.blockSignals(True)
            panel._asr_lang.setCurrentIndex(idx)
            panel._asr_lang.blockSignals(False)

    menu.addMenu(asr_lang_menu)
    menu.addSeparator()

    # --- Export submenu ---
    export_menu = QMenu(t("export_menu"))
    export_orig_action = QAction(t("export_original"))
    export_trans_action = QAction(t("export_translation"))
    export_all_action = QAction(t("export_all"))
    export_orig_action.triggered.connect(lambda: overlay.export_messages("original", parent=panel))
    export_trans_action.triggered.connect(lambda: overlay.export_messages("translation", parent=panel))
    export_all_action.triggered.connect(lambda: overlay.export_messages("both", parent=panel))
    export_menu.addAction(export_orig_action)
    export_menu.addAction(export_trans_action)
    export_menu.addAction(export_all_action)
    menu.addMenu(export_menu)
    menu.addSeparator()

    # --- Quit ---
    quit_action = QAction(t("quit"))

    def on_quit():
        live_trans.stop()
        app.quit()

    quit_action.triggered.connect(on_quit)
    menu.addAction(quit_action)

    # --- Connect overlay signals ---
    overlay.settings_requested.connect(on_toggle_panel)
    overlay.target_language_changed.connect(live_trans._on_target_language_changed)

    def _on_overlay_source_lang(code):
        """Overlay source language combo → sync to panel + ASR engine + tray."""
        _on_tray_asr_lang(code)
        overlay.set_source_language(code)

    def _on_panel_asr_lang_changed(_index):
        """Panel ASR language combo → sync to overlay."""
        code = panel._asr_lang.currentData() or "auto"
        overlay.set_source_language(code)

    overlay.source_language_changed.connect(_on_overlay_source_lang)
    panel._asr_lang.currentIndexChanged.connect(_on_panel_asr_lang_changed)
    overlay.model_switch_requested.connect(on_overlay_model_switch)
    overlay.start_requested.connect(on_resume)
    overlay.stop_requested.connect(on_pause)
    overlay.hide_requested.connect(on_toggle_overlay)
    overlay.quit_requested.connect(on_quit)

    tray.setContextMenu(menu)
    tray.show()

    def _on_memory_warning(rss_mb: float):
        tray.showMessage(
            "LiveTranslate",
            t("mem_warning_msg").format(rss=int(rss_mb)),
            QSystemTrayIcon.MessageIcon.Warning,
            10000,
        )

    live_trans.set_memory_warning_callback(_on_memory_warning)

    QTimer.singleShot(500, on_start)

    signal.signal(signal.SIGINT, lambda *_: on_quit())
    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(200)

    sys.exit(app.exec())


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        sys.exit(run_self_check())
    main()
