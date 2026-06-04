from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

from PyQt6.QtCore import QObject, pyqtSignal

from .hotkey_parser import parse_hotkey

log = logging.getLogger("LiveTranslate.Hotkey")

MOD_SHIFT = 0x0004
MOD_CONTROL = 0x0002
VK_T = 0x54
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class GlobalHotkeyManager(QObject):
    triggered = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, hotkey: str = "Ctrl+Shift+T", hotkey_id: int = 0x4C54, parent=None):
        super().__init__(parent)
        self._hotkey_id = hotkey_id
        self._hotkey = hotkey
        self._modifiers, self._vk, self._label = parse_hotkey(hotkey)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._running = False

    @property
    def hotkey(self) -> str:
        return self._label

    def set_hotkey(self, hotkey: str):
        modifiers, vk, label = parse_hotkey(hotkey)
        was_running = self._thread is not None and self._thread.is_alive()
        if was_running:
            self.stop()
        self._hotkey = hotkey
        self._modifiers = modifiers
        self._vk = vk
        self._label = label
        if was_running:
            self.start()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="LiveTranslateHotkey",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        self._thread_id = 0

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()

        if not user32.RegisterHotKey(None, self._hotkey_id, self._modifiers, self._vk):
            message = f"注册全局快捷键 {self._label} 失败，可能已被其他程序占用"
            log.warning(message)
            self.failed.emit(message)
            return

        log.info("Global hotkey registered: %s", self._label)
        msg = MSG()
        try:
            while self._running:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                    self.triggered.emit()
        finally:
            user32.UnregisterHotKey(None, self._hotkey_id)
            log.info("Global hotkey unregistered")
