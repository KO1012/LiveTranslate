"""Centralized design system for LiveTranslate.

This module is the single source of truth for the application's visual
language: color tokens, spacing/radius/typography scales, the UI font
resolver, and reusable QSS templates. Before this module existed, ~90
`setStyleSheet` calls and ~180 hard-coded hex colors were scattered across
``subtitle_overlay.py``, ``control_panel.py`` and ``dialogs.py``, with the
same semantic color (e.g. "success green") taking different values in
different files.

Design goals:
  - Semantic naming: refer to ``Color.SUCCESS`` / ``Color.DANGER`` instead of
    raw hex, so the same intent always renders identically.
  - Two surfaces: the control panel / dialogs use a clean LIGHT theme; the
    overlay and subtitle windows stay DARK (they float over video).
  - Cheap to consume: helpers return ready-to-use QSS strings so callers do
    ``widget.setStyleSheet(qss_primary_button())`` without re-deriving colors.

Nothing here imports heavy modules (no torch, no app code), so it is safe to
import from anywhere, including before the Qt application is constructed.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


# ---------------------------------------------------------------------------
# Color tokens
# ---------------------------------------------------------------------------
class Color:
    """Semantic color palette shared by the whole application.

    Names describe *intent*, not appearance, so a single edit here re-themes
    every consumer. Raw hex values should live only in this class.

    Visual language: modeled on Anthropic / Claude — a warm "paper" cream
    surface, a single terracotta (clay) accent for primary actions, warm
    near-black text and hairline warm-gray borders. No cold blues/teals.
    """

    # Brand / accent (terracotta clay) -- primary actions, focus rings, active.
    PRIMARY = "#c96442"
    PRIMARY_HOVER = "#b0563a"
    PRIMARY_PRESSED = "#9a4b32"
    PRIMARY_TINT = "#e8927c"  # lighter clay for outlines on dark surfaces
    ACCENT = "#d97757"
    # Soft terracotta wash used for selection highlights / accent chips.
    ACCENT_WASH = "#f5e6df"

    # Paper-collage secondary accent (olive sage) -- nav selection, paper chips.
    # Introduced with the Paper Collage art pack. Warm earth tone that pairs
    # with the terracotta primary; never a cold blue/teal (see hard rule).
    OLIVE = "#687252"
    OLIVE_HOVER = "#596345"
    OLIVE_PRESSED = "#4c553c"
    OLIVE_SOFT = "#d7ddc7"  # hover wash for paper nav rows
    # Extra paper palette neutrals for decorative use.
    SAND = "#d7b98e"
    LAVENDER = "#9e93d4"

    # Status colors (warmed to fit the paper palette: sage green / brick red).
    SUCCESS = "#5e7a52"
    SUCCESS_TEXT = "#4f6a44"
    DANGER = "#b04b38"
    DANGER_HOVER = "#c4543f"
    DANGER_TEXT = "#a3422f"
    WARNING = "#b07a3c"
    WARNING_TINT = "#d99a4e"

    # Light surface (control panel, dialogs) -- warm cream "paper".
    BG = "#f0eee6"
    SURFACE = "#fcfbf7"
    SURFACE_ALT = "#f4f2ea"
    SURFACE_INPUT = "#ffffff"
    BORDER = "#e3dfd3"
    BORDER_STRONG = "#d8d3c5"
    BORDER_SOFT = "#ece8dd"

    # Light-surface text (warm near-black -> warm grays).
    TEXT = "#2b2a27"
    TEXT_STRONG = "#1a1915"
    TEXT_MUTED = "#85817a"
    TEXT_SUBTLE = "#53514c"
    TEXT_FAINT = "#a8a49c"

    # Info banner (soft clay wash).
    INFO_BG = "#f5ece4"
    INFO_TEXT = "#8a5230"
    # Success banner background (soft sage wash).
    SUCCESS_BG = "#eef1e8"

    # Dark surface (overlay, subtitle window, log/console views) -- warm
    # charcoal instead of cold black, same terracotta accent.
    DARK_BG = "#1a1916"
    DARK_PANEL = "#262420"
    DARK_HEADER = "#211f1b"
    DARK_CONSOLE = "#211f1b"
    DARK_CONSOLE_TEXT = "#e8e3d8"
    DARK_TEXT = "#ece7dc"
    DARK_TEXT_MUTED = "#a8a399"
    DARK_TEXT_FAINT = "#7a756b"
    DARK_BORDER = "#3a3833"

    # Common neutrals.
    WHITE = "#ffffff"
    BLACK = "#000000"

    # Disabled (warm gray).
    DISABLED_BG = "#e0dbcf"
    DISABLED_TEXT = "#a8a49c"

    # Scrollbar (light surface).
    SCROLL_HANDLE = "#cfc9bc"
    SCROLL_HANDLE_HOVER = "#b0a99a"


# ---------------------------------------------------------------------------
# Spacing / radius / typography scales
# ---------------------------------------------------------------------------
class Radius:
    SM = 6
    MD = 8
    LG = 10
    XL = 14
    PILL = 999


class Space:
    """4px-based spacing scale."""

    XS = 4
    SM = 6
    MD = 8
    LG = 12
    XL = 16
    XXL = 22


class FontSize:
    XS = 8
    SM = 9
    BASE = 11
    MD = 13
    LG = 14
    XL = 18
    HERO = 30


# Monospace stack for log / console / metrics readouts.
MONO_FONT = "Consolas, 'Cascadia Mono', 'Courier New', monospace"


# ---------------------------------------------------------------------------
# UI font resolution (previously duplicated in control_panel + subtitle_overlay)
# ---------------------------------------------------------------------------
_PREFERRED_UI_FONTS = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "SimHei",
    "SimSun",
    "Arial Unicode MS",
)

_FALLBACK_FONT_FILES = (
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/simsun.ttc"),
)


@lru_cache(maxsize=1)
def ui_font_family() -> str:
    """Return the best available CJK-capable UI font family.

    Resolved once and cached. Falls back to registering a Windows font file
    for headless/offscreen Qt runs that do not enumerate system fonts, and
    finally to "Segoe UI" when nothing else is found.
    """
    # Imported lazily so this module stays importable without a QApplication.
    from PyQt6.QtGui import QFontDatabase

    families = set(QFontDatabase.families())
    for name in _PREFERRED_UI_FONTS:
        if name in families:
            return name

    for font_path in _FALLBACK_FONT_FILES:
        if not font_path.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            loaded = QFontDatabase.applicationFontFamilies(font_id)
            if loaded:
                return loaded[0]
    return "Segoe UI"


# ---------------------------------------------------------------------------
# Reusable QSS templates
# ---------------------------------------------------------------------------
def panel_stylesheet(font_family: str | None = None) -> str:
    """Application-level QSS for the light control-panel / settings surface.

    Replaces the long inline block previously living in
    ``ControlPanel._apply_easy_mode_style``.
    """
    font = font_family or ui_font_family()
    return f"""
        QWidget {{
            background: {Color.BG};
            color: {Color.TEXT};
            font-family: "{font}", "Microsoft YaHei UI", "Microsoft YaHei", "SimHei";
        }}
        QTabWidget::pane {{
            border: 1px solid {Color.BORDER};
            border-radius: {Radius.LG}px;
            background: {Color.SURFACE};
            top: -1px;
        }}
        QTabBar::tab {{
            background: transparent;
            color: {Color.TEXT_MUTED};
            padding: 11px 18px;
            margin-right: {Space.SM}px;
            border-top-left-radius: {Radius.LG}px;
            border-top-right-radius: {Radius.LG}px;
            min-width: 84px;
        }}
        QTabBar::tab:selected {{
            background: {Color.SURFACE};
            color: {Color.TEXT_STRONG};
            border: 1px solid {Color.BORDER};
            border-bottom-color: {Color.SURFACE};
            font-weight: 700;
        }}
        QTabBar::tab:hover:!selected {{
            color: {Color.TEXT_STRONG};
        }}
        QGroupBox {{
            border: 1px solid {Color.BORDER_SOFT};
            border-radius: {Radius.LG}px;
            margin-top: {Space.XL}px;
            padding: 18px 18px 14px 18px;
            background: {Color.SURFACE};
            font-weight: 700;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 14px;
            padding: 2px 10px;
            background: {Color.BG};
            border-radius: {Radius.MD}px;
            color: {Color.TEXT_STRONG};
        }}
        QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
            background: {Color.SURFACE_INPUT};
            border: 1px solid {Color.BORDER_STRONG};
            border-radius: 9px;
            padding: 9px 12px;
            selection-background-color: {Color.ACCENT};
        }}
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus,
        QSpinBox:focus, QDoubleSpinBox:focus {{
            border: 1px solid {Color.ACCENT};
            background: {Color.SURFACE};
        }}
        QComboBox::drop-down {{
            border: 0;
            width: 26px;
        }}
        QComboBox QAbstractItemView {{
            background: {Color.SURFACE};
            color: {Color.TEXT};
            border: 1px solid {Color.BORDER_STRONG};
            selection-background-color: {Color.ACCENT_WASH};
            selection-color: {Color.PRIMARY};
        }}
        QPushButton {{
            background: {Color.PRIMARY};
            color: {Color.WHITE};
            border: 0;
            border-radius: 9px;
            padding: 9px 16px;
            font-weight: 700;
            min-height: 22px;
        }}
        QPushButton:hover {{
            background: {Color.PRIMARY_HOVER};
        }}
        QPushButton:pressed {{
            background: {Color.PRIMARY_PRESSED};
        }}
        QPushButton:disabled {{
            background: {Color.DISABLED_BG};
            color: {Color.DISABLED_TEXT};
        }}
        QCheckBox {{
            spacing: 8px;
        }}
        QTableWidget, QListWidget {{
            background: {Color.SURFACE};
            border: 1px solid {Color.BORDER_SOFT};
            border-radius: {Radius.LG}px;
            alternate-background-color: {Color.SURFACE_ALT};
        }}
        QHeaderView::section {{
            background: {Color.SURFACE_ALT};
            color: {Color.TEXT_SUBTLE};
            border: 0;
            padding: 8px;
            font-weight: 700;
        }}
        QScrollArea {{
            border: 0;
            background: transparent;
        }}
        QScrollBar:vertical {{
            background: transparent;
            width: 10px;
            margin: 4px 2px 4px 2px;
        }}
        QScrollBar::handle:vertical {{
            background: {Color.SCROLL_HANDLE};
            border-radius: 5px;
            min-height: 32px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {Color.SCROLL_HANDLE_HOVER};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
    """


def console_stylesheet() -> str:
    """Dark console/log view QSS (was duplicated verbatim in dialogs.py)."""
    return (
        f"background: {Color.DARK_CONSOLE};"
        f" color: {Color.DARK_CONSOLE_TEXT};"
        f" border: 1px solid {Color.DARK_BORDER};"
        " border-radius: 8px;"
    )


def info_banner_stylesheet() -> str:
    """Soft indigo info banner used for hints/intros in the control panel."""
    return (
        "padding: 10px 12px; border-radius: 8px;"
        f" background: {Color.INFO_BG}; color: {Color.INFO_TEXT};"
    )


def status_text_stylesheet(kind: str) -> str:
    """Inline color for status labels: 'success' | 'danger' | 'muted' | ''."""
    mapping = {
        "success": Color.SUCCESS_TEXT,
        "danger": Color.DANGER_TEXT,
        "muted": Color.TEXT_MUTED,
    }
    color = mapping.get(kind)
    return f"color:{color};" if color else ""


def hero_title_stylesheet(size: int = FontSize.HERO) -> str:
    """Large page/hero title on a light surface."""
    return (
        f"font-size: {size}px; font-weight: 800;"
        f" color: {Color.TEXT_STRONG}; background: transparent;"
    )


def hero_subtitle_stylesheet(size: int = FontSize.LG) -> str:
    """Supporting subtitle text under a hero title."""
    return f"font-size: {size}px; color: {Color.TEXT_SUBTLE}; background: transparent;"


def section_title_stylesheet(size: int = 22) -> str:
    """Section header on a light surface."""
    return f"font-size: {size}px; font-weight: 800; color: {Color.DARK_PANEL};"


def hint_text_stylesheet(size: int = FontSize.MD) -> str:
    """Muted helper/description text."""
    return f"color: {Color.TEXT_MUTED}; font-size: {size}px; background: transparent;"


def success_banner_stylesheet() -> str:
    """Soft green tip/success banner."""
    return (
        "padding: 12px 14px; border-radius: 10px;"
        f" background: {Color.SUCCESS_BG}; color: {Color.SUCCESS};"
        f" font-size: {FontSize.MD}px;"
    )


def secondary_button_stylesheet() -> str:
    """Quiet neutral button for non-primary actions (e.g. 'Advanced')."""
    return (
        f"QPushButton {{ background: {Color.SURFACE_ALT}; color: {Color.TEXT_SUBTLE};"
        f"  border: 1px solid {Color.BORDER}; }}"
        f"QPushButton:hover {{ background: {Color.BORDER_SOFT}; }}"
    )


def step_card_stylesheet() -> str:
    """Numbered onboarding step card (GroupBox with accent title chip)."""
    return (
        "QGroupBox {"
        f"  font-size: 16px; font-weight: 800; color: {Color.TEXT_STRONG};"
        f"  border: 1px solid {Color.BORDER_SOFT}; border-radius: {Radius.XL}px;"
        "  margin-top: 16px; padding: 18px 18px 14px 18px;"
        f"  background: {Color.SURFACE};"
        "}"
        "QGroupBox::title {"
        "  subcontrol-origin: margin; subcontrol-position: top left;"
        f"  left: 14px; padding: 2px 10px; background: {Color.ACCENT_WASH};"
        f"  color: {Color.PRIMARY}; border-radius: {Radius.MD}px;"
        "}"
    )


def status_color(ok: bool) -> str:
    """Return the unified success/danger hex for a boolean test result."""
    return Color.SUCCESS if ok else Color.DANGER


def primary_button_stylesheet(radius: int = Radius.LG, min_height: int = 0) -> str:
    """Filled terracotta primary-action button (welcome card, dialogs)."""
    mh = f"min-height: {min_height}px;" if min_height else ""
    return (
        "QPushButton {"
        f"  background: {Color.PRIMARY}; color: {Color.WHITE};"
        "  font-weight: 700; border: none;"
        f"  border-radius: {radius}px; padding: 8px 16px; {mh}"
        "}"
        f"QPushButton:hover {{ background: {Color.PRIMARY_HOVER}; }}"
        f"QPushButton:pressed {{ background: {Color.PRIMARY_PRESSED}; }}"
        f"QPushButton:disabled {{ background: {Color.DISABLED_BG};"
        f"  color: {Color.DISABLED_TEXT}; }}"
    )


def dialog_stylesheet(font_family: str | None = None) -> str:
    """Light-surface QSS for QDialog windows (setup wizard, model dialogs).

    Built on top of ``panel_stylesheet`` so dialogs share the exact visual
    language of the control panel instead of falling back to raw native Qt
    widgets (the previous "unfinished" look). Dark console/log views inside a
    dialog keep their own explicit ``console_stylesheet`` and are unaffected.
    """
    return panel_stylesheet(font_family) + f"""
        QDialog {{
            background: {Color.BG};
        }}
        QLabel {{
            background: transparent;
        }}
        QFormLayout QLabel {{
            color: {Color.TEXT_SUBTLE};
        }}
    """


def nav_sidebar_stylesheet() -> str:
    """QSS for the settings-center left navigation sidebar (QListWidget).

    Paper-collage look: a warm cream "paper" panel with rounded rows. Section
    headers are rendered as disabled items; real entries are pill-style rows
    that highlight on selection with the olive sage paper accent (hover uses a
    soft olive wash). Pairs with the SVG line icons loaded in control_panel.
    """
    return f"""
        QListWidget {{
            background: transparent;
            border: 0;
            border-radius: 0;
            outline: 0;
            padding: 0;
        }}
        QListWidget::item {{
            color: {Color.TEXT_SUBTLE};
            border-radius: 12px;
            padding: 9px 12px;
            margin: 2px 2px;
        }}
        QListWidget::item:hover {{
            background: {Color.OLIVE_SOFT};
            color: {Color.TEXT_STRONG};
        }}
        QListWidget::item:selected {{
            background: {Color.OLIVE};
            color: {Color.WHITE};
        }}
    """


def nav_section_header_stylesheet() -> str:
    """Inline style for a non-selectable section header row in the sidebar."""
    return (
        f"color: {Color.TEXT_FAINT}; font-size: {FontSize.XS + 1}px;"
        " font-weight: 700; background: transparent;"
    )


def busy_progress_stylesheet() -> str:
    """QSS for an indeterminate (busy) progress bar on a light surface."""
    return f"""
        QProgressBar {{
            background: {Color.SURFACE_ALT};
            border: 1px solid {Color.BORDER};
            border-radius: {Radius.SM}px;
            height: 8px;
            text-align: center;
        }}
        QProgressBar::chunk {{
            background: {Color.PRIMARY};
            border-radius: {Radius.SM}px;
        }}
    """


def link_button_stylesheet() -> str:
    """A quiet, text-only 'link' style button (e.g. a Show details toggle)."""
    return (
        "QPushButton {"
        f"  background: transparent; border: none; color: {Color.TEXT_MUTED};"
        "  padding: 2px 4px; text-align: left; font-weight: 400;"
        "}"
        f"QPushButton:hover {{ color: {Color.PRIMARY}; text-decoration: underline; }}"
    )


def loading_title_stylesheet() -> str:
    """Prominent status line for loading/download dialogs."""
    return (
        f"font-size: {FontSize.LG}px; font-weight: 700;"
        f" color: {Color.TEXT_STRONG}; background: transparent;"
    )


# ---------------------------------------------------------------------------
# Paper Collage art pack helpers
# ---------------------------------------------------------------------------
def paper_card_stylesheet() -> str:
    """Warm paper card used for hints / empty states / step cards.

    Apply to a ``QFrame`` whose ``objectName`` is ``paperCard``; child labels
    can use ``cardTitle`` / ``cardDescription`` object names.
    """
    return (
        "QFrame#paperCard {"
        f"  background: {Color.SURFACE};"
        f"  border: 1px solid {Color.BORDER_STRONG};"
        f"  border-radius: 12px;"
        "}"
        "QLabel#cardTitle {"
        f"  color: {Color.TEXT_STRONG}; font-size: {FontSize.LG}px; font-weight: 700;"
        "  background: transparent;"
        "}"
        "QLabel#cardDescription {"
        f"  color: {Color.TEXT_MUTED}; font-size: {FontSize.MD}px;"
        "  background: transparent;"
        "}"
    )


def step_badge_stylesheet(bg: str, diameter: int = 28) -> str:
    """Circular numbered badge for an onboarding step card header.

    ``bg`` is the fill (e.g. ``Color.OLIVE`` / ``Color.PRIMARY``); the digit is
    rendered white and bold inside a perfect circle.
    """
    return (
        "QLabel {"
        f"  background: {bg}; color: {Color.WHITE};"
        f"  border-radius: {diameter // 2}px;"
        f"  font-size: {FontSize.LG}px; font-weight: 800;"
        "}"
    )


def step_card_title_stylesheet() -> str:
    """Title text beside a step badge."""
    return (
        "font-size: 15px; font-weight: 800;"
        f" color: {Color.TEXT_STRONG}; background: transparent;"
    )


def step_card_subtitle_stylesheet() -> str:
    """Muted one-line description under a step title."""
    return (
        f"font-size: {FontSize.SM}px; color: {Color.TEXT_MUTED};"
        " background: transparent;"
    )


def hero_overlay_title_stylesheet() -> str:
    """Large centered title overlaid on the paper-collage hero banner."""
    return (
        "font-size: 27px; font-weight: 800; letter-spacing: 2px;"
        f" color: {Color.TEXT_STRONG}; background: transparent;"
    )


def hero_overlay_subtitle_stylesheet() -> str:
    """Centered supporting line under the overlaid hero title."""
    return (
        f"font-size: {FontSize.MD}px; color: {Color.TEXT_SUBTLE};"
        " background: transparent; letter-spacing: 1px;"
    )


def tips_card_title_stylesheet() -> str:
    """Title for the home 'tips' paper card."""
    return (
        f"font-size: {FontSize.MD}px; font-weight: 700;"
        f" color: {Color.TEXT_STRONG}; background: transparent;"
    )


def empty_state_title_stylesheet() -> str:
    """Title line for an illustrated empty-state placeholder."""
    return (
        f"font-size: {FontSize.LG}px; font-weight: 700;"
        f" color: {Color.TEXT_SUBTLE}; background: transparent;"
    )


def empty_state_text_stylesheet() -> str:
    """Supporting description under an empty-state title."""
    return (
        f"font-size: {FontSize.MD}px; color: {Color.TEXT_MUTED};"
        " background: transparent;"
    )


def _tinted_svg_pixmap(svg_text: str, color: str, size: int):
    """Render an SVG string (with its stroke recolored) to a square QPixmap."""
    import re

    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter, QPixmap
    from PyQt6.QtSvg import QSvgRenderer

    # The art-pack icons draw with a fixed dark stroke; swap it for the target.
    recolored = re.sub(r'stroke="#[0-9A-Fa-f]{3,6}"', f'stroke="{color}"', svg_text)
    renderer = QSvgRenderer(bytearray(recolored, encoding="utf-8"))
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return QPixmap.fromImage(image)


@lru_cache(maxsize=64)
def nav_svg_icon(svg_path: str, size: int = 22):
    """Return a two-state ``QIcon`` for a paper-collage nav SVG.

    Normal rows render the line icon in the muted text color; selected rows
    (olive background) render it white. Cached by path+size. Returns an empty
    ``QIcon`` if the file is missing or QtSvg is unavailable, so callers never
    crash on a packaging gap.
    """
    from PyQt6.QtGui import QIcon

    try:
        with open(svg_path, encoding="utf-8") as f:
            svg_text = f.read()
    except OSError:
        return QIcon()
    try:
        icon = QIcon()
        icon.addPixmap(
            _tinted_svg_pixmap(svg_text, Color.TEXT_SUBTLE, size),
            QIcon.Mode.Normal,
        )
        icon.addPixmap(
            _tinted_svg_pixmap(svg_text, Color.WHITE, size),
            QIcon.Mode.Selected,
        )
        return icon
    except Exception:
        return QIcon()
