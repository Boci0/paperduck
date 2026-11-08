"""Macro editor: name, a recipe built by tapping step chips (or typing), and a live plain-English preview."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout, QWidget)

from src.tools.macros import MAX_NAME, MAX_RECIPE, STEPS, describe, parse_recipe
from src.ui.chat_widgets import _BUTTON_CSS, _FIELD_CSS, _Frosted
from src.ui.quick_tools import HOTKEY_LABEL
from src.ui.theme import ACCENT_HEX, DANGER_HEX, TEXT_DIM_HEX, TEXT_HEX, ui_font

# Chips insert these; steps with an argument get a starter the user then edits
_CHIP_TEXT = {"text": "text:Explain this", "open": "open:https://www.youtube.com/results?search_query={text}",
              "tool": "tool:pomodoro:toggle"}


class MacroEditor(QDialog):
    def __init__(self, name: str = "", recipe: str = "", double_click: bool = False, hotkey: int = 0,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("Paperduck - Macro")
        self.setFixedWidth(400)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = _Frosted()
        root.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        title = QLabel("Edit Macro" if name else "New Macro")
        title.setFont(ui_font(10.5, bold=True))
        title.setStyleSheet(f"color: {TEXT_HEX};")
        lay.addWidget(title)

        self.name = QLineEdit(name)
        self.name.setPlaceholderText("Name (shown on the wheel)")
        self.name.setMaxLength(MAX_NAME)
        self.recipe = QLineEdit(recipe)
        self.recipe.setPlaceholderText("region > text:Explain this > ask")
        self.recipe.setMaxLength(MAX_RECIPE)
        for field in (self.name, self.recipe):
            field.setStyleSheet(_FIELD_CSS)
            field.setFont(ui_font(9.5))
            lay.addWidget(field)

        # Tapping a chip appends that step, so a macro needs no typing at all
        grid = QGridLayout()
        grid.setSpacing(4)
        for i, step in enumerate(STEPS):
            chip = QPushButton(step)
            chip.setStyleSheet(_BUTTON_CSS.replace("padding: 4px 12px", "padding: 2px 6px"))
            chip.setFont(ui_font(8.5))
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip(STEPS[step][1].format(arg="...").capitalize())
            chip.clicked.connect(lambda _c=False, s=step: self._append(_CHIP_TEXT.get(s, s)))
            grid.addWidget(chip, i // 6, i % 6)
        lay.addLayout(grid)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setFont(ui_font(9))
        lay.addWidget(self.preview)

        self.double_click = QCheckBox("Run when I double-click the pet")
        self.double_click.setChecked(double_click)
        self.double_click.setStyleSheet(f"color: {TEXT_HEX};")
        lay.addWidget(self.double_click)
        if hotkey:
            hint = QLabel(f"Hotkey: {HOTKEY_LABEL}+{hotkey}   ·   also on the wheel and right-click menu")
            hint.setFont(ui_font(8.5))
            hint.setStyleSheet(f"color: {TEXT_DIM_HEX};")
            lay.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        self.save = QPushButton("Save")
        for btn in (cancel, self.save):
            btn.setStyleSheet(_BUTTON_CSS + f" QPushButton:disabled {{ color: {TEXT_DIM_HEX}; }}")
            buttons.addWidget(btn)
        cancel.clicked.connect(self.reject)
        self.save.clicked.connect(self.accept)
        self.save.setDefault(True)
        lay.addLayout(buttons)

        self.name.textChanged.connect(self._refresh)
        self.recipe.textChanged.connect(self._refresh)
        self._refresh()
        (self.recipe if name else self.name).setFocus()

    def _append(self, step: str) -> None:
        current = self.recipe.text().strip()
        self.recipe.setText(f"{current} > {step}" if current else step)
        self.recipe.setFocus()

    def _refresh(self) -> None:
        try:
            parse_recipe(self.recipe.text())
            ok = True
        except ValueError:
            ok = False
        text = describe(self.recipe.text()) if self.recipe.text().strip() else "Tap steps to build it"
        self.preview.setText(text[:1].upper() + text[1:])
        self.preview.setStyleSheet(f"color: {ACCENT_HEX if ok else DANGER_HEX if self.recipe.text().strip() else TEXT_DIM_HEX};")
        self.save.setEnabled(ok and bool(self.name.text().strip()))

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.buttons() & Qt.MouseButton.LeftButton and hasattr(self, "_drag"):
            self.move(event.globalPosition().toPoint() - self._drag)
