from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from i18n import t


class SelectionResultBridge(QObject):
    result_ready = pyqtSignal(dict)
    error_ready = pyqtSignal(str)


class SelectionResultDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("sel_dialog_title"))
        self.resize(520, 420)

        layout = QVBoxLayout(self)
        self._status = QLabel("")
        layout.addWidget(self._status)

        self._content = QTextEdit()
        self._content.setReadOnly(True)
        layout.addWidget(self._content, 1)

        buttons = QHBoxLayout()
        self._copy_translation = QPushButton(t("copy_translation"))
        self._copy_all = QPushButton(t("copy_all"))
        self._close = QPushButton(t("btn_close"))
        self._copy_translation.clicked.connect(self.copy_translation)
        self._copy_all.clicked.connect(self.copy_all)
        self._close.clicked.connect(self.close)
        buttons.addWidget(self._copy_translation)
        buttons.addWidget(self._copy_all)
        buttons.addStretch(1)
        buttons.addWidget(self._close)
        layout.addLayout(buttons)

        self._last_result = {}

    def show_loading(self, text: str):
        self._last_result = {"original": text}
        self._status.setText(t("sel_loading"))
        self._content.setPlainText(text)
        self.show()
        self.raise_()
        self.activateWindow()

    def show_error(self, message: str):
        self._status.setText(t("sel_error"))
        self._content.setPlainText(message)
        self.show()
        self.raise_()
        self.activateWindow()

    def show_result(self, result: dict):
        self._last_result = result or {}
        self._status.setText(t("sel_done"))
        self._content.setPlainText(self._format_result(self._last_result))
        self.show()
        self.raise_()
        self.activateWindow()

    def copy_translation(self):
        text = self._last_result.get("translation") or ""
        if text:
            QGuiApplication.clipboard().setText(text)

    def copy_all(self):
        text = self._content.toPlainText()
        if text:
            QGuiApplication.clipboard().setText(text)

    @staticmethod
    def _format_result(result: dict) -> str:
        sections = [
            (t("history_original"), result.get("original")),
            (t("history_translation"), result.get("translation")),
            (t("history_explanation"), result.get("explanation")),
            (t("history_polished"), result.get("polished")),
        ]
        return "\n\n".join(f"{label}:\n{value}" for label, value in sections if value)
