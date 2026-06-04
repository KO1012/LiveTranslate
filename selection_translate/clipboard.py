from __future__ import annotations

import ctypes

from PyQt6.QtCore import QEventLoop, QMimeData, QTimer
from PyQt6.QtGui import QGuiApplication

KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_C = 0x43


def clone_mime_data(source: QMimeData | None) -> QMimeData:
    cloned = QMimeData()
    if source is None:
        return cloned
    for fmt in source.formats():
        cloned.setData(fmt, source.data(fmt))
    return cloned


def wait_ms(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def simulate_ctrl_c():
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_C, 0, 0, 0)
    user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def copy_selected_text(timeout_ms: int = 180) -> tuple[str, str | None]:
    clipboard = QGuiApplication.clipboard()
    original = clone_mime_data(clipboard.mimeData())

    try:
        clipboard.clear()
        wait_ms(30)
        simulate_ctrl_c()
        wait_ms(timeout_ms)
        text = clipboard.text().strip()
        if not text:
            return "", "没有读取到选中文本，请先选中文字后再按快捷键"
        return text, None
    finally:
        clipboard.setMimeData(original)
