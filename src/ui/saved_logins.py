"""Saved Logins dialog: the sites `type_login` holds a login for, with their usernames; passwords are never shown.
Forget takes a second click on the same site, since a forgotten login cannot be brought back."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout

from src.tools import login_vault
from src.ui.theme import (ACCENT_HEX, APP_STYLESHEET, FONT_FAMILY, GLASS_RAISED_RGBA, GLASS_RGBA, HOVER_RGBA,
                          OUTLINE_RGBA, TEXT_HEX, rgba_css)

EMPTY_NOTE = "No saved logins yet. When the AI needs to sign in, it asks you on a card in the chat."


class SavedLoginsDialog(QDialog):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Paperduck - Saved Logins")
        self.setMinimumWidth(460)
        self.setStyleSheet(APP_STYLESHEET + f"""
            QDialog {{ background-color: {rgba_css(GLASS_RGBA)}; color: {TEXT_HEX}; font-family: "{FONT_FAMILY}"; }}
            QLabel {{ color: {TEXT_HEX}; background: transparent; }}
            QListWidget {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX};
                border: 1px solid {rgba_css(OUTLINE_RGBA)}; border-radius: 6px; padding: 4px; }}
            QListWidget::item {{ padding: 6px 4px; }}
            QListWidget::item:selected {{ background: {rgba_css(HOVER_RGBA)}; color: {ACCENT_HEX}; }}
            QPushButton {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX};
                border: 1px solid {rgba_css(OUTLINE_RGBA)}; border-radius: 6px; padding: 6px 14px; font-weight: 600; }}
            QPushButton:hover {{ background: {rgba_css(HOVER_RGBA)}; color: {ACCENT_HEX}; border-color: {ACCENT_HEX}; }}
            QPushButton:disabled {{ color: {rgba_css(OUTLINE_RGBA)}; }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)
        self.note = QLabel("Logins the AI can type for you, each only into its own https site. They are encrypted "
                           "with your Windows account, and the AI never sees them.")
        self.note.setWordWrap(True)
        root.addWidget(self.note)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda *_: self._arm(""))
        root.addWidget(self.list)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        row = QHBoxLayout()
        self.forget_btn = QPushButton("Forget")
        self.forget_btn.clicked.connect(self._forget)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(self.forget_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        root.addLayout(row)
        self._armed = ""
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        self._arm("")
        try:
            logins = login_vault.load()
        except OSError as exc:
            self.status.setText(f"Could not open the saved logins: {exc}.")
            self.forget_btn.setEnabled(False)
            return
        for host in sorted(logins):
            user = logins[host].get("username") or "(no username)"
            item = QListWidgetItem(f"{host}    {user}")
            item.setData(Qt.ItemDataRole.UserRole, host)
            self.list.addItem(item)
        self.status.setText("" if logins else EMPTY_NOTE)
        self.forget_btn.setEnabled(bool(logins))
        if logins:
            self.list.setCurrentRow(0)

    def _arm(self, host: str) -> None:
        self._armed = host
        self.forget_btn.setText(f"Click again to forget {host}" if host else "Forget")

    def _forget(self) -> None:
        item = self.list.currentItem()
        host = item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
        if not host:
            return
        if self._armed != host:  # first click only arms it
            self._arm(host)
            return
        try:
            login_vault.forget(host)
        except OSError as exc:
            self.status.setText(f"Could not forget it: {exc}.")
            return
        self.reload()
        self.status.setText(f"Forgot the login for {host}." if self.list.count() else EMPTY_NOTE)
