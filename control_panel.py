import json
import logging
import os
import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSize
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from benchmark import run_benchmark
from diagnostics import create_diagnostic_bundle
from exporters import export_subtitles, subtitle_rows_to_export_items
from dialogs import (
    ModelEditDialog,
)
from model_manager import (
    dir_size,
    format_size,
    get_cache_entries,
    get_models_dir,
)
from i18n import t, LANGUAGES
from subtitle_settings import SubtitleSettingsWidget
from history import HistoryStore
from logging_utils import redact_secret_data
from selection_translate import SelectionTranslateService
from runtime_paths import data_path, resource_path
from theme import (
    Color,
    console_stylesheet,
    empty_state_text_stylesheet,
    empty_state_title_stylesheet,
    hint_text_stylesheet,
    info_banner_stylesheet,
    nav_sidebar_stylesheet,
    nav_svg_icon,
    paper_card_stylesheet,
    panel_stylesheet,
    secondary_button_stylesheet,
    section_title_stylesheet,
    status_color,
    status_text_stylesheet,
    step_badge_stylesheet,
    step_card_subtitle_stylesheet,
    step_card_title_stylesheet,
    tips_card_title_stylesheet,
    ui_font_family,
)

log = logging.getLogger("LiveTranslate.Panel")

SETTINGS_FILE = data_path("user_settings.json")


# UI font resolution now lives in theme.ui_font_family(); kept as a module-level
# alias for backward compatibility with existing call sites.
_ui_font_family = ui_font_family


def _migrate_style(data: dict) -> dict:
    """One-time visual migration to the Paper Collage look.

    Older installs shipped the plain black "default" subtitle style. If the
    saved style is still that untouched old default (preset == "default" with a
    black background and no accent_color), upgrade it to the warm paper_dark
    preset so the paper-collage redesign is actually visible. A user who picked
    any other preset, or customized colors, is left untouched.
    """
    try:
        from subtitle_presets import STYLE_PRESETS

        style = data.get("style")
        if not isinstance(style, dict):
            return data
        is_old_default = (
            style.get("preset", "default") == "default"
            and str(style.get("bg_color", "")).lower() in ("#000000", "")
            and "accent_color" not in style
        )
        if is_old_default:
            data["style"] = dict(STYLE_PRESETS["paper_dark"])
            log.info("Migrated subtitle style: default -> paper_dark")
    except Exception as e:
        log.warning(f"Style migration skipped: {e}")
    return data


def _load_saved_settings() -> dict | None:
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            log.info(f"Loaded saved settings from {SETTINGS_FILE}")
            return _migrate_style(data)
    except Exception as e:
        log.warning(f"Failed to load settings: {e}")
    return None


def _save_settings(settings: dict):
    try:
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(SETTINGS_FILE)
        log.info(f"Settings saved to {SETTINGS_FILE}")
    except Exception as e:
        log.warning(f"Failed to save settings: {e}")


def _glossary_to_text(glossary) -> str:
    """Render a stored glossary dict as editable 'term => translation' lines."""
    if isinstance(glossary, dict):
        items = glossary.items()
    elif isinstance(glossary, (list, tuple)):
        items = [
            (e[0], e[1])
            for e in glossary
            if isinstance(e, (list, tuple)) and len(e) == 2
        ]
    else:
        return ""
    return "\n".join(f"{term} => {translation}" for term, translation in items)


def _text_to_glossary(text: str) -> dict:
    """Parse 'term => translation' lines into a dict. Tolerates '=>', '=', '：', ':'."""
    glossary: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        term = translation = None
        for sep in ("=>", "==", "\t", "：", ":", "="):
            if sep in line:
                left, _, right = line.partition(sep)
                term, translation = left.strip(), right.strip()
                break
        if term and translation:
            glossary[term] = translation
    return glossary


class NoScrollComboBox(QComboBox):
    """QComboBox that ignores mouse-wheel scrolling.

    Prevents the wheel from accidentally switching the selected provider/model
    when the pointer merely passes over the box while the user scrolls the page.
    The dropdown popup still scrolls normally (it is a separate view), and the
    wheel event is passed to the parent so the page can scroll instead.
    """

    def wheelEvent(self, event):  # noqa: N802 (Qt override)
        event.ignore()


