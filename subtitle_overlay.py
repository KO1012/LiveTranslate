import ctypes
import os
import re

import psutil
from exporters import SubtitleExportItem, export_subtitles
from i18n import t, LANGUAGES
from PyQt6.QtCore import QPoint, QRect, QRectF, QPropertyAnimation, QEasingCurve, QSize, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QBrush, QCursor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from theme import Color, primary_button_stylesheet, ui_font_family
from runtime_paths import resource_path
from subtitle_presets import (
    DEFAULT_STYLE,
    STYLE_PRESETS,
    hex_to_rgba as _hex_to_rgba,
)

# Re-exported for backward compatibility: external code historically did
# `from subtitle_overlay import DEFAULT_STYLE / STYLE_PRESETS`. The canonical
# home is now subtitle_presets.py.
__all__ = ["SubtitleOverlay", "ChatMessage", "DEFAULT_STYLE", "STYLE_PRESETS"]

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x20


# UI font resolution lives in theme.ui_font_family(); module-level alias keeps
# existing call sites (self._ui_font = _ui_font_family()) working unchanged.
_ui_font_family = ui_font_family


def _overlay_icon(name: str):
    """Load a paper-collage overlay control icon (PNG) as a QIcon.

    Icons live under assets/paper_collage/overlay/icons/. Returns an empty
    QIcon if the file is missing so callers can fall back to a text glyph.
    """
    from PyQt6.QtGui import QIcon

    p = resource_path("assets", "paper_collage", "overlay", "icons", f"{name}.png")
    if p.exists():
        return QIcon(str(p))
    return QIcon()


def _line_icon(kind: str, color: str, size: int = 64) -> QIcon:
    """Small font-independent line icons for overlay chrome."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color)
    pen = QPen(c, max(4, size // 14), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    s = float(size)

    if kind == "pin":
        painter.drawLine(int(s * 0.5), int(s * 0.12), int(s * 0.5), int(s * 0.78))
        painter.drawLine(int(s * 0.27), int(s * 0.34), int(s * 0.73), int(s * 0.34))
        painter.drawLine(int(s * 0.38), int(s * 0.2), int(s * 0.62), int(s * 0.2))
        painter.drawLine(int(s * 0.5), int(s * 0.78), int(s * 0.42), int(s * 0.92))
    elif kind == "close":
        painter.drawLine(int(s * 0.32), int(s * 0.32), int(s * 0.68), int(s * 0.68))
        painter.drawLine(int(s * 0.68), int(s * 0.32), int(s * 0.32), int(s * 0.68))
    elif kind == "play":
        painter.setBrush(QBrush(c))
        points = QPolygonF()
        from PyQt6.QtCore import QPointF
        points.append(QPointF(s * 0.38, s * 0.27))
        points.append(QPointF(s * 0.38, s * 0.73))
        points.append(QPointF(s * 0.72, s * 0.5))
        painter.drawPolygon(points)
    elif kind == "pause":
        painter.drawLine(int(s * 0.36), int(s * 0.28), int(s * 0.36), int(s * 0.72))
        painter.drawLine(int(s * 0.64), int(s * 0.28), int(s * 0.64), int(s * 0.72))
    elif kind == "stop":
        painter.setBrush(QBrush(c))
        painter.drawRoundedRect(QRectF(s * 0.34, s * 0.34, s * 0.32, s * 0.32), 3, 3)
    elif kind == "menu":
        painter.drawLine(int(s * 0.3), int(s * 0.36), int(s * 0.7), int(s * 0.36))
        painter.drawLine(int(s * 0.3), int(s * 0.5), int(s * 0.7), int(s * 0.5))
        painter.drawLine(int(s * 0.3), int(s * 0.64), int(s * 0.7), int(s * 0.64))
    elif kind == "gear":
        painter.drawEllipse(QRectF(s * 0.35, s * 0.35, s * 0.3, s * 0.3))
        for x1, y1, x2, y2 in [
            (0.5, 0.18, 0.5, 0.28),
            (0.5, 0.72, 0.5, 0.82),
            (0.18, 0.5, 0.28, 0.5),
            (0.72, 0.5, 0.82, 0.5),
            (0.28, 0.28, 0.35, 0.35),
            (0.72, 0.28, 0.65, 0.35),
            (0.28, 0.72, 0.35, 0.65),
            (0.72, 0.72, 0.65, 0.65),
        ]:
            painter.drawLine(int(s * x1), int(s * y1), int(s * x2), int(s * y2))
    elif kind == "panel":
        painter.drawRoundedRect(QRectF(s * 0.24, s * 0.3, s * 0.52, s * 0.4), 3, 3)
        painter.drawLine(int(s * 0.24), int(s * 0.43), int(s * 0.76), int(s * 0.43))
        painter.drawLine(int(s * 0.42), int(s * 0.43), int(s * 0.42), int(s * 0.7))

    painter.end()
    return QIcon(pix)


class ChatMessage(QWidget):
    """Single chat message widget with original + async translation."""

    _current_style = DEFAULT_STYLE
    _compact_mode = False
    # Target language code, kept in sync by SubtitleOverlay.set_target_language
    # so the translation chip can show "译文（中文）" like the paper-collage mock.
    _target_language = "zh"

    def __init__(
        self,
        msg_id: int,
        timestamp: str,
        original: str,
        source_lang: str,
        asr_ms: float,
        parent=None,
    ):
        super().__init__(parent)
        self.msg_id = msg_id
        self._original = original
        self._translated = ""
        self._streaming_visible_text = ""
        self._streaming_target_text = ""
        self._timestamp = timestamp
        self._source_lang = source_lang
        self._asr_ms = asr_ms
        self._translate_ms = 0.0
        self._start_seconds = None
        self._end_seconds = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(22, 4, 22, 6)
        self._layout.setSpacing(4)
        self.setObjectName("subtitleMessage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        s = self._current_style

        # ── Original block: "原文" chip + meta, then the source text ──
        self._orig_head = QHBoxLayout()
        self._orig_head.setContentsMargins(0, 0, 0, 0)
        self._orig_head.setSpacing(6)
        self._orig_chip = QLabel()
        self._orig_chip.setTextFormat(Qt.TextFormat.PlainText)
        self._orig_head.addWidget(self._orig_chip, 0, Qt.AlignmentFlag.AlignLeft)
        self._orig_meta = QLabel()
        self._orig_meta.setTextFormat(Qt.TextFormat.RichText)
        self._orig_meta.setStyleSheet("background: transparent;")
        self._orig_head.addWidget(self._orig_meta, 0, Qt.AlignmentFlag.AlignLeft)
        self._orig_head.addStretch(1)
        self._layout.addLayout(self._orig_head)

        self._header_label = QLabel()
        self._header_label.setFont(
            QFont(s["original_font_family"], s["original_font_size"])
        )
        self._header_label.setTextFormat(Qt.TextFormat.RichText)
        self._header_label.setWordWrap(True)
        self._header_label.setStyleSheet("background: transparent;")
        self._header_label.setMargin(0)
        self._layout.addWidget(self._header_label)

        # ── Torn-paper divider with a small plant on the right ──
        self._divider = _TornDivider()
        self._layout.addWidget(self._divider)

        # ── Translation block: "译文" chip, then the translated text ──
        self._trans_chip = QLabel()
        self._trans_chip.setTextFormat(Qt.TextFormat.PlainText)
        self._layout.addWidget(self._trans_chip, 0, Qt.AlignmentFlag.AlignLeft)

        self._trans_label = QLabel(
            f'<span style="color:#999; font-style:italic;">{t("translating")}</span>'
        )
        self._trans_label.setFont(
            QFont(s["translation_font_family"], s["translation_font_size"])
        )
        self._trans_label.setTextFormat(Qt.TextFormat.RichText)
        self._trans_label.setWordWrap(True)
        self._trans_label.setStyleSheet("background: transparent;")
        self._trans_label.setMargin(0)
        self._layout.addWidget(self._trans_label)

        self._compact_source_label = QLabel()
        self._compact_source_label.setTextFormat(Qt.TextFormat.RichText)
        self._compact_source_label.setWordWrap(True)
        self._compact_source_label.setStyleSheet("background: transparent;")
        self._compact_source_label.setMargin(0)
        self._layout.addWidget(self._compact_source_label)

        self._apply_chrome(s)
        self._header_label.setText(self._build_original_html(s))

    def _lang_display(self) -> str:
        """Human-friendly source language name for the original chip."""
        code = (self._source_lang or "").lower()
        if not code or code == "auto":
            return t("asr_lang_auto")
        for c, native in LANGUAGES:
            if c == code:
                return native
        return self._source_lang

    def _target_display(self) -> str:
        """Human-friendly target language name for the translation chip."""
        code = (self._target_language or "").lower()
        for c, native in LANGUAGES:
            if c == code and native:
                return native
        return code or ""

    def _apply_chrome(self, s: dict):
        """Style the chips + divider from the current style dict."""
        accent = s.get("accent_color", "#d97757")
        compact = self._compact_mode
        window_pct = _window_opacity_pct(s)
        self._orig_chip.setText(f"{t('subtitle_chip_original')}（{self._lang_display()}）")
        self._orig_chip.setStyleSheet(_chip_stylesheet(accent, "source", window_pct))
        tgt = self._target_display()
        if tgt:
            self._trans_chip.setText(f"{t('subtitle_chip_translation')}（{tgt}）")
        else:
            self._trans_chip.setText(t("subtitle_chip_translation"))
        self._trans_chip.setStyleSheet(_chip_stylesheet(accent, "translation", window_pct))
        self._divider.set_accent(accent)
        self._divider.set_opacity_percent(window_pct)
        self.setStyleSheet(_message_stylesheet(s, compact))
        if compact:
            self._layout.setContentsMargins(18, 12, 18, 14)
            self._layout.setSpacing(10)
        else:
            self._layout.setContentsMargins(22, 4, 22, 6)
            self._layout.setSpacing(4)
        self._header_label.setFont(
            QFont(s["original_font_family"], max(12, s["original_font_size"] + (1 if compact else 3)))
        )
        self._trans_label.setFont(
            QFont(s["translation_font_family"], max(22, s["translation_font_size"] + (8 if compact else 3)))
        )
        self._compact_source_label.setFont(QFont(s["original_font_family"], max(10, s["original_font_size"])))
        self._orig_meta.setText(self._build_meta_html(s))
        self._orig_chip.setVisible(not compact)
        self._trans_chip.setVisible(not compact)
        self._divider.setVisible(not compact)
        self._orig_meta.setVisible(not compact)
        self._header_label.setVisible(True)
        self._trans_label.setVisible(True)
        self._compact_source_label.setVisible(False)
        self._trans_label.setMinimumHeight(78 if compact else 0)
        self._trans_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding if compact else QSizePolicy.Policy.Preferred,
        )

    def _build_original_html(self, s):
        return f'<span style="color:{s["original_color"]};">{_escape(self._original)}</span>'

    def _build_meta_html(self, s):
        window_pct = _window_opacity_pct(s)
        time_color = _css_color_with_opacity(s["timestamp_color"], window_pct)
        asr_color = _css_color_with_opacity("#88bb88", window_pct)
        return (
            f'<span style="color:{time_color};">[{self._timestamp}]</span> '
            f'<span style="color:{asr_color}; font-size:9pt;">ASR {self._asr_ms:.0f}ms</span>'
        )

    def _build_compact_source_html(self, s):
        tl = f" · TL {self._translate_ms:.0f}ms" if self._translate_ms else ""
        meta = f"{self._lang_display()} · ASR {self._asr_ms:.0f}ms{tl}"
        return (
            f'<span style="color:{s["timestamp_color"]}; font-size:9pt;">{_escape(meta)}</span>'
            f'<span style="color:{s["original_color"]}; font-size:10pt;">  {_escape(self._original)}</span>'
        )

    # Back-compat: some call sites / tests may still reference this.
    def _build_header_html(self, s):
        return self._build_original_html(s)

    def update_streaming(self, partial_text: str):
        """Update translation label with the provider's current accumulated text."""
        if partial_text is None:
            return
        target = str(partial_text)
        if not target:
            return
        if hasattr(self, "_streaming_timer"):
            self._streaming_timer.stop()
        self._streaming_target_text = target
        self._streaming_visible_text = target
        self._set_translation_text(target)

    def _flush_streaming(self):
        target = getattr(self, "_streaming_target_text", "")
        if not target:
            return
        if hasattr(self, "_streaming_timer"):
            self._streaming_timer.stop()
        self._streaming_visible_text = target
        self._set_translation_text(target)

    def _set_translation_text(self, text: str):
        s = self._current_style
        self._trans_label.setText(
            f'<span style="color:{s["translation_color"]};">{_escape(text)}</span>'
        )

    def set_original(self, original: str, source_lang: str | None = None):
        self._original = original or ""
        if source_lang:
            self._source_lang = source_lang
        s = self._current_style
        self._orig_chip.setText(f"{t('subtitle_chip_original')} ({self._lang_display()})")
        self._header_label.setText(self._build_original_html(s))
        self._compact_source_label.setText(self._build_compact_source_html(s))

    def set_translation(self, translated: str, translate_ms: float):
        self._translated = translated or ""
        self._translate_ms = translate_ms
        if self._end_seconds is None:
            import time
            self._end_seconds = time.monotonic()
        # Stop streaming throttle if active
        if hasattr(self, "_streaming_timer"):
            self._streaming_timer.stop()
        if not (translated and self._compact_mode):
            self._streaming_target_text = ""
            self._streaming_visible_text = ""
        s = self._current_style
        self._compact_source_label.setText(self._build_compact_source_html(s))
        if translated:
            if self._compact_mode:
                self.update_streaming(translated)
            else:
                self._trans_label.setText(
                    f'<span style="color:{s["translation_color"]};">{_escape(translated)}</span>'
                )
        else:
            self._trans_label.setText(
                f'<span style="color:#aaa; font-style:italic;">{t("same_language")}</span>'
            )

    def apply_style(self, s: dict):
        self._apply_chrome(s)
        self._orig_meta.setText(self._build_meta_html(s))
        self._header_label.setText(self._build_original_html(s))
        self._compact_source_label.setText(self._build_compact_source_html(s))
        if self._translated:
            self._trans_label.setText(
                f'<span style="color:{s["translation_color"]};">{_escape(self._translated)}</span>'
            )

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_CSS)
        copy_orig = menu.addAction(t("copy_original"))
        copy_trans = menu.addAction(t("copy_translation"))
        copy_all = menu.addAction(t("copy_all"))
        menu.addSeparator()
        export_menu = menu.addMenu(t("export_menu"))
        export_orig = export_menu.addAction(t("export_original"))
        export_trans = export_menu.addAction(t("export_translation"))
        export_both = export_menu.addAction(t("export_all"))
        menu.addSeparator()
        clear_list = menu.addAction(t("clear_list"))
        action = menu.exec(event.globalPos())
        if action == copy_orig:
            QApplication.clipboard().setText(self._original)
        elif action == copy_trans:
            QApplication.clipboard().setText(self._translated)
        elif action == copy_all:
            QApplication.clipboard().setText(f"{self._original}\n{self._translated}")
        elif action == clear_list:
            overlay = self.window()
            if hasattr(overlay, '_on_clear'):
                overlay._on_clear()
        elif action in (export_orig, export_trans, export_both):
            mode = {export_orig: "original", export_trans: "translation", export_both: "both"}[action]
            overlay = self.window()
            if hasattr(overlay, "export_messages"):
                overlay.export_messages(mode, parent=self)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _window_opacity_pct(style: dict) -> int:
    return max(0, min(100, int(style.get("window_opacity", DEFAULT_STYLE["window_opacity"]))))


