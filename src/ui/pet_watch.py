"""Hints and Watch mode of the pet window (mixed into PetWindow)."""

from __future__ import annotations

import time
from collections.abc import Callable

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QColor, QGuiApplication, QImage

from src.engine.models import PetState
from src.tools.ai_assist import AiAssistPlugin, AiJob, AiReply, fit_thinking, load_key, thinking_levels
from src.tools.clip_vault import clipboard_is_private
from src.tools.hints import HintsPlugin, Suggestion, foreground_app, foreground_title, idle_seconds, display_kept_on
from src.tools.watch import (ACTION_LABELS, EDITORS, LABELS as WATCH_LABELS, SOURCES as WATCH_SOURCES, WatchReply,
                              action_lenses, apply_moves, ask_context, editor_title_parts, editor_workspace,
                              folder_check, hotkey_lens, run_lens, undo_moves)
from src.tools import computer
from src.ui.ai_tool_host import AiToolHost, grab_screen
from src.ui.sfx import prewarm as prewarm_ticks




class WatchMixin:
    """Methods of PetWindow; state and Qt signals live on PetWindow itself."""

    def _hints_plugin(self) -> HintsPlugin | None:
        plugin = self._state_machine.registry.get_plugin("hints")
        if not isinstance(plugin, HintsPlugin):
            return None
        ai = self._state_machine.registry.get_plugin("ai")
        if isinstance(ai, AiAssistPlugin):
            plugin.levels = lambda: thinking_levels(ai.base_url, ai.model)
        return plugin

    def _check_away(self) -> None:
        away = idle_seconds() >= self.AWAY_AFTER_S and not display_kept_on()  # watching a video is not away
        if away == self._away:
            return
        self._away = away
        self._sync_animation_to_pet_state(self._state_machine.state)
        if not away and self._anim.base_state == "idle":
            self._anim.play_oneshot("yawn")  # waking up as the user comes back
        self._schedule_next_anim_tick()
        self.update()

    def _poll_hint(self) -> None:
        self._check_away()
        self._poll_errands()
        app = foreground_app()  # ~0.03ms per 3s tick
        if (hints := self._hints_plugin()) is not None:
            hints.note_app(app)
        if self._hint is not None:
            if time.monotonic() >= self._hint_until and not self._watch_open:
                hints = self._hints_plugin()
                if (self._watch_reply is not None or self._watch_job is not None) and hints is not None:
                    hints.ignored_ready()  # a pre-fetch the user let expire, finished or not
                self._clear_hint()
            return
        if (self._state_machine.state == PetState.SLEEPING or self._open_wheels()
                or self._press_global_pos is not None or self._snip_session is not None):
            return
        if self.tips_seen < len(self.TIPS) and time.monotonic() >= self._tip_due:
            text, how = self.TIPS[self.tips_seen]
            self.tips_seen += 1
            self._hint, self._hint_until = Suggestion("tip", text, "", how), time.monotonic() + 12.0
            self._tip_due = self._hint_until + self.TIP_GAP_S
            self._anim.play_oneshot("spin")
            self._schedule_next_anim_tick()
            self.update()
            return
        hints = self._hints_plugin()
        if hints is None or not hints.enabled:
            return
        cb = QGuiApplication.clipboard()
        pomo = self._state_machine.pomodoro_plugin
        # a password manager's copy is never inspected or offered to Watch
        clip = cb.text() if cb is not None and not clipboard_is_private(cb.mimeData()) else ""
        hint = hints.poll(app, clip,
                          pomo is not None and not pomo.running, title=foreground_title(),
                          watch_ok=hints.watch and self._watch_ready())
        if hint is not None and hint.action == "watch:tidy" and not hint.data:
            self._check_folder(hint, foreground_title())  # the card shows only if the folder is messy
            return
        if hint is not None:
            self._hint, self._hint_until = hint, time.monotonic() + 12.0
            if hint.action.startswith("watch:") and hints.can_prefetch(hint):
                self._start_watch(hint, hint.data)
            self._anim.play_oneshot("spin")
            self._schedule_next_anim_tick()
            self.update()

    def _clear_hint(self) -> None:
        self._hint = None
        self._drop_watch()
        self.update()

    def _run_hint(self) -> None:
        hint, hints = self._hint, self._hints_plugin()
        self._hint = None  # not _clear_hint: a running Watch answer is kept for the click
        self.update()
        if hint is None or hint.rule == "tip":  # a tip's click just closes it
            return
        if hint.rule.startswith("errand"):
            if hint.rule == "errand_ready":
                self._run_errand()
            self.ensure_ai_chat()
            return
        if hints is not None:
            hints.mark_used()
        if hint.action.startswith("watch:"):
            self._run_watch(hint)
            return
        if hint.action == "pet:focus":
            self.trigger_play()
        else:
            self.trigger_tool_action(hint.action)

    # Watch mode: the model is called on a click, or ahead of it when hints.can_prefetch allows

    def _watch_ready(self) -> bool:
        """A key and a model are set, the chat is not mid-answer and no lens answer is in flight."""
        ai = self._state_machine.registry.get_plugin("ai")
        return (isinstance(ai, AiAssistPlugin) and bool(ai.model) and self._watch_job is None
                and (self._ai_chat is None or self._ai_chat._job is None) and ai.has_key())

    def _start_watch(self, hint: Suggestion, data: str | QImage | None) -> None:
        """`data` is the clipboard text, or the screen grab for screen lenses."""
        hints, ai = self._hints_plugin(), self._state_machine.registry.get_plugin("ai")
        if hints is None or not isinstance(ai, AiAssistPlugin) or data is None or data == "":
            self._clear_hint()
            return
        if hints.budget_left() <= 0 and hint.rule != "watch_now":  # the hotkey is the user asking, like the chat
            self._show_toast("AI BUDGET USED", QColor("#EF7D57"))
            self._clear_hint()
            return
        lens = hint.action.removeprefix("watch:")
        url, key, model, thinking = ai.base_url, load_key(ai.base_url), ai.model, fit_thinking(ai.base_url, hints.watch_thinking, ai.model)
        image = data if not isinstance(data, str) else None
        hints.record_ai(0)  # counted at the start, so a failing provider cannot loop past the budget
        if self._watch_open and self._ai_chat is None:
            self._make_ai_chat(ai)  # the host's approval cards live in the chat
        # Tools only after a click: a pre-fetch must never pop approvals the user did not ask for
        host = self._ai_chat._tool_host if self._watch_open and self._ai_chat is not None else None
        project = str(getattr(self._state_machine.registry.get_plugin("project"), "repo", None) or "")

        def run() -> AiReply:
            text = AiToolHost._ocr(image) if image is not None else data
            reply = run_lens(lens, text, url, key, model, thinking=thinking, host=host, project=project)
            reply.tools = [*reply.tools, (WATCH_SOURCES.get(lens, "read_clipboard()"), text)]
            return reply

        job = self._watch_job = AiJob(self)
        job.finished.connect(lambda reply, error, h=hint: self._on_watch_done(job, h, reply, error))
        job.start(run)
        QTimer.singleShot(60, lambda: self._warm_watch(ai))  # after the card repaints

    def _warm_watch(self, ai: AiAssistPlugin) -> None:
        """While the model works: wake the audio output (a ~0.2s blocking resume) and build the chat, so the
        answer opens without those hitches. The output stays awake IDLE_SUSPEND_MS (20s)."""
        if self._watch_job is None:
            return
        prewarm_ticks()
        if self._ai_chat is None:
            self._make_ai_chat(ai)

    def _on_watch_done(self, job: AiJob, hint: Suggestion, reply: AiReply | None, error: str) -> None:
        job.deleteLater()
        if job is not self._watch_job:
            return  # dismissed or replaced meanwhile
        self._watch_job = None
        hints = self._hints_plugin()
        if hints is not None and reply is not None:
            hints.ai_tokens += max(0, reply.total_tokens)
        if error or reply is None:
            self._watch_open = False
            self._show_toast("AI ERROR", QColor("#EF7D57"))
            self._clear_hint()
            return
        self._watch_reply = reply
        if self._watch_open:
            self._show_watch(hint)
        elif self._hint is hint:
            self._hint = Suggestion(hint.rule, "Answer ready. Click to see it", hint.action, hint.data)
            self._hint_until = time.monotonic() + 20.0
            self.update()

    def _run_watch(self, hint: Suggestion) -> None:
        if self._watch_reply is not None:
            self._show_watch(hint)
            return
        self._hint, self._hint_until = Suggestion(hint.rule, "Working on it...", hint.action, hint.data), 0.0
        self._watch_open = True
        self.update()
        if self._watch_job is not None:
            return
        if not self._watch_ready():
            self._clear_hint()
            self.open_ai_chat()  # shows Setup when no key or model is set
            return
        if hint.data:
            self._start_watch(hint, hint.data)
        else:  # screen lenses read the screen only now, after the click
            grab_screen(self, lambda image, h=hint: self._start_watch(h, image) if self._watch_open else None,
                    window=True)

    def watch_now(self) -> None:
        """Alt+`, Ctrl+Shift+Alt+W or the menu: ask about the front window. The chat opens with that window
        attached, the app and title as context, the Project Folder set from an editor or File Explorer window, and
        quick actions (Enter on an empty draft runs the best fit). A typed question goes to the full agent.
        Works with Watch mode off; actions are counted but not stopped by the daily budget (an explicit ask)."""
        if self._watch_job is not None or self._watch_open:
            self._show_toast("ALREADY ON IT", QColor("#8A9AF2"))
            return
        if not self._watch_ready():
            self.open_ai_chat()  # Setup when no key or model; a chat mid-answer is shown as is
            return
        app, title = foreground_app(), foreground_title()
        lens = hotkey_lens(app, title)
        project = editor_workspace(app, title)
        grab_screen(self, lambda image: self._open_ask(app, title, lens, project, image), window=True)

    def _open_ask(self, app: str, title: str, lens: str, project: str, image: QImage | None) -> None:
        chat = self.ensure_ai_chat()
        if chat is None or chat._job is not None:
            return
        chat.clear_ask()
        label = EDITORS.get(app.lower(), ("", ""))[1] or app.removesuffix(".exe").removesuffix(".EXE") or "Screen"
        if image is not None and not image.isNull():
            chat.attach_image(image, f"{label} window")
        if project:
            self._use_project(project, f"from {label}")
        file = editor_title_parts(title)[0] if project else ""

        def actions(folder: str = "") -> list[tuple[str, Callable[[], None]]]:
            out = []
            for each in action_lenses(lens):
                if each == "tidy" and not folder:
                    continue  # tidy needs the folder path; offered once File Explorer answers
                data = folder if each == "tidy" else image
                out.append((ACTION_LABELS.get(each, each), lambda l=each, d=data: self._run_action(l, d)))
            return out

        chat.set_ask(ask_context(app, title, project, file), actions())
        if app.lower() == "explorer.exe":  # resolve the folder (about 0.3s) for Tidy and the Project Folder
            job = self._folder_job = AiJob(self)

            def done(found: tuple[str, int] | None, _error: str) -> None:
                job.deleteLater()
                if job is not self._folder_job or found is None or chat._actions == []:
                    return
                self._folder_job = None
                self._use_project(found[0], "from File Explorer")
                chat.set_ask(ask_context(app, title, found[0]), actions(found[0]))

            job.finished.connect(done)
            job.start(lambda: folder_check(computer.foreground_window(), title, min_files=1))

    def _use_project(self, path: str, why: str) -> None:
        """Point the chat's file tools at `path` (the folder the user is looking at), with a visible note."""
        project = self._state_machine.registry.get_plugin("project")
        if project is None or str(getattr(project, "repo", "") or "") == path:
            return
        project.set_repo(path)
        if self._ai_chat is not None:
            self._ai_chat.add_note(f"Project Folder: {path} ({why}). The AI can read files there and edit them "
                                   "only with your OK.")

    def _run_action(self, lens: str, data: str | QImage | None) -> None:
        """A quick action from the ask card: the lens answer lands in the chat like a Watch answer."""
        hint = Suggestion("watch_now", "Working on it...", f"watch:{lens}", data if isinstance(data, str) else "")
        self._hint, self._hint_until, self._watch_open = hint, 0.0, True
        self.update()
        if self._ai_chat is not None:
            self._ai_chat.add_note(f"Working on: {ACTION_LABELS.get(lens, lens)}...")
        self._start_watch(hint, data)

    def _check_folder(self, hint: Suggestion, title: str) -> None:
        """Resolve the front Explorer window's folder on a worker thread (about 0.3s of PowerShell); the offered
        tidy hint shows only for a messy folder."""
        hwnd = computer.foreground_window()
        job = self._folder_job = AiJob(self)

        def done(found: tuple[str, int] | None, _error: str) -> None:
            job.deleteLater()
            if job is not self._folder_job:
                return
            self._folder_job = None
            if found is not None and self._hint is None and foreground_title() == title:
                self._hint = Suggestion(hint.rule, f"Tidy this folder? ({found[1]} files)", hint.action, found[0])
                self._hint_until = time.monotonic() + 12.0
                self._anim.play_oneshot("spin")
                self._schedule_next_anim_tick()
                self.update()

        job.finished.connect(done)
        job.start(lambda: folder_check(hwnd, title))

    def _show_watch(self, hint: Suggestion) -> None:
        reply = self._watch_reply
        self._clear_hint()
        chat = self.ensure_ai_chat()
        if reply is None or chat is None:
            return
        if not chat.add_watch_answer(WATCH_LABELS.get(hint.action.removeprefix("watch:"), "Watch"), reply):
            self._show_toast("CHAT BUSY", QColor("#EF7D57"))
            return
        if not isinstance(reply, WatchReply):
            return
        hints = self._hints_plugin()
        cb = QGuiApplication.clipboard()
        if reply.copy_text and cb is not None:
            cb.setText(reply.copy_text)  # copied, never typed or sent: the user pastes it where they want
            if hints is not None:
                hints.note_own_copy(reply.copy_text)
            self._show_toast("REPLY COPIED", QColor("#38B764"))
        if reply.moves:
            folder, moves = reply.folder, reply.moves
            n = sum(len(f) for f in moves.values())
            plan = "\n".join(f"{name}/  <-  " + ", ".join(files) for name, files in moves.items())
            self._tidy_asks += 1
            chat.confirm(f"tidy:{self._tidy_asks}", f"Move {n} files into {len(moves)} folders inside {folder}? "
                         "Nothing is deleted or overwritten.\n\n" + plan[:4000], "Tidy folder",
                         lambda ok: self._apply_tidy(folder, moves) if ok else None, once=False)

    def _apply_tidy(self, folder: str, moves: dict[str, list[str]]) -> None:
        done, created, skipped = apply_moves(folder, moves)
        hints = self._hints_plugin()
        if hints is not None and done:
            hints.last_tidy = (done, created)
        if self._ai_chat is not None:
            self._ai_chat.add_note(f"Moved {len(done)} files into {len(moves)} folders"
                                   + (f", skipped {skipped} (name taken or file gone)" if skipped else "")
                                   + ". Undo: right-click the pet, Hints, Undo Last Tidy.")
        self._show_toast(f"MOVED {len(done)}", QColor("#38B764"))

    def _undo_tidy(self) -> None:
        hints = self._hints_plugin()
        if hints is None or hints.last_tidy is None:
            return
        back = undo_moves(*hints.last_tidy)
        hints.last_tidy = None
        self._show_toast(f"PUT BACK {back}", QColor("#8A9AF2"))

    def _drop_watch(self) -> None:
        self._watch_job = None  # its late answer is ignored by _on_watch_done
        self._watch_reply = None
        self._watch_open = False

    def _dismiss_hint(self) -> None:
        hint, hints = self._hint, self._hints_plugin()
        self._clear_hint()
        if hint is not None and hint.rule.startswith("errand"):
            return  # closes the card; an errand_ready one shows again on the next tick
        if hint is not None and hint.rule == "tip":
            self.tips_seen = len(self.TIPS)
            self._show_toast("TIPS OFF", QColor("#8A9AF2"))
            return
        if hint is not None and hints is not None:
            hints.dismiss(hint.rule)
        self._show_toast("MUTED", QColor("#8A9AF2"))
