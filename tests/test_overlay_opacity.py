from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from subtitle_overlay import ChatMessage, DEFAULT_STYLE, SubtitleOverlay  # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


def test_zero_opacity_keeps_text_visible_without_layout_jump():
    app = _app()
    style = {**DEFAULT_STYLE, "window_opacity": 0}
    ChatMessage._current_style = style
    ChatMessage._compact_mode = False

    msg = ChatMessage(1, "12:00:00", "Hello world", "en", 12.0)
    msg.set_translation("translated text", 123.0)
    msg.apply_style(style)
    msg.show()
    app.processEvents()
    y0 = msg._header_label.geometry().y()

    assert not msg._header_label.isHidden()
    assert not msg._trans_label.isHidden()
    assert not msg._orig_chip.isHidden()
    assert not msg._trans_chip.isHidden()
    assert not msg._divider.isHidden()
    assert ",0)" in msg._orig_chip.styleSheet()

    msg.apply_style({**DEFAULT_STYLE, "window_opacity": 4})
    app.processEvents()
    assert msg._header_label.geometry().y() == y0


def test_near_zero_opacity_keeps_opacity_control_geometry_stable():
    app = _app()
    overlay = SubtitleOverlay({})
    overlay.resize(640, 260)
    overlay.apply_style({**DEFAULT_STYLE, "window_opacity": 0})
    overlay.show()
    app.processEvents()

    slider = overlay._handle._opacity_slider
    x0 = slider.mapTo(overlay, slider.rect().topLeft()).x()
    assert overlay._handle._opacity_slider.isEnabled()
    assert not overlay._handle._play_btn.isEnabled()

    overlay.apply_style({**DEFAULT_STYLE, "window_opacity": 4})
    app.processEvents()
    x4 = slider.mapTo(overlay, slider.rect().topLeft()).x()

    assert x4 == x0


def test_streaming_revision_updates_immediately_without_blank_restart():
    app = _app()
    ChatMessage._current_style = DEFAULT_STYLE
    ChatMessage._compact_mode = True

    msg = ChatMessage(1, "12:00:00", "Hello world", "en", 12.0)
    msg.update_streaming("previous full translation")
    app.processEvents()

    assert msg._streaming_visible_text == "previous full translation"
    assert "previous full translation" in msg._trans_label.text()

    msg.update_streaming("new revised translation")
    app.processEvents()

    assert msg._streaming_visible_text == "new revised translation"
    assert "new revised translation" in msg._trans_label.text()


def test_compact_watch_mode_hides_chrome_until_hover():
    app = _app()
    overlay = SubtitleOverlay({})
    overlay.resize(640, 260)
    overlay.apply_style({**DEFAULT_STYLE, "window_opacity": 64})
    overlay.show()
    overlay.add_message(1, "12:00:00", "Hello world", "en", 12.0)
    app.processEvents()

    assert overlay._handle.isHidden()
    assert overlay._bottom_bar.isHidden()

    overlay._chrome_hovered = True
    overlay._sync_watch_chrome()
    app.processEvents()

    assert not overlay._handle.isHidden()
    assert not overlay._bottom_bar.isHidden()
