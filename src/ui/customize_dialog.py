"""Unified Customize dialog for Wheel slices, Right-Click Menu, Hotkeys, and Macros."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui.menu_layout import HIDEABLE_SECTIONS, PINNABLE_ACTIONS
from src.ui.quick_tools import HOTKEY_ACTIONS, default_hotkey
from src.ui.theme import (
    ACCENT_HEX,
    FONT_FAMILY,
    GLASS_RAISED_RGBA,
    GLASS_RGBA,
    ON_ACCENT_HEX,
    OUTLINE_RGBA,
    TEXT_DIM_HEX,
    TEXT_HEX,
    rgba_css,
    ui_font,
)
from src.ui.tool_wheel import MAX_SLICES


class CustomizeDialog(QDialog):
    """Tabbed settings dialog to customize the Tool Wheel, Menu, Hotkeys, and Macros."""

    def __init__(self, win: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._win = win
        self.setWindowTitle("Paperduck - Customize")
        self.setMinimumSize(540, 480)
        self.setStyleSheet(
            f"QDialog {{ background: {rgba_css(GLASS_RGBA)}; color: {TEXT_HEX}; font-family: \"{FONT_FAMILY}\"; }}"
            f"QLabel {{ color: {TEXT_HEX}; background: transparent; }}"
            f"QTabWidget::pane {{ border: 1px solid {rgba_css(OUTLINE_RGBA)}; border-radius: 8px; padding: 6px; }}"
            f"QTabBar::tab {{ background: transparent; color: {TEXT_DIM_HEX}; padding: 6px 12px; }}"
            f"QTabBar::tab:selected {{ color: {ACCENT_HEX}; border-bottom: 2px solid {ACCENT_HEX}; }}"
            f"QScrollArea {{ background: {rgba_css(GLASS_RAISED_RGBA)}; border: 1px solid {rgba_css(OUTLINE_RGBA)}; border-radius: 6px; }}"
            f"QScrollArea > QWidget > QWidget {{ background: transparent; }}"
            f"QCheckBox {{ color: {TEXT_HEX}; background: transparent; spacing: 8px; padding: 3px 0; }}"
            f"QListWidget, QComboBox {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX}; "
            f"border: 1px solid {rgba_css(OUTLINE_RGBA)}; border-radius: 6px; padding: 4px; }}"
            f"QComboBox QAbstractItemView {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX}; }}"
            f"QPushButton {{ background: transparent; color: {TEXT_HEX}; border: 1px solid {rgba_css(OUTLINE_RGBA)}; "
            f"border-radius: 6px; padding: 5px 12px; }}"
            f"QPushButton:hover {{ border-color: {ACCENT_HEX}; color: {ACCENT_HEX}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._build_wheel_tab(), "Wheel")
        self._tabs.addTab(self._build_menu_tab(), "Menu")
        self._tabs.addTab(self._build_shortcuts_tab(), "Shortcuts && Macros")
        root.addWidget(self._tabs, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("Close", self)
        close_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT_HEX}; color: {ON_ACCENT_HEX}; border: none; font-weight: 600; }}"
        )
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)

    # ------------------------------------------------------------------ Wheel

    def _build_wheel_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setSpacing(8)

        self._wheel_count = QLabel("", page)
        self._wheel_count.setFont(ui_font(9, bold=True))
        self._wheel_count.setStyleSheet(f"color: {ACCENT_HEX};")
        lay.addWidget(self._wheel_count)

        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.viewport().setAutoFillBackground(False)
        self._wheel_box = QWidget(scroll)
        self._wheel_box.setAutoFillBackground(False)
        self._wheel_box.setStyleSheet("background: transparent;")
        self._wheel_list_lay = QVBoxLayout(self._wheel_box)
        self._wheel_list_lay.setContentsMargins(8, 6, 8, 6)
        self._wheel_list_lay.setSpacing(4)
        scroll.setWidget(self._wheel_box)
        lay.addWidget(scroll, 1)

        btns = QHBoxLayout()
        for label, key in (("Add Website...", "add_web"), ("Add App/File...", "add_app")):
            b = QPushButton(label, page)
            b.clicked.connect(lambda _=False, k=key: self._wheel_action(k))
            btns.addWidget(b)
        self._remove_custom_btn = QPushButton("Remove Custom...", page)
        self._remove_custom_btn.clicked.connect(self._remove_selected_custom)
        btns.addWidget(self._remove_custom_btn)
        btns.addStretch(1)
        lay.addLayout(btns)

        self._refresh_wheel_tab()
        return page

    def _refresh_wheel_tab(self) -> None:
        while self._wheel_list_lay.count():
            item = self._wheel_list_lay.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        base = self._win.all_tool_wheel_options()
        catalog = self._win.wheel.catalog(base)
        visible_n = sum(1 for _o, on in catalog if on)
        self._wheel_count.setText(f"Visible slices: {visible_n} / {MAX_SLICES}")
        self._remove_custom_btn.setEnabled(bool(self._win.wheel.custom))

        for opt, on in catalog:
            cb = QCheckBox(f"{opt.label}  ({opt.subtitle})".replace("&", "&&"), self._wheel_box)
            cb.setChecked(on)
            cb.toggled.connect(lambda _chk, cid=opt.callback_id, box=cb: self._toggle_wheel(cid, box))
            self._wheel_list_lay.addWidget(cb)
        self._wheel_list_lay.addStretch(1)

    def _toggle_wheel(self, cid: str, box: QCheckBox) -> None:
        base = self._win.all_tool_wheel_options()
        res = self._win.wheel.toggle(cid, base)
        if res in ("WHEEL FULL", "KEEP ONE", "GONE"):
            box.blockSignals(True)
            box.setChecked(not box.isChecked())
            box.blockSignals(False)
            self._win._show_toast(res)
        self._refresh_wheel_count()

    def _refresh_wheel_count(self) -> None:
        catalog = self._win.wheel.catalog(self._win.all_tool_wheel_options())
        visible_n = sum(1 for _o, on in catalog if on)
        self._wheel_count.setText(f"Visible slices: {visible_n} / {MAX_SLICES}")

    def _wheel_action(self, key: str) -> None:
        self._win.trigger_tool_action(f"wheel:{key}")
        self._refresh_wheel_tab()

    def _remove_selected_custom(self) -> None:
        if not self._win.wheel.custom:
            return
        self._win.wheel.remove_custom(len(self._win.wheel.custom) - 1)
        self._refresh_wheel_tab()

    # ------------------------------------------------------------------ Menu

    def _build_menu_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QHBoxLayout(page)
        lay.setSpacing(12)

        left = QVBoxLayout()
        pin_lbl = QLabel("Pinned at Top", page)
        pin_lbl.setFont(ui_font(9.5, bold=True))
        left.addWidget(pin_lbl)
        pin_scroll = QScrollArea(page)
        pin_scroll.setWidgetResizable(True)
        pin_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        pin_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        pin_scroll.viewport().setAutoFillBackground(False)
        pin_box = QWidget(pin_scroll)
        pin_box.setAutoFillBackground(False)
        pin_box.setStyleSheet("background: transparent;")
        pin_lay = QVBoxLayout(pin_box)
        pin_lay.setContentsMargins(8, 6, 8, 6)
        for label, cid in PINNABLE_ACTIONS:
            cb = QCheckBox(label.replace("&", "&&"), pin_box)
            cb.setChecked(self._win.menu_layout.is_pinned(cid))
            cb.toggled.connect(lambda chk, c=cid: self._win.menu_layout.set_pinned(c, chk))
            pin_lay.addWidget(cb)
        pin_lay.addStretch(1)
        pin_scroll.setWidget(pin_box)
        left.addWidget(pin_scroll, 1)

        right = QVBoxLayout()
        sec_lbl = QLabel("Show in Main Menu (unchecked -> More Tools)", page)
        sec_lbl.setFont(ui_font(9.5, bold=True))
        right.addWidget(sec_lbl)
        sec_scroll = QScrollArea(page)
        sec_scroll.setWidgetResizable(True)
        sec_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sec_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        sec_scroll.viewport().setAutoFillBackground(False)
        sec_box = QWidget(sec_scroll)
        sec_box.setAutoFillBackground(False)
        sec_box.setStyleSheet("background: transparent;")
        sec_lay = QVBoxLayout(sec_box)
        sec_lay.setContentsMargins(8, 6, 8, 6)
        for sec in HIDEABLE_SECTIONS:
            cb = QCheckBox(sec.replace("&", "&&"), sec_box)
            cb.setChecked(not self._win.menu_layout.is_hidden(sec))
            cb.toggled.connect(lambda chk, s=sec: self._win.menu_layout.set_hidden(s, not chk))
            sec_lay.addWidget(cb)
        sec_lay.addStretch(1)
        sec_scroll.setWidget(sec_box)
        right.addWidget(sec_scroll, 1)

        lay.addLayout(left, 1)
        lay.addLayout(right, 1)
        return page

    # ------------------------------------------------------------------ Shortcuts & Macros

    def _build_shortcuts_tab(self) -> QWidget:
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setSpacing(10)

        hk_lbl = QLabel("Global Hotkeys", page)
        hk_lbl.setFont(ui_font(9.5, bold=True))
        lay.addWidget(hk_lbl)

        self._hk_list = QListWidget(page)
        self._hk_list.setMaximumHeight(140)
        lay.addWidget(self._hk_list)

        hk_btns = QHBoxLayout()
        change_hk = QPushButton("Change Hotkey...", page)
        change_hk.clicked.connect(self._edit_hotkey)
        reset_hk = QPushButton("Reset Defaults", page)
        reset_hk.clicked.connect(self._reset_hotkeys)
        hk_btns.addWidget(change_hk)
        hk_btns.addWidget(reset_hk)
        hk_btns.addStretch(1)
        lay.addLayout(hk_btns)

        m_lbl = QLabel("Macros", page)
        m_lbl.setFont(ui_font(9.5, bold=True))
        lay.addWidget(m_lbl)

        self._macro_list = QListWidget(page)
        self._macro_list.setMaximumHeight(110)
        lay.addWidget(self._macro_list)

        m_btns = QHBoxLayout()
        new_m = QPushButton("New...", page)
        new_m.clicked.connect(lambda: self._macro_action("macros:new"))
        edit_m = QPushButton("Edit...", page)
        edit_m.clicked.connect(self._edit_macro)
        del_m = QPushButton("Remove", page)
        del_m.clicked.connect(self._remove_macro)
        m_btns.addWidget(new_m)
        m_btns.addWidget(edit_m)
        m_btns.addWidget(del_m)
        m_btns.addWidget(QLabel("Double-Click:", page))
        self._dbl_combo = QComboBox(page)
        self._dbl_combo.currentIndexChanged.connect(self._on_dbl_changed)
        m_btns.addWidget(self._dbl_combo, 1)
        lay.addLayout(m_btns)

        self._refresh_shortcuts_tab()
        return page

    def _refresh_shortcuts_tab(self) -> None:
        self._hk_list.clear()
        self._hk_modes = list(HOTKEY_ACTIONS.keys())
        for mode, label in HOTKEY_ACTIONS.items():
            combo = self._win.hotkeys.get(mode) or default_hotkey(mode)
            self._hk_list.addItem(f"{label}  -  {combo}")

        self._macro_list.clear()
        for m in self._win.macros.macros:
            self._macro_list.addItem(f"{m.name}  ({m.recipe})")

        self._dbl_combo.blockSignals(True)
        self._dbl_combo.clear()
        self._dbl_combo.addItem("Snip (default)", "")
        for m in self._win.macros.macros:
            self._dbl_combo.addItem(m.name, m.name)
        idx = max(0, self._dbl_combo.findData(self._win.macros.double_click))
        self._dbl_combo.setCurrentIndex(idx)
        self._dbl_combo.blockSignals(False)

    def _edit_hotkey(self) -> None:
        row = self._hk_list.currentRow()
        if 0 <= row < len(self._hk_modes):
            self._win.trigger_tool_action(f"hotkey:{self._hk_modes[row]}")
            self._refresh_shortcuts_tab()

    def _reset_hotkeys(self) -> None:
        self._win.hotkeys.clear()
        if callable(self._win.on_hotkeys_changed):
            self._win.on_hotkeys_changed()
        self._refresh_shortcuts_tab()

    def _macro_action(self, cid: str) -> None:
        self._win.trigger_tool_action(cid)
        self._refresh_shortcuts_tab()

    def _edit_macro(self) -> None:
        row = self._macro_list.currentRow()
        if 0 <= row < len(self._win.macros.macros):
            self._macro_action(f"macros:edit:{self._win.macros.macros[row].name}")

    def _remove_macro(self) -> None:
        row = self._macro_list.currentRow()
        if 0 <= row < len(self._win.macros.macros):
            self._macro_action(f"macros:remove:{self._win.macros.macros[row].name}")

    def _on_dbl_changed(self) -> None:
        name = self._dbl_combo.currentData() or ""
        self._win.macros.double_click = name
