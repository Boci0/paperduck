"""Folded-paper right-click context menu for Paperduck Multi-Tool."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QAction, QContextMenuEvent, QIcon
from PyQt6.QtWidgets import (QAbstractScrollArea, QApplication, QLabel, QLineEdit, QMenu, QPlainTextEdit, QTextEdit,
                             QWidget, QWidgetAction)

from src.engine.models import PetState
from src.engine.pet_state import PetStateMachine
from src.storage.recent_files import format_age
from src import __version__
from src.storage.storage_manager import autostart_enabled, set_autostart
from src.ui.menu_layout import MenuLayout
from src.ui.quick_tools import HOTKEY_LABEL
from src.ui.theme import (
    ACCENT_HEX,
    DANGER_HEX,
    FONT_FAMILY,
    HAIRLINE_RGBA,
    HOVER_RGBA,
    SHADOW,
    SLEEP_HEX,
    TEXT_DIM_HEX,
    TEXT_HEX,
    THEME_CHOICES,
    install_panel_frame,
    rgba_css,
)

# Padding holds the panel's soft shadow (SHADOW) plus 6px of paper around the rows
MENU_STYLESHEET = f"""
QMenu {{
    background: transparent;
    color: {TEXT_HEX};
    border: none;
    padding: {6 + SHADOW // 2}px {6 + SHADOW}px {6 + SHADOW * 3 // 2}px {6 + SHADOW}px;
    font-family: "{FONT_FAMILY}";
    font-size: 10pt;
}}
QMenu::item {{
    padding: 5px 30px 5px 12px;
    margin: 0;
    border-radius: 6px;
    background: transparent;
}}
QMenu::item:selected {{
    background-color: {rgba_css(HOVER_RGBA)};
    color: {ACCENT_HEX};
}}
QMenu::item:disabled {{
    color: {TEXT_DIM_HEX};
}}
QMenu::separator {{
    height: 1px;
    background: {rgba_css(HAIRLINE_RGBA)};
    margin: 5px 10px;
}}
QMenu::indicator {{
    width: 14px;
    height: 14px;
    margin-left: 6px;
}}
QMenu::right-arrow {{
    width: 8px;
    height: 8px;
    margin-right: 10px;
}}
"""

# Plugin submenus grouped apart from the desk tools: the AI ones get their own block, the timer one joins the timer
AI_SUBMENUS = frozenset({"Hints", "AI Assist", "Errands"})
TIMER_SUBMENUS = frozenset({"Pomodoro Length"})

QUICK_TOOL_ITEMS: tuple[tuple[str, str], ...] = (
    (f"Snip Region -> Copy Image	{HOTKEY_LABEL}+S", "snip:image"),
    (f"Snip Region -> Copy Text (OCR)	{HOTKEY_LABEL}+T", "snip:text"),
    (f"Pick Screen Color -> Copy HEX	{HOTKEY_LABEL}+C", "snip:color"),
)

_STATE_LABELS: dict[PetState, tuple[str, str]] = {
    PetState.IDLE: ("Online", ACCENT_HEX),
    PetState.PLAYING: ("Focus", ACCENT_HEX),
    PetState.HUNGRY: ("Alert", DANGER_HEX),
    PetState.SLEEPING: ("Do Not Disturb", SLEEP_HEX),
}


def style_menu(menu: QMenu) -> QMenu:
    """Apply the frameless folded-paper look (rounded corners and the shadow need a clear backdrop)."""
    menu.setWindowFlags(
        menu.windowFlags()
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.NoDropShadowWindowHint
    )
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    menu.setStyleSheet(MENU_STYLESHEET)
    install_panel_frame(menu)
    return menu


class _TextBoxMenus(QObject):
    """Right-click menus of every text box (line edits, the chat draft, chat bubbles) in the folded-paper look.
    Qt's own menu took the dark box styling and Windows' black theme icons, so its items were unreadable."""

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if not isinstance(event, QContextMenuEvent) or event.type() != QEvent.Type.ContextMenu:
            return False
        box = obj.parent() if isinstance(obj, QWidget) and isinstance(obj.parent(), QAbstractScrollArea) else obj
        if isinstance(box, QAbstractScrollArea) and obj is not box.viewport():
            return False
        if not isinstance(box, (QLineEdit, QTextEdit, QPlainTextEdit)):
            return False
        menu = box.createStandardContextMenu()
        if menu is None:
            return False
        for action in menu.actions():
            action.setIcon(QIcon())  # black theme icons vanish on the dark panel; the other menus have none
        style_menu(menu).exec(event.globalPos())
        menu.deleteLater()
        return True


def style_text_box_menus(app: QApplication) -> None:
    """Gives every text box's right-click menu the pet's menu look (call once, after QApplication exists)."""
    app.installEventFilter(_TextBoxMenus(app))


class PetContextMenu(QMenu):
    """Context menu providing multi-tool actions, HUD switcher, and window toggles."""

    def __init__(
        self,
        *,
        state_machine: PetStateMachine,
        always_on_top: bool,
        auto_wander: bool,
        on_feed: Callable[[], object],
        on_play: Callable[[], object],
        on_toggle_sleep: Callable[[], object],
        on_reset_position: Callable[[], object],
        on_toggle_on_top: Callable[[bool], object],
        on_toggle_wander: Callable[[bool], object],
        on_quit: Callable[[], object],
        on_pet: Callable[[], object] | None = None,
        on_select_tool: Callable[[str], object] | None = None,
        on_tool_action: Callable[[str], object] | None = None,
        recent_files: Sequence[tuple[Path, float]] = (),
        wheel_slices: Sequence[tuple[str, str, bool]] = (),
        wheel_custom: Sequence[str] = (),
        macros: Sequence[str] = (),
        double_click_macro: str = "",
        pet_scale: int = 4,
        hotkeys: Sequence[tuple[str, str, str]] = (),
        pet_scales: dict[int, str] | None = None,
        theme: str = "system",
        phone_bridge_on: bool | None = None,
        menu_layout: MenuLayout | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        style_menu(self)

        stats = state_machine.stats
        state = state_machine.state
        state_text, state_color = _STATE_LABELS.get(state, (state.value.title(), TEXT_HEX))

        # Header card: name + status dot on line one, live telemetry on line two
        header = QLabel(
            f'<div style="font-size:10.5pt; font-weight:600; color:{TEXT_HEX};">Paperduck'
            f'&nbsp;&nbsp;<span style="color:{state_color}; font-size:9pt;">&#9679; {state_text}</span></div>'
            f'<div style="font-size:8.5pt; color:{TEXT_DIM_HEX};">'
            f"CPU {int(round(stats.cpu_usage))}%&nbsp;&nbsp;&middot;&nbsp;&nbsp;"
            f"RAM {int(round(stats.ram_usage))}%&nbsp;&nbsp;&middot;&nbsp;&nbsp;"
            f"Disk {int(round(stats.disk_usage))}%</div>"
        )
        header.setStyleSheet(f'background: transparent; padding: 6px 12px 4px 12px; font-family: "{FONT_FAMILY}";')
        self.header_action = QWidgetAction(self)
        self.header_action.setDefaultWidget(header)
        self.header_action.setEnabled(False)
        self.addAction(self.header_action)
        self.addSeparator()

        def connect(act: QAction, cid: str) -> None:
            if on_tool_action is not None:
                act.triggered.connect(lambda _checked=False, c=cid: on_tool_action(c))
            else:
                act.triggered.connect(
                    lambda _checked=False, c=cid: state_machine.execute_tool_action(c)
                )

        layout = menu_layout or MenuLayout()
        more_menu_holder: list[QMenu] = []

        def more_menu() -> QMenu:
            if not more_menu_holder:
                more_menu_holder.append(style_menu(QMenu("More Tools", self)))
            return more_menu_holder[0]

        def add_section_menu(section_name: str, menu: QMenu) -> None:
            if layout.is_hidden(section_name):
                more_menu().addMenu(menu)
            else:
                self.addMenu(menu)

        self.plugin_actions: dict[str, QAction] = {}
        self.plugin_submenus: dict[str, QMenu] = {}

        if layout.pinned:
            added_pin = False
            for cid in layout.pinned:
                label = layout.label_for(cid)
                if not label:
                    continue
                p_act = QAction(label, self)
                connect(p_act, cid)
                self.addAction(p_act)
                self.plugin_actions.setdefault(cid, p_act)
                added_pin = True
            if added_pin:
                self.addSeparator()

        # Tools: screenshot first (most used), then plugin submenus
        self.quick_menu = style_menu(QMenu("Screenshot", self))
        for label, cid in QUICK_TOOL_ITEMS:
            q_act = QAction(label, self.quick_menu)
            if on_tool_action is not None:
                q_act.triggered.connect(lambda _checked=False, c=cid: on_tool_action(c))
            self.quick_menu.addAction(q_act)
            self.plugin_actions[cid] = q_act
        add_section_menu("Screenshot", self.quick_menu)

        # Macros: run one, or build chains of quick tools (hotkey Ctrl+Shift+Alt+1..9 in list order)
        self.macro_menu = style_menu(QMenu("Macros", self))

        def macro_action(label: str, cid: str, menu: QMenu = self.macro_menu) -> QAction:
            act = QAction(label.replace("&", "&&"), menu)
            if on_tool_action is not None:
                act.triggered.connect(lambda _checked=False, c=cid: on_tool_action(c))
            menu.addAction(act)
            self.plugin_actions[cid] = act
            return act

        for idx, name in enumerate(macros, start=1):
            macro_action(f"{name}	{HOTKEY_LABEL}+{idx}", f"macro:{name}")
        self.macro_menu.addSeparator()
        macro_action("New Macro...", "macros:new")
        if macros:
            edit_menu = style_menu(QMenu("Edit", self.macro_menu))
            dbl_menu = style_menu(QMenu("Double-Click Runs", self.macro_menu))
            remove_menu = style_menu(QMenu("Remove", self.macro_menu))
            for name in macros:
                macro_action(name, f"macros:edit:{name}", edit_menu)
                macro_action(name, f"macros:remove:{name}", remove_menu)
            for label, name in (("Snip (default)", ""), *((n, n) for n in macros)):
                d_act = macro_action(label, f"macros:dbl:{name}", dbl_menu)
                d_act.setCheckable(True)
                d_act.setChecked(name == double_click_macro)
            for sub in (edit_menu, dbl_menu, remove_menu):
                self.macro_menu.addMenu(sub)
        add_section_menu("Macros", self.macro_menu)

        # Recent outputs: click reveals the file selected in Explorer
        self.recent_menu = style_menu(QMenu("Recent Files", self))
        now = time.time()
        for idx, (path, added) in enumerate(recent_files):
            name = path.name if len(path.name) <= 34 else f"{path.name[:31]}..."
            r_act = QAction(f"{name}   {format_age(now - added)}".replace("&", "&&"), self.recent_menu)
            if on_tool_action is not None:
                r_act.triggered.connect(lambda _checked=False, c=f"recent:{idx}": on_tool_action(c))
            self.recent_menu.addAction(r_act)
            self.plugin_actions[f"recent:{idx}"] = r_act
        if not recent_files:
            empty = QAction("Nothing yet: snip or drop a file", self.recent_menu)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
        self.recent_menu.addSeparator()
        for label, cid in (("Open Snips Folder", "recent:snips"), ("Open Web Drops Folder", "recent:drops")):
            f_act = QAction(label, self.recent_menu)
            if on_tool_action is not None:
                f_act.triggered.connect(lambda _checked=False, c=cid: on_tool_action(c))
            self.recent_menu.addAction(f_act)
            self.plugin_actions[cid] = f_act
        add_section_menu("Recent Files", self.recent_menu)

        top_level_items = []
        timer_subs: list[QMenu] = []
        groups_started: set[str] = set()
        for item in state_machine.get_active_menu_items():
            if item.callback_id in {"sysmon:refresh", "pomodoro:toggle"}:
                continue
            if not item.submenu:
                top_level_items.append(item)
                continue
            sub = self.plugin_submenus.get(item.submenu)
            if sub is None:
                # "&&" renders a literal "&" instead of a mnemonic underline
                sub = style_menu(QMenu(item.submenu.replace("&", "&&"), self))
                group = "timer" if item.submenu in TIMER_SUBMENUS else "ai" if item.submenu in AI_SUBMENUS else "tools"
                if group == "timer":
                    timer_subs.append(sub)
                elif layout.is_hidden(item.submenu):
                    more_menu().addMenu(sub)
                else:
                    if group not in groups_started:
                        self.addSeparator()
                        groups_started.add(group)
                    self.addMenu(sub)
                self.plugin_submenus[item.submenu] = sub
            p_act = QAction(item.label, sub)
            p_act.setEnabled(item.enabled)
            if item.checkable:
                p_act.setCheckable(True)
                p_act.setChecked(item.checked)
            connect(p_act, item.callback_id)
            sub.addAction(p_act)
            self.plugin_actions[item.callback_id] = p_act

        if phone_bridge_on is not None and on_tool_action is not None:
            phone_menu = style_menu(QMenu("Phone Control", self))
            toggle_act = QAction("Enable Phone Bridge (Tailscale)", phone_menu)
            toggle_act.setCheckable(True)
            toggle_act.setChecked(phone_bridge_on)
            connect(toggle_act, "remote:toggle")
            phone_menu.addAction(toggle_act)
            self.plugin_actions["remote:toggle"] = toggle_act
            pair_act = QAction("Show Phone Link & Token...", phone_menu)
            connect(pair_act, "remote:pair")
            phone_menu.addAction(pair_act)
            self.plugin_actions["remote:pair"] = pair_act
            rot_act = QAction("Rotate Phone Token", phone_menu)
            connect(rot_act, "remote:rotate")
            phone_menu.addAction(rot_act)
            self.plugin_actions["remote:rotate"] = rot_act
            self.plugin_submenus["Phone Control"] = phone_menu
            add_section_menu("Phone Control", phone_menu)

        # Focus timer group
        pomo = state_machine.pomodoro_plugin
        pomo_label = (
            "Pause Pomodoro Timer"
            if (pomo is not None and pomo.running)
            else f"Start / Resume Pomodoro ({pomo.work_minutes if pomo is not None else 25}m)"
        )
        pomo_hidden = layout.is_hidden("Pomodoro")
        pomo_target = style_menu(QMenu("Pomodoro", more_menu())) if pomo_hidden else self
        if not pomo_hidden:
            self.addSeparator()
        self.play_action = QAction(pomo_label, pomo_target)
        self.play_action.triggered.connect(lambda _checked=False: on_play())
        pomo_target.addAction(self.play_action)
        for item in top_level_items:
            p_act = QAction(item.label, pomo_target)
            p_act.setEnabled(item.enabled)
            if item.checkable:
                p_act.setCheckable(True)
                p_act.setChecked(item.checked)
            connect(p_act, item.callback_id)
            pomo_target.addAction(p_act)
            self.plugin_actions[item.callback_id] = p_act
        for sub in timer_subs:
            pomo_target.addMenu(sub)
        if pomo_hidden:
            more_menu().addMenu(pomo_target)

        if more_menu_holder:
            self.addSeparator()
            self.addMenu(more_menu_holder[0])

        self.addSeparator()

        # HUD view switcher (radio list) + metrics refresh
        self.hud_menu = style_menu(QMenu("HUD View", self))
        self.tool_view_actions: dict[str, QAction] = {}
        for plugin in state_machine.registry.plugins:
            tid = plugin.tool_id
            act = QAction(plugin.display_name, self.hud_menu)
            act.setCheckable(True)
            act.setChecked(tid == state_machine.active_tool_id)
            if on_select_tool is not None:
                act.triggered.connect(lambda _checked=False, t=tid: on_select_tool(t))
            else:
                act.triggered.connect(
                    lambda _checked=False, t=tid: state_machine.select_tool(t)
                )
            self.hud_menu.addAction(act)
            self.tool_view_actions[tid] = act
        self.hud_menu.addSeparator()
        if on_pet is not None:
            self.pet_action: QAction | None = QAction("Cycle View (Click Sprite)", self.hud_menu)
            self.pet_action.triggered.connect(lambda _checked=False: on_pet())
            self.hud_menu.addAction(self.pet_action)
        else:
            self.pet_action = None
        self.feed_action = QAction("Refresh System Metrics", self.hud_menu)
        self.feed_action.triggered.connect(lambda _checked=False: on_feed())
        self.hud_menu.addAction(self.feed_action)
        self.addMenu(self.hud_menu)

        if on_tool_action is not None:
            self.customize_action: QAction | None = QAction("Customize...", self)
            self.customize_action.triggered.connect(lambda _checked=False: on_tool_action("pet:customize"))
            self.addAction(self.customize_action)
            self.plugin_actions["pet:customize"] = self.customize_action
        else:
            self.customize_action = None

        # Window settings
        self.settings_menu = style_menu(QMenu("Settings", self))
        self.always_on_top_action = QAction("Always on Top", self.settings_menu)
        self.always_on_top_action.setCheckable(True)
        self.always_on_top_action.setChecked(always_on_top)
        self.always_on_top_action.toggled.connect(on_toggle_on_top)
        self.settings_menu.addAction(self.always_on_top_action)

        self.auto_wander_action = QAction("Gentle Wander", self.settings_menu)
        self.auto_wander_action.setCheckable(True)
        self.auto_wander_action.setChecked(auto_wander)
        self.auto_wander_action.toggled.connect(on_toggle_wander)
        self.settings_menu.addAction(self.auto_wander_action)

        self.autostart_action = QAction("Start with Windows", self.settings_menu)
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(autostart_enabled())
        self.autostart_action.toggled.connect(set_autostart)
        self.settings_menu.addAction(self.autostart_action)

        self.reset_position_action = QAction("Reset Position", self.settings_menu)
        self.reset_position_action.triggered.connect(
            lambda _checked=False: on_reset_position()
        )
        self.settings_menu.addAction(self.reset_position_action)

        if pet_scales and on_tool_action is not None:
            self.size_menu = style_menu(QMenu("Pet Size", self.settings_menu))
            for scale, label in pet_scales.items():
                act = QAction(label, self.size_menu)
                act.setCheckable(True)
                act.setChecked(scale == pet_scale)
                act.triggered.connect(lambda _checked=False, c=f"size:{scale}": on_tool_action(c))
                self.size_menu.addAction(act)
            self.settings_menu.addMenu(self.size_menu)
            self.theme_menu = style_menu(QMenu("Theme", self.settings_menu))
            for key, label in THEME_CHOICES.items():
                act = QAction(label, self.theme_menu)
                act.setCheckable(True)
                act.setChecked(key == theme)
                act.triggered.connect(lambda _checked=False, c=f"theme:{key}": on_tool_action(c))
                self.theme_menu.addAction(act)
            self.settings_menu.addMenu(self.theme_menu)

        # Shortcut hints elsewhere in the menu follow rebound hotkeys (snip:<mode>, and the AI chat's ai:chat)
        for _label, combo, mode in hotkeys:
            act = self.plugin_actions.get("ai:chat" if mode == "ai" else f"snip:{mode}")
            if act is not None:
                act.setText(f"{act.text().split(chr(9))[0]}\t{combo}")

        if hotkeys and on_tool_action is not None:
            self.hotkey_menu = style_menu(QMenu("Hotkeys", self.settings_menu))
            for label, combo, mode in hotkeys:
                act = QAction(f"{label}\t{combo}", self.hotkey_menu)
                act.triggered.connect(lambda _checked=False, c=f"hotkey:{mode}": on_tool_action(c))
                self.hotkey_menu.addAction(act)
            self.settings_menu.addMenu(self.hotkey_menu)

        if wheel_slices and on_tool_action is not None:
            self.wheel_menu = style_menu(QMenu("Customize Wheel", self.settings_menu))

            def wheel_action(label: str, key: str, menu: QMenu = self.wheel_menu) -> QAction:
                act = QAction(label, menu)
                act.triggered.connect(lambda _checked=False, c=f"wheel:{key}": on_tool_action(c))
                menu.addAction(act)
                return act

            for label, cid, shown in wheel_slices:
                w_act = wheel_action(label.replace("&", "&&"), f"toggle:{cid}")
                w_act.setCheckable(True)
                w_act.setChecked(shown)
            self.wheel_menu.addSeparator()
            wheel_action("Add Website...", "add_web")
            wheel_action("Add App or File...", "add_app")
            if wheel_custom:
                remove_menu = style_menu(QMenu("Remove Custom", self.wheel_menu))
                for idx, label in enumerate(wheel_custom):
                    wheel_action(label.replace("&", "&&"), f"remove:{idx}", remove_menu)
                self.wheel_menu.addMenu(remove_menu)
            self.settings_menu.addMenu(self.wheel_menu)

        if on_tool_action is not None:
            update_action = QAction(f"Check for Updates (v{__version__})", self.settings_menu)
            update_action.triggered.connect(lambda _checked=False: on_tool_action("update:check"))
            self.settings_menu.addAction(update_action)
            log_action = QAction("Copy Log", self.settings_menu)
            log_action.triggered.connect(lambda _checked=False: on_tool_action("log:copy"))
            self.settings_menu.addAction(log_action)
        self.addMenu(self.settings_menu)

        sleep_label = (
            "Exit Do Not Disturb"
            if state == PetState.SLEEPING
            else "Do Not Disturb"
        )
        self.sleep_action = QAction(sleep_label, self)
        self.sleep_action.triggered.connect(lambda _checked=False: on_toggle_sleep())
        self.addAction(self.sleep_action)

        self.addSeparator()

        self.quit_action = QAction("Quit Paperduck", self)
        self.quit_action.triggered.connect(lambda _checked=False: on_quit())
        self.addAction(self.quit_action)