def _scaled_alpha(alpha: int, opacity_pct: int, min_alpha: int = 0) -> int:
    return max(min_alpha, min(255, round(int(alpha) * opacity_pct / 100)))


def _css_color_with_opacity(color: str, opacity_pct: int, alpha: int = 255) -> str:
    c = QColor(color)
    if not c.isValid():
        c = QColor("#ffffff")
    return f"rgba({c.red()},{c.green()},{c.blue()},{_scaled_alpha(alpha, opacity_pct)})"


def _chrome_opacity_pct(style_or_pct) -> int:
    """Opacity used by regular overlay chrome."""
    if isinstance(style_or_pct, dict):
        pct = _window_opacity_pct(style_or_pct)
    else:
        pct = max(0, min(100, int(style_or_pct)))
    return pct


def _qcolor_from_css_rgba(rgba: str) -> QColor:
    match = re.fullmatch(r"rgba\((\d+),(\d+),(\d+),(\d+)\)", rgba.replace(" ", ""))
    if match:
        return QColor(*(int(part) for part in match.groups()))
    return QColor(rgba)


def _chip_stylesheet(accent: str, variant: str = "source", opacity_pct: int = 100) -> str:
    """Rounded 'paper tag' chip used to label the original / translation text.

    Uses the supplied chip_paper.png texture as a paper background (via
    border-image). The label text is a dark warm ink (matching the paper-collage
    mock) so it reads clearly on the cream paper; falls back to a
    semi-transparent accent fill if the texture is missing.
    """
    from PyQt6.QtGui import QColor

    if variant == "translation":
        c = QColor("#d8c8ef")
        ink = _css_color_with_opacity("#57436f", opacity_pct)
        alpha = 232
        stroke_alpha = 155
    else:
        c = QColor("#d8c29a")
        ink = _css_color_with_opacity("#5a5347", opacity_pct)
        alpha = 222
        stroke_alpha = 135
    fill = f"rgba({c.red()},{c.green()},{c.blue()},{_scaled_alpha(alpha, opacity_pct)})"
    stroke = f"rgba({c.red()},{c.green()},{c.blue()},{_scaled_alpha(stroke_alpha, opacity_pct)})"
    return (
        "QLabel {"
        f"  background: {fill}; color: {ink}; border: 1px solid {stroke};"
        "  border-radius: 7px; padding: 3px 12px;"
        "  font-size: 9pt; font-weight: 700;"
        "}"
    )


def _message_stylesheet(s: dict, compact: bool) -> str:
    """Subtle subtitle card styling used by each message row."""
    window_pct = _window_opacity_pct(s)
    if compact:
        bg_alpha = _scaled_alpha(236, window_pct)
        border_alpha = _scaled_alpha(70, window_pct)
        return (
            "QWidget#subtitleMessage {"
            f"  background: rgba(28,28,28,{bg_alpha});"
            f"  border: 1px solid rgba(255,255,255,{border_alpha});"
            "  border-radius: 16px;"
            "}"
        )

    bg_alpha = _scaled_alpha(min(210, max(80, s["header_opacity"] - 35)), window_pct)
    bg = _hex_to_rgba(s["header_color"], bg_alpha)
    border_alpha = _scaled_alpha(28, window_pct)
    return (
        "QWidget#subtitleMessage {"
        f"  background: {bg};"
        f"  border: 1px solid rgba(255,255,255,{border_alpha});"
        "  border-radius: 10px;"
        "}"
    )


