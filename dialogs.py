import json
import logging
import re
import sys
import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFileDialog,
    QMessageBox,
    QProgressBar,
    QSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from model_manager import (
    detect_hardware,
    download_asr,
    download_silero,
    get_models_dir,
    recommend_asr,
    set_models_dir,
)
from i18n import t
from logging_utils import add_secret_redaction
from runtime_paths import resource_path
from theme import (
    busy_progress_stylesheet,
    console_stylesheet,
    dialog_stylesheet,
    link_button_stylesheet,
    loading_title_stylesheet,
    status_text_stylesheet,
)

log = logging.getLogger("LiveTranslate.Dialogs")

SETTINGS_FILE = None  # set by control_panel on import
_save_settings = None  # set by control_panel on import


class _LogCapture(logging.Handler):
    """Captures log output and emits via callback."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self.setFormatter(logging.Formatter("%(message)s"))
        add_secret_redaction(self)

    def emit(self, record):
        try:
            self._callback(self.format(record))
        except Exception:
            pass


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class _StderrCapture:
    """Captures stderr (tqdm) and forwards cleaned lines via callback."""

    def __init__(self, callback, original):
        self._cb = callback
        self._orig = original

    def write(self, text):
        if self._orig:
            self._orig.write(text)
        if not text:
            return
        cleaned = _ANSI_RE.sub("", text)
        for line in cleaned.splitlines():
            line = line.strip()
            if line:
                self._cb(line)

    def flush(self):
        if self._orig:
            self._orig.flush()

    def isatty(self):
        return False


def _build_loading_body(dialog, layout, status_text: str, busy: bool = True):
    """Populate a dialog with a clean loading UI: status line + (busy) progress
    bar + a collapsible 'details' log box (hidden by default).

    Returns (status_label, progress_bar, log_view). The log view is wired to be
    shown/hidden by a quiet 'show details' toggle so普通用户默认看不到原始日志，
    高级用户可展开。Keeps the same QTextEdit so existing log-append code works.
    """
    status_label = QLabel(status_text)
    status_label.setWordWrap(True)
    status_label.setStyleSheet(loading_title_stylesheet())
    layout.addWidget(status_label)

    progress = QProgressBar()
    progress.setStyleSheet(busy_progress_stylesheet())
    progress.setTextVisible(False)
    if busy:
        progress.setRange(0, 0)  # indeterminate / busy animation
    layout.addWidget(progress)

    details_btn = QPushButton(t("show_details"))
    details_btn.setStyleSheet(link_button_stylesheet())
    details_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    layout.addWidget(details_btn)

    log_view = QTextEdit()
    log_view.setReadOnly(True)
    log_view.setFont(QFont("Consolas", 8))
    log_view.setStyleSheet(console_stylesheet())
    log_view.setFixedHeight(150)
    log_view.setVisible(False)
    layout.addWidget(log_view)

    def _toggle_details():
        show = not log_view.isVisible()
        log_view.setVisible(show)
        details_btn.setText(t("hide_details") if show else t("show_details"))
        dialog.adjustSize()

    details_btn.clicked.connect(_toggle_details)
    return status_label, progress, log_view


class _ModelLoadDialog(QDialog):
    """Modal dialog shown during model load with a clean status + busy bar.

    The raw log is kept but collapsed behind a 'details' toggle so普通用户
    only sees a friendly status line and a busy progress bar.
    """

    _log_signal = pyqtSignal(str)

    def __init__(self, message, parent=None):
        super().__init__(parent)
        self.setStyleSheet(dialog_stylesheet())
        self.setWindowTitle("LiveTranslate")
        self.setMinimumWidth(420)
        self.setModal(True)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.MSWindowsFixedSizeDialogHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        self._label, self._progress, self._log_view = _build_loading_body(
            self, layout, message or t("loading_please_wait")
        )

        self._log_signal.connect(self._append_log)
        self._log_handler = _LogCapture(self._log_signal.emit)
        self._log_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self._log_handler)

    def _append_log(self, text):
        self._log_view.append(text)
        self._log_view.verticalScrollBar().setValue(
            self._log_view.verticalScrollBar().maximum()
        )

    def done(self, result):
        logging.getLogger().removeHandler(self._log_handler)
        super().done(result)


class SetupWizardDialog(QDialog):
    """First-launch guided setup.

    Produces a *complete, validated* configuration so the user lands in a
    ready-to-run state instead of a half-configured one:
      - ASR: local SenseVoice by default (offline, no key), or a cloud API.
      - Translation: pick a provider preset, paste a key, test the connection.
      - Sensible flags auto-configured per provider.
    """

    _test_result_signal = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(dialog_stylesheet())
        self.setWindowTitle(t("window_setup"))
        self.setMinimumWidth(560)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.CustomizeWindowHint
        )

        from home_presets import LLM_PRESETS, ASR_PRESETS

        self._llm_presets = LLM_PRESETS
        self._asr_presets = ASR_PRESETS

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "欢迎使用 LiveTranslate。完成下面两步即可开始：\n"
            "1) 选择语音识别方式  2) 配置一个翻译模型并测试连接"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ── Hardware detection banner ────────────────────────────────
        # Probe the machine so non-technical users land on a sane default
        # (local on capable GPUs, cloud API on weak machines) instead of
        # guessing between offline models they may not be able to run.
        self._hw = detect_hardware()
        self._reco = recommend_asr(self._hw)
        hw_box = QGroupBox("硬件检测")
        hw_layout = QVBoxLayout(hw_box)
        if self._hw.get("gpus"):
            g = max(self._hw["gpus"], key=lambda x: x.get("vram_gb", 0))
            hw_text = t("hw_detected_gpu").format(
                name=g["name"], vram=g["vram_gb"], ram=self._hw.get("ram_gb", "?")
            )
        else:
            hw_text = t("hw_detected_cpu").format(ram=self._hw.get("ram_gb", "?"))
        hw_info = QLabel(hw_text)
        hw_info.setWordWrap(True)
        hw_layout.addWidget(hw_info)
        reco_info = QLabel("💡 " + t(self._reco["reason_key"]))
        reco_info.setWordWrap(True)
        reco_info.setStyleSheet(status_text_stylesheet("success"))
        hw_layout.addWidget(reco_info)
        layout.addWidget(hw_box)

        # ── Step 1: ASR ──────────────────────────────────────────────
        asr_group = QGroupBox("第 1 步 · 语音识别 (ASR)")
        asr_layout = QVBoxLayout(asr_group)
        self._asr_mode = QComboBox()
        self._asr_mode.addItem("本地 SenseVoice（推荐 · 离线免费 · 首次下载约 1GB）", "local")
        self._asr_mode.addItem("云端 API（OpenAI / Groq / 硅基流动 等）", "cloud")
        self._asr_mode.currentIndexChanged.connect(self._on_asr_mode_changed)
        asr_layout.addWidget(self._asr_mode)

        self._asr_cloud_widget = QWidget()
        cloud_form = QFormLayout(self._asr_cloud_widget)
        cloud_form.setContentsMargins(0, 4, 0, 0)
        self._asr_preset = QComboBox()
        for p in self._asr_presets:
            self._asr_preset.addItem(p["name"], p)
        self._asr_preset.currentIndexChanged.connect(self._on_asr_preset_changed)
        self._asr_base = QLineEdit()
        self._asr_model = QComboBox()
        self._asr_model.setEditable(True)
        self._asr_key = QLineEdit()
        self._asr_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._asr_key.setPlaceholderText("粘贴语音识别服务的 API Key")
        cloud_form.addRow("服务", self._asr_preset)
        cloud_form.addRow("API Base", self._asr_base)
        cloud_form.addRow("模型", self._asr_model)
        cloud_form.addRow("API Key", self._asr_key)
        asr_layout.addWidget(self._asr_cloud_widget)
        self._asr_cloud_widget.setVisible(False)
        layout.addWidget(asr_group)

        # ── Step 2: Translation ──────────────────────────────────────
        llm_group = QGroupBox("第 2 步 · 翻译模型")
        llm_form = QFormLayout(llm_group)
        self._llm_preset = QComboBox()
        for p in self._llm_presets:
            self._llm_preset.addItem(p["name"], p)
        self._llm_preset.currentIndexChanged.connect(self._on_llm_preset_changed)
        self._llm_base = QLineEdit()
        self._llm_model = QComboBox()
        self._llm_model.setEditable(True)
        self._llm_key = QLineEdit()
        self._llm_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._llm_key.setPlaceholderText("粘贴翻译服务的 API Key（本地服务可留空）")
        self._llm_saved_keys = {}
        self._llm_active_preset_idx = 0
        llm_form.addRow("服务", self._llm_preset)
        llm_form.addRow("API Base", self._llm_base)
        llm_form.addRow("模型", self._llm_model)
        llm_form.addRow("API Key", self._llm_key)
        layout.addWidget(llm_group)

        # ── Test connection ──────────────────────────────────────────
        test_row = QHBoxLayout()
        self._test_btn = QPushButton("测试翻译连接")
        self._test_btn.clicked.connect(self._test_connection)
        test_row.addWidget(self._test_btn)
        self._test_label = QLabel("")
        self._test_label.setWordWrap(True)
        test_row.addWidget(self._test_label, 1)
        layout.addLayout(test_row)

        # ── Advanced (hub + cache path) ──────────────────────────────
        adv_group = QGroupBox("高级（可保持默认）")
        adv_form = QFormLayout(adv_group)
        self._hub_combo = QComboBox()
        self._hub_combo.addItems([t("hub_modelscope_full"), t("hub_huggingface_full")])
        adv_form.addRow("模型下载源", self._hub_combo)
        cache_row = QHBoxLayout()
        self._cache_path = QLineEdit(str(get_models_dir().resolve()))
        cache_row.addWidget(self._cache_path, 1)
        browse_btn = QPushButton(t("btn_browse"))
        browse_btn.clicked.connect(self._browse_cache_path)
        cache_row.addWidget(browse_btn)
        cache_wrap = QWidget()
        cache_wrap.setLayout(cache_row)
        adv_form.addRow("模型缓存目录", cache_wrap)
        layout.addWidget(adv_group)

        # ── Finish ───────────────────────────────────────────────────
        self._finish_btn = QPushButton("完成并开始")
        self._finish_btn.clicked.connect(self._finish)
        layout.addWidget(self._finish_btn)

        self._error = None
        self._test_result_signal.connect(self._on_test_result)

        # Initialize preset-driven fields.
        self._on_llm_preset_changed(0)
        self._on_asr_preset_changed(0)

        # Pre-select ASR mode from the hardware recommendation: weak machines
        # default to a cloud API (no big download), capable ones to local.
        if self._reco.get("mode") == "cloud":
            cloud_idx = self._asr_mode.findData("cloud")
            if cloud_idx >= 0:
                self._asr_mode.setCurrentIndex(cloud_idx)

    # ── ASR mode / preset handlers ───────────────────────────────────
    def _on_asr_mode_changed(self, _idx):
        is_cloud = self._asr_mode.currentData() == "cloud"
        self._asr_cloud_widget.setVisible(is_cloud)
        self.adjustSize()

    def _on_asr_preset_changed(self, idx):
        if idx < 0 or idx >= len(self._asr_presets):
            return
        p = self._asr_presets[idx]
        self._asr_base.setText(p["api_base"])
        self._asr_model.clear()
        self._asr_model.addItems(p["models"])

    def _on_llm_preset_changed(self, idx):
        if idx < 0 or idx >= len(self._llm_presets):
            return
        previous_idx = getattr(self, "_llm_active_preset_idx", None)
        if previous_idx is not None and 0 <= previous_idx < len(self._llm_presets):
            self._llm_saved_keys[previous_idx] = self._llm_key.text().strip()
        self._llm_active_preset_idx = idx
        p = self._llm_presets[idx]
        self._llm_base.setText(p["api_base"])
        self._llm_model.clear()
        self._llm_model.addItems(p["models"])
        self._llm_key.setText(self._llm_saved_keys.get(idx, ""))
        # Local servers usually need no key.
        is_local = "127.0.0.1" in p["api_base"] or "localhost" in p["api_base"]
        self._llm_key.setPlaceholderText(
            "本地服务可留空" if is_local else "粘贴翻译服务的 API Key"
        )

    def _browse_cache_path(self):
        path = QFileDialog.getExistingDirectory(
            self,
            t("group_cache_path"),
            self._cache_path.text().strip() or str(get_models_dir().resolve()),
        )
        if path:
            self._cache_path.setText(path)

    # ── Config assembly ──────────────────────────────────────────────
    def _build_model_config(self) -> dict:
        provider = self._llm_presets[self._llm_preset.currentIndex()]["provider"]
        model = self._llm_model.currentText().strip()
        api_base = self._llm_base.text().strip()
        # Qwen-MT rejects the system role; merge prompt into the user message.
        no_system_role = model.lower().startswith("qwen-mt")
        return {
            "name": model or "translator",
            "provider": provider,
            "api_base": api_base,
            "api_key": self._llm_key.text().strip(),
            "model": model,
            "streaming": True,
            "no_think": True,
            "no_system_role": no_system_role,
        }

    # ── Test connection ──────────────────────────────────────────────
    def _test_connection(self):
        model = self._llm_model.currentText().strip()
        if not model:
            self._test_label.setText("❌ 请先填写翻译模型名称")
            self._test_label.setStyleSheet(status_text_stylesheet("danger"))
            return
        self._test_btn.setEnabled(False)
        self._test_label.setStyleSheet("")
        self._test_label.setText("正在测试…")
        cfg = self._build_model_config()
        threading.Thread(
            target=self._test_worker, args=(cfg,), daemon=True
        ).start()

    def _test_worker(self, cfg: dict):
        try:
            from translator import test_connection

            result = test_connection(cfg, timeout=15)
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "message": str(e), "detail": ""}
        self._test_result_signal.emit(result)

    def _on_test_result(self, result: dict):
        self._test_btn.setEnabled(True)
        if result.get("ok"):
            detail = result.get("detail", "")
            txt = "✅ 连接成功"
            if detail:
                txt += f"（示例译文：{detail}）"
            self._test_label.setStyleSheet(status_text_stylesheet("success"))
            self._test_ok = True
        else:
            self._test_label.setStyleSheet(status_text_stylesheet("danger"))
            txt = f"❌ {result.get('message', '连接失败')}"
            self._test_ok = False
        self._test_label.setText(txt)

    # ── Finish ───────────────────────────────────────────────────────
    def _finish(self):
        model_cfg = self._build_model_config()
        if not model_cfg["model"]:
            QMessageBox.warning(self, t("window_setup"), "请填写翻译模型名称。")
            return
        is_local = (
            "127.0.0.1" in model_cfg["api_base"] or "localhost" in model_cfg["api_base"]
        )
        if not model_cfg["api_key"] and not is_local:
            r = QMessageBox.question(
                self,
                t("window_setup"),
                "翻译服务的 API Key 为空，确定继续吗？（之后可在设置里补填）",
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        if not getattr(self, "_test_ok", False):
            r = QMessageBox.question(
                self,
                t("window_setup"),
                "尚未测试通过翻译连接，确定直接完成吗？",
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        hub = "ms" if self._hub_combo.currentIndex() == 0 else "hf"
        set_models_dir(self._cache_path.text().strip())
        from control_panel import _save_settings
        from subtitle_presets import STYLE_PRESETS

        # ASR: local SenseVoice, or cloud API.
        if self._asr_mode.currentData() == "cloud":
            asr_preset = self._asr_presets[self._asr_preset.currentIndex()]
            asr_engine = asr_preset["engine"]
            asr_api = {
                "api_base": self._asr_base.text().strip() or asr_preset["api_base"],
                "api_key": self._asr_key.text().strip(),
                "model": self._asr_model.currentText().strip()
                or asr_preset["models"][0],
                "timeout": 30,
            }
        else:
            asr_engine = "sensevoice"
            asr_api = {
                "api_base": "https://api.openai.com/v1",
                "api_key": "",
                "model": "whisper-1",
                "timeout": 30,
            }

        settings = {
            "hub": hub,
            "asr_engine": asr_engine,
            "asr_api": asr_api,
            "asr_device": self._reco.get("device", "cpu"),
            "whisper_model_size": self._reco.get("whisper_size", "medium"),
            "vad_mode": "energy",
            "vad_threshold": 0.3,
            "energy_threshold": 0.02,
            "min_speech_duration": 1.0,
            "max_speech_duration": 8.0,
            "silence_mode": "auto",
            "silence_duration": 0.8,
            "asr_language": "auto",
            "target_language": "zh",
            "cache_path": self._cache_path.text().strip(),
            "models": [model_cfg],
            "active_model": 0,
            # Ship the warm paper-collage subtitle look by default.
            "style": dict(STYLE_PRESETS["paper_dark"]),
        }
        _save_settings(settings)
        self.accept()

    def _save_settings_and_accept(self):
        # Backward-compatible alias kept for any external callers.
        self._finish()


class ModelDownloadDialog(QDialog):
    """Download missing models (non-first-launch) with live log."""

    _log_signal = pyqtSignal(str)

    def __init__(self, missing_models, hub="ms", parent=None):
        super().__init__(parent)
        self.setStyleSheet(dialog_stylesheet())
        self.setWindowTitle(t("window_download"))
        self.setMinimumWidth(440)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.MSWindowsFixedSizeDialogHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        names = ", ".join(m["name"] for m in missing_models)
        self._status, self._progress, self._log_view = _build_loading_body(
            self, layout, t("downloading_models").format(names=names)
        )

        self._close_btn = QPushButton(t("btn_close"))
        self._close_btn.clicked.connect(self.reject)
        self._close_btn.hide()
        layout.addWidget(self._close_btn)

        self._missing = missing_models
        self._hub = hub
        self._error = None

        self._log_signal.connect(self._append_log)
        self._log_handler = _LogCapture(self._log_signal.emit)

        QTimer.singleShot(100, self._start_download)

    def _append_log(self, text):
        self._log_view.append(text)
        self._log_view.verticalScrollBar().setValue(
            self._log_view.verticalScrollBar().maximum()
        )

    def _start_download(self):
        logging.getLogger().addHandler(self._log_handler)
        self._orig_stderr = sys.stderr
        sys.stderr = _StderrCapture(self._log_signal.emit, self._orig_stderr)

        self._download_thread = threading.Thread(
            target=self._download_worker, daemon=True
        )
        self._download_thread.start()

        self._poll_timer = QTimer()
        self._poll_timer.setInterval(200)
        self._poll_timer.timeout.connect(self._check_done)
        self._poll_timer.start()

    def _download_worker(self):
        try:
            for m in self._missing:
                if m["type"] == "silero-vad":
                    download_silero()
                elif m["type"] in (
                    "sensevoice",
                    "funasr-nano",
                    "funasr-mlt-nano",
                    "anime-whisper",
                ):
                    download_asr(m["type"], hub=self._hub)
                elif m["type"].startswith("whisper-"):
                    size = m["type"].replace("whisper-", "")
                    download_asr("whisper", model_size=size, hub=self._hub)
        except Exception as e:
            self._error = str(e)
            log.error(f"Download failed: {e}", exc_info=True)

    def _check_done(self):
        if self._download_thread.is_alive():
            return
        self._poll_timer.stop()
        sys.stderr = self._orig_stderr
        logging.getLogger().removeHandler(self._log_handler)

        if self._error:
            self._progress.setRange(0, 1)
            self._progress.setValue(0)
            self._status.setText(t("download_failed").format(error=self._error))
            self._status.setStyleSheet(status_text_stylesheet("danger"))
            self._append_log(f"\n{t('download_failed').format(error=self._error)}")
            self._close_btn.show()
            return

        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self._status.setText(t("download_complete"))
        self._status.setStyleSheet(status_text_stylesheet("success"))
        self._append_log(f"\n{t('download_complete')}")
        QTimer.singleShot(500, self.accept)


class ModelEditDialog(QDialog):
    """Dialog for adding/editing a model configuration."""

    def __init__(self, parent=None, model_data=None):
        super().__init__(parent)
        self.setStyleSheet(dialog_stylesheet())
        self.setWindowTitle(
            t("dialog_edit_model") if model_data else t("dialog_add_model")
        )
        self.setMinimumWidth(500)

        root = QVBoxLayout(self)

        # --- Basic section ---
        basic_group = QGroupBox()
        basic_group.setFlat(True)
        layout = QFormLayout(basic_group)

        self._name = QLineEdit()
        self._provider = QComboBox()
        self._provider.addItem("OpenAI Compatible", "openai-compatible")
        self._provider.addItem("Ollama", "ollama")
        self._provider.currentIndexChanged.connect(self._on_provider_changed)
        self._api_base = QLineEdit()
        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._model = QLineEdit()

        self._proxy_mode = QComboBox()
        self._proxy_mode.addItems(
            [t("proxy_none"), t("proxy_system"), t("proxy_custom")]
        )
        self._proxy_mode.currentIndexChanged.connect(self._on_proxy_mode_changed)
        self._proxy_url = QLineEdit()
        self._proxy_url.setPlaceholderText("http://127.0.0.1:7890")
        self._proxy_url.setEnabled(False)

        self._no_system_role = QCheckBox(t("no_system_role"))
        self._no_system_role.setToolTip(t("no_system_role_hint"))
        self._no_think = QCheckBox(t("no_think"))
        self._no_think.setToolTip(t("no_think_hint"))
        self._no_think.setChecked(True)
        self._streaming = QCheckBox(t("streaming"))
        self._streaming.setToolTip(t("streaming_hint"))
        self._streaming.setChecked(True)
        self._json_response = QCheckBox(t("json_response"))
        self._json_response.setToolTip(t("json_response_hint"))
        self._context_turns = QSpinBox()
        self._context_turns.setRange(0, 20)
        self._context_turns.setValue(0)
        self._context_turns.setToolTip(t("context_turns_hint"))

        price_suffix = t("price_suffix")
        self._input_price = QDoubleSpinBox()
        self._input_price.setRange(0, 999)
        self._input_price.setDecimals(2)
        self._input_price.setSuffix(price_suffix)
        self._input_price.setSpecialValueText("—")
        self._output_price = QDoubleSpinBox()
        self._output_price.setRange(0, 999)
        self._output_price.setDecimals(2)
        self._output_price.setSuffix(price_suffix)
        self._output_price.setSpecialValueText("—")

        price_row = QHBoxLayout()
        price_row.addWidget(QLabel(t("label_input_price")))
        price_row.addWidget(self._input_price)
        price_row.addWidget(QLabel(t("label_output_price")))
        price_row.addWidget(self._output_price)

        layout.addRow(t("label_display_name"), self._name)
        layout.addRow("Provider", self._provider)
        layout.addRow(t("label_api_base"), self._api_base)
        layout.addRow(t("label_api_key"), self._api_key)
        layout.addRow(t("label_model"), self._model)
        layout.addRow(t("label_proxy"), self._proxy_mode)
        layout.addRow(t("label_proxy_url"), self._proxy_url)
        layout.addRow(t("label_pricing"), price_row)
        layout.addRow(t("label_context_turns"), self._context_turns)
        layout.addRow("", self._streaming)
        layout.addRow("", self._json_response)
        layout.addRow("", self._no_system_role)
        layout.addRow("", self._no_think)

        root.addWidget(basic_group)

        # --- Advanced section ---
        adv_group = QGroupBox(t("label_advanced_params"))
        adv_layout = QFormLayout(adv_group)
        adv_group.setToolTip(t("override_hint"))

        self._adv_temperature = QDoubleSpinBox()
        self._adv_temperature.setRange(0.0, 2.0)
        self._adv_temperature.setDecimals(2)
        self._adv_temperature.setSingleStep(0.1)
        self._adv_temperature.setValue(0.3)

        self._adv_top_p = QDoubleSpinBox()
        self._adv_top_p.setRange(0.0, 1.0)
        self._adv_top_p.setDecimals(2)
        self._adv_top_p.setSingleStep(0.05)
        self._adv_top_p.setValue(1.0)

        self._adv_max_tokens = QSpinBox()
        self._adv_max_tokens.setRange(1, 32768)
        self._adv_max_tokens.setValue(256)

        self._adv_freq_penalty = QDoubleSpinBox()
        self._adv_freq_penalty.setRange(-2.0, 2.0)
        self._adv_freq_penalty.setDecimals(2)
        self._adv_freq_penalty.setSingleStep(0.1)

        self._adv_presence_penalty = QDoubleSpinBox()
        self._adv_presence_penalty.setRange(-2.0, 2.0)
        self._adv_presence_penalty.setDecimals(2)
        self._adv_presence_penalty.setSingleStep(0.1)

        self._adv_seed = QSpinBox()
        self._adv_seed.setRange(0, 2_000_000_000)

        self._adv_rows = {
            "temperature": self._make_override_row(self._adv_temperature),
            "top_p": self._make_override_row(self._adv_top_p),
            "max_tokens": self._make_override_row(self._adv_max_tokens),
            "frequency_penalty": self._make_override_row(self._adv_freq_penalty),
            "presence_penalty": self._make_override_row(self._adv_presence_penalty),
            "seed": self._make_override_row(self._adv_seed),
        }
        adv_layout.addRow(t("label_temperature"), self._adv_rows["temperature"][1])
        adv_layout.addRow(t("label_top_p"), self._adv_rows["top_p"][1])
        adv_layout.addRow(t("label_max_tokens"), self._adv_rows["max_tokens"][1])
        adv_layout.addRow(
            t("label_frequency_penalty"), self._adv_rows["frequency_penalty"][1]
        )
        adv_layout.addRow(
            t("label_presence_penalty"), self._adv_rows["presence_penalty"][1]
        )
        adv_layout.addRow(t("label_seed"), self._adv_rows["seed"][1])

        self._adv_extra_body = QTextEdit()
        self._adv_extra_body.setPlaceholderText(
            '{"thinking_budget": 1024}'
        )
        self._adv_extra_body.setToolTip(t("extra_body_hint"))
        self._adv_extra_body.setFixedHeight(70)
        adv_layout.addRow(t("label_extra_body"), self._adv_extra_body)

        root.addWidget(adv_group)

        # --- Populate from model_data ---
        if model_data:
            self._name.setText(model_data.get("name", ""))
            provider = model_data.get("provider", "openai-compatible")
            idx = self._provider.findData(provider)
            if idx >= 0:
                self._provider.setCurrentIndex(idx)
            self._api_base.setText(model_data.get("api_base", ""))
            self._api_key.setText(model_data.get("api_key", ""))
            self._model.setText(model_data.get("model", ""))
            proxy = model_data.get("proxy", "none")
            if proxy == "system":
                self._proxy_mode.setCurrentIndex(1)
            elif proxy not in ("none", "system") and proxy:
                self._proxy_mode.setCurrentIndex(2)
                self._proxy_url.setText(proxy)
            else:
                self._proxy_mode.setCurrentIndex(0)
            self._no_system_role.setChecked(model_data.get("no_system_role", False))
            self._no_think.setChecked(model_data.get("no_think", True))
            self._streaming.setChecked(model_data.get("streaming", True))
            self._json_response.setChecked(model_data.get("json_response", False))
            self._context_turns.setValue(model_data.get("context_turns", 0))
            self._input_price.setValue(model_data.get("input_price", 0))
            self._output_price.setValue(model_data.get("output_price", 0))

            overrides = model_data.get("overrides") or {}
            for key, (cb, _row, widget) in self._adv_rows.items():
                if key in overrides and overrides[key] is not None:
                    cb.setChecked(True)
                    if isinstance(widget, QSpinBox):
                        widget.setValue(int(overrides[key]))
                    else:
                        widget.setValue(float(overrides[key]))
            extra_body = model_data.get("extra_body")
            if extra_body:
                try:
                    self._adv_extra_body.setPlainText(
                        json.dumps(extra_body, ensure_ascii=False, indent=2)
                    )
                except (TypeError, ValueError):
                    pass
            self._on_provider_changed(self._provider.currentIndex())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _make_override_row(self, widget):
        """Build a [checkbox + widget] row that disables the widget when unchecked."""
        cb = QCheckBox(t("override_enable"))
        widget.setEnabled(False)
        cb.toggled.connect(widget.setEnabled)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(cb)
        h.addWidget(widget, 1)
        return cb, row, widget

    def _on_proxy_mode_changed(self, index):
        self._proxy_url.setEnabled(index == 2)

    def _on_provider_changed(self, _index):
        provider = self._provider.currentData() or "openai-compatible"
        if provider == "ollama":
            self._api_key.setEnabled(False)
            self._api_key.setPlaceholderText("Ollama 不需要 API Key")
            if not self._api_base.text().strip():
                self._api_base.setText("http://localhost:11434/api/chat")
            if not self._model.text().strip():
                self._model.setText("qwen2.5:7b")
        else:
            self._api_key.setEnabled(True)
            self._api_key.setPlaceholderText("")

    def _parse_extra_body(self):
        """Return (ok, data_or_error_msg). Empty text → (True, None)."""
        text = self._adv_extra_body.toPlainText().strip()
        if not text:
            return True, None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return False, f"{e}"
        if not isinstance(data, dict):
            return False, "extra_body must be a JSON object"
        return True, data

    def _on_accept(self):
        ok, _ = self._parse_extra_body()
        if not ok:
            QMessageBox.warning(
                self, t("error_title"), t("extra_body_invalid")
            )
            return
        self.accept()

    def get_data(self) -> dict:
        proxy_idx = self._proxy_mode.currentIndex()
        if proxy_idx == 1:
            proxy = "system"
        elif proxy_idx == 2:
            proxy = self._proxy_url.text().strip() or "none"
        else:
            proxy = "none"
        result = {
            "name": self._name.text().strip(),
            "provider": self._provider.currentData() or "openai-compatible",
            "api_base": self._api_base.text().strip(),
            "api_key": self._api_key.text().strip(),
            "model": self._model.text().strip(),
            "proxy": proxy,
        }
        if self._no_system_role.isChecked():
            result["no_system_role"] = True
        if not self._no_think.isChecked():
            result["no_think"] = False
        if not self._streaming.isChecked():
            result["streaming"] = False
        if self._json_response.isChecked():
            result["json_response"] = True
        if self._context_turns.value() > 0:
            result["context_turns"] = self._context_turns.value()
        if self._input_price.value() > 0:
            result["input_price"] = self._input_price.value()
        if self._output_price.value() > 0:
            result["output_price"] = self._output_price.value()

        overrides = {}
        for key, (cb, _row, widget) in self._adv_rows.items():
            if cb.isChecked():
                val = widget.value()
                if isinstance(widget, QDoubleSpinBox):
                    val = round(val, 2)
                overrides[key] = val
        if overrides:
            result["overrides"] = overrides

        ok, data = self._parse_extra_body()
        if ok and data:
            result["extra_body"] = data
        return result


class TTSConfigEditDialog(QDialog):
    """Dialog for adding/editing a TTS voice/API configuration profile."""

    _fetch_done = pyqtSignal(bool, object, str)

    def __init__(self, parent=None, profile_data=None, target_language="zh"):
        super().__init__(parent)
        self.setStyleSheet(dialog_stylesheet())
        self.setWindowTitle(
            t("tts_dialog_edit") if profile_data else t("tts_dialog_add")
        )
        self.setMinimumWidth(480)
        self._target_language = target_language or "zh"

        root = QVBoxLayout(self)

        form_group = QGroupBox()
        form_group.setFlat(True)
        layout = QFormLayout(form_group)

        self._name = QLineEdit()
        self._engine = QComboBox()
        self._engine.addItem(t("tts_engine_edge"), "edge")
        self._engine.addItem(t("tts_engine_openai"), "openai-speech")
        self._engine.addItem(t("tts_engine_azure"), "azure")
        self._engine.addItem(t("tts_engine_elevenlabs"), "elevenlabs")
        self._engine.currentIndexChanged.connect(self._on_engine_changed)

        # Voice/model: an editable combo so users can type or pick a fetched value.
        self._voice = QComboBox()
        self._voice.setEditable(True)
        self._voice.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._voice.setPlaceholderText(t("tts_voice_placeholder"))

        self._model = QComboBox()
        self._model.setEditable(True)
        self._model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._model.setEditText("tts-1")

        self._fetch_btn = QPushButton(t("tts_btn_fetch"))
        self._fetch_btn.clicked.connect(self._on_fetch)
        self._fetch_status = QLabel("")
        self._fetch_status.setWordWrap(True)

        self._api_base = QLineEdit()
        self._api_base.setPlaceholderText("https://api.openai.com/v1")
        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)

        self._region = QLineEdit("eastus")
        self._region.setPlaceholderText("eastus")

        self._proxy_mode = QComboBox()
        self._proxy_mode.addItems(
            [t("proxy_none"), t("proxy_system"), t("proxy_custom")]
        )
        self._proxy_mode.currentIndexChanged.connect(self._on_proxy_mode_changed)
        self._proxy_url = QLineEdit()
        self._proxy_url.setPlaceholderText("http://127.0.0.1:7890")
        self._proxy_url.setEnabled(False)

        self._rate = QLineEdit("+0%")
        self._rate.setPlaceholderText("+0%")

        self._speed = QDoubleSpinBox()
        self._speed.setRange(0.25, 4.0)
        self._speed.setSingleStep(0.05)
        self._speed.setValue(1.0)

        layout.addRow(t("label_display_name"), self._name)
        layout.addRow(t("label_tts_engine"), self._engine)
        layout.addRow(t("label_tts_voice"), self._voice)
        # Model row (openai-speech only)
        self._model_label = QLabel(t("label_tts_model"))
        layout.addRow(self._model_label, self._model)
        # Fetch button row
        fetch_row = QWidget()
        fetch_h = QHBoxLayout(fetch_row)
        fetch_h.setContentsMargins(0, 0, 0, 0)
        fetch_h.addWidget(self._fetch_btn)
        fetch_h.addWidget(self._fetch_status, 1)
        layout.addRow("", fetch_row)
        # API rows (openai-speech only)
        self._api_base_label = QLabel(t("label_tts_api_base"))
        layout.addRow(self._api_base_label, self._api_base)
        self._api_key_label = QLabel(t("label_tts_api_key"))
        layout.addRow(self._api_key_label, self._api_key)
        self._region_label = QLabel(t("label_tts_region"))
        layout.addRow(self._region_label, self._region)
        self._proxy_label = QLabel(t("label_proxy"))
        layout.addRow(self._proxy_label, self._proxy_mode)
        self._proxy_url_label = QLabel(t("label_proxy_url"))
        layout.addRow(self._proxy_url_label, self._proxy_url)
        # Rate (edge only)
        self._rate_label = QLabel(t("label_tts_rate"))
        layout.addRow(self._rate_label, self._rate)
        # Speed (openai-speech only)
        self._speed_label = QLabel(t("label_tts_speed"))
        layout.addRow(self._speed_label, self._speed)

        root.addWidget(form_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._fetch_done.connect(self._on_fetch_done)

        # Populate
        if profile_data:
            self._name.setText(profile_data.get("name", ""))
            eidx = self._engine.findData(profile_data.get("engine", "edge"))
            if eidx >= 0:
                self._engine.setCurrentIndex(eidx)
            self._voice.setEditText(profile_data.get("voice", ""))
            self._model.setEditText(profile_data.get("model", "tts-1"))
            self._api_base.setText(
                profile_data.get("api_base", "https://api.openai.com/v1")
            )
            self._api_key.setText(profile_data.get("api_key", ""))
            self._region.setText(profile_data.get("region", "eastus"))
            proxy = profile_data.get("proxy", "none")
            if proxy == "system":
                self._proxy_mode.setCurrentIndex(1)
            elif proxy not in ("none", "system") and proxy:
                self._proxy_mode.setCurrentIndex(2)
                self._proxy_url.setText(proxy)
            self._rate.setText(profile_data.get("rate", "+0%"))
            self._speed.setValue(float(profile_data.get("speed", 1.0)))
        else:
            self._name.setText("Edge")

        self._on_engine_changed(self._engine.currentIndex())

    def _on_engine_changed(self, _index=0):
        from tts_providers import ENGINE_CAPS

        engine = self._engine.currentData() or "edge"
        caps = ENGINE_CAPS.get(engine, ENGINE_CAPS["edge"])
        fields = caps.get("fields", set())
        # Map each optional widget+label to the capability field that enables it.
        field_widgets = {
            "voice": (self._voice,),
            "model": (self._model, self._model_label),
            "api_base": (self._api_base, self._api_base_label),
            "api_key": (self._api_key, self._api_key_label),
            "region": (self._region, self._region_label),
            "proxy": (
                self._proxy_mode,
                self._proxy_label,
                self._proxy_url,
                self._proxy_url_label,
            ),
            "rate": (self._rate, self._rate_label),
            "speed": (self._speed, self._speed_label),
        }
        for field, widgets in field_widgets.items():
            visible = field in fields
            for w in widgets:
                w.setVisible(visible)
        if "proxy" in fields:
            self._proxy_url.setEnabled(self._proxy_mode.currentIndex() == 2)
        # Fetch button: label depends on what the engine exposes; hidden when
        # there is nothing to fetch.
        fetch_kind = caps.get("fetch")
        self._fetch_btn.setVisible(bool(fetch_kind))
        self._fetch_status.setVisible(bool(fetch_kind))
        if fetch_kind == "models":
            self._fetch_btn.setText(t("tts_btn_fetch_models"))
        elif fetch_kind == "voices":
            self._fetch_btn.setText(t("tts_btn_fetch_voices"))
        # Sensible API-base placeholder per engine.
        self._api_base.setPlaceholderText(
            {
                "elevenlabs": "https://api.elevenlabs.io",
                "azure": "https://<region>.tts.speech.microsoft.com",
            }.get(engine, "https://api.openai.com/v1")
        )

    def _on_proxy_mode_changed(self, index):
        self._proxy_url.setEnabled(index == 2)

    def _current_proxy(self) -> str:
        idx = self._proxy_mode.currentIndex()
        if idx == 1:
            return "system"
        if idx == 2:
            return self._proxy_url.text().strip() or "none"
        return "none"

    def _on_fetch(self):
        engine = self._engine.currentData() or "edge"
        self._fetch_btn.setEnabled(False)
        self._fetch_status.setStyleSheet(status_text_stylesheet("muted"))
        self._fetch_status.setText(t("tts_fetching"))
        api_base = self._api_base.text().strip()
        api_key = self._api_key.text().strip()
        region = self._region.text().strip() or "eastus"
        proxy = self._current_proxy()
        lang = self._target_language

        def _run():
            try:
                from tts_providers import fetch_tts_items

                kind, items = fetch_tts_items(
                    engine,
                    api_base=api_base,
                    api_key=api_key,
                    region=region,
                    proxy=proxy,
                    locale_prefix=lang,
                )
                if not items:
                    self._fetch_done.emit(False, kind, t("tts_fetch_empty"))
                    return
                self._fetch_done.emit(
                    True, (kind, items), t("tts_fetch_ok").format(n=len(items))
                )
            except Exception as e:
                self._fetch_done.emit(False, "", f"{e}")

        threading.Thread(target=_run, daemon=True).start()

    def _on_fetch_done(self, ok: bool, payload: object, msg: str):
        self._fetch_btn.setEnabled(True)
        self._fetch_status.setStyleSheet(
            status_text_stylesheet("success" if ok else "danger")
        )
        self._fetch_status.setText(msg)
        if not ok:
            return
        kind, items = payload
        combo = self._model if kind == "models" else self._voice
        current = combo.currentText().strip()
        names = [str(x) for x in items if str(x).strip()]
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(names)
        if current and current in names:
            combo.setCurrentIndex(names.index(current))
        elif current:
            combo.setEditText(current)
        combo.blockSignals(False)
        combo.showPopup()

    def _on_accept(self):
        if not self._name.text().strip():
            QMessageBox.warning(self, t("error_title"), t("tts_name_required"))
            return
        self.accept()

    def get_data(self) -> dict:
        engine = self._engine.currentData() or "edge"
        result = {
            "name": self._name.text().strip(),
            "engine": engine,
            "voice": self._voice.currentText().strip(),
            "rate": self._rate.text().strip() or "+0%",
            "api_base": self._api_base.text().strip()
            or "https://api.openai.com/v1",
            "api_key": self._api_key.text().strip(),
            "model": self._model.currentText().strip() or "tts-1",
            "speed": round(self._speed.value(), 2),
            "proxy": self._current_proxy(),
            "region": self._region.text().strip() or "eastus",
        }
        return result


_I18N_DIR = resource_path("i18n")


def _changelog_to_html(text: str) -> str:
    """Convert CHANGELOG.md subset to HTML (headings, bold, lists)."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            lines.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            lines.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            continue  # skip file title
        elif stripped.startswith("- "):
            item = stripped[2:]
            item = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", item)
            item = re.sub(r"`(.+?)`", r"<code>\1</code>", item)
            lines.append(f"<li>{item}</li>")
        elif stripped:
            lines.append(f"<p>{stripped}</p>")
    return "\n".join(lines)


def _load_latest_changelog() -> tuple[str, str]:
    """Return (first_h2_title, html) for the latest changelog. Uses i18n lang."""
    from i18n import get_lang
    lang = get_lang()
    path = _I18N_DIR / f"CHANGELOG_{lang}.md"
    if not path.exists():
        path = _I18N_DIR / "CHANGELOG_en.md"
    if not path.exists():
        return "", ""
    text = path.read_text("utf-8")
    # First H2 (## date) is the latest entry and serves as the tracking key
    m = re.search(r"^## (.+)$", text, re.MULTILINE)
    if not m:
        return "", ""
    title = m.group(1).strip()
    # Drop the top-level file heading (# Title) — keep everything from first H2 onwards
    body = text[m.start():]
    return title, _changelog_to_html(body)