class ControlPanel(QWidget):
    """Settings and monitoring panel."""

    settings_changed = pyqtSignal(dict)
    model_changed = pyqtSignal(dict)
    models_list_changed = pyqtSignal(list, int)
    subtitle_settings_changed = pyqtSignal(dict)
    tts_enabled_changed = pyqtSignal(bool)
    _bench_result = pyqtSignal(str)
    _cache_result = pyqtSignal(list)
    _manual_translate_result = pyqtSignal(dict)
    _manual_translate_error = pyqtSignal(str)
    _audio_translate_progress = pyqtSignal(int, int, str)
    _audio_translate_result = pyqtSignal(object)
    _audio_translate_error = pyqtSignal(str)
    _test_connection_result = pyqtSignal(dict)
    _home_asr_test_done = pyqtSignal(bool, str)
    _home_asr_models_done = pyqtSignal(bool, object, str)
    _home_llm_test_done = pyqtSignal(bool, str)
    _home_llm_models_done = pyqtSignal(bool, object, str)
    _tts_test_done = pyqtSignal(bool, str)
    reset_positions = pyqtSignal()

    def __init__(self, config, saved_settings=None):
        super().__init__()
        self._config = config
        self.setWindowTitle(t("window_control_panel"))
        self._ui_font = _ui_font_family()
        self.setFont(QFont(self._ui_font, 10))
        self.setMinimumSize(1040, 720)
        self.resize(1120, 760)
        self._history_store = HistoryStore(data_path("data", "history.db"))
        self._history_rows = []
        self._manual_translate_result.connect(self._on_manual_translate_result)
        self._manual_translate_error.connect(self._on_manual_translate_error)
        self._audio_translate_progress.connect(self._on_audio_translate_progress)
        self._audio_translate_result.connect(self._on_audio_translate_result)
        self._audio_translate_error.connect(self._on_audio_translate_error)
        self._test_connection_result.connect(self._on_test_connection_result)
        self._home_asr_test_done.connect(self._on_asr_test_done)
        self._home_asr_models_done.connect(self._on_asr_models_done)
        self._home_llm_test_done.connect(self._on_llm_test_done)
        self._home_llm_models_done.connect(self._on_llm_models_done)

        saved = saved_settings or _load_saved_settings()
        if saved:
            self._current_settings = saved
        else:
            tc = config["translation"]
            self._current_settings = {
                "vad_mode": "energy",
                "vad_threshold": config["asr"]["vad_threshold"],
                "energy_threshold": 0.02,
                "min_speech_duration": config["asr"]["min_speech_duration"],
                "max_speech_duration": config["asr"]["max_speech_duration"],
                "silence_mode": "auto",
                "silence_duration": 0.8,
                "asr_language": config["asr"].get("language", "auto"),
                "asr_engine": config["asr"].get("engine", "openai-audio"),
                "asr_device": "cpu",
                "asr_api": config.get(
                    "asr_api",
                    {
                        "api_base": "https://api.openai.com/v1",
                        "api_key": "",
                        "model": "whisper-1",
                        "timeout": 30,
                    },
                ),
                "models": [
                    {
                        "name": f"{tc['model']}",
                        "provider": tc.get("provider", "openai-compatible"),
                        "api_base": tc["api_base"],
                        "api_key": tc["api_key"],
                        "model": tc["model"],
                    }
                ],
                "active_model": 0,
                "hub": "ms",
                "translation_mode": config["translation"].get("mode", "natural"),
            }

        if "models" not in self._current_settings:
            tc = config["translation"]
            self._current_settings["models"] = [
                {
                    "name": f"{tc['model']}",
                    "provider": tc.get("provider", "openai-compatible"),
                    "api_base": tc["api_base"],
                    "api_key": tc["api_key"],
                    "model": tc["model"],
                }
            ]
            self._current_settings["active_model"] = 0
        self._current_settings.setdefault(
            "translation_mode", config["translation"].get("mode", "natural")
        )
        self._current_settings.setdefault(
            "hotkey",
            config.get("hotkey", {"enabled": True, "shortcut": "Ctrl+Shift+T"}),
        )
        self._current_settings.setdefault(
            "privacy",
            config.get("privacy", {"history_enabled": True}),
        )
        self._current_settings.setdefault(
            "local_api",
            config.get("local_api", {"host": "127.0.0.1", "port": 17891, "token": ""}),
        )
        self._current_settings.setdefault(
            "asr_api",
            config.get(
                "asr_api",
                {
                    "api_base": "https://api.openai.com/v1",
                    "api_key": "",
                    "model": "whisper-1",
                    "timeout": 30,
                },
            ),
        )
        self._current_settings.setdefault(
            "tts",
            config.get(
                "tts",
                {
                    "enabled": False,
                    "volume": 1.0,
                    "output_device": "",
                    "active_profile": 0,
                    "profiles": [
                        {
                            "name": "Edge",
                            "engine": "edge",
                            "voice": "",
                            "rate": "+0%",
                            "api_base": "https://api.openai.com/v1",
                            "api_key": "",
                            "model": "tts-1",
                            "speed": 1.0,
                            "proxy": "none",
                        }
                    ],
                },
            ),
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._apply_easy_mode_style()

        # New information architecture: a single window with a grouped left
        # sidebar (QListWidget) driving a QStackedWidget. This replaces the old
        # "5 top tabs + a separate Advanced window with 7 sub-tabs" layout
        # (12 fragmented panels). Each page is still produced by its original
        # _create_*_tab() method untouched — only the container changed.
        from PyQt6.QtWidgets import QStackedWidget, QSplitter
        from PyQt6.QtGui import QColor

        sidebar = QWidget()
        sidebar.setObjectName("paperSidebar")
        sidebar.setFixedWidth(230)
        sidebar.setStyleSheet(
            f"QWidget#paperSidebar {{ background: {Color.SURFACE_ALT};"
            f" border-right: 1px solid {Color.BORDER}; }}"
        )
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(14, 18, 14, 16)
        sidebar_layout.setSpacing(12)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand_row.setSpacing(10)
        logo = QLabel()
        logo.setFixedSize(28, 28)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_path = resource_path(
            "assets", "paper_collage", "decorative", "cropped",
            "sidebar_logo_from_ref.png"
        )
        if logo_path.exists():
            from PyQt6.QtGui import QPixmap

            logo_pix = QPixmap(str(logo_path))
            if not logo_pix.isNull():
                logo.setPixmap(
                    logo_pix.scaled(
                        28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        logo.setStyleSheet("background: transparent;")
        brand_row.addWidget(logo)
        brand_title = QLabel("LiveTranslate")
        brand_title.setStyleSheet(
            f"color: {Color.TEXT_STRONG}; background: transparent;"
            "font-size: 17px; font-weight: 800;"
        )
        brand_row.addWidget(brand_title)
        version = QLabel("v1.2.0")
        version.setStyleSheet(
            f"color: {Color.TEXT_MUTED}; background: transparent; font-size: 10px;"
        )
        brand_row.addWidget(version, 0, Qt.AlignmentFlag.AlignBottom)
        brand_row.addStretch(1)
        sidebar_layout.addLayout(brand_row)

        self._nav = QListWidget()
        self._nav.setStyleSheet(nav_sidebar_stylesheet())
        self._nav.setFont(QFont(self._ui_font, 9))
        self._nav.setFrameShape(QFrame.Shape.NoFrame)
        self._nav.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._stack = QStackedWidget()

        # (i18n section key, [(i18n page key, factory), ...])
        nav_spec = [
            ("nav_section_common", [
                ("nav_home", self._create_home_tab),
                ("nav_manual_translate", self._create_manual_translate_tab),
                ("nav_audio_translate", self._create_audio_translate_tab),
            ]),
            ("nav_section_appearance", [
                ("nav_overlay_style", self._create_style_tab),
                ("nav_subtitle_window", self._create_subtitle_tab),
            ]),
            ("nav_section_engine", [
                ("nav_asr_vad", self._create_vad_tab),
                ("nav_translation", self._create_translation_tab),
            ]),
            ("nav_section_voice", [
                ("nav_tts", self._create_tts_tab),
            ]),
            ("nav_section_data", [
                ("nav_history", self._create_history_tab),
            ]),
            ("nav_section_tools", [
                ("nav_benchmark", self._create_benchmark_tab),
                ("nav_diagnostics", self._create_diagnostics_tab),
                ("nav_cache", self._create_cache_tab),
                ("nav_changelog", self._create_changelog_tab),
            ]),
        ]

        self._nav_page_index = {}  # i18n page key -> stack index
        self._cache_page_index = None
        # Paper-collage line icons (rendered tinted per nav state). Maps the
        # i18n page key to its SVG basename under assets/paper_collage/icons/svg.
        nav_icon_map = {
            "nav_home": "home",
            "nav_manual_translate": "manual_translate",
            "nav_audio_translate": "audio_file",
            "nav_overlay_style": "subtitle_style",
            "nav_subtitle_window": "subtitle_window",
            "nav_asr_vad": "asr_vad",
            "nav_translation": "translation_service",
            "nav_tts": "tts",
            "nav_history": "history",
            "nav_benchmark": "benchmark",
            "nav_diagnostics": "diagnostics",
            "nav_cache": "cache",
            "nav_changelog": "changelog",
        }
        self._nav.setIconSize(QSize(20, 20))
        for section_key, pages in nav_spec:
            header = QListWidgetItem(t(section_key).upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)  # non-selectable header
            header.setForeground(QColor(Color.TEXT_FAINT))
            self._nav.addItem(header)
            for page_key, factory in pages:
                page = factory()
                stack_idx = self._stack.addWidget(page)
                self._nav_page_index[page_key] = stack_idx
                item = QListWidgetItem("  " + t(page_key))
                item.setData(Qt.ItemDataRole.UserRole, stack_idx)
                icon_name = nav_icon_map.get(page_key)
                if icon_name:
                    svg_path = resource_path(
                        "assets", "paper_collage", "icons", "svg", f"{icon_name}.svg"
                    )
                    icon = nav_svg_icon(str(svg_path))
                    if not icon.isNull():
                        item.setIcon(icon)
                self._nav.addItem(item)
                if page_key == "nav_cache":
                    self._cache_page_index = stack_idx

        self._nav.currentItemChanged.connect(self._on_nav_changed)

        sidebar_layout.addWidget(self._nav, 1)

        sidebar_footer = QHBoxLayout()
        sidebar_footer.setContentsMargins(2, 0, 2, 0)
        sidebar_footer.setSpacing(12)
        for glyph, tip in (("⚙", t("settings")), ("?", t("about"))):
            b = QPushButton(glyph)
            b.setFixedSize(28, 28)
            b.setToolTip(tip)
            b.setStyleSheet(
                "QPushButton { background: transparent; border: 0;"
                f" color: {Color.TEXT_SUBTLE}; font-size: 17px; }}"
                f"QPushButton:hover {{ color: {Color.TEXT_STRONG};"
                f" background: {Color.OLIVE_SOFT}; border-radius: 14px; }}"
            )
            sidebar_footer.addWidget(b)
        sidebar_footer.addStretch(1)
        sidebar_layout.addLayout(sidebar_footer)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(sidebar)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(22, 22, 22, 22)
        content_layout.addWidget(self._stack)
        splitter.addWidget(content)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(0, False)
        layout.addWidget(splitter)

        # Select the first real (non-header) entry.
        for i in range(self._nav.count()):
            if self._nav.item(i).flags() != Qt.ItemFlag.NoItemFlags:
                self._nav.setCurrentRow(i)
                break

        self._bench_result.connect(self._on_bench_result)
        self._cache_result.connect(self._on_cache_result)
        self._tts_test_done.connect(self._on_tts_test_done)

        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self._do_auto_save)

        # Fit initial height based on whisper group visibility
        QTimer.singleShot(0, lambda: self.resize(self.width(), self.sizeHint().height() + 20))
        QTimer.singleShot(0, self._refresh_home_status)

    def _on_nav_changed(self, current, _previous):
        """Sidebar selection → switch stacked page; refresh cache on demand."""
        if current is None:
            return
        idx = current.data(Qt.ItemDataRole.UserRole)
        if idx is None:
            return
        self._stack.setCurrentIndex(idx)
        if self._cache_page_index is not None and idx == self._cache_page_index:
            self._refresh_cache()

    def _apply_easy_mode_style(self):
        self.setStyleSheet(panel_stylesheet(self._ui_font))

    def _show_advanced_settings(self):
        """Backward-compat: the old standalone "advanced" window is gone; the
        former advanced sub-tabs now live in the sidebar. Jump to ASR & VAD."""
        idx = self._nav_page_index.get("nav_asr_vad")
        if idx is None:
            return
        for i in range(self._nav.count()):
            if self._nav.item(i).data(Qt.ItemDataRole.UserRole) == idx:
                self._nav.setCurrentRow(i)
                break
        self.show()
        self.raise_()
        self.activateWindow()

    def _create_home_tab(self):
        from home_presets import (
            ASR_PRESETS,
            LLM_PRESETS,
            find_asr_preset,
            find_llm_preset,
            is_local_asr_engine,
        )
        from model_manager import ASR_DISPLAY_NAMES

        widget = QWidget()
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 0, 0, 0)

        from PyQt6.QtWidgets import QScrollArea

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)

        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setSpacing(14)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Hero: paper-collage banner with centered overlaid title ──
        # Matches the art-pack mockup: the illustration is the backdrop and the
        # title/subtitle sit centered on top of it (the cropped hero leaves the
        # middle light enough for warm near-black text to stay readable).
        hero = QFrame()
        hero.setObjectName("homeHero")
        hero_img_path = resource_path(
            "assets", "paper_collage", "illustrations", "cropped",
            "home_hero_target_from_ref.png",
        )
        if hero_img_path.exists():
            hero_url = hero_img_path.as_posix()
            hero.setStyleSheet(
                "QFrame#homeHero {"
                "  border-radius: 14px;"
                f"  border: 1px solid {Color.BORDER_SOFT};"
                f"  border-image: url({hero_url}) 0 0 0 0 stretch stretch;"
                "}"
            )
        else:
            hero.setStyleSheet(
                "QFrame#homeHero {"
                f"  border-radius: 14px; background: {Color.SURFACE_ALT};"
                "}"
            )
        hero.setFixedHeight(172)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 18, 24, 18)
        hero_layout.setSpacing(6)

        layout.addWidget(hero)

        # The three onboarding steps sit side-by-side as paper cards, each with
        # a circular number badge (olive / terracotta / olive).
        steps_row = QHBoxLayout()
        steps_row.setSpacing(14)
        layout.addLayout(steps_row, 1)

        # ── Step 1: ASR ────────────────────────────────────
        # The home card only carries presets for CLOUD engines. When the active
        # engine is a LOCAL on-device model (sensevoice/whisper/funasr/anime),
        # we prepend a read-only "local model" sentinel item (itemData="local")
        # and select it, so the card honestly reflects reality instead of
        # defaulting to "OpenAI Whisper". The sentinel also guards write-back:
        # editing/selecting it never overwrites asr_engine/asr_api. Local engine
        # switching stays in the Advanced (ASR & VAD) page.
        s = self._current_settings
        asr_api = s.get("asr_api", {})
        cur_asr_engine = s.get("asr_engine", "openai-audio")
        is_local = is_local_asr_engine(cur_asr_engine)
        self._home_asr_has_local = is_local
        cur_asr_idx = find_asr_preset(asr_api.get("api_base", ""), cur_asr_engine)
        if cur_asr_idx < 0:
            cur_asr_idx = 0
        # Index into ASR_PRESETS used to seed the cloud-oriented fields below.
        # For the local case the fields are disabled anyway, but we still seed
        # them from saved asr_api so nothing is lost if the user later switches.
        seed_idx = cur_asr_idx

        asr_card = self._make_step_card(
            "1", t("home_step_asr_title"), t("home_step_asr_sub"), Color.OLIVE
        )
        asr_layout = asr_card.layout()

        self._home_asr_preset = NoScrollComboBox()
        if is_local:
            local_name = ASR_DISPLAY_NAMES.get(cur_asr_engine, cur_asr_engine)
            self._home_asr_preset.addItem(t("home_asr_local_prefix") + local_name)
            self._home_asr_preset.setItemData(0, "local")
        for p in ASR_PRESETS:
            self._home_asr_preset.addItem(p["name"])
        # Select the sentinel (index 0) for local engines; otherwise the matched
        # cloud preset (shifted by 0 since no sentinel was inserted).
        self._home_asr_preset.setCurrentIndex(0 if is_local else cur_asr_idx)
        self._home_asr_preset.currentIndexChanged.connect(self._on_home_asr_preset)
        asr_layout.addRow("服务商", self._home_asr_preset)

        init_hint = (
            t("home_asr_local_hint")
            if is_local
            else ASR_PRESETS[seed_idx].get("hint", "")
        )
        self._home_asr_hint = QLabel(init_hint)
        self._home_asr_hint.setWordWrap(True)
        self._home_asr_hint.setStyleSheet(hint_text_stylesheet())

        self._home_asr_base = QLineEdit(
            asr_api.get("api_base", ASR_PRESETS[seed_idx]["api_base"])
        )
        self._home_asr_base.setPlaceholderText("例如 https://api.openai.com/v1 或其它听写服务地址")
        self._home_asr_base.textChanged.connect(self._on_home_asr_field_changed)
        asr_layout.addRow("Base URL", self._home_asr_base)

        self._home_asr_key = QLineEdit(asr_api.get("api_key", ""))
        self._home_asr_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._home_asr_key.setPlaceholderText("粘贴你的 API Key")
        self._home_asr_key.textChanged.connect(self._on_home_asr_field_changed)

        asr_key_row = QHBoxLayout()
        asr_key_row.addWidget(self._home_asr_key, 1)
        asr_show_key = QCheckBox("显示")
        asr_show_key.toggled.connect(
            lambda v: self._home_asr_key.setEchoMode(
                QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password
            )
        )
        asr_key_row.addWidget(asr_show_key)
        asr_key_widget = QWidget()
        asr_key_widget.setLayout(asr_key_row)
        asr_key_widget.setObjectName("transparentRow")
        asr_key_widget.setStyleSheet("QWidget#transparentRow { background: transparent; }")
        asr_layout.addRow("API Key", asr_key_widget)

        self._home_asr_model = NoScrollComboBox()
        self._home_asr_model.setEditable(True)
        self._home_asr_model.addItems(ASR_PRESETS[seed_idx]["models"])
        cur_model = asr_api.get(
            "model", ASR_PRESETS[seed_idx]["models"][0]
        )
        if cur_model not in ASR_PRESETS[seed_idx]["models"]:
            self._home_asr_model.insertItem(0, cur_model)
        self._home_asr_model.setCurrentText(cur_model)
        self._home_asr_model.editTextChanged.connect(
            self._on_home_asr_field_changed
        )
        asr_layout.addRow("模型", self._home_asr_model)

        asr_test_row = QHBoxLayout()
        self._home_asr_test_btn = QPushButton("测试模型")
        self._home_asr_test_btn.setMinimumHeight(34)
        self._home_asr_test_btn.setStyleSheet(secondary_button_stylesheet())
        self._home_asr_test_btn.clicked.connect(self._test_asr_connection)
        self._home_asr_models_btn = QPushButton("获取模型")
        self._home_asr_models_btn.setMinimumHeight(34)
        self._home_asr_models_btn.clicked.connect(self._fetch_asr_models)
        asr_test_row.addWidget(self._home_asr_models_btn)
        asr_test_row.addWidget(self._home_asr_test_btn)
        self._home_asr_test_status = QLabel("")
        self._home_asr_test_status.setWordWrap(True)
        self._home_asr_test_status.setStyleSheet("background: transparent;")
        asr_test_row.addWidget(self._home_asr_test_status, 1)
        asr_test_widget = QWidget()
        asr_test_widget.setLayout(asr_test_row)
        asr_test_widget.setObjectName("transparentRow")
        asr_test_widget.setStyleSheet("QWidget#transparentRow { background: transparent; }")
        asr_layout.addRow("", asr_test_widget)

        # Set initial enabled/disabled state of the cloud fields (disabled when
        # the local-model sentinel is the current selection).
        self._apply_home_asr_field_state()

        steps_row.addWidget(asr_card, 1)

        # ── Step 2: LLM ────────────────────────────────────
        models = s.get("models", [])
        active_idx = s.get("active_model", 0)
        if not (0 <= active_idx < len(models)):
            active_idx = 0
        cur_model_cfg = models[active_idx] if models else {
            "name": "",
            "provider": "openai-compatible",
            "api_base": "https://api.openai.com/v1",
            "api_key": "",
            "model": "",
        }
        cur_llm_idx = find_llm_preset(
            cur_model_cfg.get("api_base", ""),
            cur_model_cfg.get("provider", "openai-compatible"),
        )
        if cur_llm_idx < 0:
            cur_llm_idx = len(LLM_PRESETS) - 1  # custom

        llm_card = self._make_step_card(
            "2", t("home_step_llm_title"), t("home_step_llm_sub"), Color.PRIMARY
        )
        llm_layout = llm_card.layout()

        self._home_llm_preset = NoScrollComboBox()
        for p in LLM_PRESETS:
            self._home_llm_preset.addItem(p["name"])
        self._home_llm_preset.setCurrentIndex(cur_llm_idx)
        self._home_llm_saved_keys = {}
        for model_cfg in models:
            preset_idx = find_llm_preset(
                model_cfg.get("api_base", ""),
                model_cfg.get("provider", "openai-compatible"),
            )
            if preset_idx >= 0:
                self._home_llm_saved_keys[preset_idx] = model_cfg.get("api_key", "")
        self._home_llm_saved_keys[cur_llm_idx] = cur_model_cfg.get("api_key", "")
        self._home_llm_active_preset_idx = cur_llm_idx
        self._home_llm_preset.currentIndexChanged.connect(self._on_home_llm_preset)
        llm_layout.addRow("服务商", self._home_llm_preset)

        self._home_llm_hint = QLabel(LLM_PRESETS[cur_llm_idx].get("hint", ""))
        self._home_llm_hint.setWordWrap(True)
        self._home_llm_hint.setStyleSheet(hint_text_stylesheet())

        self._home_llm_base = QLineEdit(cur_model_cfg.get("api_base", ""))
        self._home_llm_base.setPlaceholderText("例如 https://api.openai.com/v1 或 http://127.0.0.1:1234/v1")
        self._home_llm_base.textChanged.connect(self._on_home_llm_field_changed)
        llm_layout.addRow("Base URL", self._home_llm_base)

        self._home_llm_key = QLineEdit(cur_model_cfg.get("api_key", ""))
        self._home_llm_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._home_llm_key.setPlaceholderText(
            "粘贴你的 API Key (本地 LM Studio / Ollama 可留空)"
        )
        self._home_llm_key.textChanged.connect(self._on_home_llm_field_changed)
        llm_key_row = QHBoxLayout()
        llm_key_row.addWidget(self._home_llm_key, 1)
        llm_show_key = QCheckBox("显示")
        llm_show_key.toggled.connect(
            lambda v: self._home_llm_key.setEchoMode(
                QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password
            )
        )
        llm_key_row.addWidget(llm_show_key)
        llm_key_widget = QWidget()
        llm_key_widget.setLayout(llm_key_row)
        llm_key_widget.setObjectName("transparentRow")
        llm_key_widget.setStyleSheet("QWidget#transparentRow { background: transparent; }")
        llm_layout.addRow("API Key", llm_key_widget)

        self._home_llm_model = NoScrollComboBox()
        self._home_llm_model.setEditable(True)
        self._home_llm_model.addItems(LLM_PRESETS[cur_llm_idx]["models"])
        cur_llm_model = cur_model_cfg.get(
            "model", LLM_PRESETS[cur_llm_idx]["models"][0]
        )
        if cur_llm_model not in LLM_PRESETS[cur_llm_idx]["models"]:
            self._home_llm_model.insertItem(0, cur_llm_model)
        self._home_llm_model.setCurrentText(cur_llm_model)
        self._home_llm_model.editTextChanged.connect(
            self._on_home_llm_field_changed
        )
        llm_layout.addRow("模型", self._home_llm_model)

        llm_test_row = QHBoxLayout()
        self._home_llm_test_btn = QPushButton("测试模型")
        self._home_llm_test_btn.setMinimumHeight(34)
        self._home_llm_test_btn.setStyleSheet(secondary_button_stylesheet())
        self._home_llm_test_btn.clicked.connect(self._test_llm_connection)
        self._home_llm_models_btn = QPushButton("获取模型")
        self._home_llm_models_btn.setMinimumHeight(34)
        self._home_llm_models_btn.clicked.connect(self._fetch_llm_models)
        llm_test_row.addWidget(self._home_llm_models_btn)
        llm_test_row.addWidget(self._home_llm_test_btn)
        self._home_llm_test_status = QLabel("")
        self._home_llm_test_status.setWordWrap(True)
        self._home_llm_test_status.setStyleSheet("background: transparent;")
        llm_test_row.addWidget(self._home_llm_test_status, 1)
        llm_test_widget = QWidget()
        llm_test_widget.setLayout(llm_test_row)
        llm_test_widget.setObjectName("transparentRow")
        llm_test_widget.setStyleSheet("QWidget#transparentRow { background: transparent; }")
        llm_layout.addRow("", llm_test_widget)

        steps_row.addWidget(llm_card, 1)

        # ── Step 3: Target language ────────────────────────
        lang_card = self._make_step_card(
            "3", t("home_step_lang_title"), t("home_step_lang_sub"), Color.LAVENDER
        )
        lang_layout = lang_card.layout()

        self._home_target_lang = NoScrollComboBox()
        for code, native in LANGUAGES:
            if code == "auto":
                continue
            self._home_target_lang.addItem(f"{native}  ({code})", code)
        cur_target = s.get(
            "target_language",
            self._config["translation"].get("target_language", "zh"),
        )
        idx = self._home_target_lang.findData(cur_target)
        if idx >= 0:
            self._home_target_lang.setCurrentIndex(idx)
        self._home_target_lang.currentIndexChanged.connect(
            self._on_home_target_lang_changed
        )
        lang_layout.addRow(t("home_target_lang_label"), self._home_target_lang)

        source_lang = NoScrollComboBox()
        source_lang.addItem("自动检测", "auto")
        for code, native in LANGUAGES:
            if code == "auto":
                continue
            source_lang.addItem(f"{native}  ({code})", code)
        asr_lang = s.get(
            "asr_language",
            self._config["translation"].get("source_language", "auto"),
        )
        src_idx = source_lang.findData(asr_lang)
        if src_idx >= 0:
            source_lang.setCurrentIndex(src_idx)
        source_lang.currentIndexChanged.connect(
            lambda _idx: self._current_settings.__setitem__(
                "asr_language", source_lang.currentData() or "auto"
            )
        )
        lang_layout.addRow("源语言（可选）", source_lang)

        output_box = QWidget()
        output_box.setObjectName("transparentRow")
        output_box.setStyleSheet("QWidget#transparentRow { background: transparent; }")
        output_layout = QVBoxLayout(output_box)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.setSpacing(6)
        show_original = QCheckBox("显示原文")
        show_original.setChecked(True)
        show_translation = QCheckBox("显示翻译")
        show_translation.setChecked(True)
        merge_display = QCheckBox("双语合并显示")
        for cb in (show_original, show_translation, merge_display):
            cb.setStyleSheet("background: transparent;")
            output_layout.addWidget(cb)
        lang_layout.addRow("输出选项", output_box)

        start_btn = QPushButton("保存并开始")
        start_btn.setMinimumHeight(34)
        start_btn.setStyleSheet(
            "QPushButton {"
            f" background: {Color.LAVENDER}; color: {Color.WHITE};"
            " border: 0; border-radius: 9px; font-weight: 700; padding: 8px 16px;"
            "}"
            "QPushButton:hover { background: #8d81c4; }"
            "QPushButton:pressed { background: #7c70b3; }"
        )
        start_btn.clicked.connect(self._save_home_settings)
        lang_layout.addRow("", start_btn)

        steps_row.addWidget(lang_card, 1)

        # ── Footer: decorated "tips" paper card ────────────
        # Matches the mockup: a paper card with a magnifier decoration, a short
        # tips list, and quick actions (open docs / open log folder).
        tips_card = QFrame()
        tips_card.setObjectName("paperCard")
        tips_card.setStyleSheet(paper_card_stylesheet())
        tips_row = QHBoxLayout(tips_card)
        tips_row.setContentsMargins(18, 14, 18, 14)
        tips_row.setSpacing(14)

        deco_path = resource_path(
            "assets", "paper_collage", "decorative", "cropped",
            "tips_magnifier_from_ref.png"
        )
        if deco_path.exists():
            from PyQt6.QtGui import QPixmap

            deco = QLabel()
            pix = QPixmap(str(deco_path))
            if not pix.isNull():
                deco.setPixmap(
                    pix.scaledToHeight(78, Qt.TransformationMode.SmoothTransformation)
                )
            deco.setStyleSheet("background: transparent;")
            tips_row.addWidget(deco, 0, Qt.AlignmentFlag.AlignVCenter)

        tips_text_col = QVBoxLayout()
        tips_text_col.setSpacing(2)
        tips_title = QLabel(t("home_tips_title"))
        tips_title.setStyleSheet(tips_card_title_stylesheet())
        tips_text_col.addWidget(tips_title)
        tips_body = QLabel(t("home_tips_body"))
        tips_body.setWordWrap(True)
        tips_body.setStyleSheet(hint_text_stylesheet())
        tips_text_col.addWidget(tips_body)
        self._home_save_status = QLabel("")
        self._home_save_status.setWordWrap(True)
        self._home_save_status.setStyleSheet(status_text_stylesheet("muted"))
        tips_text_col.addWidget(self._home_save_status)
        tips_row.addLayout(tips_text_col, 1)

        docs_btn = QPushButton(t("home_open_docs"))
        docs_btn.setStyleSheet(secondary_button_stylesheet())
        docs_btn.clicked.connect(self._open_docs_folder)
        tips_row.addWidget(docs_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        logs_btn = QPushButton(t("home_open_logs"))
        logs_btn.setStyleSheet(secondary_button_stylesheet())
        logs_btn.clicked.connect(self._open_logs_folder)
        tips_row.addWidget(logs_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(tips_card)

        layout.addStretch(1)
        return widget

    def _open_docs_folder(self):
        """Open the bundled docs folder (falls back to the app root)."""
        docs_dir = resource_path("docs")
        target = docs_dir if docs_dir.exists() else resource_path(".")
        try:
            os.startfile(str(target))
        except OSError:
            pass

    def _open_logs_folder(self):
        log_dir = data_path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(log_dir))

    def _make_step_card(self, number, title, subtitle=None, badge_color=None):
        """Create a paper step card: circular number badge + title/subtitle
        header, then a ``QFormLayout`` body for the fields.

        Returns a ``QFrame#paperCard`` whose ``layout()`` is the form, so the
        existing ``card.layout().addRow(...)`` callers keep working. The header
        is added as a full-width spanning row at the top.
        """
        from PyQt6.QtWidgets import QFormLayout

        badge_color = badge_color or Color.OLIVE

        card = QFrame()
        card.setObjectName("paperCard")
        card.setStyleSheet(paper_card_stylesheet())
        card.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        form = QFormLayout(card)
        form.setContentsMargins(16, 14, 16, 14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(7)

        # Header: circular badge + title (+ optional one-line subtitle).
        header = QWidget()
        header.setObjectName("transparentRow")
        header.setStyleSheet("QWidget#transparentRow { background: transparent; }")
        h = QHBoxLayout(header)
        h.setContentsMargins(0, 0, 0, 6)
        h.setSpacing(10)

        badge = QLabel(str(number))
        badge.setFixedSize(28, 28)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(step_badge_stylesheet(badge_color))
        h.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(step_card_title_stylesheet())
        text_col.addWidget(title_lbl)
        if subtitle:
            sub_lbl = QLabel(subtitle)
            sub_lbl.setWordWrap(True)
            sub_lbl.setStyleSheet(step_card_subtitle_stylesheet())
            text_col.addWidget(sub_lbl)
        h.addLayout(text_col, 1)

        form.addRow(header)
        return card

    def _make_empty_state(self, illustration: str, title_key: str, desc_key: str):
        """Build an illustrated empty-state widget (paper-collage).

        Returns a centered ``QWidget`` with the cropped illustration plus a
        title and description (both i18n keys). Used when a list/table has no
        rows yet, so pages feel warm instead of blank.
        """
        from PyQt6.QtGui import QPixmap

        holder = QWidget()
        v = QVBoxLayout(holder)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.setSpacing(10)

        art_path = resource_path(
            "assets", "paper_collage", "illustrations", "cropped", illustration
        )
        if art_path.exists():
            pix = QPixmap(str(art_path))
            if not pix.isNull():
                img = QLabel()
                img.setPixmap(
                    pix.scaledToWidth(280, Qt.TransformationMode.SmoothTransformation)
                )
                img.setAlignment(Qt.AlignmentFlag.AlignCenter)
                img.setStyleSheet("background: transparent;")
                v.addWidget(img, 0, Qt.AlignmentFlag.AlignCenter)

        title = QLabel(t(title_key))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(empty_state_title_stylesheet())
        v.addWidget(title)

        desc = QLabel(t(desc_key))
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setStyleSheet(empty_state_text_stylesheet())
        v.addWidget(desc)
        return holder

    def _create_manual_translate_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        header = QLabel("临时翻译一段文字")
        header.setStyleSheet(section_title_stylesheet())
        layout.addWidget(header)

        desc = QLabel("把文本粘贴进来，点翻译即可。这里使用快速开始里选好的翻译服务。")
        desc.setWordWrap(True)
        desc.setStyleSheet(hint_text_stylesheet())
        layout.addWidget(desc)

        manual_group = QGroupBox("文本翻译")
        manual_layout = QVBoxLayout(manual_group)
        manual_layout.addWidget(QLabel(t("manual_input_label")))
        self._manual_input = QTextEdit()
        self._manual_input.setPlaceholderText(t("manual_input_placeholder"))
        self._manual_input.setMinimumHeight(140)
        manual_layout.addWidget(self._manual_input)

        manual_ctrl = QHBoxLayout()
        manual_ctrl.addWidget(QLabel(t("manual_mode_label")))
        self._manual_mode = QComboBox()
        self._manual_mode.addItem(t("manual_mode_literal"), "literal")
        self._manual_mode.addItem(t("manual_mode_natural"), "natural")
        self._manual_mode.addItem(t("manual_mode_explain"), "explain")
        self._manual_mode.addItem(t("manual_mode_polish"), "polish")
        manual_ctrl.addWidget(self._manual_mode)
        self._manual_translate_btn = QPushButton(t("manual_translate"))
        self._manual_translate_btn.clicked.connect(self._start_manual_translate)
        manual_ctrl.addWidget(self._manual_translate_btn)
        copy_btn = QPushButton(t("manual_copy_translation"))
        copy_btn.clicked.connect(self._copy_manual_translation)
        manual_ctrl.addWidget(copy_btn)
        manual_ctrl.addStretch(1)
        manual_layout.addLayout(manual_ctrl)

        self._manual_output = QTextEdit()
        self._manual_output.setReadOnly(True)
        self._manual_output.setMinimumHeight(190)
        manual_layout.addWidget(self._manual_output)
        self._manual_last_result = {}
        layout.addWidget(manual_group, 1)
        return widget

    def _create_audio_translate_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        header = QLabel(t("audio_translate_title"))
        header.setStyleSheet(section_title_stylesheet())
        layout.addWidget(header)

        desc = QLabel(t("audio_translate_desc"))
        desc.setWordWrap(True)
        desc.setStyleSheet(hint_text_stylesheet())
        layout.addWidget(desc)

        group = QGroupBox(t("audio_translate_title"))
        group_layout = QVBoxLayout(group)

        # File picker row
        file_row = QHBoxLayout()
        file_row.addWidget(QLabel(t("audio_translate_file_label")))
        self._audio_file_label = QLabel(t("audio_translate_no_file"))
        self._audio_file_label.setStyleSheet(hint_text_stylesheet())
        file_row.addWidget(self._audio_file_label, 1)
        choose_btn = QPushButton(t("audio_translate_choose"))
        choose_btn.clicked.connect(self._choose_audio_file)
        file_row.addWidget(choose_btn)
        group_layout.addLayout(file_row)

        # Target language + actions row
        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel(t("audio_translate_target_label")))
        self._audio_target_lang = QComboBox()
        for code, native in LANGUAGES:
            if code == "auto":
                continue
            label = f"{native}  ({code})" if native else code
            self._audio_target_lang.addItem(label, code)
        cur_target = self._current_settings.get(
            "target_language",
            self._config["translation"].get("target_language", "zh"),
        )
        idx = self._audio_target_lang.findData(cur_target)
        if idx >= 0:
            self._audio_target_lang.setCurrentIndex(idx)
        ctrl_row.addWidget(self._audio_target_lang)

        self._audio_translate_btn = QPushButton(t("audio_translate_start"))
        self._audio_translate_btn.clicked.connect(self._start_audio_translate)
        ctrl_row.addWidget(self._audio_translate_btn)

        self._audio_cancel_btn = QPushButton(t("audio_translate_cancel"))
        self._audio_cancel_btn.clicked.connect(self._cancel_audio_translate)
        self._audio_cancel_btn.setEnabled(False)
        ctrl_row.addWidget(self._audio_cancel_btn)
        ctrl_row.addStretch(1)
        group_layout.addLayout(ctrl_row)

        # Progress
        self._audio_progress = QProgressBar()
        self._audio_progress.setRange(0, 100)
        self._audio_progress.setValue(0)
        self._audio_progress.setVisible(False)
        group_layout.addWidget(self._audio_progress)

        self._audio_status = QLabel("")
        self._audio_status.setStyleSheet(hint_text_stylesheet())
        group_layout.addWidget(self._audio_status)

        # Result table (time / original / translation)
        self._audio_result_table = QTableWidget(0, 3)
        self._audio_result_table.setHorizontalHeaderLabels(
            [
                t("audio_translate_col_time"),
                t("audio_translate_col_original"),
                t("audio_translate_col_translation"),
            ]
        )
        self._audio_result_table.horizontalHeader().setStretchLastSection(True)
        self._audio_result_table.setColumnWidth(0, 110)
        self._audio_result_table.setColumnWidth(1, 300)
        self._audio_result_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._audio_result_table.setWordWrap(True)
        self._audio_result_table.setMinimumHeight(220)
        group_layout.addWidget(self._audio_result_table, 1)

        # Footer actions
        footer = QHBoxLayout()
        footer.addStretch(1)
        copy_btn = QPushButton(t("audio_translate_copy"))
        copy_btn.clicked.connect(self._copy_audio_translation)
        footer.addWidget(copy_btn)
        self._audio_export_btn = QPushButton(t("audio_translate_export"))
        self._audio_export_btn.clicked.connect(self._export_audio_translation)
        self._audio_export_btn.setEnabled(False)
        footer.addWidget(self._audio_export_btn)
        group_layout.addLayout(footer)

        layout.addWidget(group, 1)

        self._audio_selected_file = None
        self._audio_last_result = None
        self._audio_cancel_flag = False
        self._audio_worker_running = False
        return widget

    # ── Audio file translation handlers ──

    def _choose_audio_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            t("audio_translate_choose"),
            "",
            t("audio_translate_file_filter"),
        )
        if not path:
            return
        self._audio_selected_file = path
        self._audio_file_label.setText(os.path.basename(path))
        self._audio_file_label.setToolTip(path)

    def _start_audio_translate(self):
        if self._audio_worker_running:
            return
        if not self._audio_selected_file:
            QMessageBox.information(
                self, "LiveTranslate", t("audio_translate_pick_first")
            )
            return

        model_config = self.get_active_model()
        if not model_config:
            QMessageBox.warning(
                self, "LiveTranslate", t("audio_translate_no_model")
            )
            return

        target_lang = self._audio_target_lang.currentData() or "zh"

        self._audio_cancel_flag = False
        self._audio_worker_running = True
        self._audio_translate_btn.setEnabled(False)
        self._audio_cancel_btn.setEnabled(True)
        self._audio_export_btn.setEnabled(False)
        self._audio_result_table.setRowCount(0)
        self._audio_last_result = None
        self._audio_progress.setVisible(True)
        self._audio_progress.setRange(0, 0)  # busy until first progress tick
        self._audio_status.setText(t("audio_translate_decoding"))

        threading.Thread(
            target=self._audio_translate_worker,
            args=(self._audio_selected_file, target_lang, model_config),
            daemon=True,
        ).start()

    def _cancel_audio_translate(self):
        self._audio_cancel_flag = True
        self._audio_cancel_btn.setEnabled(False)

    def _audio_translate_worker(self, path: str, target_lang: str, model_config: dict):
        try:
            from asr_providers import ASRProviderConfig, create_asr_provider
            from translator import PROMPT_PRESETS, Translator
            from audio_file_translate import transcribe_and_translate

            settings = self._current_settings
            engine_type = settings.get("asr_engine", "openai-audio")
            asr_api = settings.get("asr_api", {}) or {}
            asr_language = settings.get("asr_language", "auto")

            asr = create_asr_provider(
                ASRProviderConfig(
                    engine_type=engine_type,
                    device=settings.get("asr_device", "cpu"),
                    model_size=settings.get(
                        "whisper_model_size",
                        self._config["asr"].get("model_size", "medium"),
                    ),
                    compute_type=self._config["asr"].get("compute_type", "int8"),
                    language=asr_language,
                    hub=settings.get("hub", "ms"),
                    api_base=asr_api.get("api_base", "https://api.openai.com/v1"),
                    api_key=asr_api.get("api_key", ""),
                    api_model=asr_api.get("model", "whisper-1"),
                    timeout=asr_api.get("timeout", 60),
                )
            )

            mode = settings.get("translation_mode", "natural")
            mode = mode if mode in PROMPT_PRESETS else "natural"
            overrides = model_config.get("overrides", {}) or {}
            translator = Translator(
                api_base=model_config.get("api_base", ""),
                api_key=model_config.get("api_key", ""),
                model=model_config.get("model", ""),
                provider_type=model_config.get("provider", "openai-compatible"),
                target_language=target_lang,
                max_tokens=overrides.get("max_tokens", 512),
                temperature=overrides.get("temperature", 0.3),
                streaming=False,
                system_prompt=PROMPT_PRESETS[mode],
                proxy=model_config.get("proxy", "none"),
                no_system_role=model_config.get("no_system_role", False),
                no_think=model_config.get("no_think", True),
                json_response=False,
                timeout=settings.get("timeout", 30),
                overrides=overrides,
                extra_body=model_config.get("extra_body"),
                mode=mode,
            )

            def _progress(done, total, stage):
                self._audio_translate_progress.emit(int(done), int(total), stage)

            result = transcribe_and_translate(
                path,
                asr,
                translator,
                target_language=target_lang,
                source_language=asr_language,
                progress=_progress,
                should_cancel=lambda: self._audio_cancel_flag,
            )

            try:
                asr.unload()
            except Exception:
                pass
        except Exception as e:
            self._audio_translate_error.emit(str(e))
            return
        self._audio_translate_result.emit(result)

    def _on_audio_translate_progress(self, done: int, total: int, stage: str):
        if total > 0:
            self._audio_progress.setRange(0, total)
            self._audio_progress.setValue(min(done, total))
        if stage == "transcribe":
            self._audio_status.setText(
                t("audio_translate_transcribing").format(done=done + 1, total=total)
            )
        elif stage == "translate":
            self._audio_status.setText(
                t("audio_translate_translating").format(done=done + 1, total=total)
            )

    def _on_audio_translate_result(self, result):
        self._audio_worker_running = False
        self._audio_translate_btn.setEnabled(True)
        self._audio_cancel_btn.setEnabled(False)
        self._audio_progress.setVisible(False)
        self._audio_last_result = result

        segments = getattr(result, "segments", []) or []
        self._audio_result_table.setRowCount(len(segments))
        for row, seg in enumerate(segments):
            time_text = f"{self._format_seconds(seg.start_seconds)}"
            self._audio_result_table.setItem(row, 0, QTableWidgetItem(time_text))
            self._audio_result_table.setItem(row, 1, QTableWidgetItem(seg.original))
            self._audio_result_table.setItem(row, 2, QTableWidgetItem(seg.translation))
        self._audio_result_table.resizeRowsToContents()

        if self._audio_cancel_flag and not segments:
            self._audio_status.setText(t("audio_translate_cancelled"))
        elif not segments:
            self._audio_status.setText(t("audio_translate_empty_result"))
        else:
            self._audio_export_btn.setEnabled(True)
            msg = t("audio_translate_done").format(count=len(segments))
            if self._audio_cancel_flag:
                msg = f"{t('audio_translate_cancelled')} {msg}"
            self._audio_status.setText(msg)

    def _on_audio_translate_error(self, message: str):
        self._audio_worker_running = False
        self._audio_translate_btn.setEnabled(True)
        self._audio_cancel_btn.setEnabled(False)
        self._audio_progress.setVisible(False)
        self._audio_status.setText(t("audio_translate_failed").format(error=message))

    def _copy_audio_translation(self):
        if not self._audio_last_result:
            return
        text = self._audio_last_result.full_translation
        if text:
            QApplication.clipboard().setText(text)

    def _export_audio_translation(self):
        result = self._audio_last_result
        if not result or not result.segments:
            QMessageBox.information(self, "LiveTranslate", t("export_empty"))
            return
        base = "livetrans_audio"
        if self._audio_selected_file:
            base = os.path.splitext(os.path.basename(self._audio_selected_file))[0]
        default_name = f"{base}_translated.txt"
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            t("export_dialog_title"),
            default_name,
            t("export_filter"),
        )
        if not path:
            return
        fmt = self._detect_history_export_format(path, selected_filter)
        if "." not in os.path.basename(path):
            path = f"{path}.{fmt}"

        from exporters import SubtitleExportItem

        items = [
            SubtitleExportItem(
                index=seg.index,
                timestamp=self._format_seconds(seg.start_seconds),
                original=seg.original,
                translation=seg.translation,
                start_seconds=seg.start_seconds,
                end_seconds=seg.end_seconds,
            )
            for seg in result.segments
        ]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(export_subtitles(items, "both", fmt))
        except OSError as e:
            QMessageBox.critical(
                self,
                "LiveTranslate",
                t("export_failed").format(error=str(e)),
            )

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        seconds = max(0.0, float(seconds))
        total = int(seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    # ── Home tab handlers ──

    def _home_asr_preset_index(self) -> int:
        """Map the ASR 服务商 combo's current item to an ASR_PRESETS index.

        Returns -1 when the read-only local-model sentinel is selected (the
        sentinel only exists when the active engine is a local on-device
        model). All callers that index into ASR_PRESETS must go through this
        so the optional sentinel row at index 0 is accounted for.
        """
        combo = self._home_asr_preset
        if combo.itemData(combo.currentIndex()) == "local":
            return -1
        offset = 1 if getattr(self, "_home_asr_has_local", False) else 0
        return combo.currentIndex() - offset

    def _apply_home_asr_field_state(self):
        """Enable/disable the cloud-oriented ASR fields.

        When the local-model sentinel is selected, the Base URL / API Key /
        model / test / fetch-models widgets are meaningless, so we disable them
        (and clear any stale test status). Selecting a cloud preset re-enables.
        """
        is_sentinel = self._home_asr_preset_index() < 0
        enabled = not is_sentinel
        for w in (
            self._home_asr_base,
            self._home_asr_key,
            self._home_asr_model,
            self._home_asr_test_btn,
            self._home_asr_models_btn,
        ):
            w.setEnabled(enabled)
        if is_sentinel:
            self._home_asr_test_status.setText("")

    def _on_home_asr_preset(self, idx):
        from home_presets import ASR_PRESETS

        pidx = self._home_asr_preset_index()
        # Local-model sentinel selected: keep cloud fields disabled and never
        # overwrite the saved local asr_engine/asr_api.
        if pidx < 0:
            self._apply_home_asr_field_state()
            return
        if not (0 <= pidx < len(ASR_PRESETS)):
            return
        # A real cloud preset is now selected (re-enable the cloud fields).
        self._apply_home_asr_field_state()
        preset = ASR_PRESETS[pidx]
        # Don't blow away a key the user already typed for the same preset.
        self._home_asr_base.blockSignals(True)
        self._home_asr_base.setText(preset["api_base"])
        self._home_asr_base.blockSignals(False)
        self._home_asr_hint.setText(preset.get("hint", ""))
        self._home_asr_model.blockSignals(True)
        self._home_asr_model.clear()
        self._home_asr_model.addItems(preset["models"])
        self._home_asr_model.setCurrentText(preset["models"][0])
        self._home_asr_model.blockSignals(False)
        # Also flip the underlying engine selector in the advanced tab so
        # the runtime knows which provider to instantiate.
        engine_combo_idx = {
            "openai-audio": 5,
            "assemblyai": 6,
            "assemblyai-streaming": 7,
        }.get(preset["engine"], 5)
        if hasattr(self, "_asr_engine"):
            self._asr_engine.blockSignals(True)
            self._asr_engine.setCurrentIndex(engine_combo_idx)
            self._asr_engine.blockSignals(False)
        self._home_asr_test_status.setText("")
        self._on_home_asr_field_changed()

    def _on_home_asr_field_changed(self):
        from home_presets import ASR_PRESETS

        # Local-model sentinel selected: never overwrite asr_engine/asr_api.
        pidx = self._home_asr_preset_index()
        if pidx < 0:
            return
        offset = 1 if getattr(self, "_home_asr_has_local", False) else 0
        base = self._home_asr_base.text().strip()
        if 0 <= pidx < len(ASR_PRESETS):
            selected = ASR_PRESETS[pidx]
            selected_base = selected["api_base"].rstrip("/").lower()
            if base.lower().startswith("wss://streaming.assemblyai.com"):
                streaming_idx = next(
                    (
                        i
                        for i, p in enumerate(ASR_PRESETS)
                        if p["engine"] == "assemblyai-streaming"
                    ),
                    -1,
                )
                if streaming_idx >= 0 and pidx != streaming_idx:
                    self._home_asr_preset.blockSignals(True)
                    self._home_asr_preset.setCurrentIndex(streaming_idx + offset)
                    self._home_asr_preset.blockSignals(False)
                    self._home_asr_hint.setText(ASR_PRESETS[streaming_idx].get("hint", ""))
                    pidx = streaming_idx
                    if hasattr(self, "_asr_engine"):
                        self._asr_engine.blockSignals(True)
                        self._asr_engine.setCurrentIndex(7)
                        self._asr_engine.blockSignals(False)
            elif (
                base
                and base.rstrip("/").lower() != selected_base
                and selected["name"] != "自定义 (OpenAI 兼容)"
            ):
                custom_idx = len(ASR_PRESETS) - 1
                self._home_asr_preset.blockSignals(True)
                self._home_asr_preset.setCurrentIndex(custom_idx + offset)
                self._home_asr_preset.blockSignals(False)
                self._home_asr_hint.setText(ASR_PRESETS[custom_idx].get("hint", ""))
                pidx = custom_idx
                if hasattr(self, "_asr_engine"):
                    self._asr_engine.blockSignals(True)
                    self._asr_engine.setCurrentIndex(5)
                    self._asr_engine.blockSignals(False)
        # Sync to the advanced ASR tab widgets (single source of truth =
        # _current_settings, but we keep both UIs displaying the same value).
        if hasattr(self, "_asr_api_base"):
            self._asr_api_base.blockSignals(True)
            self._asr_api_key.blockSignals(True)
            self._asr_api_model.blockSignals(True)
            self._asr_api_base.setText(base)
            self._asr_api_key.setText(self._home_asr_key.text())
            self._asr_api_model.setText(self._home_asr_model.currentText().strip())
            self._asr_api_base.blockSignals(False)
            self._asr_api_key.blockSignals(False)
            self._asr_api_model.blockSignals(False)
        engine_type = (
            ASR_PRESETS[pidx]["engine"]
            if 0 <= pidx < len(ASR_PRESETS)
            else "openai-audio"
        )
        self._current_settings["asr_engine"] = engine_type
        self._current_settings["asr_api"] = {
            "api_base": base,
            "api_key": self._home_asr_key.text().strip(),
            "model": self._home_asr_model.currentText().strip(),
            "timeout": self._current_settings.get("asr_api", {}).get("timeout", 30),
        }

    def _on_home_llm_preset(self, idx):
        from home_presets import LLM_PRESETS

        if not (0 <= idx < len(LLM_PRESETS)):
            return
        previous_idx = getattr(self, "_home_llm_active_preset_idx", None)
        if previous_idx is not None and 0 <= previous_idx < len(LLM_PRESETS):
            self._home_llm_saved_keys[previous_idx] = self._home_llm_key.text().strip()
        self._home_llm_active_preset_idx = idx
        preset = LLM_PRESETS[idx]
        self._home_llm_base.blockSignals(True)
        self._home_llm_base.setText(preset["api_base"])
        self._home_llm_base.blockSignals(False)
        self._home_llm_key.blockSignals(True)
        self._home_llm_key.setText(self._home_llm_saved_keys.get(idx, ""))
        self._home_llm_key.blockSignals(False)
        self._home_llm_hint.setText(preset.get("hint", ""))
        self._home_llm_model.blockSignals(True)
        self._home_llm_model.clear()
        self._home_llm_model.addItems(preset["models"])
        self._home_llm_model.setCurrentText(preset["models"][0])
        self._home_llm_model.blockSignals(False)
        self._home_llm_test_status.setText("")
        self._on_home_llm_field_changed()

    def _on_home_llm_field_changed(self):
        from home_presets import LLM_PRESETS

        idx = self._home_llm_preset.currentIndex()
        base = self._home_llm_base.text().strip()
        if 0 <= idx < len(LLM_PRESETS):
            selected = LLM_PRESETS[idx]
            selected_base = selected["api_base"].rstrip("/").lower()
            if (
                base
                and base.rstrip("/").lower() != selected_base
                and selected["name"] != "自定义 (OpenAI 兼容)"
            ):
                custom_idx = len(LLM_PRESETS) - 1
                self._home_llm_preset.blockSignals(True)
                self._home_llm_preset.setCurrentIndex(custom_idx)
                self._home_llm_preset.blockSignals(False)
                self._home_llm_hint.setText(LLM_PRESETS[custom_idx].get("hint", ""))
                idx = custom_idx
                self._home_llm_active_preset_idx = custom_idx
        if 0 <= idx < len(LLM_PRESETS):
            self._home_llm_saved_keys[idx] = self._home_llm_key.text().strip()
        provider = (
            LLM_PRESETS[idx]["provider"]
            if 0 <= idx < len(LLM_PRESETS)
            else "openai-compatible"
        )
        models = self._current_settings.setdefault("models", [])
        active = self._current_settings.get("active_model", 0)
        if not (0 <= active < len(models)):
            models.append({})
            active = len(models) - 1
            self._current_settings["active_model"] = active
        cur = models[active]
        new_name = self._home_llm_model.currentText().strip() or cur.get("name") or "model"
        cur.update({
            "name": new_name,
            "provider": provider,
            "api_base": base,
            "api_key": self._home_llm_key.text().strip(),
            "model": self._home_llm_model.currentText().strip(),
        })
        cur.setdefault("streaming", True)
        cur.setdefault("no_think", True)
        # Refresh the advanced "Translation" tab list view if it exists.
        if hasattr(self, "_refresh_model_list"):
            try:
                self._refresh_model_list()
            except Exception:
                pass

    def _on_home_target_lang_changed(self, _idx=None):
        code = self._home_target_lang.currentData() or "zh"
        self._current_settings["target_language"] = code

    def _save_home_settings(self):
        self._on_home_asr_field_changed()
        self._on_home_llm_field_changed()
        self._on_home_target_lang_changed()
        if self._save_timer.isActive():
            self._save_timer.stop()
        self._apply_settings()
        _save_settings(self._current_settings)
        self._refresh_home_status()
        active_model = self.get_active_model()
        if active_model and hasattr(self, "model_changed"):
            self.model_changed.emit(dict(active_model))
        if hasattr(self, "_home_save_status"):
            self._home_save_status.setStyleSheet(status_text_stylesheet("success"))
            self._home_save_status.setText(t("home_save_success"))

    # ── Connection tests ──

    def _test_asr_connection(self):
        self._home_asr_test_btn.setEnabled(False)
        self._home_asr_test_status.setText("⏳ 测试中…")
        self._home_asr_test_status.setStyleSheet(status_text_stylesheet("muted"))
        threading.Thread(target=self._asr_test_worker, daemon=True).start()

    def _asr_test_worker(self):
        from home_presets import ASR_PRESETS

        try:
            import numpy as np
            from asr_providers import ASRProviderConfig, create_asr_provider

            idx = self._home_asr_preset_index()
            if idx < 0:
                # Local-model sentinel selected; this button is disabled in that
                # state, so this is just a defensive guard.
                self._home_asr_test_done.emit(False, "✗ 本地模型请在高级设置中测试")
                return
            engine_type = (
                ASR_PRESETS[idx]["engine"]
                if 0 <= idx < len(ASR_PRESETS)
                else "openai-audio"
            )
            cfg = ASRProviderConfig(
                engine_type=engine_type,
                device="cpu",
                language="auto",
                api_base=self._home_asr_base.text().strip(),
                api_key=self._home_asr_key.text().strip(),
                api_model=self._home_asr_model.currentText().strip(),
                timeout=20.0,
            )
            provider = create_asr_provider(cfg)
            silent = np.zeros(16000, dtype=np.float32)  # 1 s of silence
            try:
                provider.transcribe(silent)
            finally:
                try:
                    provider.unload()
                except Exception:
                    pass
            self._home_asr_test_done.emit(True, "✓ 连接成功,可以使用。")
        except Exception as e:
            self._home_asr_test_done.emit(False, f"✗ 失败:{e}")

    def _on_asr_test_done(self, ok: bool, msg: str):
        color = status_color(ok)
        self._home_asr_test_status.setStyleSheet(
            f"color: {color}; font-size: 12px;"
        )
        self._home_asr_test_status.setText(msg)
        self._home_asr_test_btn.setEnabled(True)

    def _fetch_asr_models(self):
        self._home_asr_models_btn.setEnabled(False)
        self._home_asr_test_status.setText("⏳ 正在获取模型列表…")
        self._home_asr_test_status.setStyleSheet(status_text_stylesheet("muted"))
        threading.Thread(target=self._asr_models_worker, daemon=True).start()

    def _asr_models_worker(self):
        try:
            from home_presets import ASR_PRESETS

            idx = self._home_asr_preset_index()
            if not (0 <= idx < len(ASR_PRESETS)):
                raise RuntimeError("未知听写服务")
            preset = ASR_PRESETS[idx]
            base = self._home_asr_base.text().strip().rstrip("/")
            key = self._home_asr_key.text().strip()
            if not base:
                raise ValueError("请先填写 Base URL")

            if preset["engine"] == "openai-audio":
                models = self._fetch_openai_compatible_models(base, key)
                if not models:
                    raise RuntimeError("接口可访问，但没有返回模型")
            else:
                models = list(preset.get("models", []))
                if not models:
                    raise RuntimeError("该服务没有可自动获取的模型列表")
            self._home_asr_models_done.emit(
                True,
                models,
                f"✓ 找到 {len(models)} 个模型，已填入下拉框。",
            )
        except Exception as e:
            self._home_asr_models_done.emit(False, [], f"✗ 获取失败:{e}")

    def _on_asr_models_done(self, ok: bool, models: object, msg: str):
        color = status_color(ok)
        self._home_asr_test_status.setStyleSheet(
            f"color: {color}; font-size: 12px;"
        )
        self._home_asr_test_status.setText(msg)
        if ok:
            current = self._home_asr_model.currentText().strip()
            model_names = [str(m) for m in models if str(m).strip()]
            self._home_asr_model.blockSignals(True)
            self._home_asr_model.clear()
            self._home_asr_model.addItems(model_names)
            if current and current in model_names:
                self._home_asr_model.setCurrentIndex(model_names.index(current))
            elif model_names:
                self._home_asr_model.setCurrentIndex(0)
            self._home_asr_model.blockSignals(False)
            self._on_home_asr_field_changed()
            self._home_asr_model.showPopup()
        self._home_asr_models_btn.setEnabled(True)

    def _test_llm_connection(self):
        self._home_llm_test_btn.setEnabled(False)
        self._home_llm_test_status.setText("⏳ 测试中…")
        self._home_llm_test_status.setStyleSheet(status_text_stylesheet("muted"))
        threading.Thread(target=self._llm_test_worker, daemon=True).start()

    def _llm_test_worker(self):
        try:
            from translator import Translator

            base = self._home_llm_base.text().strip()
            key = self._home_llm_key.text().strip() or "EMPTY"
            model = self._home_llm_model.currentText().strip()
            provider_type = "openai-compatible"
            from home_presets import LLM_PRESETS

            preset_idx = self._home_llm_preset.currentIndex()
            if 0 <= preset_idx < len(LLM_PRESETS):
                provider_type = LLM_PRESETS[preset_idx]["provider"]

            tr = Translator(
                api_base=base,
                api_key=key,
                model=model,
                target_language=self._home_target_lang.currentData() or "zh",
                max_tokens=64,
                temperature=0.3,
                streaming=False,
                timeout=15,
                provider_type=provider_type,
            )
            out = tr.translate("hello", source_language="en")
            sample = (out or "").strip()
            sample = (sample[:40] + "…") if len(sample) > 40 else sample
            self._home_llm_test_done.emit(
                True, f"✓ 连接成功。 示例输出:{sample or '(空)'}"
            )
        except Exception as e:
            self._home_llm_test_done.emit(False, f"✗ 失败:{e}")

    def _on_llm_test_done(self, ok: bool, msg: str):
        color = status_color(ok)
        self._home_llm_test_status.setStyleSheet(
            f"color: {color}; font-size: 12px;"
        )
        self._home_llm_test_status.setText(msg)
        self._home_llm_test_btn.setEnabled(True)

    def _fetch_llm_models(self):
        self._home_llm_models_btn.setEnabled(False)
        self._home_llm_test_status.setText("⏳ 正在获取模型列表…")
        self._home_llm_test_status.setStyleSheet(status_text_stylesheet("muted"))
        threading.Thread(target=self._llm_models_worker, daemon=True).start()

    def _llm_models_worker(self):
        try:
            from home_presets import LLM_PRESETS

            idx = self._home_llm_preset.currentIndex()
            provider_type = (
                LLM_PRESETS[idx]["provider"]
                if 0 <= idx < len(LLM_PRESETS)
                else "openai-compatible"
            )
            base = self._home_llm_base.text().strip().rstrip("/")
            key = self._home_llm_key.text().strip()
            if not base:
                raise ValueError("请先填写 Base URL")

            if provider_type == "ollama":
                models = self._fetch_ollama_models(base)
            else:
                models = self._fetch_openai_compatible_models(base, key)

            if not models:
                raise RuntimeError("接口可访问，但没有返回模型")
            self._home_llm_models_done.emit(
                True,
                models,
                f"✓ 找到 {len(models)} 个模型，已填入下拉框。",
            )
        except Exception as e:
            self._home_llm_models_done.emit(False, [], f"✗ 获取失败:{e}")

    @staticmethod
    def _fetch_openai_compatible_models(base: str, api_key: str) -> list[str]:
        import httpx

        url = f"{base}/models"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), trust_env=False) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        rows = data.get("data", []) if isinstance(data, dict) else []
        models = []
        for row in rows:
            if isinstance(row, dict):
                model_id = row.get("id") or row.get("name")
            else:
                model_id = str(row)
            if model_id:
                models.append(str(model_id))
        return sorted(dict.fromkeys(models), key=str.lower)

    @staticmethod
    def _fetch_ollama_models(base: str) -> list[str]:
        import httpx

        root = base
        if root.endswith("/api/chat"):
            root = root[: -len("/api/chat")]
        elif root.endswith("/api"):
            root = root[: -len("/api")]
        url = f"{root.rstrip('/')}/api/tags"
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), trust_env=False) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        rows = data.get("models", []) if isinstance(data, dict) else []
        models = []
        for row in rows:
            if isinstance(row, dict):
                name = row.get("name") or row.get("model")
            else:
                name = str(row)
            if name:
                models.append(str(name))
        return sorted(dict.fromkeys(models), key=str.lower)

    def _on_llm_models_done(self, ok: bool, models: object, msg: str):
        color = status_color(ok)
        self._home_llm_test_status.setStyleSheet(
            f"color: {color}; font-size: 12px;"
        )
        self._home_llm_test_status.setText(msg)
        if ok:
            current = self._home_llm_model.currentText().strip()
            model_names = [str(m) for m in models if str(m).strip()]
            self._home_llm_model.blockSignals(True)
            self._home_llm_model.clear()
            self._home_llm_model.addItems(model_names)
            if current and current in model_names:
                self._home_llm_model.setCurrentIndex(model_names.index(current))
            elif model_names:
                self._home_llm_model.setCurrentIndex(0)
            self._home_llm_model.blockSignals(False)
            self._on_home_llm_field_changed()
            self._home_llm_model.showPopup()
        self._home_llm_models_btn.setEnabled(True)

    def _refresh_home_status(self):
        # Older home tab used inline status labels; the rewritten home tab
        # encodes status implicitly via field values, so this becomes a no-op
        # but is kept because the tab-changed handler still calls it.
        return

    # ── VAD / ASR Tab ──

    def _create_vad_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        s = self._current_settings

        asr_group = QGroupBox(t("group_asr_engine"))
        asr_layout = QGridLayout(asr_group)
        asr_layout.setColumnStretch(0, 1)
        asr_layout.setColumnMinimumWidth(1, 180)

        self._asr_engine = QComboBox()
        self._asr_engine.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._asr_engine.addItems(
            [
                f"[{t('asr_accurate')}] Whisper (faster-whisper)",
                f"[{t('asr_fast')}] SenseVoice (FunASR)",
                "Fun-ASR-Nano (FunASR)",
                "Fun-ASR-MLT-Nano (FunASR, 31 langs)",
                "Anime-Whisper (ja, anime/galgame)",
                "OpenAI Audio API",
                "AssemblyAI",
                "AssemblyAI Streaming",
            ]
        )
        engine_map_idx = {
            "whisper": 0,
            "sensevoice": 1,
            "funasr-nano": 2,
            "funasr-mlt-nano": 3,
            "anime-whisper": 4,
            "openai-audio": 5,
            "assemblyai": 6,
            "assemblyai-streaming": 7,
        }
        engine_idx = engine_map_idx.get(s.get("asr_engine", "openai-audio"), 5)
        self._asr_engine.setCurrentIndex(engine_idx)
        asr_layout.addWidget(QLabel(t("label_engine")), 0, 0)
        asr_layout.addWidget(self._asr_engine, 0, 1)
        self._asr_engine.currentIndexChanged.connect(self._auto_save)

        self._asr_lang = QComboBox()
        for code, native in LANGUAGES:
            label = t("asr_lang_auto") if code == "auto" else native
            self._asr_lang.addItem(f"{code} - {label}", code)
        lang = s.get("asr_language", self._config["asr"].get("language", "auto"))
        idx = self._asr_lang.findData(lang)
        if idx >= 0:
            self._asr_lang.setCurrentIndex(idx)
        asr_layout.addWidget(QLabel(t("label_language_hint")), 1, 0)
        asr_layout.addWidget(self._asr_lang, 1, 1)
        self._asr_lang.currentIndexChanged.connect(self._auto_save)

        self._asr_device = QComboBox()
        devices = ["cuda", "cpu"]
        try:
            import torch

            for i in range(torch.cuda.device_count()):
                name = torch.cuda.get_device_name(i)
                devices.insert(i, f"cuda:{i} ({name})")
            if torch.cuda.device_count() > 0:
                devices = [d for d in devices if d != "cuda"]
        except Exception:
            pass
        self._asr_device.addItems(devices)
        saved_dev = s.get("asr_device", self._config["asr"].get("device", "cuda"))
        for i in range(self._asr_device.count()):
            if self._asr_device.itemText(i).startswith(saved_dev):
                self._asr_device.setCurrentIndex(i)
                break
        asr_layout.addWidget(QLabel(t("label_device")), 2, 0)
        asr_layout.addWidget(self._asr_device, 2, 1)
        self._asr_device.currentIndexChanged.connect(self._auto_save)
        self._asr_device.currentIndexChanged.connect(
            lambda _i: self._update_whisper_vram_warning(
                self._whisper_size_combo.currentText()
            )
            if hasattr(self, "_whisper_size_combo")
            else None
        )

        self._audio_device = QComboBox()
        self._audio_device.addItem(t("audio_disabled"))
        self._audio_device.addItem(t("system_default"))
        try:
            from audio_providers import list_output_devices

            for name in list_output_devices():
                self._audio_device.addItem(name)
        except Exception:
            pass
        saved_audio = s.get("audio_device")
        if saved_audio == "__disabled__":
            self._audio_device.setCurrentIndex(0)
        elif saved_audio:
            idx = self._audio_device.findText(saved_audio)
            if idx >= 0:
                self._audio_device.setCurrentIndex(idx)
        else:
            self._audio_device.setCurrentIndex(1)  # system default
        asr_layout.addWidget(QLabel(t("label_audio")), 3, 0)
        asr_layout.addWidget(self._audio_device, 3, 1)
        self._audio_device.currentIndexChanged.connect(self._auto_save)

        self._mic_device = QComboBox()
        self._mic_device.addItem(t("mic_disabled"))
        self._mic_device.addItem(t("system_default"))
        try:
            from audio_providers import list_input_devices

            for name in list_input_devices():
                self._mic_device.addItem(name)
        except Exception:
            pass
        saved_mic = s.get("mic_device")
        if saved_mic:
            if saved_mic in ("__default__", "default"):
                self._mic_device.setCurrentIndex(1)
            else:
                idx = self._mic_device.findText(saved_mic)
                if idx >= 0:
                    self._mic_device.setCurrentIndex(idx)
        asr_layout.addWidget(QLabel(t("label_mic")), 4, 0)
        asr_layout.addWidget(self._mic_device, 4, 1)
        self._mic_device.currentIndexChanged.connect(self._auto_save)

        self._hub_combo = QComboBox()
        self._hub_combo.addItems([t("hub_modelscope"), t("hub_huggingface")])
        saved_hub = s.get("hub", "ms")
        self._hub_combo.setCurrentIndex(0 if saved_hub == "ms" else 1)
        asr_layout.addWidget(QLabel(t("label_hub")), 5, 0)
        asr_layout.addWidget(self._hub_combo, 5, 1)
        self._hub_combo.currentIndexChanged.connect(self._auto_save)

        self._ui_lang_combo = QComboBox()
        self._ui_lang_combo.addItems(["English", "中文"])
        from i18n import get_lang

        saved_lang = s.get("ui_lang", get_lang())
        self._ui_lang_combo.setCurrentIndex(0 if saved_lang == "en" else 1)
        asr_layout.addWidget(QLabel(t("label_ui_lang")), 6, 0)
        asr_layout.addWidget(self._ui_lang_combo, 6, 1)
        self._ui_lang_combo.currentIndexChanged.connect(self._on_ui_lang_changed)

        # Hardware-aware recommendation: detect GPU/VRAM/RAM and offer a
        # one-click "apply recommended" that sets engine/device/whisper-size.
        from model_manager import detect_hardware, recommend_asr

        self._hw = detect_hardware()
        self._reco = recommend_asr(self._hw)
        reco_row = QHBoxLayout()
        self._reco_label = QLabel("💡 " + t(self._reco["reason_key"]))
        self._reco_label.setWordWrap(True)
        self._reco_label.setStyleSheet("color: #888; font-size: 11px;")
        reco_row.addWidget(self._reco_label, 1)
        self._reco_btn = QPushButton(t("hw_recommend_btn"))
        self._reco_btn.clicked.connect(self._apply_asr_recommendation)
        reco_row.addWidget(self._reco_btn)
        asr_layout.addLayout(reco_row, 7, 0, 1, 2)

        layout.addWidget(asr_group)

        self._asr_api_group = QGroupBox(t("group_asr_api"))
        asr_api_layout = QGridLayout(self._asr_api_group)
        asr_api = s.get("asr_api", {})
        self._asr_api_base = QLineEdit(
            asr_api.get("api_base", "https://api.openai.com/v1")
        )
        self._asr_api_base.setPlaceholderText("https://api.openai.com/v1")
        self._asr_api_base.textChanged.connect(self._auto_save)
        asr_api_layout.addWidget(QLabel(t("label_asr_api_base")), 0, 0)
        asr_api_layout.addWidget(self._asr_api_base, 0, 1)

        self._asr_api_key = QLineEdit(asr_api.get("api_key", ""))
        self._asr_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._asr_api_key.textChanged.connect(self._auto_save)
        asr_api_layout.addWidget(QLabel(t("label_asr_api_key")), 1, 0)
        asr_api_layout.addWidget(self._asr_api_key, 1, 1)

        self._asr_api_model = QLineEdit(asr_api.get("model", "whisper-1"))
        self._asr_api_model.setPlaceholderText("whisper-1")
        self._asr_api_model.textChanged.connect(self._auto_save)
        asr_api_layout.addWidget(QLabel(t("label_asr_api_model")), 2, 0)
        asr_api_layout.addWidget(self._asr_api_model, 2, 1)

        self._asr_api_timeout = QSpinBox()
        self._asr_api_timeout.setRange(5, 300)
        self._asr_api_timeout.setValue(int(asr_api.get("timeout", 30)))
        self._asr_api_timeout.valueChanged.connect(self._auto_save)
        asr_api_layout.addWidget(QLabel(t("label_asr_api_timeout")), 3, 0)
        asr_api_layout.addWidget(self._asr_api_timeout, 3, 1)
        layout.addWidget(self._asr_api_group)
        self._asr_api_group.setVisible(engine_idx in (5, 6, 7))
        self._asr_engine.currentIndexChanged.connect(
            self._on_engine_changed_asr_api_vis
        )
        self._update_asr_api_placeholders(engine_idx)

        # Whisper model download — only visible when engine is Whisper
        self._whisper_group = QGroupBox(t("group_download_whisper"))
        whisper_layout = QHBoxLayout(self._whisper_group)
        self._whisper_size_combo = QComboBox()
        self._whisper_size_combo.addItems(
            ["tiny", "base", "small", "medium", "large-v3"]
        )
        saved_size = s.get(
            "whisper_model_size", self._config["asr"].get("model_size", "medium")
        )
        size_idx = self._whisper_size_combo.findText(saved_size)
        if size_idx >= 0:
            self._whisper_size_combo.setCurrentIndex(size_idx)
        self._whisper_size_combo.currentIndexChanged.connect(
            self._on_whisper_size_changed
        )
        whisper_layout.addWidget(self._whisper_size_combo)
        self._whisper_status = QLabel("")
        self._whisper_status.setStyleSheet("color: #888; font-size: 11px;")
        whisper_layout.addWidget(self._whisper_status, 1)
        self._whisper_dl_btn = QPushButton(t("btn_download_whisper"))
        self._whisper_dl_btn.clicked.connect(self._download_whisper)
        whisper_layout.addWidget(self._whisper_dl_btn)
        layout.addWidget(self._whisper_group)
        self._whisper_group.setVisible(engine_idx == 0)

        # VRAM-fit warning for the selected Whisper size (shown under the row).
        self._whisper_vram_warn = QLabel("")
        self._whisper_vram_warn.setWordWrap(True)
        self._whisper_vram_warn.setStyleSheet("color: #c96442; font-size: 11px;")
        self._whisper_vram_warn.setVisible(False)
        layout.addWidget(self._whisper_vram_warn)

        self._asr_engine.currentIndexChanged.connect(
            self._on_engine_changed_whisper_vis
        )
        self._update_whisper_size_label()

        mode_group = QGroupBox(t("group_vad_mode"))
        mode_layout = QVBoxLayout(mode_group)
        self._vad_mode = QComboBox()
        self._vad_mode.addItems([t("vad_silero"), t("vad_energy"), t("vad_disabled")])
        mode_map = {"silero": 0, "energy": 1, "disabled": 2}
        self._vad_mode.setCurrentIndex(mode_map.get(s.get("vad_mode", "energy"), 1))
        self._vad_mode.currentIndexChanged.connect(self._on_vad_mode_changed)
        self._vad_mode.currentIndexChanged.connect(self._auto_save)
        mode_layout.addWidget(self._vad_mode)
        layout.addWidget(mode_group)

        silero_group = QGroupBox(t("group_silero_threshold"))
        silero_layout = QGridLayout(silero_group)
        self._vad_threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self._vad_threshold_slider.setRange(0, 100)
        vad_pct = int(s.get("vad_threshold", 0.5) * 100)
        self._vad_threshold_slider.setValue(vad_pct)
        self._vad_threshold_slider.valueChanged.connect(self._on_threshold_changed)
        self._vad_threshold_slider.sliderReleased.connect(self._auto_save)
        self._vad_threshold_label = QLabel(f"{vad_pct}%")
        self._vad_threshold_label.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        silero_layout.addWidget(QLabel(t("label_threshold")), 0, 0)
        silero_layout.addWidget(self._vad_threshold_slider, 0, 1)
        silero_layout.addWidget(self._vad_threshold_label, 0, 2)
        layout.addWidget(silero_group)

        energy_group = QGroupBox(t("group_energy_threshold"))
        energy_layout = QGridLayout(energy_group)
        self._energy_slider = QSlider(Qt.Orientation.Horizontal)
        self._energy_slider.setRange(1, 100)
        energy_pm = int(s.get("energy_threshold", 0.03) * 1000)
        self._energy_slider.setValue(energy_pm)
        self._energy_slider.valueChanged.connect(self._on_energy_changed)
        self._energy_slider.sliderReleased.connect(self._auto_save)
        self._energy_label = QLabel(f"{energy_pm}\u2030")
        self._energy_label.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        energy_layout.addWidget(QLabel(t("label_threshold")), 0, 0)
        energy_layout.addWidget(self._energy_slider, 0, 1)
        energy_layout.addWidget(self._energy_label, 0, 2)
        layout.addWidget(energy_group)

        timing_group = QGroupBox(t("group_timing"))
        timing_layout = QGridLayout(timing_group)
        timing_layout.setColumnStretch(0, 1)
        timing_layout.setColumnMinimumWidth(1, 180)
        self._min_speech = QDoubleSpinBox()
        self._min_speech.setRange(0.1, 5.0)
        self._min_speech.setSingleStep(0.1)
        self._min_speech.setValue(s.get("min_speech_duration", 2.0))
        self._min_speech.setSuffix(" s")
        self._min_speech.valueChanged.connect(self._on_timing_changed)
        self._min_speech.valueChanged.connect(self._auto_save)
        self._max_speech = QDoubleSpinBox()
        self._max_speech.setRange(2.0, 30.0)
        self._max_speech.setSingleStep(1.0)
        self._max_speech.setValue(s.get("max_speech_duration", 6.0))
        self._max_speech.setSuffix(" s")
        self._max_speech.valueChanged.connect(self._on_timing_changed)
        self._max_speech.valueChanged.connect(self._auto_save)
        self._silence_mode = QComboBox()
        self._silence_mode.addItems([t("silence_auto"), t("silence_fixed")])
        saved_smode = s.get("silence_mode", "auto")
        self._silence_mode.setCurrentIndex(0 if saved_smode == "auto" else 1)
        self._silence_mode.currentIndexChanged.connect(self._on_silence_mode_changed)
        self._silence_mode.currentIndexChanged.connect(self._on_timing_changed)
        self._silence_mode.currentIndexChanged.connect(self._auto_save)

        self._silence_duration = QDoubleSpinBox()
        self._silence_duration.setRange(0.1, 3.0)
        self._silence_duration.setSingleStep(0.1)
        self._silence_duration.setValue(s.get("silence_duration", 0.8))
        self._silence_duration.setSuffix(" s")
        self._silence_duration.setEnabled(saved_smode != "auto")
        self._silence_duration.valueChanged.connect(self._on_timing_changed)
        self._silence_duration.valueChanged.connect(self._auto_save)

        timing_layout.addWidget(QLabel(t("label_min_speech")), 0, 0)
        timing_layout.addWidget(self._min_speech, 0, 1)
        timing_layout.addWidget(QLabel(t("label_max_speech")), 1, 0)
        timing_layout.addWidget(self._max_speech, 1, 1)
        timing_layout.addWidget(QLabel(t("label_silence")), 2, 0)
        timing_layout.addWidget(self._silence_mode, 2, 1)
        timing_layout.addWidget(QLabel(t("label_silence_dur")), 3, 0)
        timing_layout.addWidget(self._silence_duration, 3, 1)

        from PyQt6.QtWidgets import QCheckBox

        self._incremental_asr_cb = QCheckBox(t("label_incremental_asr"))
        self._incremental_asr_cb.setToolTip(t("incremental_asr_tooltip"))
        self._incremental_asr_cb.setChecked(s.get("incremental_asr", True))
        self._incremental_asr_cb.toggled.connect(self._on_timing_changed)
        self._incremental_asr_cb.toggled.connect(self._auto_save)
        timing_layout.addWidget(self._incremental_asr_cb, 4, 0)

        self._interim_interval_spin = QDoubleSpinBox()
        self._interim_interval_spin.setRange(1.0, 10.0)
        self._interim_interval_spin.setSingleStep(0.5)
        self._interim_interval_spin.setValue(s.get("interim_interval", 2.0))
        self._interim_interval_spin.setSuffix(" s")
        self._interim_interval_spin.setEnabled(s.get("incremental_asr", True))
        self._interim_interval_spin.valueChanged.connect(self._on_timing_changed)
        self._interim_interval_spin.valueChanged.connect(self._auto_save)
        self._incremental_asr_cb.toggled.connect(self._interim_interval_spin.setEnabled)
        timing_layout.addWidget(QLabel(t("label_interim_interval")), 5, 0)
        timing_layout.addWidget(self._interim_interval_spin, 5, 1)

        layout.addWidget(timing_group)

        layout.addStretch()
        return widget

    # ── Translation Tab ──

    def _create_translation_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        s = self._current_settings

        models_group = QGroupBox(t("group_model_configs"))
        models_layout = QVBoxLayout(models_group)

        self._model_list = QListWidget()
        self._model_list.setFont(QFont("Consolas", 9))
        self._model_list.itemDoubleClicked.connect(self._on_model_double_clicked)
        self._refresh_model_list()
        models_layout.addWidget(self._model_list)

        btn_row = QHBoxLayout()
        add_btn = QPushButton(t("btn_add"))
        add_btn.clicked.connect(self._add_model)
        btn_row.addWidget(add_btn)
        edit_btn = QPushButton(t("btn_edit"))
        edit_btn.clicked.connect(self._edit_model)
        btn_row.addWidget(edit_btn)
        dup_btn = QPushButton(t("btn_duplicate"))
        dup_btn.clicked.connect(self._dup_model)
        btn_row.addWidget(dup_btn)
        del_btn = QPushButton(t("btn_remove"))
        del_btn.clicked.connect(self._remove_model)
        btn_row.addWidget(del_btn)
        models_layout.addLayout(btn_row)

        test_row = QHBoxLayout()
        self._test_conn_btn = QPushButton(t("btn_test_connection"))
        self._test_conn_btn.clicked.connect(self._test_model_connection)
        test_row.addWidget(self._test_conn_btn)
        self._test_conn_label = QLabel("")
        self._test_conn_label.setWordWrap(True)
        test_row.addWidget(self._test_conn_label, 1)
        models_layout.addLayout(test_row)
        layout.addWidget(models_group)

        prompt_group = QGroupBox(t("group_system_prompt"))
        prompt_layout = QVBoxLayout(prompt_group)

        from translator import DEFAULT_PROMPT, PROMPT_PRESETS

        # Preset selector
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel(t("label_prompt_preset")))
        self._prompt_preset = QComboBox()
        self._prompt_preset.addItem("直译模式", "literal")
        self._prompt_preset.addItem("自然表达", "natural")
        self._prompt_preset.addItem("学习解释", "explain")
        self._prompt_preset.addItem("专业润色", "polish")
        self._prompt_preset.addItem(t("prompt_daily"), "daily")
        self._prompt_preset.addItem(t("prompt_esports"), "esports")
        self._prompt_preset.addItem(t("prompt_anime"), "anime")
        self._prompt_preset.addItem(t("prompt_custom"), "custom")

        current_prompt = s.get("system_prompt", DEFAULT_PROMPT)
        preset_keys = [
            "literal",
            "natural",
            "explain",
            "polish",
            "daily",
            "esports",
            "anime",
        ]
        preset_idx = len(preset_keys)  # default to custom
        for i, key in enumerate(preset_keys):
            if current_prompt.strip() == PROMPT_PRESETS[key].strip():
                preset_idx = i
                break
        self._prompt_preset.setCurrentIndex(preset_idx)
        self._prompt_preset.currentIndexChanged.connect(self._on_prompt_preset_changed)
        preset_row.addWidget(self._prompt_preset, 1)
        prompt_layout.addLayout(preset_row)

        # Prompt text editor
        self._prompt_edit = QTextEdit()
        self._prompt_edit.setFont(QFont("Consolas", 9))
        self._prompt_edit.setMaximumHeight(100)
        self._prompt_edit.setPlainText(current_prompt)
        self._prompt_debounce = QTimer()
        self._prompt_debounce.setSingleShot(True)
        self._prompt_debounce.setInterval(600)
        self._prompt_debounce.timeout.connect(self._apply_prompt)
        self._prompt_edit.textChanged.connect(self._prompt_debounce.start)
        prompt_layout.addWidget(self._prompt_edit)
        layout.addWidget(prompt_group)

        glossary_group = QGroupBox(t("group_glossary"))
        glossary_layout = QVBoxLayout(glossary_group)
        glossary_hint = QLabel(t("glossary_hint"))
        glossary_hint.setWordWrap(True)
        glossary_layout.addWidget(glossary_hint)
        self._glossary_edit = QTextEdit()
        self._glossary_edit.setFont(QFont("Consolas", 9))
        self._glossary_edit.setMaximumHeight(110)
        self._glossary_edit.setPlaceholderText("Mikasa => 三笠\nTitan => 巨人")
        self._glossary_edit.setPlainText(
            _glossary_to_text(s.get("glossary"))
        )
        self._glossary_debounce = QTimer()
        self._glossary_debounce.setSingleShot(True)
        self._glossary_debounce.setInterval(600)
        self._glossary_debounce.timeout.connect(self._apply_glossary)
        self._glossary_edit.textChanged.connect(self._glossary_debounce.start)
        glossary_layout.addWidget(self._glossary_edit)
        layout.addWidget(glossary_group)

        net_group = QGroupBox(t("group_network"))
        net_layout = QGridLayout(net_group)
        net_layout.setColumnStretch(0, 1)
        net_layout.setColumnMinimumWidth(1, 180)
        net_layout.addWidget(QLabel(t("label_timeout")), 0, 0)
        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(1, 60)
        self._timeout_spin.setValue(s.get("timeout", 5))
        self._timeout_spin.setSuffix(" s")
        self._timeout_spin.valueChanged.connect(
            lambda v: self._current_settings.update({"timeout": v})
        )
        self._timeout_spin.valueChanged.connect(self._auto_save)
        net_layout.addWidget(self._timeout_spin, 0, 1)
        net_layout.addWidget(QLabel(t("label_local_api_token")), 1, 0)
        local_api_settings = s.get("local_api", {})
        self._local_api_token = QLineEdit(local_api_settings.get("token", ""))
        self._local_api_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._local_api_token.setPlaceholderText(t("local_api_token_placeholder"))
        self._local_api_token.textChanged.connect(self._on_local_api_changed)
        self._local_api_token.textChanged.connect(self._auto_save)
        net_layout.addWidget(self._local_api_token, 1, 1)
        layout.addWidget(net_group)

        hotkey_group = QGroupBox(t("group_hotkey"))
        hotkey_layout = QGridLayout(hotkey_group)
        hotkey_settings = s.get("hotkey", {})
        self._hotkey_enabled = QCheckBox(t("label_hotkey_enabled"))
        self._hotkey_enabled.setChecked(hotkey_settings.get("enabled", True))
        self._hotkey_enabled.toggled.connect(self._on_hotkey_changed)
        self._hotkey_enabled.toggled.connect(self._auto_save)
        hotkey_layout.addWidget(self._hotkey_enabled, 0, 0, 1, 2)
        hotkey_layout.addWidget(QLabel(t("label_hotkey_shortcut")), 1, 0)
        self._hotkey_shortcut = QLineEdit(hotkey_settings.get("shortcut", "Ctrl+Shift+T"))
        self._hotkey_shortcut.setPlaceholderText("Ctrl+Shift+T")
        self._hotkey_shortcut.textChanged.connect(self._on_hotkey_changed)
        self._hotkey_shortcut.textChanged.connect(self._auto_save)
        hotkey_layout.addWidget(self._hotkey_shortcut, 1, 1)
        layout.addWidget(hotkey_group)

        layout.addStretch()
        return widget

    # ── Style Tab ──

    def _create_style_tab(self):
        from subtitle_presets import DEFAULT_STYLE

        widget = QWidget()
        layout = QVBoxLayout(widget)
        s = self._current_settings.get("style", dict(DEFAULT_STYLE))

        # Preset group
        preset_group = QGroupBox(t("group_preset"))
        preset_layout = QHBoxLayout(preset_group)
        self._style_preset = QComboBox()
        preset_names = [
            ("default", t("preset_default")),
            ("transparent", t("preset_transparent")),
            ("compact", t("preset_compact")),
            ("light", t("preset_light")),
            ("dracula", t("preset_dracula")),
            ("nord", t("preset_nord")),
            ("monokai", t("preset_monokai")),
            ("solarized", t("preset_solarized")),
            ("gruvbox", t("preset_gruvbox")),
            ("tokyo_night", t("preset_tokyo_night")),
            ("catppuccin", t("preset_catppuccin")),
            ("one_dark", t("preset_one_dark")),
            ("everforest", t("preset_everforest")),
            ("kanagawa", t("preset_kanagawa")),
            ("paper_dark", t("preset_paper_dark")),
            ("custom", t("preset_custom")),
        ]
        self._preset_keys = [k for k, _ in preset_names]
        for _, label in preset_names:
            self._style_preset.addItem(label)
        current_preset = s.get("preset", "default")
        if current_preset in self._preset_keys:
            self._style_preset.setCurrentIndex(self._preset_keys.index(current_preset))
        else:
            self._style_preset.setCurrentIndex(self._preset_keys.index("custom"))
        self._style_preset.currentIndexChanged.connect(self._on_preset_changed)
        preset_layout.addWidget(self._style_preset, 1)
        reset_btn = QPushButton(t("btn_reset_style"))
        reset_btn.clicked.connect(self._reset_style)
        preset_layout.addWidget(reset_btn)
        reset_pos_btn = QPushButton(t("btn_reset_positions"))
        reset_pos_btn.clicked.connect(self.reset_positions.emit)
        preset_layout.addWidget(reset_pos_btn)
        layout.addWidget(preset_group)

        # Background group
        bg_group = QGroupBox(t("group_background"))
        bg_layout = QGridLayout(bg_group)
        bg_layout.setColumnStretch(0, 1)
        bg_layout.setColumnMinimumWidth(1, 180)

        bg_layout.addWidget(QLabel(t("label_bg_color")), 0, 0)
        self._bg_color_btn = self._make_color_btn(
            s.get("bg_color", DEFAULT_STYLE["bg_color"])
        )
        self._bg_color_btn.clicked.connect(lambda: self._pick_color(self._bg_color_btn))
        bg_layout.addWidget(self._bg_color_btn, 0, 1)

        bg_layout.addWidget(QLabel(t("label_bg_opacity")), 1, 0)
        self._bg_opacity = QSpinBox()
        self._bg_opacity.setRange(0, 100)
        self._bg_opacity.setSuffix("%")
        self._bg_opacity.setValue(round(s.get("bg_opacity", DEFAULT_STYLE["bg_opacity"]) / 255 * 100))
        self._bg_opacity.valueChanged.connect(self._on_style_value_changed)
        self._bg_opacity.valueChanged.connect(self._auto_save)
        bg_layout.addWidget(self._bg_opacity, 1, 1)

        bg_layout.addWidget(QLabel(t("label_header_color")), 2, 0)
        self._header_color_btn = self._make_color_btn(
            s.get("header_color", DEFAULT_STYLE["header_color"])
        )
        self._header_color_btn.clicked.connect(
            lambda: self._pick_color(self._header_color_btn)
        )
        bg_layout.addWidget(self._header_color_btn, 2, 1)

        bg_layout.addWidget(QLabel(t("label_header_opacity")), 3, 0)
        self._header_opacity = QSpinBox()
        self._header_opacity.setRange(0, 100)
        self._header_opacity.setSuffix("%")
        self._header_opacity.setValue(round(s.get("header_opacity", DEFAULT_STYLE["header_opacity"]) / 255 * 100))
        self._header_opacity.valueChanged.connect(self._on_style_value_changed)
        self._header_opacity.valueChanged.connect(self._auto_save)
        bg_layout.addWidget(self._header_opacity, 3, 1)

        bg_layout.addWidget(QLabel(t("label_border_radius")), 4, 0)
        self._border_radius = QSpinBox()
        self._border_radius.setRange(0, 30)
        self._border_radius.setValue(
            s.get("border_radius", DEFAULT_STYLE["border_radius"])
        )
        self._border_radius.setSuffix(" px")
        self._border_radius.valueChanged.connect(self._on_style_value_changed)
        self._border_radius.valueChanged.connect(self._auto_save)
        bg_layout.addWidget(self._border_radius, 4, 1)

        layout.addWidget(bg_group)

        # Text group
        text_group = QGroupBox(t("group_text"))
        text_layout = QGridLayout(text_group)
        text_layout.setColumnStretch(0, 1)
        text_layout.setColumnMinimumWidth(1, 180)

        text_layout.addWidget(QLabel(t("label_original_font")), 0, 0)
        self._orig_font_combo = QFontComboBox()
        self._orig_font_combo.setCurrentFont(
            QFont(s.get("original_font_family", DEFAULT_STYLE["original_font_family"]))
        )
        self._orig_font_combo.currentFontChanged.connect(self._on_style_value_changed)
        self._orig_font_combo.currentFontChanged.connect(self._auto_save)
        text_layout.addWidget(self._orig_font_combo, 0, 1)

        text_layout.addWidget(QLabel(t("label_original_font_size")), 1, 0)
        self._orig_font_size = QSpinBox()
        self._orig_font_size.setRange(6, 24)
        self._orig_font_size.setValue(
            s.get("original_font_size", DEFAULT_STYLE["original_font_size"])
        )
        self._orig_font_size.setSuffix(" pt")
        self._orig_font_size.valueChanged.connect(self._on_style_value_changed)
        self._orig_font_size.valueChanged.connect(self._auto_save)
        text_layout.addWidget(self._orig_font_size, 1, 1)

        text_layout.addWidget(QLabel(t("label_original_color")), 2, 0)
        self._orig_color_btn = self._make_color_btn(
            s.get("original_color", DEFAULT_STYLE["original_color"])
        )
        self._orig_color_btn.clicked.connect(
            lambda: self._pick_color(self._orig_color_btn)
        )
        text_layout.addWidget(self._orig_color_btn, 2, 1)

        text_layout.addWidget(QLabel(t("label_translation_font")), 3, 0)
        self._trans_font_combo = QFontComboBox()
        self._trans_font_combo.setCurrentFont(
            QFont(
                s.get(
                    "translation_font_family", DEFAULT_STYLE["translation_font_family"]
                )
            )
        )
        self._trans_font_combo.currentFontChanged.connect(self._on_style_value_changed)
        self._trans_font_combo.currentFontChanged.connect(self._auto_save)
        text_layout.addWidget(self._trans_font_combo, 3, 1)

        text_layout.addWidget(QLabel(t("label_translation_font_size")), 4, 0)
        self._trans_font_size = QSpinBox()
        self._trans_font_size.setRange(6, 24)
        self._trans_font_size.setValue(
            s.get("translation_font_size", DEFAULT_STYLE["translation_font_size"])
        )
        self._trans_font_size.setSuffix(" pt")
        self._trans_font_size.valueChanged.connect(self._on_style_value_changed)
        self._trans_font_size.valueChanged.connect(self._auto_save)
        text_layout.addWidget(self._trans_font_size, 4, 1)

        text_layout.addWidget(QLabel(t("label_translation_color")), 5, 0)
        self._trans_color_btn = self._make_color_btn(
            s.get("translation_color", DEFAULT_STYLE["translation_color"])
        )
        self._trans_color_btn.clicked.connect(
            lambda: self._pick_color(self._trans_color_btn)
        )
        text_layout.addWidget(self._trans_color_btn, 5, 1)

        text_layout.addWidget(QLabel(t("label_timestamp_color")), 6, 0)
        self._ts_color_btn = self._make_color_btn(
            s.get("timestamp_color", DEFAULT_STYLE["timestamp_color"])
        )
        self._ts_color_btn.clicked.connect(lambda: self._pick_color(self._ts_color_btn))
        text_layout.addWidget(self._ts_color_btn, 6, 1)

        layout.addWidget(text_group)

        # Window group
        win_group = QGroupBox(t("group_window"))
        win_layout = QGridLayout(win_group)
        win_layout.setColumnStretch(0, 1)
        win_layout.setColumnMinimumWidth(1, 180)
        win_layout.addWidget(QLabel(t("label_window_opacity")), 0, 0)
        self._window_opacity = QSpinBox()
        self._window_opacity.setRange(0, 100)
        self._window_opacity.setSuffix("%")
        self._window_opacity.setValue(s.get("window_opacity", DEFAULT_STYLE["window_opacity"]))
        self._window_opacity.valueChanged.connect(self._on_style_value_changed)
        self._window_opacity.valueChanged.connect(self._auto_save)
        win_layout.addWidget(self._window_opacity, 0, 1)
        layout.addWidget(win_group)

        layout.addStretch()
        return widget

    def _make_color_btn(self, color: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedSize(60, 24)
        btn.setProperty("hex_color", color)
        btn.setStyleSheet(
            f"background-color: {color}; border: 1px solid #888; border-radius: 3px;"
        )
        return btn

    def _pick_color(self, btn: QPushButton):
        from PyQt6.QtGui import QColor as _QColor

        current = _QColor(btn.property("hex_color"))
        color = QColorDialog.getColor(current, self)
        if color.isValid():
            hex_c = color.name()
            btn.setProperty("hex_color", hex_c)
            btn.setStyleSheet(
                f"background-color: {hex_c}; border: 1px solid #888; border-radius: 3px;"
            )
            self._on_style_value_changed()
            self._auto_save()

    def _collect_style(self) -> dict:
        return {
            "preset": self._preset_keys[self._style_preset.currentIndex()],
            "bg_color": self._bg_color_btn.property("hex_color"),
            "bg_opacity": round(self._bg_opacity.value() / 100 * 255),
            "header_color": self._header_color_btn.property("hex_color"),
            "header_opacity": round(self._header_opacity.value() / 100 * 255),
            "border_radius": self._border_radius.value(),
            "original_font_family": self._orig_font_combo.currentFont().family(),
            "translation_font_family": self._trans_font_combo.currentFont().family(),
            "original_font_size": self._orig_font_size.value(),
            "translation_font_size": self._trans_font_size.value(),
            "original_color": self._orig_color_btn.property("hex_color"),
            "translation_color": self._trans_color_btn.property("hex_color"),
            "timestamp_color": self._ts_color_btn.property("hex_color"),
            "window_opacity": self._window_opacity.value(),
        }

    def _apply_style_to_controls(self, s: dict):
        """Update all style controls to match a style dict, without triggering auto-save."""
        self._bg_color_btn.setProperty("hex_color", s["bg_color"])
        self._bg_color_btn.setStyleSheet(
            f"background-color: {s['bg_color']}; border: 1px solid #888; border-radius: 3px;"
        )
        self._bg_opacity.setValue(round(s["bg_opacity"] / 255 * 100))
        self._header_color_btn.setProperty("hex_color", s["header_color"])
        self._header_color_btn.setStyleSheet(
            f"background-color: {s['header_color']}; border: 1px solid #888; border-radius: 3px;"
        )
        self._header_opacity.setValue(round(s["header_opacity"] / 255 * 100))
        self._border_radius.setValue(s["border_radius"])
        self._orig_font_combo.setCurrentFont(QFont(s["original_font_family"]))
        self._trans_font_combo.setCurrentFont(QFont(s["translation_font_family"]))
        self._orig_font_size.setValue(s["original_font_size"])
        self._trans_font_size.setValue(s["translation_font_size"])
        self._orig_color_btn.setProperty("hex_color", s["original_color"])
        self._orig_color_btn.setStyleSheet(
            f"background-color: {s['original_color']}; border: 1px solid #888; border-radius: 3px;"
        )
        self._trans_color_btn.setProperty("hex_color", s["translation_color"])
        self._trans_color_btn.setStyleSheet(
            f"background-color: {s['translation_color']}; border: 1px solid #888; border-radius: 3px;"
        )
        self._ts_color_btn.setProperty("hex_color", s["timestamp_color"])
        self._ts_color_btn.setStyleSheet(
            f"background-color: {s['timestamp_color']}; border: 1px solid #888; border-radius: 3px;"
        )
        self._window_opacity.setValue(s["window_opacity"])

    def _on_preset_changed(self, index):
        from subtitle_presets import STYLE_PRESETS

        key = self._preset_keys[index]
        if key == "custom":
            return
        preset = STYLE_PRESETS.get(key)
        if not preset:
            return
        self._block_style_signals(True)
        self._apply_style_to_controls(preset)
        self._block_style_signals(False)
        self._auto_save()

    def _on_style_value_changed(self, *_args):
        """When any style control changes manually, switch preset to Custom."""
        custom_idx = len(self._preset_keys) - 1
        if self._style_preset.currentIndex() != custom_idx:
            self._style_preset.blockSignals(True)
            self._style_preset.setCurrentIndex(custom_idx)
            self._style_preset.blockSignals(False)
        self._auto_save()

    def _reset_style(self):
        from subtitle_presets import DEFAULT_STYLE

        self._style_preset.blockSignals(True)
        self._style_preset.setCurrentIndex(0)  # default
        self._style_preset.blockSignals(False)
        self._block_style_signals(True)
        self._apply_style_to_controls(DEFAULT_STYLE)
        self._block_style_signals(False)
        self._auto_save()

    def _block_style_signals(self, block: bool):
        for w in (
            self._bg_opacity,
            self._header_opacity,
            self._border_radius,
            self._orig_font_combo,
            self._trans_font_combo,
            self._orig_font_size,
            self._trans_font_size,
            self._window_opacity,
        ):
            w.blockSignals(block)

    # ── Subtitle Tab ──

    def _create_subtitle_tab(self):
        subtitle_settings = self._current_settings.get("subtitle_mode") or {}
        self._subtitle_widget = SubtitleSettingsWidget(subtitle_settings)
        self._subtitle_widget.settings_changed.connect(self._on_subtitle_settings_changed)
        return self._subtitle_widget

    def _on_subtitle_settings_changed(self, s):
        self._current_settings["subtitle_mode"] = s
        self._auto_save()
        self.subtitle_settings_changed.emit(s)

    def update_subtitle_settings(self, s):
        self._current_settings["subtitle_mode"] = s
        self._subtitle_widget.update_settings(s)

    # ── History Tab ──

    def _create_history_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        ctrl_row = QHBoxLayout()
        self._history_enabled = QCheckBox(t("history_enabled"))
        self._history_enabled.setChecked(
            self._current_settings.get("privacy", {}).get("history_enabled", True)
        )
        self._history_enabled.toggled.connect(self._on_history_enabled_changed)
        self._history_enabled.toggled.connect(self._auto_save)
        ctrl_row.addWidget(self._history_enabled)
        ctrl_row.addWidget(QLabel(t("history_filter")))
        self._history_filter = QComboBox()
        self._history_filter.addItem(t("history_filter_all"), "all")
        self._history_filter.addItem(t("history_filter_subtitle"), "subtitle")
        self._history_filter.addItem(t("history_filter_selection"), "selection")
        self._history_filter.addItem(t("history_filter_hotkey"), "global-hotkey")
        self._history_filter.addItem(t("history_filter_extension"), "chrome-extension")
        self._history_filter.addItem(t("history_filter_manual"), "manual")
        self._history_filter.currentIndexChanged.connect(self._refresh_history)
        ctrl_row.addWidget(self._history_filter)
        ctrl_row.addStretch()
        refresh_btn = QPushButton(t("history_refresh"))
        refresh_btn.clicked.connect(self._refresh_history)
        ctrl_row.addWidget(refresh_btn)
        export_btn = QPushButton(t("history_export_session"))
        export_btn.clicked.connect(self._export_selected_history_session)
        ctrl_row.addWidget(export_btn)
        delete_btn = QPushButton(t("history_delete_selected"))
        delete_btn.clicked.connect(self._delete_selected_history)
        ctrl_row.addWidget(delete_btn)
        clear_btn = QPushButton(t("history_clear_all"))
        clear_btn.clicked.connect(self._clear_history)
        ctrl_row.addWidget(clear_btn)
        layout.addLayout(ctrl_row)

        self._history_table = QTableWidget(0, 5)
        self._history_table.setHorizontalHeaderLabels(
            [
                t("history_time"),
                t("history_type"),
                t("history_original"),
                t("history_translation"),
                t("history_model"),
            ]
        )
        self._history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._history_table.itemSelectionChanged.connect(self._show_selected_history_detail)
        self._history_table.horizontalHeader().setStretchLastSection(True)

        # Swap between the populated table and an illustrated empty state.
        from PyQt6.QtWidgets import QStackedWidget

        self._history_stack = QStackedWidget()
        self._history_stack.addWidget(self._history_table)
        self._history_empty = self._make_empty_state(
            "empty_history_folder_plant.png",
            "history_empty_title",
            "history_empty_desc",
        )
        self._history_stack.addWidget(self._history_empty)
        layout.addWidget(self._history_stack, 2)

        self._history_detail = QTextEdit()
        self._history_detail.setReadOnly(True)
        self._history_detail.setMaximumHeight(180)
        layout.addWidget(self._history_detail, 1)

        QTimer.singleShot(0, self._refresh_history)
        return widget

    def _refresh_history(self):
        if not hasattr(self, "_history_table"):
            return
        source_filter = self._history_filter.currentData() or "all"
        self._history_rows = self._history_store.list_history(source_filter, limit=300)
        if hasattr(self, "_history_stack"):
            self._history_stack.setCurrentWidget(
                self._history_table if self._history_rows else self._history_empty
            )
        self._history_table.setRowCount(len(self._history_rows))
        for row_idx, row in enumerate(self._history_rows):
            values = [
                row.get("created_at") or "",
                row.get("source_type") or "",
                self._short_text(row.get("original_text") or ""),
                self._short_text(row.get("translated_text") or ""),
                row.get("model") or "",
            ]
            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row_idx)
                self._history_table.setItem(row_idx, col_idx, item)
        self._history_table.resizeColumnsToContents()
        self._history_detail.clear()

    def _on_history_enabled_changed(self):
        self._current_settings["privacy"] = {
            "history_enabled": self._history_enabled.isChecked()
        }

    def _show_selected_history_detail(self):
        row = self._selected_history_row()
        if not row:
            self._history_detail.clear()
            return
        parts = [
            f"{t('history_time')}: {row.get('created_at') or ''}",
            f"{t('history_type')}: {row.get('source_type') or ''}",
            f"Provider: {row.get('provider') or ''}",
            f"Model: {row.get('model') or ''}",
            "",
            f"{t('history_original')}:\n{row.get('original_text') or ''}",
            "",
            f"{t('history_translation')}:\n{row.get('translated_text') or ''}",
        ]
        if row.get("explanation"):
            parts.extend(["", f"{t('history_explanation')}:\n{row.get('explanation')}"])
        if row.get("polished_text"):
            parts.extend(["", f"{t('history_polished')}:\n{row.get('polished_text')}"])
        self._history_detail.setPlainText("\n".join(parts))

    def _delete_selected_history(self):
        row = self._selected_history_row()
        if not row:
            QMessageBox.information(self, "LiveTranslate", t("history_select_first"))
            return
        reply = QMessageBox.question(
            self,
            "LiveTranslate",
            t("history_delete_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._history_store.delete_history(row["table_name"], row["id"])
        self._refresh_history()

    def _clear_history(self):
        reply = QMessageBox.question(
            self,
            "LiveTranslate",
            t("history_clear_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._history_store.clear_history("all")
        self._refresh_history()

    def _export_selected_history_session(self):
        row = self._selected_history_row()
        if not row:
            QMessageBox.information(self, "LiveTranslate", t("history_select_first"))
            return
        session_id = row.get("session_id")
        if not session_id:
            QMessageBox.information(self, "LiveTranslate", t("history_export_subtitle_only"))
            return
        subtitles = self._history_store.get_session_subtitles(session_id)
        if not subtitles:
            QMessageBox.information(self, "LiveTranslate", t("export_empty"))
            return
        default_name = f"livetrans_session_{session_id[:8]}.md"
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            t("export_dialog_title"),
            default_name,
            t("export_filter"),
        )
        if not path:
            return
        fmt = self._detect_history_export_format(path, selected_filter)
        if "." not in os.path.basename(path):
            path = f"{path}.{fmt}"
        items = subtitle_rows_to_export_items(subtitles)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(export_subtitles(items, "both", fmt))
        except OSError as e:
            QMessageBox.critical(
                self,
                "LiveTranslate",
                t("export_failed").format(error=str(e)),
            )

    @staticmethod
    def _detect_history_export_format(path: str, selected_filter: str) -> str:
        lower_path = path.lower()
        if lower_path.endswith(".srt"):
            return "srt"
        if lower_path.endswith(".md") or lower_path.endswith(".markdown"):
            return "md"
        if "srt" in (selected_filter or "").lower():
            return "srt"
        if "markdown" in (selected_filter or "").lower():
            return "md"
        return "txt"

    def _selected_history_row(self):
        selected = self._history_table.selectedItems()
        if not selected:
            return None
        row_idx = selected[0].data(Qt.ItemDataRole.UserRole)
        if row_idx is None or row_idx >= len(self._history_rows):
            return None
        return self._history_rows[row_idx]

    @staticmethod
    def _short_text(text: str, limit: int = 80) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    # ── TTS Tab (simultaneous interpretation) ──

    def _create_tts_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        s = self._current_settings
        tts = s.get("tts", {})

        intro = QLabel(t("tts_intro"))
        intro.setWordWrap(True)
        intro.setStyleSheet(info_banner_stylesheet())
        layout.addWidget(intro)

        enable_group = QGroupBox(t("group_tts_enable"))
        enable_layout = QGridLayout(enable_group)
        enable_layout.setColumnStretch(0, 1)
        enable_layout.setColumnMinimumWidth(1, 200)

        self._tts_enabled_cb = QCheckBox(t("label_tts_enabled"))
        self._tts_enabled_cb.setChecked(bool(tts.get("enabled", False)))
        self._tts_enabled_cb.toggled.connect(self._on_tts_enabled_toggled)
        enable_layout.addWidget(self._tts_enabled_cb, 0, 0, 1, 2)

        self._tts_volume = QSlider(Qt.Orientation.Horizontal)
        self._tts_volume.setRange(0, 100)
        vol_pct = int(round(float(tts.get("volume", 1.0)) * 100))
        self._tts_volume.setValue(vol_pct)
        self._tts_volume_label = QLabel(f"{vol_pct}%")
        self._tts_volume_label.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        self._tts_volume.valueChanged.connect(
            lambda v: self._tts_volume_label.setText(f"{v}%")
        )
        self._tts_volume.sliderReleased.connect(self._auto_save)
        vol_row = QHBoxLayout()
        vol_row.addWidget(self._tts_volume)
        vol_row.addWidget(self._tts_volume_label)
        enable_layout.addWidget(QLabel(t("label_tts_volume")), 1, 0)
        enable_layout.addLayout(vol_row, 1, 1)

        self._tts_output = QComboBox()
        self._tts_output.addItem(t("system_default"), "")
        try:
            from audio_providers import list_output_devices

            for name in list_output_devices():
                self._tts_output.addItem(name, name)
        except Exception:
            pass
        saved_out = tts.get("output_device", "")
        out_idx = self._tts_output.findData(saved_out) if saved_out else 0
        self._tts_output.setCurrentIndex(out_idx if out_idx >= 0 else 0)
        self._tts_output.currentIndexChanged.connect(self._auto_save)
        enable_layout.addWidget(QLabel(t("label_tts_output")), 2, 0)
        enable_layout.addWidget(self._tts_output, 2, 1)

        layout.addWidget(enable_group)

        # Voice/API profiles — multiple configs, switchable.
        profiles_group = QGroupBox(t("group_tts_profiles"))
        profiles_layout = QVBoxLayout(profiles_group)

        self._tts_profile_list = QListWidget()
        self._tts_profile_list.setFont(QFont("Consolas", 9))
        self._tts_profile_list.itemDoubleClicked.connect(
            self._on_tts_profile_double_clicked
        )
        self._tts_profile_list.currentRowChanged.connect(self._on_tts_profile_selected)
        profiles_layout.addWidget(self._tts_profile_list)

        btn_row = QHBoxLayout()
        add_btn = QPushButton(t("btn_add"))
        add_btn.clicked.connect(self._add_tts_profile)
        btn_row.addWidget(add_btn)
        edit_btn = QPushButton(t("btn_edit"))
        edit_btn.clicked.connect(self._edit_tts_profile)
        btn_row.addWidget(edit_btn)
        dup_btn = QPushButton(t("btn_duplicate"))
        dup_btn.clicked.connect(self._dup_tts_profile)
        btn_row.addWidget(dup_btn)
        del_btn = QPushButton(t("btn_remove"))
        del_btn.clicked.connect(self._remove_tts_profile)
        btn_row.addWidget(del_btn)
        profiles_layout.addLayout(btn_row)

        test_row = QHBoxLayout()
        self._tts_test_btn = QPushButton(t("btn_tts_test"))
        self._tts_test_btn.clicked.connect(self._test_tts)
        test_row.addWidget(self._tts_test_btn)
        self._tts_test_label = QLabel("")
        self._tts_test_label.setWordWrap(True)
        test_row.addWidget(self._tts_test_label, 1)
        profiles_layout.addLayout(test_row)

        layout.addWidget(profiles_group)
        self._refresh_tts_profile_list()

        layout.addStretch()
        return widget

    def _on_tts_enabled_toggled(self, checked: bool):
        """Settings checkbox → persist + broadcast so overlay/tray stay in sync."""
        self._auto_save()
        self.tts_enabled_changed.emit(bool(checked))

    def set_tts_enabled(self, enabled: bool):
        """External one-click toggle (overlay/tray) → update the checkbox.

        Updates the checkbox without re-broadcasting to avoid signal loops; the
        caller is responsible for applying the change to the running engine.
        """
        if not hasattr(self, "_tts_enabled_cb"):
            return
        if self._tts_enabled_cb.isChecked() == bool(enabled):
            return
        self._tts_enabled_cb.blockSignals(True)
        self._tts_enabled_cb.setChecked(bool(enabled))
        self._tts_enabled_cb.blockSignals(False)
        self._auto_save()

    def is_tts_enabled(self) -> bool:
        if hasattr(self, "_tts_enabled_cb"):
            return self._tts_enabled_cb.isChecked()
        return bool(self._current_settings.get("tts", {}).get("enabled", False))

    # ── TTS profile management ──

    def _tts_profiles(self) -> list:
        tts = self._current_settings.setdefault("tts", {})
        profiles = tts.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            from tts_providers import DEFAULT_PROFILE, resolve_profile

            # Migrate a legacy flat config into a single profile.
            migrated = resolve_profile(tts)
            if not migrated.get("name"):
                migrated = dict(DEFAULT_PROFILE)
            profiles = [migrated]
            tts["profiles"] = profiles
            tts.setdefault("active_profile", 0)
        return profiles

    def _refresh_tts_profile_list(self):
        self._tts_profile_list.blockSignals(True)
        self._tts_profile_list.clear()
        profiles = self._tts_profiles()
        active = self._current_settings.get("tts", {}).get("active_profile", 0)
        for i, p in enumerate(profiles):
            prefix = ">>> " if i == active else "    "
            engine = p.get("engine", "edge")
            if engine == "openai-speech":
                detail = f"{p.get('api_base', '')}  |  {p.get('model', 'tts-1')}"
            else:
                detail = p.get("voice", "") or t("tts_voice_auto")
            text = f"{prefix}{p.get('name', '?')}  [{engine}]\n     {detail}"
            item = QListWidgetItem(text)
            if i == active:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self._tts_profile_list.addItem(item)
        if 0 <= active < len(profiles):
            self._tts_profile_list.setCurrentRow(active)
        self._tts_profile_list.blockSignals(False)

    def _on_tts_profile_selected(self, row: int):
        profiles = self._tts_profiles()
        if 0 <= row < len(profiles):
            self._current_settings.setdefault("tts", {})["active_profile"] = row
            self._refresh_tts_profile_list()
            self._auto_save()

    def _on_tts_profile_double_clicked(self, item):
        row = self._tts_profile_list.row(item)
        self._tts_profile_list.setCurrentRow(row)
        self._edit_tts_profile()

    def _add_tts_profile(self):
        from dialogs import TTSConfigEditDialog

        target_lang = self._current_settings.get("target_language", "zh")
        dlg = TTSConfigEditDialog(self, target_language=target_lang)
        if dlg.exec():
            data = dlg.get_data()
            if data["name"]:
                self._tts_profiles().append(data)
                self._refresh_tts_profile_list()
                self._auto_save()

    def _edit_tts_profile(self):
        from dialogs import TTSConfigEditDialog

        row = self._tts_profile_list.currentRow()
        profiles = self._tts_profiles()
        if row < 0 or row >= len(profiles):
            return
        target_lang = self._current_settings.get("target_language", "zh")
        dlg = TTSConfigEditDialog(self, profiles[row], target_language=target_lang)
        if dlg.exec():
            data = dlg.get_data()
            if data["name"]:
                profiles[row] = data
                self._refresh_tts_profile_list()
                self._auto_save()

    def _dup_tts_profile(self):
        row = self._tts_profile_list.currentRow()
        profiles = self._tts_profiles()
        if row < 0 or row >= len(profiles):
            return
        dup = dict(profiles[row])
        dup["name"] = dup.get("name", "TTS") + " (copy)"
        profiles.append(dup)
        self._refresh_tts_profile_list()
        self._auto_save()

    def _remove_tts_profile(self):
        row = self._tts_profile_list.currentRow()
        profiles = self._tts_profiles()
        if row < 0 or row >= len(profiles) or len(profiles) <= 1:
            return
        profiles.pop(row)
        tts = self._current_settings.setdefault("tts", {})
        active = tts.get("active_profile", 0)
        # Adjust the active index for the removal so it keeps pointing at the
        # same profile: removing an entry *before* the active one shifts it down.
        if row < active:
            active -= 1
        active = max(0, min(active, len(profiles) - 1))
        tts["active_profile"] = active
        self._refresh_tts_profile_list()
        self._auto_save()

    def _collect_tts(self) -> dict:
        tts = dict(self._current_settings.get("tts", {}))
        tts["enabled"] = self._tts_enabled_cb.isChecked()
        tts["volume"] = round(self._tts_volume.value() / 100.0, 2)
        tts["output_device"] = self._tts_output.currentData() or ""
        tts["profiles"] = self._tts_profiles()
        tts["active_profile"] = self._current_settings.get("tts", {}).get(
            "active_profile", 0
        )
        return tts

    def _test_tts(self):
        """Synthesize and play a short sample using the active TTS profile."""
        self._apply_settings()
        self._tts_test_label.setStyleSheet("")
        self._tts_test_label.setText(t("tts_testing"))
        QApplication.processEvents()
        cfg = self._current_settings.get("tts", {})
        target_lang = self._current_settings.get("target_language", "zh")
        sample = t("tts_sample_text")

        def _run():
            try:
                from tts_providers import build_provider_config, create_tts_provider
                import numpy as np
                import pyaudiowpatch as pyaudio

                provider = create_tts_provider(build_provider_config(cfg))
                result = provider.synthesize(sample, target_lang)
                provider.close()
                if result is None:
                    self._tts_test_done.emit(False, t("tts_test_empty"))
                    return
                samples, sr = result
                vol = float(cfg.get("volume", 1.0))
                if vol != 1.0:
                    samples = samples * vol
                samples = np.clip(samples, -1.0, 1.0).astype(np.float32)
                ch = samples.shape[1] if samples.ndim > 1 else 1
                pa = pyaudio.PyAudio()
                out_idx = None
                dev_name = cfg.get("output_device", "")
                if dev_name:
                    for i in range(pa.get_device_count()):
                        d = pa.get_device_info_by_index(i)
                        if d.get("maxOutputChannels", 0) > 0 and d.get("name") == dev_name:
                            out_idx = i
                            break
                stream = pa.open(
                    format=pyaudio.paFloat32,
                    channels=ch,
                    rate=sr,
                    output=True,
                    output_device_index=out_idx,
                )
                stream.write(samples.tobytes())
                stream.stop_stream()
                stream.close()
                pa.terminate()
                self._tts_test_done.emit(True, t("tts_test_ok"))
            except Exception as e:
                self._tts_test_done.emit(False, str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_tts_test_done(self, ok: bool, msg: str):
        color = "#2e7d32" if ok else "#c62828"
        self._tts_test_label.setStyleSheet(f"color: {color};")
        self._tts_test_label.setText(msg)

    # ── Benchmark Tab ──

    def _create_benchmark_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel(t("label_source")))
        self._bench_lang = QComboBox()
        self._bench_lang.addItems(["ja", "en", "zh", "ko", "fr", "de"])
        self._bench_lang.setCurrentIndex(0)
        ctrl_row.addWidget(self._bench_lang)
        ctrl_row.addWidget(QLabel(t("target_label")))
        self._bench_target = QComboBox()
        self._bench_target.addItems(["zh", "en", "ja", "ko", "fr", "de", "es", "ru"])
        ctrl_row.addWidget(self._bench_target)
        ctrl_row.addStretch()
        self._bench_btn = QPushButton(t("btn_test_all"))
        self._bench_btn.clicked.connect(self._run_benchmark)
        ctrl_row.addWidget(self._bench_btn)
        layout.addLayout(ctrl_row)

        self._bench_output = QTextEdit()
        self._bench_output.setReadOnly(True)
        self._bench_output.setFont(QFont("Consolas", 9))
        self._bench_output.setStyleSheet(console_stylesheet())
        layout.addWidget(self._bench_output)

        return widget

    # ── Diagnostics Tab ──

    def _create_diagnostics_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel(t("diagnostics_desc"))
        info.setWordWrap(True)
        layout.addWidget(info)

        export_btn = QPushButton(t("btn_export_diagnostics"))
        export_btn.clicked.connect(self._export_diagnostics_bundle)
        layout.addWidget(export_btn)

        self._diagnostics_status = QTextEdit()
        self._diagnostics_status.setReadOnly(True)
        self._diagnostics_status.setMinimumHeight(140)
        self._diagnostics_status.setPlainText(t("diagnostics_privacy_hint"))
        layout.addWidget(self._diagnostics_status)

        layout.addStretch()
        return widget

    def _export_diagnostics_bundle(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            t("diagnostics_save_title"),
            "livetranslate-diagnostics.zip",
            "Zip Files (*.zip)",
        )
        if not path:
            return
        try:
            bundle = create_diagnostic_bundle(path)
            self._diagnostics_status.setPlainText(
                t("diagnostics_exported").format(path=str(bundle))
            )
        except Exception as e:
            self._diagnostics_status.setPlainText(
                t("diagnostics_failed").format(error=str(e))
            )

    # ── Cache Tab ──

    def _create_changelog_tab(self):
        from dialogs import _load_latest_changelog
        widget = QWidget()
        layout = QVBoxLayout(widget)
        _, html = _load_latest_changelog()
        from PyQt6.QtWidgets import QTextBrowser
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(html)
        browser.setFont(QFont("Microsoft YaHei UI", 10))
        layout.addWidget(browser)
        return widget

    def _create_cache_tab(self):
        from PyQt6.QtWidgets import QCheckBox

        widget = QWidget()
        layout = QVBoxLayout(widget)
        s = self._current_settings

        # Transcript auto-save group
        ts_group = QGroupBox(t("group_transcript"))
        ts_layout = QHBoxLayout(ts_group)
        self._auto_save_transcript_cb = QCheckBox(t("label_auto_save_transcript"))
        self._auto_save_transcript_cb.setToolTip(t("auto_save_transcript_tooltip"))
        self._auto_save_transcript_cb.setChecked(s.get("auto_save_transcript", True))
        self._auto_save_transcript_cb.toggled.connect(self._auto_save)
        ts_layout.addWidget(self._auto_save_transcript_cb, 1)
        ts_open_btn = QPushButton(t("btn_open_transcripts"))
        ts_open_btn.clicked.connect(self._open_transcripts_folder)
        ts_layout.addWidget(ts_open_btn)
        layout.addWidget(ts_group)

        top_row = QHBoxLayout()
        self._cache_total = QLabel("")
        self._cache_total.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        top_row.addWidget(self._cache_total, 1)
        open_btn = QPushButton(t("btn_open_folder"))
        open_btn.clicked.connect(
            lambda: (
                get_models_dir().mkdir(parents=True, exist_ok=True),
                os.startfile(str(get_models_dir())),
            )
        )
        top_row.addWidget(open_btn)
        delete_all_btn = QPushButton(t("btn_delete_all_exit"))
        delete_all_btn.clicked.connect(self._delete_all_and_exit)
        top_row.addWidget(delete_all_btn)
        layout.addLayout(top_row)

        self._cache_list = QListWidget()
        self._cache_list.setFont(QFont("Consolas", 9))
        self._cache_list.setAlternatingRowColors(True)

        from PyQt6.QtWidgets import QStackedWidget

        self._cache_stack = QStackedWidget()
        self._cache_stack.addWidget(self._cache_list)
        self._cache_empty = self._make_empty_state(
            "empty_cache_storage_box.png",
            "cache_empty_title",
            "cache_empty_desc",
        )
        self._cache_stack.addWidget(self._cache_empty)
        layout.addWidget(self._cache_stack, 1)

        self._cache_entries = []
        self._refresh_cache()

        return widget

    def _open_transcripts_folder(self):
        ts_dir = data_path("transcripts")
        ts_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(ts_dir))

    def _refresh_cache(self):
        self._cache_list.clear()
        self._cache_total.setText(t("scanning"))

        def _scan():
            entries = get_cache_entries()
            results = []
            for name, path in entries:
                size = dir_size(path)
                results.append((name, str(path), size))
            self._cache_result.emit(results)

        threading.Thread(target=_scan, daemon=True).start()

    def _on_cache_result(self, results):
        self._cache_list.clear()
        self._cache_entries = results
        total = 0
        for name, path, size in results:
            total += size
            self._cache_list.addItem(f"{name}  —  {format_size(size)}")
        if hasattr(self, "_cache_stack"):
            self._cache_stack.setCurrentWidget(
                self._cache_list if results else self._cache_empty
            )
        elif not results:
            self._cache_list.addItem(t("no_cached_models"))
        self._cache_total.setText(
            t("cache_total").format(size=format_size(total), count=len(results))
        )

    def _delete_all_and_exit(self):
        if not self._cache_entries:
            return
        import shutil

        total_size = sum(s for _, _, s in self._cache_entries)
        ret = QMessageBox.warning(
            self,
            t("dialog_delete_title"),
            t("dialog_delete_msg").format(
                count=len(self._cache_entries), size=format_size(total_size)
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        for name, path, _ in self._cache_entries:
            try:
                shutil.rmtree(path)
                log.info(f"Deleted: {path}")
            except Exception as e:
                log.error(f"Failed to delete {path}: {e}")
        QApplication.instance().quit()

    def _get_asr_lang_code(self) -> str:
        """Get the language code from the ASR language combo (stored as userData)."""
        return self._asr_lang.currentData() or "auto"

    def _on_engine_changed_whisper_vis(self, index):
        self._whisper_group.setVisible(index == 0)
        # Resize window to fit content after whisper group visibility change
        def _fit():
            self.adjustSize()
            h = self.sizeHint().height() + 20
            self.resize(self.width(), max(h, self.minimumHeight()))
        QTimer.singleShot(0, _fit)

    def _on_engine_changed_asr_api_vis(self, index):
        self._asr_api_group.setVisible(index in (5, 6, 7))
        self._update_asr_api_placeholders(index)
        QTimer.singleShot(0, self.adjustSize)

    def _update_asr_api_placeholders(self, index):
        if not hasattr(self, "_asr_api_base"):
            return
        if index == 6:
            self._asr_api_base.setPlaceholderText("https://api.assemblyai.com")
            self._asr_api_model.setPlaceholderText("universal")
            self._asr_api_group.setTitle("AssemblyAI 语音识别")
        elif index == 7:
            self._asr_api_base.setPlaceholderText("wss://streaming.assemblyai.com/v3/ws")
            self._asr_api_model.setPlaceholderText("u3-rt-pro")
            self._asr_api_group.setTitle("AssemblyAI Streaming 语音识别")
        else:
            self._asr_api_base.setPlaceholderText("https://api.openai.com/v1")
            self._asr_api_model.setPlaceholderText("whisper-1")
            self._asr_api_group.setTitle(t("group_asr_api"))

    def _update_whisper_size_label(self):
        from model_manager import is_asr_cached, _MODEL_SIZE_BYTES

        size = self._whisper_size_combo.currentText()
        cached = is_asr_cached("whisper", size, self._current_settings.get("hub", "ms"))
        if cached:
            self._whisper_status.setText(t("whisper_already_cached"))
            self._whisper_status.setStyleSheet("color: #4a4; font-size: 11px;")
            self._whisper_dl_btn.setEnabled(False)
        else:
            est = _MODEL_SIZE_BYTES.get(f"whisper-{size}", 0)
            self._whisper_status.setText(f"~{format_size(est)}")
            self._whisper_status.setStyleSheet("color: #888; font-size: 11px;")
            self._whisper_dl_btn.setEnabled(True)
        self._update_whisper_vram_warning(size)

    def _update_whisper_vram_warning(self, size: str):
        """Warn when the picked Whisper size likely won't fit the chosen GPU's VRAM."""
        if not hasattr(self, "_whisper_vram_warn"):
            return
        from model_manager import WHISPER_VRAM_NEED_GB

        vram = 0.0
        dev = self._asr_device.currentText().split(" (")[0] if hasattr(self, "_asr_device") else ""
        gpus = (getattr(self, "_hw", None) or {}).get("gpus") or []
        if dev.startswith("cuda") and gpus:
            idx = 0
            if ":" in dev:
                try:
                    idx = int(dev.split(":")[1].split(" ")[0])
                except (ValueError, IndexError):
                    idx = 0
            match = next((g for g in gpus if g.get("index") == idx), gpus[0])
            vram = match.get("vram_gb", 0.0)
        need = WHISPER_VRAM_NEED_GB.get(size, 0.0)
        if vram and need and vram < need:
            self._whisper_vram_warn.setText(
                t("hw_whisper_vram_warn").format(vram=vram, size=size)
            )
            self._whisper_vram_warn.setVisible(True)
        else:
            self._whisper_vram_warn.setVisible(False)

    def _apply_asr_recommendation(self):
        """Apply the hardware-derived ASR recommendation to the engine/device/size widgets."""
        reco = getattr(self, "_reco", None)
        if not reco:
            return
        # Device: match the recommended device string against the combo entries.
        target_dev = reco.get("device", "cpu")
        for i in range(self._asr_device.count()):
            if self._asr_device.itemText(i).startswith(target_dev.split(":")[0]) and (
                ":" not in target_dev
                or self._asr_device.itemText(i).startswith(target_dev)
            ):
                self._asr_device.setCurrentIndex(i)
                break
        # Engine: cloud machines → OpenAI Audio API (index 5); otherwise local
        # SenseVoice (index 1), the safe cross-tier default.
        engine_idx = 5 if reco.get("mode") == "cloud" else 1
        self._asr_engine.setCurrentIndex(engine_idx)
        # Whisper size hint (applies if the user later switches to Whisper).
        size_idx = self._whisper_size_combo.findText(reco.get("whisper_size", ""))
        if size_idx >= 0:
            self._whisper_size_combo.setCurrentIndex(size_idx)
        self._reco_label.setText("✅ " + t("hw_recommended_applied"))
        self._reco_label.setStyleSheet("color: #4a4; font-size: 11px;")
        self._auto_save()

    def _on_whisper_size_changed(self):
        self._current_settings["whisper_model_size"] = (
            self._whisper_size_combo.currentText()
        )
        self._update_whisper_size_label()
        # If already cached, switch engine immediately
        from model_manager import is_asr_cached

        size = self._whisper_size_combo.currentText()
        if is_asr_cached("whisper", size, self._current_settings.get("hub", "ms")):
            self._auto_save()

    def _download_whisper(self):
        from model_manager import is_asr_cached, get_missing_models

        size = self._whisper_size_combo.currentText()
        hub = self._current_settings.get("hub", "ms")
        if is_asr_cached("whisper", size, hub):
            return
        missing = get_missing_models("whisper", size, hub)
        missing = [m for m in missing if m["type"] != "silero-vad"]
        if not missing:
            return
        from dialogs import ModelDownloadDialog

        dlg = ModelDownloadDialog(missing, hub=hub, parent=self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            self._update_whisper_size_label()
            # Switch to Whisper engine with the downloaded size
            self._auto_save()

    # ── Model Management ──

    def _refresh_model_list(self):
        self._model_list.clear()
        active = self._current_settings.get("active_model", 0)
        for i, m in enumerate(self._current_settings.get("models", [])):
            prefix = ">>> " if i == active else "    "
            proxy = m.get("proxy", "none")
            proxy_tag = f"  [proxy: {proxy}]" if proxy != "none" else ""
            provider = m.get("provider", "openai-compatible")
            text = (
                f"{prefix}{m['name']}{proxy_tag}\n     {provider}  |  {m['api_base']}  |  {m['model']}"
            )
            item = QListWidgetItem(text)
            if i == active:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self._model_list.addItem(item)

    def _emit_models_list_changed(self):
        models = self._current_settings.get("models", [])
        active_idx = self._current_settings.get("active_model", 0)
        self.models_list_changed.emit(models, active_idx)

    def _add_model(self):
        dlg = ModelEditDialog(self)
        if dlg.exec():
            data = dlg.get_data()
            if data["name"] and data["model"]:
                self._current_settings.setdefault("models", []).append(data)
                self._refresh_model_list()
                _save_settings(self._current_settings)
                self._emit_models_list_changed()
                self._refresh_home_status()

    def _edit_model(self):
        row = self._model_list.currentRow()
        models = self._current_settings.get("models", [])
        if row < 0 or row >= len(models):
            return
        dlg = ModelEditDialog(self, models[row])
        if dlg.exec():
            data = dlg.get_data()
            if data["name"] and data["model"]:
                models[row] = data
                self._refresh_model_list()
                _save_settings(self._current_settings)
                self._emit_models_list_changed()
                self._refresh_home_status()
                # Re-apply if editing the active model
                active = self._current_settings.get("active_model", 0)
                if row == active:
                    self.model_changed.emit(data)

    def _dup_model(self):
        row = self._model_list.currentRow()
        models = self._current_settings.get("models", [])
        if row < 0 or row >= len(models):
            return
        dup = dict(models[row])
        dup["name"] = dup["name"] + " (copy)"
        models.append(dup)
        self._refresh_model_list()
        _save_settings(self._current_settings)
        self._emit_models_list_changed()
        self._refresh_home_status()

    def _remove_model(self):
        row = self._model_list.currentRow()
        models = self._current_settings.get("models", [])
        if row < 0 or row >= len(models) or len(models) <= 1:
            return
        active = self._current_settings.get("active_model", 0)
        prev_active_model = models[active] if 0 <= active < len(models) else None
        models.pop(row)
        # Keep active_model pointing at the same profile: removing an entry
        # before the active one shifts the active index down by one.
        if row < active:
            active -= 1
        active = max(0, min(active, len(models) - 1))
        self._current_settings["active_model"] = active
        self._refresh_model_list()
        self._model_list.setCurrentRow(min(row, len(models) - 1))
        _save_settings(self._current_settings)
        self._emit_models_list_changed()
        # If the removal changed which model is active (i.e. the active model
        # itself was removed), rebuild the runtime translator to match.
        new_active_model = models[active] if 0 <= active < len(models) else None
        if new_active_model is not None and new_active_model is not prev_active_model:
            self.model_changed.emit(dict(new_active_model))
        self._refresh_home_status()

    def _on_model_double_clicked(self, item):
        row = self._model_list.row(item)
        models = self._current_settings.get("models", [])
        if 0 <= row < len(models):
            self._model_list.setCurrentRow(row)
            self._edit_model()

    def _run_benchmark(self):
        models = self._current_settings.get("models", [])
        if not models:
            return

        source_lang = self._bench_lang.currentText()
        target_lang = self._bench_target.currentText()
        timeout_s = self._current_settings.get("timeout", 5)

        self._bench_btn.setEnabled(False)
        self._bench_btn.setText(t("testing"))
        self._bench_output.clear()

        from translator import DEFAULT_PROMPT, LANGUAGE_DISPLAY

        src = LANGUAGE_DISPLAY.get(source_lang, source_lang)
        tgt = LANGUAGE_DISPLAY.get(target_lang, target_lang)
        prompt = self._current_settings.get("system_prompt", DEFAULT_PROMPT)
        try:
            prompt = prompt.format(source_lang=src, target_lang=tgt, context="")
        except (KeyError, IndexError):
            pass

        run_benchmark(
            models, source_lang, target_lang, timeout_s, prompt, self._bench_result.emit
        )

    def _on_bench_result(self, text: str):
        if text == "__DONE__":
            self._bench_btn.setEnabled(True)
            self._bench_btn.setText(t("btn_test_all"))
        else:
            self._bench_output.append(text)

    # ── Shared logic ──

    def _on_silence_mode_changed(self, index):
        self._silence_duration.setEnabled(index == 1)

    def _on_vad_mode_changed(self, index):
        modes = ["silero", "energy", "disabled"]
        self._current_settings["vad_mode"] = modes[index]

    def _on_threshold_changed(self, value):
        val = value / 100.0
        self._current_settings["vad_threshold"] = val
        self._vad_threshold_label.setText(f"{value}%")
        if not self._vad_threshold_slider.isSliderDown():
            self._auto_save()

    def _on_energy_changed(self, value):
        val = value / 1000.0
        self._current_settings["energy_threshold"] = val
        self._energy_label.setText(f"{value}\u2030")
        if not self._energy_slider.isSliderDown():
            self._auto_save()

    def _on_timing_changed(self):
        self._current_settings["min_speech_duration"] = round(self._min_speech.value(), 2)
        self._current_settings["max_speech_duration"] = round(self._max_speech.value(), 2)
        self._current_settings["silence_mode"] = (
            "auto" if self._silence_mode.currentIndex() == 0 else "fixed"
        )
        self._current_settings["silence_duration"] = round(self._silence_duration.value(), 2)
        self._current_settings["incremental_asr"] = self._incremental_asr_cb.isChecked()
        self._current_settings["interim_interval"] = round(self._interim_interval_spin.value(), 2)

    def _on_ui_lang_changed(self, index):
        lang = "en" if index == 0 else "zh"
        self._current_settings["ui_lang"] = lang
        _save_settings(self._current_settings)
        from i18n import set_lang

        set_lang(lang)
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.information(
            self,
            "LiveTranslate",
            "Language changed. Please restart the application.\n"
            "语言已更改，请重启应用程序。",
        )

    def _auto_save(self):
        self._save_timer.start()

    def _do_auto_save(self):
        self._apply_settings()
        _save_settings(self._current_settings)
        self._refresh_home_status()

    def _on_prompt_preset_changed(self, index):
        from translator import DEFAULT_PROMPT, PROMPT_PRESETS
        key = self._prompt_preset.itemData(index)
        if key == "custom":
            return
        prompt = PROMPT_PRESETS.get(key, DEFAULT_PROMPT)
        self._current_settings["translation_mode"] = key
        self._prompt_edit.setPlainText(prompt)
        self._apply_prompt()

    def _on_hotkey_changed(self):
        if not hasattr(self, "_hotkey_enabled"):
            return
        self._current_settings["hotkey"] = {
            "enabled": self._hotkey_enabled.isChecked(),
            "shortcut": self._hotkey_shortcut.text().strip() or "Ctrl+Shift+T",
        }

    def _on_local_api_changed(self):
        if not hasattr(self, "_local_api_token"):
            return
        current = dict(self._current_settings.get("local_api", {}))
        current.setdefault("host", "127.0.0.1")
        current.setdefault("port", 17891)
        current["token"] = self._local_api_token.text().strip()
        self._current_settings["local_api"] = current

    def _start_manual_translate(self):
        text = self._manual_input.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "LiveTranslate", t("manual_empty"))
            return
        self._manual_translate_btn.setEnabled(False)
        self._manual_output.setPlainText(t("manual_translating"))
        mode = self._manual_mode.currentData() or "natural"
        threading.Thread(
            target=self._manual_translate_worker,
            args=(text, mode),
            daemon=True,
        ).start()

    def _manual_translate_worker(self, text: str, mode: str):
        try:
            service = SelectionTranslateService(
                get_model_config=self.get_active_model,
                get_target_language=lambda: self._current_settings.get(
                    "target_language", "zh"
                ),
                get_timeout=lambda: self._current_settings.get("timeout", 10),
                history_store=self._history_store,
                get_history_enabled=lambda: self._current_settings.get(
                    "privacy", {}
                ).get("history_enabled", True),
            )
            result = service.translate(text, source="manual", mode=mode)
        except Exception as e:
            self._manual_translate_error.emit(str(e))
            return
        self._manual_translate_result.emit(result)

    def _on_manual_translate_result(self, result: dict):
        self._manual_translate_btn.setEnabled(True)
        self._manual_last_result = result or {}
        self._manual_output.setPlainText(self._format_manual_result(self._manual_last_result))
        self._refresh_history()

    def _on_manual_translate_error(self, message: str):
        self._manual_translate_btn.setEnabled(True)
        self._manual_output.setPlainText(message)

    def _copy_manual_translation(self):
        text = self._manual_last_result.get("translation") or ""
        if text:
            QApplication.clipboard().setText(text)

    @staticmethod
    def _format_manual_result(result: dict) -> str:
        sections = [
            (t("history_original"), result.get("original")),
            (t("history_translation"), result.get("translation")),
            (t("history_explanation"), result.get("explanation")),
            (t("history_polished"), result.get("polished")),
        ]
        return "\n\n".join(f"{label}:\n{value}" for label, value in sections if value)

    def _apply_prompt(self):
        text = self._prompt_edit.toPlainText().strip()
        if text:
            self._current_settings["system_prompt"] = text
            active = self.get_active_model()
            if active:
                self.model_changed.emit(active)
            _save_settings(self._current_settings)
            log.info("System prompt updated")
            # Update preset combo to reflect current state
            from translator import PROMPT_PRESETS
            self._prompt_preset.blockSignals(True)
            preset_keys = [
                "literal",
                "natural",
                "explain",
                "polish",
                "daily",
                "esports",
                "anime",
            ]
            matched = len(preset_keys)  # custom
            for i, key in enumerate(preset_keys):
                if text.strip() == PROMPT_PRESETS[key].strip():
                    matched = i
                    self._current_settings["translation_mode"] = key
                    break
            self._prompt_preset.setCurrentIndex(matched)
            self._prompt_preset.blockSignals(False)

    def _apply_glossary(self):
        glossary = _text_to_glossary(self._glossary_edit.toPlainText())
        self._current_settings["glossary"] = glossary
        _save_settings(self._current_settings)
        # Push to the running pipeline via the standard settings channel.
        self.settings_changed.emit({"glossary": glossary})
        log.info("Glossary updated: %d term(s)", len(glossary))

    def _test_model_connection(self):
        row = self._model_list.currentRow()
        models = self._current_settings.get("models", [])
        if row < 0 or row >= len(models):
            # Fall back to the active model if nothing is selected.
            active = self.get_active_model()
            if not active:
                self._test_conn_label.setText(t("test_conn_no_model"))
                return
            model_config = active
        else:
            model_config = models[row]
        timeout = self._current_settings.get("timeout", 10)
        self._test_conn_btn.setEnabled(False)
        self._test_conn_label.setText(t("test_conn_running"))
        threading.Thread(
            target=self._test_connection_worker,
            args=(dict(model_config), timeout),
            daemon=True,
        ).start()

    def _test_connection_worker(self, model_config: dict, timeout: int):
        try:
            from translator import test_connection

            result = test_connection(model_config, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - defensive, worker thread
            result = {"ok": False, "message": str(e), "detail": ""}
        self._test_connection_result.emit(result)

    def _on_test_connection_result(self, result: dict):
        self._test_conn_btn.setEnabled(True)
        if result.get("ok"):
            detail = result.get("detail", "")
            text = f"✅ {result.get('message', '')}"
            if detail:
                text += f"  ({detail})"
            self._test_conn_label.setStyleSheet(status_text_stylesheet("success"))
        else:
            text = f"❌ {result.get('message', '')}"
            self._test_conn_label.setStyleSheet(status_text_stylesheet("danger"))
        self._test_conn_label.setText(text)

    def _apply_settings(self):
        self._current_settings["asr_language"] = self._get_asr_lang_code()
        engine_map = {
            0: "whisper",
            1: "sensevoice",
            2: "funasr-nano",
            3: "funasr-mlt-nano",
            4: "anime-whisper",
            5: "openai-audio",
            6: "assemblyai",
            7: "assemblyai-streaming",
        }
        self._current_settings["asr_engine"] = engine_map[
            self._asr_engine.currentIndex()
        ]
        if hasattr(self, "_asr_api_base"):
            engine_type = self._current_settings["asr_engine"]
            default_base = (
                "wss://streaming.assemblyai.com/v3/ws"
                if engine_type == "assemblyai-streaming"
                else (
                    "https://api.assemblyai.com"
                    if engine_type == "assemblyai"
                    else "https://api.openai.com/v1"
                )
            )
            default_model = (
                "u3-rt-pro"
                if engine_type == "assemblyai-streaming"
                else ("universal" if engine_type == "assemblyai" else "whisper-1")
            )
            self._current_settings["asr_api"] = {
                "api_base": self._asr_api_base.text().strip()
                or default_base,
                "api_key": self._asr_api_key.text().strip(),
                "model": self._asr_api_model.text().strip() or default_model,
                "timeout": self._asr_api_timeout.value(),
            }
        self._current_settings["whisper_model_size"] = (
            self._whisper_size_combo.currentText()
        )
        dev_text = self._asr_device.currentText()
        self._current_settings["asr_device"] = dev_text.split(" (")[0]
        audio_idx = self._audio_device.currentIndex()
        if audio_idx == 0:
            self._current_settings["audio_device"] = "__disabled__"
        elif audio_idx == 1:
            self._current_settings["audio_device"] = None
        else:
            self._current_settings["audio_device"] = self._audio_device.currentText()
        mic_idx = self._mic_device.currentIndex()
        if mic_idx == 0:
            self._current_settings["mic_device"] = None
        elif mic_idx == 1:
            self._current_settings["mic_device"] = "__default__"
        else:
            self._current_settings["mic_device"] = self._mic_device.currentText()
        self._current_settings["hub"] = (
            "ms" if self._hub_combo.currentIndex() == 0 else "hf"
        )
        prompt_text = self._prompt_edit.toPlainText().strip()
        if prompt_text:
            self._current_settings["system_prompt"] = prompt_text
        self._current_settings["timeout"] = self._timeout_spin.value()
        if hasattr(self, "_local_api_token"):
            self._on_local_api_changed()
        if hasattr(self, "_hotkey_enabled"):
            self._current_settings["hotkey"] = {
                "enabled": self._hotkey_enabled.isChecked(),
                "shortcut": self._hotkey_shortcut.text().strip() or "Ctrl+Shift+T",
            }
        if hasattr(self, "_history_enabled"):
            self._current_settings["privacy"] = {
                "history_enabled": self._history_enabled.isChecked()
            }
        if hasattr(self, "_auto_save_transcript_cb"):
            self._current_settings["auto_save_transcript"] = (
                self._auto_save_transcript_cb.isChecked()
            )
        if hasattr(self, "_style_preset"):
            self._current_settings["style"] = self._collect_style()
        if hasattr(self, "_tts_enabled_cb"):
            self._current_settings["tts"] = self._collect_tts()
        safe = redact_secret_data({
            k: v
            for k, v in self._current_settings.items()
            if k not in ("models", "system_prompt")
        })
        log.info(f"Settings applied: {safe}")
        self.settings_changed.emit(dict(self._current_settings))

    def get_settings(self):
        return dict(self._current_settings)

    def get_active_model(self) -> dict | None:
        models = self._current_settings.get("models", [])
        idx = self._current_settings.get("active_model", 0)
        if 0 <= idx < len(models):
            return models[idx]
        return None

    def has_saved_settings(self) -> bool:
        return SETTINGS_FILE.exists()