class _TornDivider(QWidget):
    """Paper divider between original and translation text."""

    _strip_pix = None
    _strip_loaded = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accent = "#d97757"
        self._opacity_pct = 100
        self.setFixedHeight(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet("background: transparent;")
        if not _TornDivider._strip_loaded:
            _TornDivider._strip_loaded = True
            from PyQt6.QtGui import QPixmap

            p = resource_path(
                "assets", "paper_collage", "overlay", "textures", "torn_strip.png"
            )
            if p.exists():
                pix = QPixmap(str(p))
                if not pix.isNull():
                    _TornDivider._strip_pix = pix

    def set_accent(self, accent: str):
        self._accent = accent
        self.update()

    def set_opacity_percent(self, pct: int):
        self._opacity_pct = max(0, min(100, int(pct)))
        self.update()

    def paintEvent(self, event):
        from PyQt6.QtGui import QColor, QPainter, QPen

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w = self.width()
        pix = _TornDivider._strip_pix
        if pix is not None and not pix.isNull():
            band_h = 9
            band_y = 15
            src_y = max(0, int(pix.height() * 0.46))
            src_h = max(1, int(pix.height() * 0.46))
            source_rect = QRect(0, src_y, pix.width(), min(src_h, pix.height() - src_y))
            target_rect = QRect(0, band_y, w, band_h)
            # The torn-paper texture is a near-opaque cream strip; drawn at full
            # width and full opacity it reads as a glaring white bar that blocks
            # the view. Draw it softly so it's just a subtle paper seam.
            painter.setOpacity(0.78 * self._opacity_pct / 100)
            painter.drawPixmap(target_rect, pix, source_rect)
            painter.setOpacity(1.0)

            self._paint_ornament(painter, w)
            painter.end()
            return

        c = QColor(self._accent)
        c.setAlpha(_scaled_alpha(110, self._opacity_pct))
        painter.setPen(QPen(c, 1.2))
        painter.drawLine(0, 13, w, 13)
        self._paint_ornament(painter, w)
        painter.end()

    def _paint_ornament(self, painter, width: int):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor, QPen

        if self._opacity_pct <= 0:
            return
        ox = max(0, width - 76)

        note = QPolygonF([
            QPointF(ox + 15, 24),
            QPointF(ox + 48, 20),
            QPointF(ox + 55, 39),
            QPointF(ox + 21, 44),
        ])
        painter.setOpacity(0.96 * self._opacity_pct / 100)
        painter.setPen(QPen(QColor(226, 209, 179, 170), 1.0))
        painter.setBrush(QBrush(QColor(214, 193, 156, 238)))
        painter.drawPolygon(note)
        painter.setPen(QPen(QColor(98, 81, 60, 150), 1.0))
        painter.drawLine(QPointF(ox + 27, 31), QPointF(ox + 42, 29))
        painter.drawLine(QPointF(ox + 30, 36), QPointF(ox + 47, 34))

        stem = QPainterPath(QPointF(ox + 47, 32))
        stem.cubicTo(ox + 44, 26, ox + 49, 16, ox + 55, 5)
        painter.setPen(QPen(QColor(78, 98, 70, 218), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(stem)

        leaf_color = QColor(111, 132, 90, 224)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(leaf_color))
        for cx, cy, angle, sx, sy in [
            (48, 24, -38, 8, 14),
            (57, 20, 34, 7, 13),
            (49, 14, -35, 7, 12),
            (59, 11, 38, 6, 11),
            (56, 5, 15, 6, 10),
        ]:
            painter.save()
            painter.translate(ox + cx, cy)
            painter.rotate(angle)
            painter.drawEllipse(QPointF(0, 0), sx / 2, sy / 2)
            painter.restore()
        painter.setOpacity(1.0)


# Flat "ghost" toolbar button: no border, transparent until hovered. This
# replaces the old bordered-pill look that made the title bar read as a row of
# chunky boxes. Only the primary action (start/stop) and quit get accent color.
# Colors follow the warm "paper/clay" palette (see theme.Color).
_BTN_CSS = f"""
    QPushButton {{
        background: transparent;
        border: none;
        border-radius: 7px;
        color: {Color.DARK_TEXT_MUTED};
        font-size: 11px;
        font-weight: 600;
        padding: 0 11px;
    }}
    QPushButton:hover {{
        background: rgba(255,255,255,24);
        color: {Color.DARK_TEXT};
    }}
    QPushButton:pressed {{
        background: rgba(255,255,255,12);
    }}
"""

_DANGER_BTN_CSS = """
    QPushButton {
        background: transparent;
        border: none;
        border-radius: 7px;
        color: #d99080;
        font-size: 11px;
        font-weight: 600;
        padding: 0 11px;
    }
    QPushButton:hover {
        background: rgba(176,75,56,150);
        color: #ffffff;
    }
    QPushButton:pressed {
        background: rgba(176,75,56,95);
    }
"""

_ACTIVE_BTN_CSS = f"""
    QPushButton {{
        background: {Color.PRIMARY};
        border: none;
        border-radius: 7px;
        color: #ffffff;
        font-size: 11px;
        font-weight: 700;
        padding: 0 11px;
    }}
    QPushButton:hover {{
        background: {Color.ACCENT};
    }}
    QPushButton:pressed {{
        background: {Color.PRIMARY_PRESSED};
    }}
"""

_PAUSED_BTN_CSS = """
    QPushButton {
        background: rgba(176,122,60,45);
        border: none;
        border-radius: 7px;
        color: #e0bd8e;
        font-size: 11px;
        font-weight: 700;
        padding: 0 11px;
    }
    QPushButton:hover {
        background: rgba(176,122,60,105);
        color: #ffffff;
    }
"""

# Square icon button (pin / close / Aa / gear) for the slim chrome.
_ICON_BTN_CSS = """
    QPushButton {
        background: rgba(255,255,255,12);
        border: 1px solid rgba(238,226,204,42);
        border-radius: 6px;
        color: #e4d9c8;
        font-size: 11px;
    }
    QPushButton:hover {
        background: rgba(255,255,255,26);
        border-color: rgba(238,226,204,74);
        color: #fff7ec;
    }
    QPushButton:pressed { background: rgba(255,255,255,18); }
"""

_ICON_BTN_ACTIVE_CSS = """
    QPushButton {
        background: rgba(255,255,255,18);
        border: 1px solid rgba(238,226,204,64);
        border-radius: 6px;
        color: #ffffff;
        font-size: 11px;
    }
    QPushButton:hover { background: rgba(255,255,255,30); }
"""


def _icon_btn_css(active: bool = False, opacity_pct: int = 100) -> str:
    bg = _scaled_alpha(18 if active else 12, opacity_pct)
    border = _scaled_alpha(64 if active else 42, opacity_pct)
    hover = _scaled_alpha(30 if active else 26, opacity_pct)
    pressed = _scaled_alpha(18, opacity_pct)
    hover_border = _scaled_alpha(74, opacity_pct)
    fg = _css_color_with_opacity("#e4d9c8", opacity_pct)
    hover_fg = _css_color_with_opacity("#fff7ec", opacity_pct)
    return f"""
    QPushButton {{
        background: rgba(255,255,255,{bg});
        border: 1px solid rgba(238,226,204,{border});
        border-radius: 6px;
        color: {fg};
        font-size: 11px;
    }}
    QPushButton:hover {{
        background: rgba(255,255,255,{hover});
        border-color: rgba(238,226,204,{hover_border});
        color: {hover_fg};
    }}
    QPushButton:pressed {{ background: rgba(255,255,255,{pressed}); }}
    QPushButton:disabled {{
        background: transparent;
        border-color: transparent;
        color: transparent;
    }}
"""


def _opacity_slider_css(opacity_pct: int = 100) -> str:
    pct = max(0, min(100, int(opacity_pct)))
    visible_pct = max(35, pct)
    groove = _scaled_alpha(78, visible_pct)
    sub = _scaled_alpha(255, visible_pct)
    handle = _scaled_alpha(255, visible_pct)
    return f"""
    QSlider::groove:horizontal {{
        height: 4px; border-radius: 2px;
        background: rgba(238,226,204,{groove});
    }}
    QSlider::sub-page:horizontal {{
        height: 4px; border-radius: 2px;
        background: rgba(215,194,157,{sub});
    }}
    QSlider::handle:horizontal {{
        width: 12px; height: 12px; margin: -4px 0; border-radius: 6px;
        background: rgba(234,217,187,{handle});
    }}
"""

_TITLE_ICON_BTN_CSS = """
    QPushButton {
        background: transparent;
        border: none;
        border-radius: 4px;
        color: #3d3328;
        font-size: 13px;
    }
    QPushButton:hover {
        background: rgba(80,62,42,24);
        color: #1f1a15;
    }
    QPushButton:pressed { background: rgba(80,62,42,36); }
"""

_TITLE_ICON_BTN_ACTIVE_CSS = """
    QPushButton {
        background: transparent;
        border: none;
        border-radius: 4px;
        color: #3d3328;
        font-size: 13px;
    }
    QPushButton:hover { background: rgba(80,62,42,24); }
"""

# Opacity slider for the bottom control bar.
_OPACITY_SLIDER_CSS = """
    QSlider::groove:horizontal {
        height: 4px; border-radius: 2px;
        background: rgba(238,226,204,78);
    }
    QSlider::sub-page:horizontal {
        height: 4px; border-radius: 2px;
        background: #d7c29d;
    }
    QSlider::handle:horizontal {
        width: 12px; height: 12px; margin: -4px 0; border-radius: 6px;
        background: #ead9bb;
    }
"""

_MENU_CSS = f"""
    QMenu {{
        background: {Color.DARK_PANEL};
        color: {Color.DARK_TEXT};
        border: 1px solid {Color.DARK_BORDER};
        border-radius: 10px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 8px 28px 8px 12px;
        border-radius: 7px;
    }}
    QMenu::item:selected {{
        background: {Color.PRIMARY};
        color: #ffffff;
    }}
    QMenu::separator {{
        height: 1px;
        background: {Color.DARK_BORDER};
        margin: 6px 4px;
    }}
"""

_BAR_CSS_TPL = f"""
    QProgressBar {{{{
        background: rgba(255,255,255,12);
        border: none;
        border-radius: 3px;
        text-align: center;
        font-size: 8pt;
        color: {Color.DARK_TEXT_MUTED};
    }}}}
    QProgressBar::chunk {{{{
        background: {{color}};
        border-radius: 2px;
    }}}}
"""


class _OverlayContainer(QWidget):
    """Rounded dark paper surface for the floating subtitle window."""

    _paper_pix = None
    _paper_loaded = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bg = QColor(48, 45, 37, 248)
        self._radius = 13
        self._surface_alpha = 248
        self.setObjectName("overlayContainer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setStyleSheet("background: transparent;")
        if not _OverlayContainer._paper_loaded:
            _OverlayContainer._paper_loaded = True
            p = resource_path(
                "assets", "paper_collage", "overlay", "textures", "paper_tile.png"
            )
            if p.exists():
                pix = QPixmap(str(p))
                if not pix.isNull():
                    _OverlayContainer._paper_pix = pix

    def set_surface(self, rgba: str, radius: int):
        c = _qcolor_from_css_rgba(rgba)
        if c.isValid():
            self._bg = c
            self._surface_alpha = c.alpha()
        self._radius = max(10, int(radius))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect().adjusted(0, 0, -1, -1))
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        painter.fillPath(path, self._bg)

        pix = _OverlayContainer._paper_pix
        if pix is not None and not pix.isNull():
            painter.save()
            painter.setClipPath(path)
            painter.setOpacity(0.075 * self._surface_alpha / 255)
            scaled = pix.scaled(
                self.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(0, 0, scaled)
            painter.restore()

        painter.setPen(QPen(QColor(238, 226, 204, _scaled_alpha(150, round(self._surface_alpha / 255 * 100))), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()


class MonitorBar(QWidget):
    """Compact system monitor displayed in the overlay."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        row1 = QHBoxLayout()
        row1.setSpacing(6)

        # MIC bar (hidden when mic is disabled)
        self._mic_lbl = QLabel("MIC")
        self._mic_lbl.setFixedWidth(26)
        self._mic_lbl.setFont(QFont("Consolas", 8))
        self._mic_lbl.setStyleSheet("color: #888; background: transparent;")
        self._mic_lbl.setVisible(False)
        row1.addWidget(self._mic_lbl)

        self._mic_bar = QProgressBar()
        self._mic_bar.setRange(0, 100)
        self._mic_bar.setFixedHeight(14)
        self._mic_bar.setTextVisible(True)
        self._mic_bar.setFormat("%v%")
        self._mic_bar.setStyleSheet(_BAR_CSS_TPL.format(color="#c586c0"))
        self._mic_bar.setVisible(False)
        row1.addWidget(self._mic_bar)

        rms_lbl = QLabel("RMS:")
        rms_lbl.setFixedWidth(26)
        rms_lbl.setFont(QFont("Consolas", 8))
        rms_lbl.setStyleSheet("color: #888; background: transparent;")
        row1.addWidget(rms_lbl)

        self._rms_bar = QProgressBar()
        self._rms_bar.setRange(0, 100)
        self._rms_bar.setFixedHeight(14)
        self._rms_bar.setTextVisible(True)
        self._rms_bar.setFormat("%v%")
        self._rms_bar.setStyleSheet(_BAR_CSS_TPL.format(color="#4ec9b0"))
        row1.addWidget(self._rms_bar)

        vad_lbl = QLabel("VAD:")
        vad_lbl.setFixedWidth(26)
        vad_lbl.setFont(QFont("Consolas", 8))
        vad_lbl.setStyleSheet("color: #888; background: transparent;")
        row1.addWidget(vad_lbl)

        self._vad_bar = QProgressBar()
        self._vad_bar.setRange(0, 100)
        self._vad_bar.setFixedHeight(14)
        self._vad_bar.setTextVisible(True)
        self._vad_bar.setFormat("%v%")
        self._vad_bar.setStyleSheet(_BAR_CSS_TPL.format(color="#dcdcaa"))
        row1.addWidget(self._vad_bar)

        layout.addLayout(row1)

        self._stats_label = QLabel()
        self._stats_label.setFont(QFont("Consolas", 8))
        self._stats_label.setStyleSheet("color: #888; background: transparent;")
        self._stats_label.setTextFormat(Qt.TextFormat.RichText)
        self._stats_label.setWordWrap(True)
        layout.addWidget(self._stats_label)

        self._proc = psutil.Process(os.getpid())
        self._proc.cpu_percent(interval=None)  # Prime the counter
        self._cpu = 0
        self._ram_mb = 0.0
        self._gpu_text = "N/A"
        self._asr_device = ""
        self._asr_count = 0
        self._tl_count = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cost = 0.0

        self._sys_timer = QTimer(self)
        self._sys_timer.timeout.connect(self._update_system)
        self._sys_timer.start(1000)
        self._update_system()
        self._refresh_stats()

    def update_audio(self, rms: float, vad: float, mic_rms=None):
        self._rms_bar.setValue(min(100, int(rms * 500)))
        self._vad_bar.setValue(min(100, int(vad * 100)))
        mic_active = mic_rms is not None
        if self._mic_lbl.isVisible() != mic_active:
            self._mic_lbl.setVisible(mic_active)
            self._mic_bar.setVisible(mic_active)
        if mic_active:
            self._mic_bar.setValue(min(100, int(mic_rms * 500)))

    def update_asr_device(self, device: str):
        self._asr_device = device
        self._refresh_stats()

    def update_pipeline_stats(
        self, asr_count, tl_count, prompt_tokens, completion_tokens, cost=0.0
    ):
        self._asr_count = asr_count
        self._tl_count = tl_count
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._cost = cost
        self._refresh_stats()

    def _update_system(self):
        try:
            self._cpu = int(self._proc.cpu_percent(interval=None) / os.cpu_count())
            self._ram_mb = self._proc.memory_info().rss / 1024 / 1024
        except Exception:
            pass
        try:
            import torch

            if torch.cuda.is_available():
                alloc = torch.cuda.memory_allocated() / 1024 / 1024
                self._gpu_text = f"{alloc:.0f}MB"
            else:
                self._gpu_text = "N/A"
        except Exception:
            self._gpu_text = "N/A"
        self._refresh_stats()

    def _refresh_stats(self):
        total = self._prompt_tokens + self._completion_tokens
        tokens_str = f"{total / 1000:.1f}k" if total >= 1000 else str(total)
        dev_str = ""
        if self._asr_device:
            dev_color = "#4ec9b0" if "cuda" in self._asr_device.lower() else "#dcdcaa"
            dev_str = (
                f'<span style="color:{dev_color};">{self._asr_device}</span> '
                f'<span style="color:#555;">|</span> '
            )
        cost_str = ""
        if self._cost > 0:
            from i18n import get_lang
            symbol = "¥" if get_lang() == "zh" else "$"
            cost_str = f' <span style="color:#fa5;">{symbol}{self._cost:.4f}</span>'
        self._stats_label.setText(
            f"{dev_str}"
            f'<span style="color:#6cf;">CPU</span> {self._cpu}% '
            f'<span style="color:#6cf;">RAM</span> {self._ram_mb:.0f}MB '
            f'<span style="color:#6cf;">GPU</span> {self._gpu_text} '
            f'<span style="color:#555;">|</span> '
            f'<span style="color:#8b8;">ASR</span> {self._asr_count} '
            f'<span style="color:#db8;">TL</span> {self._tl_count} '
            f'<span style="color:#c9c;">Tok</span> {tokens_str} '
            f'<span style="color:#666;">({self._prompt_tokens}\u2191{self._completion_tokens}\u2193)</span>'
            f'{cost_str}'
        )


class _DragArea(QWidget):
    """Small draggable area (title + grip)."""

    drag_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        self._drag_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint()
                - self.window().frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        if self._drag_pos:
            self._drag_pos = None
            self.drag_finished.emit()


_COMBO_CSS = f"""
    QComboBox {{
        background: rgba(255,255,255,14);
        border: 1px solid rgba(255,255,255,26);
        border-radius: 8px;
        color: {Color.DARK_TEXT};
        font-size: 11px;
        padding: 1px 8px;
    }}
    QComboBox:hover {{ background: rgba(255,255,255,24); color: #ffffff; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox::down-arrow {{ image: none; border: none; }}
    QComboBox QAbstractItemView {{
        background: {Color.DARK_PANEL}; color: {Color.DARK_TEXT};
        selection-background-color: {Color.PRIMARY};
        border: 1px solid {Color.DARK_BORDER};
    }}
"""

_CHECK_CSS = (
    f"QCheckBox {{ color: {Color.DARK_TEXT_MUTED}; background: transparent; spacing: 5px; }}"
    "QCheckBox::indicator { width: 13px; height: 13px; border-radius: 4px; }"
    "QCheckBox::indicator:unchecked { border: 1px solid rgba(255,255,255,60); background: rgba(255,255,255,10); }"
    f"QCheckBox::indicator:checked {{ border: 1px solid {Color.PRIMARY_TINT}; background: {Color.PRIMARY}; }}"
)


class DragHandle(QWidget):
    """Top bar: row1=title+buttons, row2=checkboxes+combos."""

    settings_clicked = pyqtSignal()
    subtitle_clicked = pyqtSignal()
    tts_toggled = pyqtSignal(bool)
    click_through_toggled = pyqtSignal(bool)
    topmost_toggled = pyqtSignal(bool)
    auto_scroll_toggled = pyqtSignal(bool)
    taskbar_toggled = pyqtSignal(bool)
    target_language_changed = pyqtSignal(str)
    source_language_changed = pyqtSignal(str)
    model_changed = pyqtSignal(int)
    start_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()
    clear_clicked = pyqtSignal()
    hide_clicked = pyqtSignal()
    quit_clicked = pyqtSignal()
    mode_changed = pyqtSignal(str)  # "full" or "compact"
    position_changed = pyqtSignal()
    opacity_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Beginner-friendly default: start in "compact" (simple) layout. The
        # second row (advanced toggles + model/lang combos) and the system
        # monitor bar are hidden until the user explicitly switches to the
        # detailed view via the toggle button.
        self._mode = "compact"
        self._ui_font = _ui_font_family()
        self._chrome_opacity_pct = 85
        # Two-zone chrome (matches the paper-collage mockup):
        #   • slim TOP bar: drag title + pin (topmost) + close
        #   • BOTTOM control bar: play/pause + opacity slider + "Aa" (detail
        #     toggle) + settings (built separately by SubtitleOverlay as
        #     `build_bottom_bar()` so it can be docked at the window bottom).
        # The advanced controls (model/lang combos, extra toggles) live in a
        # collapsible row revealed by the "Aa"/detail button.
        self._mode = "compact"
        self.setFixedHeight(48)
        self.setObjectName("overlayTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#overlayTitleBar {"
            "  background: rgba(239,226,204,246);"
            "  border-top-left-radius: 12px;"
            "  border-top-right-radius: 12px;"
            "}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 11, 12, 9)
        outer.setSpacing(0)

        # ── Top row: drag title + pin + close ──
        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(4)

        drag = _DragArea()
        drag.drag_finished.connect(self.position_changed)
        drag.setStyleSheet("background: transparent;")
        drag_layout = QHBoxLayout(drag)
        drag_layout.setContentsMargins(0, 0, 8, 0)
        drag_layout.setSpacing(8)

        self._title_label = QLabel(t("overlay_title"))
        self._title_label.setFont(QFont(self._ui_font, 9, QFont.Weight.Bold))
        self._title_label.setStyleSheet(
            "color: #3a3127; background: transparent; letter-spacing: 0px;"
        )
        drag_layout.addWidget(self._title_label)
        drag_layout.addStretch()
        row1.addWidget(drag, 1)

        def _icon_btn(glyph="", tip=None, icon_kind=None):
            b = QPushButton()
            b.setFixedSize(28, 22)
            b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            b.setFont(QFont("Segoe UI Symbol", 12))
            b.setStyleSheet(_TITLE_ICON_BTN_CSS)
            if icon_kind:
                b.setIcon(_line_icon(icon_kind, "#3d3328"))
                b.setIconSize(QSize(16, 16))
            else:
                b.setText(glyph)
            if tip:
                b.setToolTip(tip)
            return b

        # Pin = "stay on top" toggle (checkable, accent when active).
        self._pin_btn = _icon_btn(tip=t("top_most"), icon_kind="pin")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setChecked(True)
        self._pin_btn.toggled.connect(self.topmost_toggled.emit)
        self._pin_btn.toggled.connect(self._sync_pin_style)
        row1.addWidget(self._pin_btn)

        self._close_btn = _icon_btn(tip=t("hide"), icon_kind="close")
        self._close_btn.setStyleSheet(_TITLE_ICON_BTN_CSS)
        self._close_btn.clicked.connect(self.hide_clicked.emit)
        row1.addWidget(self._close_btn)

        outer.addLayout(row1)
        self._sync_pin_style(True)

        # ── Collapsible advanced row: checkboxes + model/lang combos ──
        self._row2_widget = QWidget()
        self._row2_widget.setStyleSheet("background: transparent;")
        row2_outer = QVBoxLayout(self._row2_widget)
        row2_outer.setContentsMargins(0, 0, 0, 0)
        row2_outer.setSpacing(6)

        row2a = QHBoxLayout()
        row2a.setContentsMargins(0, 0, 0, 0)
        row2a.setSpacing(12)

        self._ct_check = QCheckBox(t("click_through"))
        self._ct_check.setFont(QFont(self._ui_font, 8))
        self._ct_check.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._ct_check.setStyleSheet(_CHECK_CSS)
        self._ct_check.toggled.connect(self.click_through_toggled.emit)
        row2a.addWidget(self._ct_check)

        self._topmost_check = QCheckBox(t("top_most"))
        self._topmost_check.setFont(QFont(self._ui_font, 8))
        self._topmost_check.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._topmost_check.setStyleSheet(_CHECK_CSS)
        self._topmost_check.setChecked(True)
        self._topmost_check.toggled.connect(self.topmost_toggled.emit)
        row2a.addWidget(self._topmost_check)

        self._auto_scroll = QCheckBox(t("auto_scroll"))
        self._auto_scroll.setFont(QFont(self._ui_font, 8))
        self._auto_scroll.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._auto_scroll.setStyleSheet(_CHECK_CSS)
        self._auto_scroll.setChecked(True)
        self._auto_scroll.toggled.connect(self.auto_scroll_toggled.emit)
        row2a.addWidget(self._auto_scroll)

        self._taskbar_check = QCheckBox(t("taskbar"))
        self._taskbar_check.setFont(QFont(self._ui_font, 8))
        self._taskbar_check.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._taskbar_check.setStyleSheet(_CHECK_CSS)
        self._taskbar_check.setChecked(True)
        self._taskbar_check.toggled.connect(self.taskbar_toggled.emit)
        row2a.addWidget(self._taskbar_check)

        row2a.addStretch()
        row2_outer.addLayout(row2a)

        row2b = QHBoxLayout()
        row2b.setContentsMargins(0, 0, 0, 0)
        row2b.setSpacing(8)

        _lbl_css = f"color: {Color.DARK_TEXT_MUTED}; background: transparent;"
        _lbl_font = QFont(self._ui_font, 8)
        _combo_font = QFont(self._ui_font, 8)

        model_lbl = QLabel(t("model_label"))
        model_lbl.setFont(_lbl_font)
        model_lbl.setStyleSheet(_lbl_css)
        row2b.addWidget(model_lbl)

        self._model_combo = QComboBox()
        self._model_combo.setFixedHeight(24)
        self._model_combo.setFont(_combo_font)
        self._model_combo.setStyleSheet(_COMBO_CSS)
        self._model_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._model_combo.currentIndexChanged.connect(self.model_changed.emit)
        row2b.addWidget(self._model_combo, 3)

        src_lbl = QLabel(t("source_label"))
        src_lbl.setFont(_lbl_font)
        src_lbl.setStyleSheet(_lbl_css)
        row2b.addWidget(src_lbl)

        self._source_lang = QComboBox()
        self._source_lang.setFixedHeight(24)
        self._source_lang.setFont(_combo_font)
        self._source_lang.setStyleSheet(_COMBO_CSS)
        self._source_lang.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        for code, native in LANGUAGES:
            label = t("asr_lang_auto") if code == "auto" else native
            self._source_lang.addItem(f"{code} - {label}", code)
        self._source_lang.currentIndexChanged.connect(
            lambda idx: self.source_language_changed.emit(
                self._source_lang.currentData() or "auto"
            )
        )
        row2b.addWidget(self._source_lang, 2)

        tgt_lbl = QLabel(t("target_label"))
        tgt_lbl.setFont(_lbl_font)
        tgt_lbl.setStyleSheet(_lbl_css)
        row2b.addWidget(tgt_lbl)

        self._target_lang = QComboBox()
        self._target_lang.setFixedHeight(24)
        self._target_lang.setFont(_combo_font)
        self._target_lang.setStyleSheet(_COMBO_CSS)
        self._target_lang.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        for code, native in LANGUAGES:
            if code == "auto":
                continue
            self._target_lang.addItem(f"{code} - {native}", code)
        self._target_lang.currentIndexChanged.connect(
            lambda idx: self.target_language_changed.emit(
                self._target_lang.currentData() or "zh"
            )
        )
        row2b.addWidget(self._target_lang, 2)

        row2_outer.addLayout(row2b)
        outer.addWidget(self._row2_widget)

        # State used by the bottom bar (built by SubtitleOverlay).
        self._running = False
        self._tts_active = False

        # Advanced row hidden by default (revealed via the bottom "Aa" button).
        self._row2_widget.setVisible(False)

    def _sync_pin_style(self, checked: bool):
        self._pin_btn.setStyleSheet(_TITLE_ICON_BTN_ACTIVE_CSS if checked else _TITLE_ICON_BTN_CSS)

    def _on_start_stop(self):
        if self._running:
            self.stop_clicked.emit()
        else:
            self.start_clicked.emit()

    def _on_tts_toggle(self):
        self.set_tts_active(not self._tts_active)
        self.tts_toggled.emit(self._tts_active)

    _PAUSED_CSS = _PAUSED_BTN_CSS

    def set_target_language(self, lang: str):
        idx = self._target_lang.findData(lang)
        if idx >= 0:
            self._target_lang.blockSignals(True)
            self._target_lang.setCurrentIndex(idx)
            self._target_lang.blockSignals(False)

    def set_source_language(self, lang: str):
        idx = self._source_lang.findData(lang)
        if idx >= 0:
            self._source_lang.blockSignals(True)
            self._source_lang.setCurrentIndex(idx)
            self._source_lang.blockSignals(False)

    def set_models(self, models: list, active_index: int = 0):
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for m in models:
            self._model_combo.addItem(m.get("name", m.get("model", "?")))
        if 0 <= active_index < self._model_combo.count():
            self._model_combo.setCurrentIndex(active_index)
        self._model_combo.blockSignals(False)

    @property
    def auto_scroll(self) -> bool:
        return self._auto_scroll.isChecked()

    def set_running(self, running: bool):
        self._running = running
        if hasattr(self, "_play_btn"):
            icon_color = "#e4d9c8" if self._chrome_opacity_pct > 0 else "transparent"
            self._play_btn.setIcon(_line_icon("pause" if running else "play", icon_color))
            self._play_btn.setText("")
            self._play_btn.setStyleSheet(
                _icon_btn_css(running, self._chrome_opacity_pct)
            )
            self._play_btn.setEnabled(self._chrome_opacity_pct > 0)
            self._play_btn.setToolTip(t("running") if running else t("paused"))
        if hasattr(self, "_stop_btn"):
            self._stop_btn.setEnabled(running and self._chrome_opacity_pct > 0)

    def set_tts_active(self, active: bool):
        """Reflect TTS on/off state on the bottom-bar button (no signal emit)."""
        self._tts_active = active
        if hasattr(self, "_tts_btn"):
            self._tts_btn.setStyleSheet(
                _icon_btn_css(active, self._chrome_opacity_pct)
            )

    def _toggle_mode(self):
        new_mode = "compact" if self._mode == "full" else "full"
        self._apply_mode(new_mode)
        self.mode_changed.emit(new_mode)

    def _apply_mode(self, mode: str):
        self._mode = mode
        compact = mode == "compact"
        self._row2_widget.setVisible(not compact)
        if hasattr(self, "_detail_btn"):
            self._detail_btn.setStyleSheet(
                _icon_btn_css(not compact, self._chrome_opacity_pct)
            )
        self.setFixedHeight(48 if compact else 102)

    def set_mode(self, mode: str):
        if mode != self._mode:
            self._apply_mode(mode)

    def set_subtitle_checked(self, checked: bool):
        # The standalone-subtitle-window toggle now lives in settings/tray; the
        # slim overlay chrome no longer carries its own button. Kept as a no-op
        # so existing callers don't break.
        pass

    def set_opacity_percent(self, pct: int):
        """Reflect the current window opacity on the bottom slider (no emit)."""
        if not hasattr(self, "_opacity_slider"):
            return
        self._opacity_slider.blockSignals(True)
        self._opacity_slider.setValue(int(pct))
        self._opacity_slider.blockSignals(False)
        self._opacity_label.setText(f"{int(pct)}%")
        self._chrome_opacity_pct = _chrome_opacity_pct(pct)
        controls_enabled = self._chrome_opacity_pct > 0
        icon_color = "#e4d9c8" if controls_enabled else "transparent"
        if hasattr(self, "_play_btn"):
            self._play_btn.setIcon(_line_icon("pause" if self._running else "play", icon_color))
            self._play_btn.setStyleSheet(_icon_btn_css(self._running, self._chrome_opacity_pct))
            self._play_btn.setVisible(True)
            self._play_btn.setEnabled(controls_enabled)
        if hasattr(self, "_title_label"):
            self._title_label.setVisible(controls_enabled)
        if hasattr(self, "_pin_btn"):
            self._pin_btn.setVisible(controls_enabled)
        if hasattr(self, "_close_btn"):
            self._close_btn.setVisible(controls_enabled)
        if hasattr(self, "_tts_btn"):
            self._tts_btn.setStyleSheet(_icon_btn_css(self._tts_active, self._chrome_opacity_pct))
            self._tts_btn.setEnabled(controls_enabled)
        if hasattr(self, "_detail_btn"):
            self._detail_btn.setStyleSheet(_icon_btn_css(self._mode != "compact", self._chrome_opacity_pct))
            self._detail_btn.setVisible(True)
            self._detail_btn.setEnabled(controls_enabled)
        if hasattr(self, "_opacity_slider"):
            self._opacity_slider.setStyleSheet(_opacity_slider_css(pct))
        for btn_name in ("_stop_btn", "_menu_btn", "_settings_btn"):
            btn = getattr(self, btn_name, None)
            if btn is not None:
                icon_kind = {
                    "_stop_btn": "stop",
                    "_menu_btn": "panel",
                    "_settings_btn": "gear",
                }.get(btn_name)
                if icon_kind:
                    btn.setIcon(_line_icon(icon_kind, icon_color))
                btn.setStyleSheet(_icon_btn_css(False, self._chrome_opacity_pct))
                btn.setVisible(True)
                btn.setEnabled(controls_enabled and (btn_name != "_stop_btn" or self._running))

    def build_bottom_bar(self) -> QWidget:
        """Build the bottom control bar: play/pause · opacity · TTS · Aa · gear.

        Matches the paper-collage mockup. Returned widget is docked at the
        bottom of the overlay container by SubtitleOverlay.
        """
        bar = QWidget()
        bar.setObjectName("overlayBottomBar")
        bar.setFixedHeight(46)
        bar.setStyleSheet(
            "QWidget#overlayBottomBar {"
            "  background: rgba(255,255,255,18);"
            "  border-top: 1px solid rgba(255,255,255,28);"
            "}"
        )
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(6)

        def _icon_btn(glyph="", tip=None, icon_kind=None):
            b = QPushButton()
            b.setFixedSize(30, 26)
            b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            b.setFont(QFont("Segoe UI Symbol", 9, QFont.Weight.Bold))
            b.setStyleSheet(_ICON_BTN_CSS)
            if icon_kind:
                b.setIcon(_line_icon(icon_kind, "#e4d9c8"))
                b.setIconSize(QSize(15, 15))
            else:
                b.setText(glyph)
            if tip:
                b.setToolTip(tip)
            return b

        self._play_btn = _icon_btn(tip=t("paused"), icon_kind="play")
        self._play_btn.clicked.connect(self._on_start_stop)
        row.addWidget(self._play_btn)

        self._stop_btn = _icon_btn(tip=t("tray_stop"), icon_kind="stop")
        self._stop_btn.clicked.connect(self.stop_clicked.emit)
        row.addWidget(self._stop_btn)

        # Opacity: label + slider + percent readout.
        self._opacity_text_label = QLabel(t("overlay_opacity"))
        self._opacity_text_label.setFont(QFont(self._ui_font, 8))
        self._opacity_text_label.setStyleSheet(
            "color: #d8ccb9; background: transparent;"
        )
        row.addWidget(self._opacity_text_label)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(0, 100)
        self._opacity_slider.setValue(85)
        self._opacity_slider.setFixedWidth(68)
        self._opacity_slider.setStyleSheet(_opacity_slider_css(85))
        self._opacity_slider.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        row.addWidget(self._opacity_slider)

        self._opacity_label = QLabel("85%")
        self._opacity_label.setFont(QFont(self._ui_font, 8))
        self._opacity_label.setFixedWidth(30)
        self._opacity_label.setStyleSheet(
            "color: #d8ccb9; background: transparent;"
        )
        row.addWidget(self._opacity_label)

        row.addStretch(1)

        self._tts_btn = _icon_btn("◉", tip=t("tts_toggle_tip"))
        self._tts_btn.clicked.connect(self._on_tts_toggle)
        self._tts_btn.setVisible(False)
        row.addWidget(self._tts_btn)

        self._detail_btn = _icon_btn("Aa", tip=t("mode_toggle_tip"))
        self._detail_btn.setFont(QFont(self._ui_font, 8, QFont.Weight.Bold))
        self._detail_btn.setFixedWidth(34)
        self._detail_btn.clicked.connect(self._toggle_mode)
        row.addWidget(self._detail_btn)

        self._menu_btn = _icon_btn(tip="菜单", icon_kind="panel")
        self._menu_btn.setFixedWidth(34)
        self._menu_btn.clicked.connect(lambda: self._show_bottom_menu(self._menu_btn))
        row.addWidget(self._menu_btn)

        self._settings_btn = _icon_btn(tip=t("settings"), icon_kind="gear")
        self._settings_btn.setFixedWidth(34)
        self._settings_btn.clicked.connect(self.settings_clicked.emit)
        row.addWidget(self._settings_btn)

        return bar

    def _on_opacity_changed(self, value: int):
        self._opacity_label.setText(f"{value}%")
        self.opacity_changed.emit(int(value))

    def _show_bottom_menu(self, anchor: QPushButton):
        menu = QMenu(anchor)
        menu.setStyleSheet(_MENU_CSS)
        subtitle_action = menu.addAction(t("nav_subtitle_window"))
        clear_action = menu.addAction(t("clear"))
        menu.addSeparator()
        settings_action = menu.addAction(t("settings"))
        hide_action = menu.addAction(t("hide"))
        action = menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        if action == subtitle_action:
            self.subtitle_clicked.emit()
        elif action == clear_action:
            self.clear_clicked.emit()
        elif action == settings_action:
            self.settings_clicked.emit()
        elif action == hide_action:
            self.hide_clicked.emit()


class SubtitleOverlay(QWidget):
    """Chat-style overlay window for displaying live transcription."""

    add_message_signal = pyqtSignal(int, str, str, str, float)
    update_translation_signal = pyqtSignal(int, str, float)
    update_streaming_signal = pyqtSignal(int, str)
    update_original_signal = pyqtSignal(int, str, str)
    clear_signal = pyqtSignal()
    # Monitor signals (thread-safe)
    update_monitor_signal = pyqtSignal(float, float, object)
    update_stats_signal = pyqtSignal(int, int, int, int, float)
    update_asr_device_signal = pyqtSignal(str)

    settings_requested = pyqtSignal()
    target_language_changed = pyqtSignal(str)
    source_language_changed = pyqtSignal(str)
    model_switch_requested = pyqtSignal(int)
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    hide_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    subtitle_toggled = pyqtSignal()
    mode_changed = pyqtSignal(str)  # "full" or "compact"
    position_changed = pyqtSignal()
    tts_toggled = pyqtSignal(bool)
    opacity_changed = pyqtSignal(int)

    def __init__(self, config):
        super().__init__()
        self._config = config
        self._messages = {}
        self._max_messages = 50
        self._click_through = False
        self._height_before_compact = None
        self._mode_anim = None
        self._chrome_hovered = False
        self._watch_window_opacity = _window_opacity_pct(DEFAULT_STYLE)
        self._pos_save_timer = QTimer(self)
        self._pos_save_timer.setSingleShot(True)
        self._pos_save_timer.setInterval(500)
        self._pos_save_timer.timeout.connect(lambda: self.position_changed.emit())
        self._last_saved_geo = None
        self._watch_hover_timer = QTimer(self)
        self._watch_hover_timer.setInterval(300)
        self._watch_hover_timer.timeout.connect(self._refresh_watch_hover_state)
        self._setup_ui()

        self.add_message_signal.connect(self._on_add_message)
        self.update_translation_signal.connect(self._on_update_translation)
        self.update_streaming_signal.connect(self._on_update_streaming)
        self.update_original_signal.connect(self._on_update_original)
        self.clear_signal.connect(self._on_clear)
        self.update_monitor_signal.connect(self._on_update_monitor)
        self.update_stats_signal.connect(self._on_update_stats)
        self.update_asr_device_signal.connect(self._on_update_asr_device)

    def _setup_ui(self):
        self._ui_font = _ui_font_family()
        self.setFont(QFont(self._ui_font, 10))
        # Show in the taskbar by default so the app is discoverable there, not
        # only in the tray. Users can switch to a tool-style (tray-only) window
        # via the "taskbar" toggle, which re-adds Qt.Tool in _set_taskbar().
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowTitle("LiveTranslate")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry()
        width = min(860, max(560, int(geo.width() * 0.52)))
        height = 220
        x = geo.left() + (geo.width() - width) // 2
        y = geo.bottom() - height - 72
        self.setGeometry(x, y, width, height)
        self.setMinimumSize(360, 220)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self._container = _OverlayContainer()

        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # Drag handle
        self._handle = DragHandle()
        self._handle.settings_clicked.connect(self.settings_requested.emit)
        self._handle.subtitle_clicked.connect(self.subtitle_toggled.emit)
        self._handle.tts_toggled.connect(self.tts_toggled.emit)
        self._handle.click_through_toggled.connect(self._set_click_through)
        self._handle.topmost_toggled.connect(self._set_topmost)
        self._handle.taskbar_toggled.connect(self._set_taskbar)
        self._handle.target_language_changed.connect(self.target_language_changed.emit)
        self._handle.source_language_changed.connect(self.source_language_changed.emit)
        self._handle.model_changed.connect(self.model_switch_requested.emit)
        self._handle.start_clicked.connect(self.start_requested.emit)
        self._handle.stop_clicked.connect(self.stop_requested.emit)
        self._handle.hide_clicked.connect(self.hide_requested.emit)
        self._handle.clear_clicked.connect(self._on_clear)
        self._handle.quit_clicked.connect(self.quit_requested.emit)
        self._handle.mode_changed.connect(self._on_mode_changed)
        self._handle.position_changed.connect(self.position_changed)
        self._handle.opacity_changed.connect(self._on_opacity_changed)
        container_layout.addWidget(self._handle)

        # Monitor bar (collapsible)
        self._monitor = MonitorBar()
        container_layout.addWidget(self._monitor)

        # Scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical {
                width: 7px; background: transparent;
            }
            QScrollBar::handle:vertical {
                background: rgba(238,226,204,82); border-radius: 3px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(238,226,204,135);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)

        self._msg_container = QWidget()
        self._msg_container.setStyleSheet("background: transparent;")
        self._msg_layout = QVBoxLayout(self._msg_container)
        self._msg_layout.setContentsMargins(6, 20, 6, 6)
        self._msg_layout.setSpacing(4)

        # First-run welcome card — shown only when API keys are not yet
        # configured. It is placed at the very top of the message list so the
        # user cannot miss it. We hide it as soon as `update_welcome_card`
        # confirms both ASR and LLM are configured.
        self._welcome_card = self._build_welcome_card()
        self._welcome_card.setVisible(False)
        self._msg_layout.addWidget(self._welcome_card)

        # "Waiting for sound" empty state — shown when configured but no
        # subtitles have arrived yet, so the overlay is never a blank box.
        self._empty_state = self._build_empty_state()
        self._msg_layout.addWidget(self._empty_state, 0, Qt.AlignmentFlag.AlignCenter)

        self._msg_layout.addStretch()

        self._scroll.setWidget(self._msg_container)
        container_layout.addWidget(self._scroll)

        # Bottom control bar: play/pause · opacity · TTS · Aa · settings.
        self._bottom_bar = self._handle.build_bottom_bar()
        container_layout.addWidget(self._bottom_bar)

        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch()
        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(0, 0)
        self._grip.setStyleSheet("background: transparent;")
        grip_row.addWidget(self._grip)
        container_layout.addLayout(grip_row)

        main_layout.addWidget(self._container)

        self._ct_timer = QTimer(self)
        self._ct_timer.timeout.connect(self._check_click_through)
        self._ct_timer.start(50)

        # Beginner-friendly default: start collapsed so the overlay shows only
        # subtitles and a slim title bar. The "详细" button on the title bar
        # toggles the advanced row + monitor bar back on.
        self._monitor.setVisible(False)
        ChatMessage._compact_mode = True
        self._refresh_empty_state()
        self._sync_watch_chrome()
        self._watch_hover_timer.start()

    def _build_welcome_card(self) -> QWidget:
        """Build the first-run welcome card shown inside the message list."""
        card = QWidget()
        card.setObjectName("welcomeCard")
        card.setStyleSheet(
            "QWidget#welcomeCard {"
            "  background: rgba(252, 251, 247, 246);"
            "  border: 1px solid rgba(216,211,197,200);"
            "  border-radius: 14px;"
            "}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(22, 20, 22, 20)
        v.setSpacing(12)

        title = QLabel("欢迎使用 LiveTranslate")
        title.setObjectName("welcomeTitle")
        title.setFont(QFont(self._ui_font, 19, QFont.Weight.Bold))
        title.setStyleSheet(
            f"QLabel#welcomeTitle {{ color: {Color.TEXT_STRONG}; font-size: 19px;"
            "font-weight: 800; background: transparent; border: 0; }"
        )
        title.setWordWrap(True)
        v.addWidget(title)

        body = QLabel(
            "还差最后一步：配置听写服务和翻译服务。\n"
            "完成后播放视频、会议或网页声音，字幕会在这里实时出现。"
        )
        body.setObjectName("welcomeBody")
        body.setFont(QFont(self._ui_font, 10))
        body.setStyleSheet(
            f"QLabel#welcomeBody {{ color: {Color.TEXT_SUBTLE}; font-size: 13px;"
            "background: transparent; border: 0; }"
        )
        body.setWordWrap(True)
        v.addWidget(body)

        btn = QPushButton("打开设置")
        btn.setFont(QFont(self._ui_font, 10, QFont.Weight.Bold))
        btn.setMinimumHeight(42)
        btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn.setStyleSheet(primary_button_stylesheet())
        btn.clicked.connect(self.settings_requested.emit)
        v.addWidget(btn)
        return card

    def update_welcome_card(self, asr_ready: bool, llm_ready: bool):
        """Show/hide the welcome card based on configuration status."""
        if not hasattr(self, "_welcome_card"):
            return
        needs_help = not (asr_ready and llm_ready)
        self._welcome_card.setVisible(needs_help)
        self._refresh_empty_state()

    def _build_empty_state(self) -> QWidget:
        """Minimal idle placeholder for the subtitle overlay."""
        holder = QWidget()
        holder.setObjectName("overlayEmpty")
        holder.setStyleSheet("QWidget#overlayEmpty { background: transparent; }")
        v = QVBoxLayout(holder)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(0)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)

        hint = QLabel(t("overlay_waiting_sound"))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setFont(QFont(self._ui_font, 10))
        hint.setStyleSheet(
            f"color: {Color.DARK_TEXT_MUTED}; background: transparent;"
        )
        v.addWidget(hint)
        return holder

    def _refresh_empty_state(self):
        """Show the waiting placeholder only when idle and configured."""
        if not hasattr(self, "_empty_state"):
            return
        # The reference window is a clean dark subtitle canvas; do not show the
        # large paper-collage empty illustration inside the floating window.
        self._empty_state.setVisible(False)
        return
        welcome_visible = (
            hasattr(self, "_welcome_card") and self._welcome_card.isVisible()
        )
        show = (not self._messages) and (not welcome_visible)
        self._empty_state.setVisible(show)

    def _schedule_pos_save(self):
        if not self.isVisible():
            return
        geo = (self.x(), self.y(), self.width(), self.height())
        if geo != self._last_saved_geo:
            self._last_saved_geo = geo
            self._pos_save_timer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_pos_save()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_pos_save()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._chrome_hovered = True
        self._sync_watch_chrome()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._chrome_hovered = False
        self._sync_watch_chrome()

    def set_running(self, running: bool):
        self._handle.set_running(running)

    def set_tts_active(self, active: bool):
        self._handle.set_tts_active(active)

    def _set_topmost(self, enabled: bool):
        flags = self.windowFlags()
        if enabled:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _set_taskbar(self, enabled: bool):
        flags = self.windowFlags()
        if enabled:
            flags &= ~Qt.WindowType.Tool
        else:
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.show()

    def _set_click_through(self, enabled: bool):
        self._click_through = enabled
        if not enabled:
            hwnd = int(self.winId())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            if style & _WS_EX_TRANSPARENT:
                ctypes.windll.user32.SetWindowLongW(
                    hwnd, _GWL_EXSTYLE, style & ~_WS_EX_TRANSPARENT
                )

    def _check_click_through(self):
        if not self._click_through:
            return
        cursor = QCursor.pos()
        local = self.mapFromGlobal(cursor)
        hwnd = int(self.winId())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)

        scroll_top = self._scroll.mapTo(self, QPoint(0, 0)).y()
        in_header = 0 <= local.x() <= self.width() and 0 <= local.y() < scroll_top

        if in_header:
            if style & _WS_EX_TRANSPARENT:
                ctypes.windll.user32.SetWindowLongW(
                    hwnd, _GWL_EXSTYLE, style & ~_WS_EX_TRANSPARENT
                )
        else:
            if not (style & _WS_EX_TRANSPARENT):
                ctypes.windll.user32.SetWindowLongW(
                    hwnd, _GWL_EXSTYLE, style | _WS_EX_TRANSPARENT
                )

    def _on_mode_changed(self, mode: str):
        compact = mode == "compact"
        self._monitor.setVisible(not compact)
        ChatMessage._compact_mode = compact
        s = ChatMessage._current_style
        for msg in self._messages.values():
            msg.apply_style(s)
        self._sync_compact_message_visibility()
        self._sync_watch_chrome()
        self.mode_changed.emit(mode)

        # Animate window height
        if self._mode_anim and self._mode_anim.state() != QPropertyAnimation.State.Stopped:
            self._mode_anim.stop()

        if compact:
            self._height_before_compact = self.height()
            target_h = max(self.minimumHeight(), 240)
        else:
            target_h = self._height_before_compact or 500

        actual_h = self.frameGeometry().height()
        if abs(actual_h - target_h) < 10:
            self.resize(self.width(), target_h)
        else:
            anim = QPropertyAnimation(self, b"size")
            anim.setDuration(200)
            anim.setStartValue(QSize(self.width(), actual_h))
            anim.setEndValue(QSize(self.width(), target_h))
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._mode_anim = anim
            anim.start()

    def set_mode(self, mode: str):
        self._handle.set_mode(mode)

    def set_subtitle_checked(self, checked: bool):
        self._handle.set_subtitle_checked(checked)

    @pyqtSlot(float, float, object)
    def _on_update_monitor(self, rms: float, vad_conf: float, mic_rms):
        self._monitor.update_audio(rms, vad_conf, mic_rms)

    @pyqtSlot(int, int, int, int, float)
    def _on_update_stats(self, asr_count, tl_count, prompt_tokens, completion_tokens, cost):
        self._monitor.update_pipeline_stats(
            asr_count, tl_count, prompt_tokens, completion_tokens, cost
        )

    @pyqtSlot(str)
    def _on_update_asr_device(self, device: str):
        self._monitor.update_asr_device(device)

    @pyqtSlot(int, str, str, str, float)
    def _on_add_message(self, msg_id, timestamp, original, source_lang, asr_ms):
        msg = ChatMessage(msg_id, timestamp, original, source_lang, asr_ms)
        import time
        msg._start_seconds = time.monotonic()
        self._messages[msg_id] = msg
        self._msg_layout.insertWidget(max(0, self._msg_layout.count() - 1), msg)
        self._refresh_empty_state()
        self._sync_compact_message_visibility()
        self._sync_watch_chrome()

        if len(self._messages) > self._max_messages:
            oldest_id = min(self._messages.keys())
            old_msg = self._messages.pop(oldest_id)
            self._msg_layout.removeWidget(old_msg)
            old_msg.deleteLater()
            self._sync_compact_message_visibility()
            self._sync_watch_chrome()

        QTimer.singleShot(50, self._scroll_to_bottom)

    @pyqtSlot(int, str, float)
    def _on_update_translation(self, msg_id, translated, translate_ms):
        msg = self._messages.get(msg_id)
        if msg:
            msg.set_translation(translated, translate_ms)
            self._sync_compact_message_visibility()
            QTimer.singleShot(50, self._scroll_to_bottom)

    def _on_update_streaming(self, msg_id, partial_text):
        msg = self._messages.get(msg_id)
        if msg:
            msg.update_streaming(partial_text)

    @pyqtSlot(int, str, str)
    def _on_update_original(self, msg_id, original, source_lang):
        msg = self._messages.get(msg_id)
        if msg:
            msg.set_original(original, source_lang)
            self._sync_compact_message_visibility()
            QTimer.singleShot(50, self._scroll_to_bottom)

    @pyqtSlot()
    def _on_clear(self):
        for msg in self._messages.values():
            self._msg_layout.removeWidget(msg)
            msg.deleteLater()
        self._messages.clear()
        self._refresh_empty_state()
        self._sync_compact_message_visibility()
        self._sync_watch_chrome()

    def _scroll_to_bottom(self):
        if not self._handle.auto_scroll:
            return
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _sync_compact_message_visibility(self):
        if not self._messages:
            return
        if not ChatMessage._compact_mode:
            for msg in self._messages.values():
                msg.setVisible(True)
            return
        visible_ids = set(sorted(self._messages.keys())[-1:])
        for msg_id, msg in self._messages.items():
            msg.setVisible(msg_id in visible_ids)

    def _watch_chrome_visible(self) -> bool:
        if not ChatMessage._compact_mode:
            return True
        if not self._messages:
            return True
        if self._chrome_hovered:
            return True
        return self._watch_window_opacity <= 8

    def _refresh_watch_hover_state(self):
        if not self._chrome_hovered or not self.isVisible():
            return
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self._chrome_hovered = False
            self._sync_watch_chrome()

    def _sync_watch_chrome(self):
        if not hasattr(self, "_handle"):
            return
        visible = self._watch_chrome_visible()
        self._handle.setVisible(visible)
        bottom_visible = visible or self._watch_window_opacity <= 8
        if hasattr(self, "_bottom_bar"):
            self._bottom_bar.setVisible(bottom_visible)
        if hasattr(self, "_msg_layout"):
            self._msg_layout.setContentsMargins(
                6,
                20 if visible else 8,
                6,
                6 if bottom_visible else 10,
            )

    def apply_style(self, style: dict):
        s = {**DEFAULT_STYLE, **style}
        # Migrate old single font_family to split fields
        if "font_family" in s and "original_font_family" not in style:
            s["original_font_family"] = s["font_family"]
            s["translation_font_family"] = s["font_family"]
        # Container background
        window_pct = _window_opacity_pct(s)
        self._watch_window_opacity = window_pct
        self.setWindowOpacity(1.0)
        bg_rgba = _hex_to_rgba(s["bg_color"], _scaled_alpha(s["bg_opacity"], window_pct))
        self._container.set_surface(bg_rgba, max(12, s["border_radius"] + 5))
        chrome_pct = _chrome_opacity_pct(s)
        title_alpha = _scaled_alpha(246, chrome_pct)
        title_border_alpha = _scaled_alpha(28, chrome_pct)
        if chrome_pct <= 8:
            self._handle.setStyleSheet(
                "QWidget#overlayTitleBar {"
                "  background: transparent;"
                "  border-bottom: 1px solid transparent;"
                "  border-top-left-radius: 12px;"
                "  border-top-right-radius: 12px;"
                "}"
            )
        else:
            self._handle.setStyleSheet(
                "QWidget#overlayTitleBar {"
                f"  background: rgba(239,226,204,{title_alpha});"
                f"  border-bottom: 1px solid rgba(255,255,255,{title_border_alpha});"
                "  border-top-left-radius: 12px;"
                "  border-top-right-radius: 12px;"
                "}"
            )
        if hasattr(self, "_bottom_bar"):
            bottom_alpha = _scaled_alpha(18, chrome_pct)
            bottom_border_alpha = _scaled_alpha(28, chrome_pct)
            if chrome_pct <= 8:
                self._bottom_bar.setStyleSheet(
                    "QWidget#overlayBottomBar {"
                    "  background: transparent;"
                    "  border-top: 1px solid transparent;"
                    "}"
                )
            else:
                self._bottom_bar.setStyleSheet(
                    "QWidget#overlayBottomBar {"
                    f"  background: rgba(255,255,255,{bottom_alpha});"
                    f"  border-top: 1px solid rgba(255,255,255,{bottom_border_alpha});"
                    "}"
                )
        if hasattr(self, "_handle"):
            self._handle.set_opacity_percent(s["window_opacity"])
        # Update all existing messages
        ChatMessage._current_style = s
        for msg in self._messages.values():
            msg.apply_style(s)
        self._sync_compact_message_visibility()
        self._sync_watch_chrome()

    def _on_opacity_changed(self, pct: int):
        """Bottom-bar opacity slider → fade overlay chrome, keep subtitle text solid."""
        pct = max(0, min(100, int(pct)))
        s = dict(ChatMessage._current_style)
        s["window_opacity"] = pct
        self.apply_style(s)
        self.opacity_changed.emit(pct)

    def export_messages(self, mode: str, parent=None):
        """Export captured messages to .txt, .srt, or .md.

        mode: "original" | "translation" | "both"
        """
        if not self._messages:
            QMessageBox.information(parent or self, "LiveTranslate", t("export_empty"))
            return

        from datetime import datetime
        suffix = {"original": "original", "translation": "translation", "both": "all"}.get(mode, "all")
        default_name = f"livetrans_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{suffix}.txt"
        path, selected_filter = QFileDialog.getSaveFileName(
            parent or self,
            t("export_dialog_title"),
            default_name,
            t("export_filter"),
        )
        if not path:
            return

        fmt = self._detect_export_format(path, selected_filter)
        if "." not in os.path.basename(path):
            path = f"{path}.{fmt}"

        base_start = None
        items = []
        for idx, msg_id in enumerate(sorted(self._messages.keys()), 1):
            msg = self._messages[msg_id]
            if base_start is None and msg._start_seconds is not None:
                base_start = msg._start_seconds
            start = None
            end = None
            if base_start is not None and msg._start_seconds is not None:
                start = msg._start_seconds - base_start
            if base_start is not None and msg._end_seconds is not None:
                end = msg._end_seconds - base_start
            items.append(
                SubtitleExportItem(
                    index=idx,
                    timestamp=msg._timestamp,
                    original=msg._original or "",
                    translation=msg._translated or "",
                    start_seconds=start,
                    end_seconds=end,
                )
            )

        content = export_subtitles(items, mode, fmt)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            QMessageBox.critical(
                parent or self,
                "LiveTranslate",
                t("export_failed").format(error=str(e)),
            )

    @staticmethod
    def _detect_export_format(path: str, selected_filter: str) -> str:
        lower_path = path.lower()
        if lower_path.endswith(".srt"):
            return "srt"
        if lower_path.endswith(".md") or lower_path.endswith(".markdown"):
            return "md"
        lower_filter = (selected_filter or "").lower()
        if "srt" in lower_filter:
            return "srt"
        if "markdown" in lower_filter:
            return "md"
        return "txt"

    # Thread-safe public API
    def add_message(self, msg_id, timestamp, original, source_lang, asr_ms):
        self.add_message_signal.emit(msg_id, timestamp, original, source_lang, asr_ms)

    def update_translation(self, msg_id, translated, translate_ms):
        self.update_translation_signal.emit(msg_id, translated, translate_ms)

    def update_streaming(self, msg_id, partial_text):
        self.update_streaming_signal.emit(msg_id, partial_text)

    def update_original(self, msg_id, original, source_lang):
        self.update_original_signal.emit(msg_id, original, source_lang)

    def update_monitor(self, rms, vad_conf, mic_rms=None):
        self.update_monitor_signal.emit(rms, vad_conf, mic_rms)

    def update_stats(self, asr_count, tl_count, prompt_tokens, completion_tokens, cost=0.0):
        self.update_stats_signal.emit(
            asr_count, tl_count, prompt_tokens, completion_tokens, cost
        )

    def update_asr_device(self, device: str):
        self.update_asr_device_signal.emit(device)

    def set_target_language(self, lang: str):
        self._handle.set_target_language(lang)
        # Keep the translation chip label ("译文（中文）") in sync, and refresh
        # any messages already on screen.
        ChatMessage._target_language = lang or "zh"
        for msg in self._messages.values():
            msg._apply_chrome(ChatMessage._current_style)

    def set_source_language(self, lang: str):
        self._handle.set_source_language(lang)

    def set_models(self, models: list, active_index: int = 0):
        self._handle.set_models(models, active_index)

    def clear(self):
        self.clear_signal.emit()
