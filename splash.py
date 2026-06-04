"""Lightweight startup splash screen.

Shown right after the QApplication is created and closed once the main UI is
up. Cold start has to import torch and construct several windows, which can
leave a few seconds of blank screen — a branded splash makes "starting up"
feel intentional instead of frozen.

Kept deliberately small and dependency-free (only PyQt6 + theme) so it can be
shown before any heavy initialization.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPixmap
from PyQt6.QtWidgets import QApplication, QSplashScreen

from theme import Color, ui_font_family


def _build_pixmap() -> QPixmap:
    from runtime_paths import resource_path

    scale = 2  # render at 2x for crisp text on HiDPI
    w, h = 420 * scale, 220 * scale
    pix = QPixmap(w, h)
    pix.fill(QColor(0, 0, 0, 0))

    from PyQt6.QtGui import QPainter, QPainterPath

    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Rounded card background (brand-dark, matches overlay container).
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, 18 * scale, 18 * scale)
    p.fillPath(path, QColor(Color.DARK_PANEL))
    p.setPen(QColor(Color.PRIMARY_TINT))
    p.drawPath(path)

    # App icon, if available.
    icon_path = resource_path("assets", "app-icon.png")
    if icon_path.exists():
        logo = QPixmap(str(icon_path)).scaled(
            72 * scale,
            72 * scale,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        p.drawPixmap((w - logo.width()) // 2, 36 * scale, logo)

    font = ui_font_family()
    p.setPen(QColor(Color.WHITE))
    p.setFont(QFont(font, 17 * scale, QFont.Weight.Bold))
    p.drawText(
        0, 120 * scale, w, 32 * scale,
        Qt.AlignmentFlag.AlignHCenter, "LiveTranslate",
    )

    p.setPen(QColor(Color.DARK_TEXT_MUTED))
    p.setFont(QFont(font, 9 * scale))
    p.drawText(
        0, 160 * scale, w, 26 * scale,
        Qt.AlignmentFlag.AlignHCenter, "正在启动，请稍候…",
    )
    p.end()

    pix.setDevicePixelRatio(scale)
    return pix


class StartupSplash(QSplashScreen):
    """Frameless, centered splash with the app logo and a status line."""

    def __init__(self):
        super().__init__(_build_pixmap())
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

    def status(self, text: str):
        """Update the bottom status message."""
        self.showMessage(
            text,
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
            QColor(Color.DARK_TEXT_MUTED),
        )
        QApplication.processEvents()
