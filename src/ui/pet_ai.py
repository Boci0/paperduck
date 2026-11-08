"""AI Assist, macros and errands of the pet window (mixed into PetWindow)."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QRect, QUrl
from PyQt6.QtGui import QColor, QCursor, QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import QApplication, QFileDialog

from src.engine.models import PetState
from src.tools.ai_assist import AiAssistPlugin
from src.tools.ai_tools import skills_dir
from src.tools.errands import MAX_ERRAND_RUNS, ErrandsPlugin
from src.tools.hints import Suggestion
from src.ui.ai_chat import AiChat
from src.ui.ai_tool_host import AiToolHost
from src.ui.screen_glow import play_screen_glow
from src.storage.storage_manager import log_text
from src.ui.macro_editor import MacroEditor
from src.ui.sfx import play_cue
from src.ui.tool_wheel import google_search_url
from src.ui.theme import DANGER_HEX, THEME_CHOICES, pet_accent, save_theme, saved_theme


from src.ui.pet_helpers import VK_VOLUME_MUTE, KEYEVENTF_KEYUP


class AiMixin:
    """Methods of PetWindow; state and Qt signals live on PetWindow itself."""

    # ------------------------------------------------------------------
    # AI Assist (manual only: nothing is sent until the user presses Send, or runs a macro they built with an `ask` step)
    # ------------------------------------------------------------------

    def open_ai_chat(self) -> None:
        """Toggle the chat card beside the pet; opening plays the pixel sparkle effect and a chime."""
        plugin = self._state_machine.registry.get_plugin("ai")
        if not isinstance(plugin, AiAssistPlugin):
            return
        started = time.perf_counter()
        if self._ai_chat is None:
            self._make_ai_chat(plugin)
        elif self._ai_chat.isVisible():
            self._ai_chat.hide()
            return
        sprite = self._sprite_global_rect()
        play_cue("open")
        self._ai_chat.open_near(sprite)
        # the effects start after the card's layout pass, so their first frames are not frozen by it
        glow = play_screen_glow(self.screen(), sprite.center())
        if glow is not None:
            self._glows.append(glow)
            glow.destroyed.connect(lambda *_a, g=glow: self._glows.remove(g) if g in self._glows else None)
        self._anim.play_oneshot("spin")
        self._schedule_next_anim_tick()
        took = (time.perf_counter() - started) * 1000
        if took > 150:
            logging.getLogger(__name__).info("ai chat open took %.0f ms", took)

    PREBUILD_CHAT_MS = 4000  # after launch settles: the first open then skips about 0.65s of widget setup and first show

    def _prebuild_ai_chat(self) -> None:
        plugin = self._state_machine.registry.get_plugin("ai")
        if self._ai_chat is None and not self._is_shutting_down and isinstance(plugin, AiAssistPlugin):
            self._make_ai_chat(plugin)
            self._ai_chat.warm_up()

    def _make_ai_chat(self, plugin: AiAssistPlugin) -> None:
        """Build the chat card hidden (about 0.4s of widget setup on the Qt thread)."""
        host = AiToolHost(self)
        self._ai_chat = AiChat(plugin, on_attach_screen=lambda: self.trigger_snip("ask"), tool_host=host)
        host.activity.connect(self._on_ai_activity)
        if (errands := self._errands()) is not None:
            errands.probes.bg = host.procs.peek
        self._ai_chat.thinking_changed.connect(self._on_ai_thinking)
        self._ai_chat.answered.connect(self._on_ai_answered)

    def ensure_ai_chat(self) -> AiChat | None:
        """Open the chat if it is closed (never toggles it shut, unlike open_ai_chat)."""
        if self._ai_chat is None or not self._ai_chat.isVisible():
            self.open_ai_chat()
        return self._ai_chat

    def _edit_macro(self, old_name: str = "") -> None:
        old = self.macros.find(old_name) if old_name else None
        number = self.macros.macros.index(old) + 1 if old else len(self.macros.macros) + 1
        editor = MacroEditor(old.name if old else "", old.recipe if old else "",
                             bool(old) and self.macros.double_click == old.name, number if number <= 9 else 0)
        if not editor.exec():
            return
        try:
            toast = self.macros.save(editor.name.text(), editor.recipe.text(), old.name if old else "")
        except ValueError:
            toast = "BAD STEP"
        name = " ".join(editor.name.text().split())
        if toast in ("ADDED", "SAVED"):
            if editor.double_click.isChecked():
                self.macros.double_click = name
            elif self.macros.double_click == name:
                self.macros.double_click = ""
        self._show_toast(toast, QColor("#38B764" if toast in ("ADDED", "SAVED") else "#EF7D57"))

    def _sprite_global_rect(self) -> QRect:
        return QRect(self.mapToGlobal(self._sprite_rect.topLeft()), self._sprite_rect.size())

    def _react(self, color_hex: str, *, big: bool = False) -> None:
        """One-shot reaction to a notice by its colour: green success cheers, coral failure flinches (in any
        state), anything else (aborted, info) spins while idle. A `big` win also bursts pixel stars round the pet."""
        if big and self.isVisible():
            play_screen_glow(self.screen(), self._sprite_global_rect().center())
        if QColor(color_hex) == QColor(self.SUCCESS_HEX):
            clip = "cheer"
        elif pet_accent(color_hex) == QColor(DANGER_HEX):
            clip = "flinch"
        elif self._state_machine.state == PetState.IDLE:
            clip = "spin"
        else:
            return
        self._anim.play_oneshot(clip)
        self._schedule_next_anim_tick()

    def _on_ai_activity(self, name: str) -> None:
        """Act out what the chat model does: a new base clip per tool, and facing the chat while it waits."""
        self._ai_activity = name
        if name == "approval" and self._ai_chat is not None and self._ai_chat.isVisible():
            self._anim.facing_left = self._ai_chat.geometry().center().x() < self._sprite_global_rect().center().x()
        if not self._is_shutting_down and self._walk_steps_remaining == 0:
            clip = self._base_clip(self._state_machine.state)
            if self._anim.base_state != clip:
                self._anim.set_state(clip)
                self._schedule_next_anim_tick()
                self.update()

    # Errands: the plugin checks triggers on the hint tick; a fired one runs in the chat that set it

    def _errands(self) -> ErrandsPlugin | None:
        plugin = self._state_machine.registry.get_plugin("errands")
        return plugin if isinstance(plugin, ErrandsPlugin) else None

    def _poll_errands(self) -> None:
        errands = self._errands()
        if errands is None:
            return
        if errands.poll():
            self.update()  # the tag count
        if not errands.fired or self._is_shutting_down:
            return
        if self._ai_chat is None:
            self._prebuild_ai_chat()  # a next_startup errand can fire before the chat is built
        chat = self._ai_chat
        if chat is None or chat._job is not None or (self._hint is not None and self._hint.rule == "errand_ready"):
            return
        errand, _evidence = errands.fired[0]
        procs = getattr(chat._tool_host, "procs", None)
        if chat._sid != errand.sid and procs is not None and procs.snapshot():
            # switching chats would kill this chat's background commands: the user decides
            self._show_errand_hint("errand_ready", "Errand ready. Click to run it", errand.instruction)
            return
        self._run_errand()

    def _run_errand(self) -> None:
        errands, chat = self._errands(), self._ai_chat
        if errands is None or chat is None or not errands.fired or chat._job is not None:
            return
        errand, evidence = errands.fired.pop(0)
        if not errands.take_run():
            chat.add_note(f"Errand {errand.id} fired, but today's {MAX_ERRAND_RUNS} errand runs are used up.")
            self._show_toast("ERRAND CAP", QColor("#EF7D57"))
            return
        if chat.run_errand(errand, evidence):
            self._errand_running = True
            self._anim.play_oneshot("spin")
            self._schedule_next_anim_tick()
            if not chat.isVisible():
                self._show_errand_hint("errand_busy", "Errand: working on it", errand.instruction)
            self.update()

    def _show_errand_hint(self, rule: str, text: str, detail: str) -> None:
        self._drop_watch()
        self._hint = Suggestion(rule, text, "", detail[:60])
        self._hint_until = time.monotonic() + (20.0 if rule == "errand_done" else 3600.0)
        self.update()

    def _on_ai_answered(self, ok: bool) -> None:
        if self._errand_running:
            self._errand_running = False
            if (errands := self._errands()) is not None and ok:
                errands.done += 1
            if self._hint is not None and self._hint.rule == "errand_busy":
                self._clear_hint()
            if self._ai_chat is not None and not self._ai_chat.isVisible():
                self._show_errand_hint("errand_done", "Errand done. Click to see" if ok else "Errand failed. Click",
                                   "Right-click closes")
            self._react(self.SUCCESS_HEX if ok else "#EF7D57", big=ok)
            return
        # A quick answer is just a chat; only a long job (an agentic task) earns a cheer
        if not ok:
            self._react("#EF7D57")
        elif time.monotonic() - self._ai_since >= self.LONG_TASK_S:
            self._react(self.SUCCESS_HEX, big=True)

    def _on_ai_thinking(self, thinking: bool) -> None:
        if thinking and not self._ai_thinking:
            self._ai_since = time.monotonic()
        self._ai_thinking = thinking
        if not self._is_shutting_down:
            self._sync_animation_to_pet_state(self._state_machine.state)
            self._schedule_next_anim_tick()

    def trigger_tool_action(self, callback_id: str) -> None:
        if callback_id == "watch:now":
            self.watch_now()
            return
        if callback_id == "watch:undo_tidy":
            self._undo_tidy()
            return
        if callback_id.startswith("hotkey:"):
            self._edit_hotkey(callback_id.removeprefix("hotkey:"))
            return
        if callback_id.startswith("size:"):
            self.set_pet_scale(int(callback_id[5:]) if callback_id[5:].isdigit() else 0)
            return
        if callback_id.startswith("theme:"):
            choice = callback_id.removeprefix("theme:")
            if choice in THEME_CHOICES and choice != saved_theme():
                save_theme(choice)
                self.restart()  # every surface takes its colours at import, so a new scheme needs a fresh start
            return
        if callback_id.startswith("snip:"):
            self.trigger_snip(callback_id.split(":", 1)[1])
            return
        if callback_id == "forge:pick_files":
            selected, _flt = QFileDialog.getOpenFileNames(
                self,
                "Paperduck File Forge - Select File(s)",
            )
            if selected:
                self.trigger_drop_files(selected)
            return
        if callback_id == "forge:pick_folder":
            folder = QFileDialog.getExistingDirectory(
                self,
                "Paperduck File Forge - Select Folder to Compress",
            )
            if folder:
                self.trigger_drop_files([folder])
            return
        if callback_id.startswith("recent:"):
            key = callback_id.removeprefix("recent:")
            if key == "snips":
                self.open_output_folder(self.snips_dir())
            elif key == "drops":
                self.open_output_folder(self._web_drop_dir())
            elif key.isdigit():
                self.reveal_recent(int(key))
            return
        if callback_id == "ai:chat":
            self.open_ai_chat()
            return
        if callback_id == "ai:skills":
            self.open_output_folder(skills_dir())
            return
        if callback_id.startswith(("macro:", "macrokey:")):  # by name (wheel, menu) or number (hotkey)
            key = callback_id.partition(":")[2]
            macro = self.macros.find(key) if callback_id.startswith("macro:") else self.macros.numbered(key)
            if macro is None:
                self._show_toast("NO MACRO", QColor("#8A9AF2"))
            else:
                self._macro_runner.run(macro)
            return
        if callback_id.startswith("macros:"):
            action, _sep, name = callback_id.removeprefix("macros:").partition(":")
            if action == "new":
                self._edit_macro()
            elif action == "edit":
                self._edit_macro(name)
            elif action == "remove" and self.macros.remove(name):
                self._show_toast("REMOVED", QColor("#8A9AF2"))
            elif action == "dbl":
                self.macros.double_click = name if self.macros.find(name) else ""
                self._show_toast("DOUBLE-CLICK", QColor("#38B764"))
            return
        if callback_id.startswith("wheel:"):
            self._customize_wheel(callback_id.removeprefix("wheel:"))
            return
        if callback_id.startswith("open:"):  # a custom wheel slice: an app, file or folder the user picked
            try:
                os.startfile(callback_id.removeprefix("open:"))  # type: ignore[attr-defined]
                self._show_toast("OPEN", QColor("#38B764"))
            except OSError:
                self._show_toast("OPEN ERR", QColor("#EF7D57"))
            return
        if callback_id == "update:check":
            self.check_for_updates()
            return
        if callback_id == "log:copy":
            text, cb = log_text(), QGuiApplication.clipboard()
            if not text or cb is None:
                self._show_toast("NO LOG", QColor("#EF7D57"))
                return
            cb.setText(text)
            self._show_toast("LOG COPIED", QColor("#38B764"))
            return
        if callback_id.startswith("web:"):
            QDesktopServices.openUrl(QUrl(callback_id.removeprefix("web:")))
            return
        if callback_id == "pet:search":
            cb = QGuiApplication.clipboard()
            query = " ".join((cb.text() if cb is not None else "").split())[:500]
            if not query:
                self._show_toast("NO TEXT", QColor("#EF7D57"))
                return
            QDesktopServices.openUrl(QUrl(google_search_url(query)))
            self._show_toast("SEARCH", QColor("#38B764"))
            return
        if callback_id == "pet:mute":
            if sys.platform == "win32":
                u32 = ctypes.windll.user32
                u32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
                u32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_KEYUP, 0)
                self._show_toast("MUTE", QColor("#8A9AF2"))
            return
        if callback_id == "pet:clips":
            self._open_clip_history(QCursor.pos())
            return
        if callback_id == "project:pick":
            folder = QFileDialog.getExistingDirectory(self, "Paperduck - Choose Project Folder")
            project = self._state_machine.registry.get_plugin("project")
            if folder and project is not None:
                project.set_repo(folder)
                self._show_toast(Path(folder).name[:10].upper(), QColor("#73EFF7"))
            return
        if callback_id == "forge:clipboard_files":
            clip_paths = self._clipboard_local_paths()
            if not clip_paths:
                self._show_toast("NO FILE", QColor("#EF7D57"))
                return
            self.trigger_drop_files(clip_paths)
            return

        handled, toast_text, color_hex = self._state_machine.execute_tool_action(callback_id)
        if handled and toast_text:
            self._show_toast(toast_text, QColor(color_hex or "#73EFF7"))

    def trigger_toggle_sleep(self) -> None:
        if self._state_machine.state == PetState.SLEEPING:
            self._state_machine.wake()
            self._show_toast("ONLINE", QColor("#73EFF7"))
        else:
            self._state_machine.rest()
            self._show_toast("DND", QColor("#8A9AF2"))

    def trigger_quit(self) -> None:
        if self._on_quit_requested is not None:
            self._on_quit_requested()
        else:
            self.shutdown()
            self.close()
            app = QApplication.instance()
            if app is not None:
                app.quit()
