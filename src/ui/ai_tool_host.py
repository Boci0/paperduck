"""Runs the AI chat model's PC tools (src/tools/ai_tools.py) for one PetWindow.

`run` is called on the chat's worker thread. File tools and OCR run right there; anything touching Qt
(clipboard, screen grab, Save dialog, pet actions) is queued to the Qt thread and the worker waits for its result.
Tools of the user's MCP servers (src/tools/mcp_client.py) join the specs; each server's first call in a chat
waits for the user to allow it in the chat.
edit_file, edit_files, run_command, start_background, add_mcp_server, add_skill, update_skill, remember and forget
also wait for the user's Allow in the chat (once, or for the whole chat). Background processes (`procs`) are
killed when the app quits.
"""

from __future__ import annotations

import ctypes
import json
import tempfile
import threading
import time
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QCoreApplication, QObject, QRect, QStandardPaths, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication, QImage
from PyQt6.QtWidgets import QFileDialog

from src.tools import computer
from src.tools.ai_assist import ask, fit_thinking, load_key
from src.tools.agents import AGENT_PROMPT, AGENT_SPECS, MAX_AGENT_CALLS, Agents, ReadOnlyHost
from src.tools.ai_tools import (calculate, find_control, COMPUTER_TOOLS, MAX_BATCH_STEPS, FILE_TOOLS, PET_ACTIONS, TOOL_SPECS, BackgroundProcs, apply_edits,
                                computer_action, destructive_reason, find_skills, is_secret, load_skill, memory_path, parse_boxes, plan_edit,
                                plan_edits, plan_memory, plan_skill_update, print_to_pdf_paths, resolve,
                                PDF_WAIT_S, changed_media, file_stamp, media_stamps, wait_for_new_file, run_command, command_timeout, powershell_file, powershell_error_note, run_file_tool, save_args,
                                skill_text, skills_dir, task_list, write_memory, write_skill)
from src.tools.errands import describe as describe_errand
from src.tools.clip_vault import clipboard_is_private
from src.tools.doc_preview import preview_document
from src.tools.file_forge import scrub_metadata
from src.tools.macros import parse_recipe
from src.tools.mcp_client import SEP, McpHub, add_server, env_summary, find_servers, safe_name
from src.tools.watch import MESSAGE_APPS, READERS
from src.tools.web_drop import load_image_file
from src.ui.quick_tools import ocr_image_file

GUI_TIMEOUT_S = 15.0
SAVE_TIMEOUT_S = 600.0  # the Save dialog waits on the user
APPROVE_TIMEOUT_S = 300.0  # the user reads the MCP confirm, a diff or a command in the chat
# Allowed-for-this-chat keys that ask again once the chat holds web, MCP or agent content: what an injected
# instruction could use to act on the PC. MCP calls stay allowed (a browser server would ask on every click).
# Apps whose window on screen shows content from outside the PC: a screen read with one visible arms the guard too
OUTSIDE_APPS = READERS | MESSAGE_APPS
OUTSIDE_GUARDED = frozenset({"run_command", "start_background", "computer", "edit_file", "edit_outside", "memory", "add_skill",
                             "update_skill", "add_mcp_server"})
ERRAND_APPROVE_S = 1800.0  # an errand runs while the user may be away, so its approvals wait longer
MAX_CLIP_CHARS = 12000
MAX_SCREEN_CHARS = 8000
MAX_SHARED_NOTE_CHARS = 1500  # OCR text of shared pictures added to a sub-agent's task (the task caps at 4000)
MAX_OCR_SIDE = 4096  # longer sides are scaled down for OCR (Windows OCR refuses very large bitmaps)


def _rel_name(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _strings(value: Any) -> list[str]:
    """Every string value inside nested JSON (keys left out); a loop, so deep nesting cannot overflow the stack."""
    found, todo = [], [value]
    while todo:
        item = todo.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
    return found


def _front_window_shot(hwnd: int) -> QImage | None:
    """Only window `hwnd`, cut from a grab of the monitor it is on (None when that fails)."""
    found = computer.window_on_monitor(hwnd)
    if found is None:
        return None
    device, (left, top, right, bottom), (mon_w, mon_h) = found
    screen = next((s for s in QGuiApplication.screens() if s.name() == device), None)
    image = screen.grabWindow(0).toImage() if screen is not None else None
    if image is None or image.isNull() or not mon_w or not mon_h:
        return None
    sx, sy = image.width() / mon_w, image.height() / mon_h  # the grab may be in logical, not physical, pixels
    return image.copy(round(left * sx), round(top * sy), round((right - left) * sx), round((bottom - top) * sy))


def _ours(win: Any) -> list[Any]:
    """The pet's own windows on screen (pet, chat card, its agent sheets): left out of shots and clicked through."""
    chat = win._ai_chat
    sheets = getattr(getattr(chat, "_sheets", None), "sheets", [])
    return [w for w in (win, chat, *sheets) if w is not None and w.isVisible()]


def show_quietly(w: Any) -> None:
    """Show without taking the keyboard from the app the user (or the model) is in."""
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    w.show()
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)


def grab_screen(win: Any, done: Callable[[Any], None], primary: bool = False, window: bool = False) -> None:
    """Grab the screen under the cursor (or the primary one, with the front app's handle; or with `window`,
    just the front app's window) on the Qt thread, with the pet `win` and its chat left out of the shot.
    They stay on screen (excluded from capture); only where Windows cannot exclude them are they hidden."""
    shown = _ours(win)
    hidden = [w for w in shown if not computer.exclude_from_capture(int(w.winId()), True)]
    for w in hidden:
        w.hide()

    def grab() -> None:
        hwnd = computer.focus_front_app() if primary else computer.front_app()  # the app under the pet and chat
        image = _front_window_shot(hwnd) if window else None
        if image is None:
            screen = (None if primary else QGuiApplication.screenAt(QCursor.pos())) or QGuiApplication.primaryScreen()
            image = screen.grabWindow(0).toImage() if screen is not None else None
        for w in shown:
            if w in hidden:
                show_quietly(w)
            else:
                computer.exclude_from_capture(int(w.winId()), False)
        done((image, hwnd) if primary else image)

    QTimer.singleShot(180 if hidden else 30, grab)  # the compositor applies the affinity on its next frame


SPOT_R = 16  # half the side of the square compared around a click
ROW_LEFT, ROW_RIGHT, ROW_R = 700, 300, 10  # the strip across the click's row: a list row's label sits to the left


def _diff(old: QImage, new: QImage, rect: QRect) -> tuple[int, int]:
    """(pixels that differ by more than 48 in a channel, columns holding any of them) inside `rect`."""
    a, b = (img.copy(rect).convertToFormat(QImage.Format.Format_RGB32) for img in (old, new))
    pa, pb = a.constBits(), b.constBits()
    pa.setsize(a.sizeInBytes())
    pb.setsize(b.sizeInBytes())
    da, db, stride = bytes(pa), bytes(pb), a.bytesPerLine()
    changed, cols = 0, set()
    for row in range(a.height()):
        i = row * stride
        if da[i:i + stride] == db[i:i + stride]:
            continue
        for x in range(a.width()):
            j = i + 4 * x
            if max(abs(da[j] - db[j]), abs(da[j + 1] - db[j + 1]), abs(da[j + 2] - db[j + 2])) > 48:
                changed += 1
                cols.add(x)
    return changed, len(cols)


def spot_changed(old: QImage, new: QImage, x: int, y: int) -> bool:
    """True when the click at (x, y) could hit something else than the model saw: a fifth of the square around it
    changed (moved, scrolled, re-rendered, covered), or text along its row did, so an identical button there now
    belongs to another row. A blinking caret (under 6 columns) or a hover tint does not count."""
    if old.size() != new.size():
        return True
    square = QRect(x - SPOT_R, y - SPOT_R, 2 * SPOT_R + 1, 2 * SPOT_R + 1).intersected(old.rect())
    if square.isEmpty():
        return False
    changed, _cols = _diff(old, new, square)
    if changed * 5 > square.width() * square.height():
        return True
    row = QRect(x - ROW_LEFT, y - ROW_R, ROW_LEFT + ROW_RIGHT + 1, 2 * ROW_R + 1).intersected(old.rect())
    return _diff(old, new, row)[1] >= 6


FOLLOW_PX = 300  # how far up or down a moved target is looked for


def _rows(img: QImage, rect: QRect) -> list[bytes]:
    part = img.copy(rect).convertToFormat(QImage.Format.Format_RGB32)
    bits = part.constBits()
    bits.setsize(part.sizeInBytes())
    data, stride = bytes(bits), part.bytesPerLine()
    return [data[i * stride:(i + 1) * stride] for i in range(part.height())]


def follow(old: QImage, new: QImage, x: int, y: int) -> tuple[int, int] | None:
    """Where the target at (x, y) went: the strip along its row (label and button) found again exactly once,
    shifted up or down by up to FOLLOW_PX (a banner pushed the page, a row was inserted, the list re-sorted, it
    scrolled). None when it is gone, found more than once, or the strip is blank.
    shortcut: vertical moves with pixel-identical rendering only; a sideways move or a re-styled row is refused."""
    if old.size() != new.size():
        return None
    strip = QRect(x - ROW_LEFT, y - SPOT_R, ROW_LEFT + ROW_RIGHT + 1, 2 * SPOT_R + 1).intersected(old.rect())
    want = _rows(old, strip)
    if len(set(want)) < 2:
        return None
    have = _rows(new, QRect(strip.x(), 0, strip.width(), new.height()))
    top, n, hits = strip.y(), len(want), []
    for start in range(max(0, top - FOLLOW_PX), min(len(have) - n, top + FOLLOW_PX) + 1):
        if start != top and have[start] == want[0] and have[start:start + n] == want:
            hits.append(start - top)
            if len(hits) > 1:
                return None
    return (x, y + hits[0]) if hits else None


class AiToolHost(QObject):
    """The ToolHost the chat hands to ask(); `window` is the PetWindow."""

    _call = pyqtSignal(object)
    activity = pyqtSignal(str)  # the tool that runs now, "approval" while one waits on the user, "" when idle
    agent_event = pyqtSignal(int, str, str)  # (agent n, kind, text) from a sub-agent's thread, for its sheet

    def __init__(self, window: Any, mcp: McpHub | None = None) -> None:
        super().__init__(window)  # lives on the Qt thread, so the queued signal lands there
        self._win = window
        self.saved: list[Path] = []
        self._shot = ""
        self._ref: QImage | None = None  # the screen as the model last saw it: screen_map, then after each action
        self._maps = 0
        self._refused: tuple[int, int, int] | None = None  # (x, y, map) of the last click refused as stale
        self.mcp = mcp or McpHub()
        self.procs = BackgroundProcs()
        self.cancel = threading.Event()  # the chat swaps in a fresh one per answer and sets it on Stop
        self.approve_s = APPROVE_TIMEOUT_S  # the chat raises it for an errand's answer
        self.outside = False  # ask() sets it while the chat holds web, MCP or agent content (see _allowed)
        self.agents = Agents(self._run_agent, lambda text, source: self._win._ai_chat.add_branch_note(text, source),
                             self.agent_event.emit)
        self._call.connect(self._on_call)
        self._idle_timer = QTimer(self)
        self._idle_timer.timeout.connect(self._close_idle_mcp)
        self._idle_timer.start(60_000)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.mcp.close)
            app.aboutToQuit.connect(self.procs.stop_all)

    def _close_idle_mcp(self) -> None:
        if getattr(self._win._ai_chat, "_job", None) is None:  # never mid-answer
            threading.Thread(target=self.mcp.close_idle, daemon=True).start()  # taskkill blocks briefly

    @property
    def specs(self) -> list[dict[str, Any]]:
        """Built-in tools plus the MCP servers' (started on first use, on the worker thread)."""
        return TOOL_SPECS + AGENT_SPECS + self.mcp.specs()

    def blocked(self, name: str) -> str | None:
        """ask() refuses a research tool while this chat's sub-agents still run (see Agents.blocked)."""
        return self.agents.blocked(name, self.cancel)

    def run(self, name: str, args: dict[str, Any]) -> str:
        """One tool call; the pet acts it out meanwhile (`activity`)."""
        self._tool = name  # what the pet goes back to acting out after an approval
        self.activity.emit(name)
        try:
            return self._run(name, args)
        finally:
            self.activity.emit("")

    def _run(self, name: str, args: dict[str, Any]) -> str:
        server = self.mcp.server_of(name)
        if server is not None:
            chat = self._win._ai_chat
            shown = json.dumps(args, ensure_ascii=False)
            # Values only: an argument named "format" or "truncate" is not a destructive command
            risk = "destructive tool" if self.mcp.is_destructive(name) else destructive_reason("\n".join(_strings(args)))
            allowed = self._waiting(lambda done: chat.confirm_mcp(server, name, done, shown[:600], risk))
            return self.mcp.call(name, args) if allowed is True else "error: the user did not allow this MCP server"
        if name in FILE_TOOLS:
            secrets = False
            if name == "read_file":
                try:
                    path = resolve(self._project(), args.get("path"))
                except (OSError, ValueError):
                    path = None
                if path is not None and is_secret(path) and path.is_file():
                    secrets = self._allowed("read_secret", f"The model wants to read {path}, which looks like a "
                                            "secrets file (keys, passwords or tokens).", "secret files",
                                            "secrets file")
            return run_file_tool(self._project(), name, args, secrets=secrets)
        if name in ("edit_file", "edit_files"):
            try:
                edits = plan_edits(self._project(), args) if name == "edit_files" else [plan_edit(self._project(), args)]
            except ValueError as exc:
                return f"error: {exc}"
            if len(edits) == 1:
                verb = "create" if edits[0].before is None else "edit"
                ask = f"The model wants to {verb} {edits[0].rel}:\n\n{edits[0].diff}"
            else:
                diffs = "\n\n".join(f"{'new file' if e.before is None else 'edit'} {e.rel}\n{e.diff}" for e in edits)
                ask = f"The model wants to change {len(edits)} files, all or none:\n\n{diffs}"
            # Only edits wholly inside the Project Folder share the "edit_file" approval:
            # elsewhere (Startup, secrets, Paperduck's own skills and MCP config) asks under its own key.
            root, app_data = self._project(), skills_dir().parent.resolve()
            inside = root is not None and all(
                e.path.resolve().is_relative_to(root.resolve()) and not e.path.resolve().is_relative_to(app_data)
                and not is_secret(e.path) for e in edits)
            if not self._allowed("edit_file" if inside else "edit_outside", ask,
                                 "file edits" if inside else "edits outside the Project Folder"):
                return "error: the user declined this edit"
            return apply_edits(edits)
        if name in ("set_errand", "cancel_errand"):
            return self._errand(name, args)
        if name == "spawn_agent":  # read-only tools only, so nothing to allow; Stop on this answer stops it
            share, task = args.get("share_images") is True, args.get("task")
            if isinstance(task, str) and (notes := self._shared_notes(share)):
                task += notes
            auto = bool(getattr(getattr(getattr(self._win, "_ai_chat", None), "_plugin", None), "auto", False))
            return self.agents.spawn(task, args.get("effort"), self._project(), self.cancel,
                                     args.get("_local") is True, self._shared_images(share), self.see_image, auto)
        if name == "wait_agents":
            return self.agents.wait(args.get("timeout_s"), self.cancel)
        if name == "calculate":  # pure arithmetic, nothing to allow
            return calculate(args.get("expression"))
        if name == "plan_tasks":
            try:
                tasks = task_list(args)
            except ValueError as exc:
                return f"error: {exc}"
            self._on_gui(lambda done: done(self._win._ai_chat.set_tasks(tasks) or "ok"))
            return f"task list shown ({sum(1 for _t, s in tasks if s == 'done')} of {len(tasks)} done)"
        if name in ("run_command", "run_powershell"):
            ps = name == "run_powershell"
            command = args.get("script" if ps else "command")
            if not isinstance(command, str) or not command.strip():
                return "error: give a script" if ps else "error: give a command"
            cwd = self._project() or Path.home()
            where = "the Project Folder" if self._project() else f"your home folder ({cwd})"
            risk = destructive_reason(command)
            seconds = command_timeout(args.get("timeout_s"))
            limit = f" (allowed to run up to {seconds:g} s)" if args.get("timeout_s") is not None else ""
            what = " this PowerShell script" if ps else ""
            if not self._allowed("run_command", f"The model wants to run{what} in {where}{limit}:\n\n{command.strip()}",
                                 "commands", risk):
                return "error: the user declined this command"
            script, source = None, command
            if ps:  # the user saw the script; it runs from a file so no cmd.exe quoting touches it
                try:
                    script, command = powershell_file(command)
                except (ValueError, OSError) as exc:
                    return f"error: {exc}"
            pdfs = {pdf: file_stamp(pdf) for pdf in print_to_pdf_paths(command, cwd)}
            media = media_stamps(command, cwd)
            try:
                out = run_command(cwd, command, self.cancel, seconds)
            finally:
                if script is not None:
                    script.unlink(missing_ok=True)
            if script is not None:
                out += powershell_error_note(out, source, script.name)
            for pdf, before in pdfs.items():  # headless Edge stamps HeadlessChrome, Skia and dates
                # msedge.exe hands off to a child and returns before the PDF is written: wait for the new file
                if wait_for_new_file(pdf, before, PDF_WAIT_S, self.cancel):
                    out += f"\n[Paperduck: {pdf.name}: metadata {scrub_metadata(pdf)}]"
                else:
                    out += f"\n[Paperduck: {pdf.name} was not written within {PDF_WAIT_S:g} s; call clean_metadata on it]"
            # Other PDFs and images the command made may carry metadata too; they are the user's to keep as they
            # are (a build's own fixtures, say), so the model is told rather than the files changed
            unclean = [p for p in changed_media(media, cwd, command)
                       if p not in pdfs and scrub_metadata(p, dry_run=True) == "would clean"]
            if unclean:
                names = ", ".join(_rel_name(p, cwd) for p in unclean[:8])
                out += (f"\n[Paperduck: this made or changed {names}, which still carry metadata (author, creating app, "
                        "dates). Call clean_metadata on each one you hand to the user.]")
            return out
        if name == "start_background":
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return "error: give a command"
            cwd = self._project() or Path.home()
            where = "the Project Folder" if self._project() else f"your home folder ({cwd})"
            # its own key: allowing background starts for this chat must not also allow every foreground command
            if not self._allowed("start_background", f"The model wants to start this in the background in {where}. It keeps "
                                 f"running until you stop it:\n\n{command.strip()}", "background commands",
                                 destructive_reason(command)):
                return "error: the user declined this command"
            out = self.procs.start(cwd, command)
            self._on_gui(lambda done: done(self._win._ai_chat.watch_procs() or "ok"))
            return out
        if name == "read_background":
            return self.procs.read(args.get("id"))
        if name == "stop_background":
            return self.procs.stop(args.get("id"))
        if name == "load_skill":
            return load_skill(skills_dir(), args.get("name"))
        if name == "find_skill":
            return find_skills(skills_dir(), args.get("query"))
        if name == "find_mcp_servers":
            query = args.get("query")
            return find_servers(query.strip()) if isinstance(query, str) and query.strip() else "error: give a query"
        if name == "add_mcp_server":
            return self._add_mcp_server(args)
        if name == "add_skill":
            fields = [args.get(k) for k in ("name", "description", "instructions")]
            if not all(isinstance(f, str) and f.strip() for f in fields):
                return "error: give a name, a description and instructions"
            skill, description, instructions = (f.strip() for f in fields)
            text = skill_text(description, instructions)
            if not self._allowed("add_skill", f'The model wants to save a skill "{skill}" that later chats will '
                                 f"follow as your own instructions:\n\n{text}", "saving skills"):
                return "error: the user declined this skill"
            return write_skill(skills_dir(), skill, text)
        if name == "update_skill":
            plan = lambda: plan_skill_update(skills_dir(), args.get("name"), args.get("description"),  # noqa: E731
                                             args.get("instructions"))
            try:
                path, text, diff = plan()
                if not self._allowed("update_skill", f'The model wants to change your skill "{path.parent.name}", '
                                     f"which later chats follow as your own instructions:\n\n{diff}", "skill updates"):
                    return "error: the user declined this skill change"
                if plan()[2] != diff:
                    return "error: the skill changed while the user was deciding; load_skill it again"
                path.write_text(text, encoding="utf-8")
            except ValueError as exc:
                return f"error: {exc}"
            except OSError as exc:
                return f"error: could not save the skill ({exc.strerror or 'write failed'})"
            return f'updated skill "{path.parent.name}"'
        if name in ("remember", "forget"):
            try:
                _text, note = plan_memory(memory_path(), name, args.get("note"))
            except ValueError as exc:
                return f"error: {exc}"
            verb = "remember this in every later chat" if name == "remember" else "forget this note"
            if not self._allowed("memory", f"The model wants to {verb}:\n\n{note}", "memory changes"):
                return "error: the user declined this memory change"
            try:
                text, again = plan_memory(memory_path(), name, args.get("note"))  # the file may have changed meanwhile
            except ValueError as exc:
                return f"error: {exc}"
            if again != note:
                return "error: memory changed while the user was deciding; try again"
            return write_memory(memory_path(), text)
        if name == "read_screen":
            image = self._on_gui(self._grab_screen)
            return self._ocr(image) if isinstance(image, QImage) and not image.isNull() else "error: no screen grab"
        if name == "screen_map":
            got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
            if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
                return "error: no screen grab"
            return self._map(*got)
        if name in COMPUTER_TOOLS:
            return self._computer(name, args)
        if name == "computer_batch":
            return self._batch(args.get("steps"))
        if name == "preview_document":  # reads a private copy in a hidden Office instance, so it needs no approval
            try:
                path = resolve(self._project(), args.get("path"))
            except (OSError, ValueError):
                return "error: bad path"
            text, sheet = preview_document(path, args.get("pages"))
            if sheet is not None:
                from src.ui.chat_widgets import image_data_url

                self._shot = image_data_url(sheet)  # sent like screen_map's screenshot (dropped for blind models)
            return text
        if name == "view_image":  # a read, so no approval; the picture rides along like screen_map's screenshot
            text, self._shot = self.see_image(self._project(), args)
            return text
        if name == "clean_metadata":  # only blanks metadata in a PDF/PNG/JPEG, so it needs no approval
            try:
                path = resolve(self._project(), args.get("path"))
            except (OSError, ValueError):
                return "error: bad path"
            return scrub_metadata(path) if path.is_file() else "error: not a file"
        if name == "save_file":
            try:
                file_name, content = save_args(args)
            except ValueError as exc:
                return f"error: {exc}"
            out = self._on_gui(lambda done: done(self._save_file(file_name, content)), SAVE_TIMEOUT_S)
            return out if isinstance(out, str) else "error: the user did not answer the Save dialog in time"
        out = self._on_gui(lambda done: done(self._pet_tool(name, args)))
        return out if isinstance(out, str) else "error: the pet did not answer in time"

    def _errand(self, name: str, args: dict[str, Any]) -> str:
        """set_errand asks every time (it is an instruction that runs later, maybe while the user is away);
        cancel_errand only removes one. The plugin is touched on the Qt thread, where the pet polls it."""
        errands = self._win._state_machine.registry.get_plugin("errands")
        if errands is None:
            return "error: errands are not available"
        if name == "cancel_errand":
            out = self._on_gui(lambda done: done(errands.cancel(args.get("id"))))
            return out if isinstance(out, str) else "error: the pet did not answer in time"
        try:
            from src.tools.errands import parse_when

            kind, arg = parse_when(args.get("when"))
        except ValueError as exc:
            return f"error: {exc}"
        instruction = args.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return "error: give the instruction to follow when it fires"
        if not self._allowed(f"errand:{time.monotonic()}", f"The model wants to set an errand: {describe_errand(kind, arg)}, "
                             "it will run this in this chat (edits and commands still ask you):\n\n"
                             f"{instruction.strip()}", "errand"):
            return "error: the user declined this errand"

        def add(done: Callable[[Any], None]) -> None:
            try:
                errand = errands.add(args.get("when"), instruction, self._win._ai_chat.ensure_sid())
            except ValueError as exc:
                done(f"error: {exc}")
                return
            self._win.update()  # the errand tag
            done(f"errand {errand.id} set: {describe_errand(errand.kind, errand.arg)}. It runs in this chat when "
                 "that happens; tell the user they can leave it to you.")

        out = self._on_gui(add)
        return out if isinstance(out, str) else "error: the pet did not answer in time"

    def _computer(self, name: str, args: dict[str, Any]) -> str:
        """Mouse and keyboard on the primary monitor. Every action needs the user's Allow (once or for the chat); a
        delete key or a destructive typed command asks every time."""
        try:
            what, risk = computer_action(name, args)
        except ValueError as exc:
            return f"error: {exc}"
        if not self._allowed("computer", f"The model wants to control your computer:\n\n{what}", "computer control",
                             risk):
            return "error: the user declined this action"
        note = ""
        if name == "mouse_click":
            if args.get("name") is not None:
                found = self._find_click(args)
                if isinstance(found, str):  # an error, or pressed without the mouse
                    return found
                args = {**args, "x": found[0], "y": found[1]}
            note, x, y = self._stale_click(args["x"], args["y"])
            if note.startswith("error"):
                return note
            args = {**args, "x": x, "y": y}
        out = self._on_gui(lambda done: self._act(done, name, args), 90.0)  # up to 2000 chars at 20 ms
        return note + out if isinstance(out, str) else "error: the action did not finish in time"

    def _map(self, image: QImage, hwnd: int) -> str:
        """screen_map's text for a grab; the grab becomes what the next click is checked against."""
        with ThreadPoolExecutor(max_workers=2) as pool:  # OCR and the control tree overlap
            controls = pool.submit(computer.ui_elements, hwnd)
            lines = pool.submit(self._ocr, image, True).result()
            elements = controls.result()
        from src.ui.chat_widgets import MAX_IMAGE_SIDE, image_data_url

        side = max(image.width(), image.height())
        self._shot = image_data_url(image)
        self._ref, self._maps = image, self._maps + 1
        note = (f"The screenshot you see is scaled down {side / MAX_IMAGE_SIDE:.2f}x: do not read pixels from "
                "it, use the (x, y) listed here." if side > MAX_IMAGE_SIDE else "The screenshot is at full size.")
        return parse_boxes(lines, image.width(), image.height(), elements, note)

    def _find_click(self, args: dict[str, Any]) -> tuple[int, int] | str:
        """Where the control `name` (on the row of `near`) is right now, from a fresh look. When something else covers
        its spot (a cookie bar, a toolbar) it is pressed through UI Automation instead, never under a dialog."""
        got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
        if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
            return "error: no screen grab"
        image, hwnd = got
        near = args.get("near") or ""
        with ThreadPoolExecutor(max_workers=2) as pool:  # OCR (only needed to find `near`) and the controls overlap
            controls = pool.submit(computer.ui_elements, hwnd)
            ocr = self._ocr(image, True) if near.strip() else ""
            elements = controls.result()
        found = find_control(elements, ocr, args["name"], near, image.width(), image.height())
        if isinstance(found, str):
            return found
        x, y, label = found
        self._ref = image  # the check right before the click compares against this look
        fold = lambda t: " ".join(t.split()).casefold()  # noqa: E731
        chain = [] if computer.ours_at(x, y) else computer.uia_at(x, y)  # clicks pass through the pet and chat
        if not chain or any(fold(n) == fold(label) for _kind, n in chain):
            return x, y
        cover = next((n for _kind, n in chain if n.strip()), "something else")[:60]
        if computer.front_state()[1] or any(kind in ("dialog", "alert", "alert dialog") for kind, _n in chain):
            return f'error: not clicked: a dialog ("{cover}") covers "{label}"; answer or close it first'
        if computer.uia_invoke(hwnd, x, y, label):
            return (f'pressed "{label}" at ({x}, {y}) through Windows UI Automation, because "{cover}" covers its '
                    "spot. Call screen_map for details.")
        return f'error: not clicked: "{cover}" covers "{label}" at ({x}, {y}); close it or scroll it out of the way first'

    def _stale_click(self, x: int, y: int) -> tuple[str, int, int]:
        """(note, where to click). The note is an error when the screen at (x, y) changed since the model last saw
        it and the target cannot be found again, so the click could hit the wrong thing; a target that only moved
        is followed. A spot that changes again right after a fresh screen_map is animated and gets clicked."""
        if self._ref is None:
            return "", x, y
        got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
        if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
            return "", x, y  # no grab to compare: click as before
        if not spot_changed(self._ref, got[0], x, y):
            return "", x, y
        if (moved := follow(self._ref, got[0], x, y)) is not None:
            return f"The target moved from ({x}, {y}) to {moved} since you looked; followed it and ", *moved
        if self._refused is not None and self._refused[:2] == (x, y) and self._refused[2] != self._maps:
            return "", x, y
        self._refused = (x, y, self._maps)  # against this map; the one below is a fresh look
        return (f"error: not clicked: the screen at ({x}, {y}) changed since you last looked (the page moved, a list "
                "row shifted, it scrolled, re-rendered or something covers it), so the click could land on the wrong "
                "thing. The screen now (a fresh screen_map, no need to call it): find your target again and click its "
                "new position.\n\n" + self._map(*got), x, y)

    def _batch(self, steps: Any) -> str:
        """computer_batch: each step through `_computer` (same approvals), stopping at the first error or Stop."""
        if not isinstance(steps, list) or not steps or len(steps) > MAX_BATCH_STEPS:
            return f"error: give 1 to {MAX_BATCH_STEPS} steps"
        cancel, lines = self.cancel, []
        for i, step in enumerate(steps, 1):
            tool = step.get("tool") if isinstance(step, dict) else None
            if tool not in COMPUTER_TOOLS:
                return "\n".join([*lines, f"error: step {i} needs \"tool\" set to one of {', '.join(sorted(COMPUTER_TOOLS))}"])
            if cancel.is_set():
                return "\n".join([*lines, "error: stopped by the user"])
            out = self._computer(tool, {k: v for k, v in step.items() if k != "tool"})
            lines.append(f"{i}. {out}")
            if out.startswith("error"):
                return "\n".join([*lines, f"error: stopped at step {i} of {len(steps)}"])
        return "\n".join(lines)

    def _act(self, done: Callable[[Any], None], name: str, args: dict[str, Any]) -> None:
        """Make the pet and the chat click-through (hidden only where Windows cannot), give the keyboard to the
        app under them, act, then restore them."""
        shown = _ours(self._win)
        hidden = [w for w in shown if not computer.click_through(int(w.winId()), True)]
        for w in hidden:
            w.hide()
        before = [("", False)]

        def act() -> str:  # on a worker thread: typing takes 20 ms a character and must not freeze the pet
            computer.focus_front_app()  # the chat has the keyboard after the user pressed Enter
            before[0] = computer.front_state()
            try:
                if name == "mouse_click":
                    computer.click(args["x"], args["y"], args.get("button") or "left", bool(args.get("double")))
                    result = "clicked"
                elif name == "type_text":
                    computer.type_text(args["text"])
                    result = f"typed {min(len(args['text']), computer.MAX_TYPE_CHARS)} characters"
                elif name == "press_key":
                    computer.press(computer.parse_keys(args["keys"]))
                    result = f"pressed {args['keys']}"
                else:
                    computer.scroll(args["amount"], args.get("x"), args.get("y"))
                    result = "scrolled"
            except (ValueError, OSError, TypeError, ctypes.ArgumentError) as exc:  # a bad arg must not hang the call
                result = f"error: {exc}"
            return result

        def finish(result: str) -> None:
            for w in shown:  # back without taking the keyboard from the app the model is driving
                if w in hidden:
                    show_quietly(w)
                else:
                    computer.click_through(int(w.winId()), False)
            text = (result + "." + computer.front_note(before[0], computer.front_state()) + " Call screen_map for details."
                    if not result.startswith("error") else result)
            if self._ref is None:
                done(text)
                return

            def keep(got: Any) -> None:  # what this action left is what a planned next click aims at
                if isinstance(got, tuple) and isinstance(got[0], QImage) and not got[0].isNull():
                    self._ref = got[0]
                done(text)

            self._grab_screen(keep, primary=True)

        def work() -> None:
            result = act()
            # back on the Qt thread; the app needs a moment to react before the next look
            self._call.emit((lambda _done: QTimer.singleShot(250, lambda: finish(result)),
                             {"event": threading.Event(), "result": None}))

        QTimer.singleShot(180 if hidden else 0, lambda: threading.Thread(target=work, daemon=True).start())

    def _shared_images(self, share: bool) -> list[str]:
        """The pictures of the newest user turn that has any (data URLs, at most MAX_ATTACHMENTS), when asked for."""
        from src.ui.chat_widgets import MAX_ATTACHMENTS

        turns = list(self._win._ai_chat._history) if share else []
        return next((t["images"][:MAX_ATTACHMENTS] for t in reversed(turns) if t["role"] == "user" and t.get("images")), [])

    def _shared_notes(self, share: bool) -> str:
        """What a shared picture's task text adds: that the pictures are in the message, and their OCR text. Live,
        a model that takes an image without an error but cannot read it searched the disk for the file instead."""
        turns = list(self._win._ai_chat._history) if share else []
        notes = next((t.get("image_notes") or [] for t in reversed(turns) if t["role"] == "user" and t.get("images")), [])
        if not notes:
            return ""
        return ("\n\n[Paperduck: the user's pictures are attached to this message itself; they are not files, so do "
                "not look for them on disk. Their text, read by OCR:]\n" + "\n".join(notes))[:MAX_SHARED_NOTE_CHARS]

    def _run_agent(self, task: str, effort: str, host: ReadOnlyHost, cancel: threading.Event) -> str:
        """A sub-agent's thread: one ask() with the chat's provider and model at the agent's own thinking level."""
        plugin = self._win._ai_chat._plugin
        url, model = plugin.base_url, plugin.model
        where = f"the Project Folder {host.root}" if host.root else f"the home folder {Path.home()}"
        messages = [{"role": "system", "content": AGENT_PROMPT.format(calls=MAX_AGENT_CALLS, where=where, today=date.today())},
                    {"role": "user", "content": [{"type": "text", "text": task}, *(
                        {"type": "image_url", "image_url": {"url": u}} for u in host.images)] if host.images else task}]
        return ask(url, load_key(url), model, messages, thinking=fit_thinking(url, effort, model), host=host,
                   cancel=cancel).text

    def _add_mcp_server(self, args: dict[str, Any]) -> str:
        server, command = args.get("name"), args.get("command")
        cmd_args, env = args.get("args") or [], args.get("env") or {}
        if (not isinstance(server, str) or not server or safe_name(server) != server or SEP in server
                or not isinstance(command, str) or not command.strip()
                or not isinstance(cmd_args, list) or not all(isinstance(a, str) for a in cmd_args)
                or not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values())):
            return "error: give a name (letters, digits, single _ and -), a command, string args and string env values"
        line = " ".join([command.strip(), *cmd_args])
        env_note = env_summary(env)  # values too (NODE_OPTIONS can run code), secret-looking ones hidden
        if not self._allowed("add_mcp_server", f'The model wants to install MCP server "{server}". It will run this '
                             f"on your PC with your permissions whenever the AI chat starts:\n\n{line}{env_note}",
                             "MCP installs"):
            return "error: the user declined this MCP server"
        out = add_server(server, command.strip(), cmd_args, {str(k): v for k, v in env.items()}, self.mcp.path)
        if out.startswith("error"):
            return out
        tools = [s["function"]["name"] for s in self.mcp.specs() if self.mcp.server_of(s["function"]["name"]) == server]
        if not tools:
            return out + ", but it did not start (wrong command, missing npx / uvx, or a missing env value)"
        return out + (f"; it started with these tools: {', '.join(tools)}. If they are not in your tool list yet, "
                      "they are from the user's next message, so tell the user to send one.")

    def _project(self) -> Path | None:
        repo = getattr(self._win._state_machine.registry.get_plugin("project"), "repo", None)
        return repo if repo is not None and Path(repo).is_dir() else None  # a deleted folder would fail every command

    def _allowed(self, key: str, text: str, what: str, risk: str = "") -> bool:
        """The user's answer in the chat; no answer within APPROVE_TIMEOUT_S counts as declined.
        A `risk` (destructive command) is asked every time, even when the chat allowed the tool before. So is a
        tool in OUTSIDE_GUARDED once the chat holds outside content, which could have told the model what to do."""
        chat = self._win._ai_chat
        extra: dict[str, Any] = {"danger": risk} if risk else {"untrusted": True} if (
            self.outside and key in OUTSIDE_GUARDED) else {}
        ask = lambda done: chat.confirm(key, text, what, done, **extra)  # noqa: E731
        return self._waiting(ask) is True

    def _waiting(self, ask: Callable[[Callable[[Any], None]], None]) -> Any:
        """An approval on the Qt thread: the pet waits with the user (`activity` "approval") until it is answered."""
        self.activity.emit("approval")
        try:
            return self._on_gui(ask, self.approve_s)
        finally:
            self.activity.emit(getattr(self, "_tool", ""))

    def allow_link(self, name: str, url: str) -> bool:
        """A link the model built after reading local data: the user checks the full URL for their data."""
        verb = {"open_link": "open this link in your browser", "fetch_page": "fetch this page",
                "spawn_agent": "give a sub-agent this link"}.get(name, f"pass this link to the MCP tool {name}")
        return self._allowed("link", f"The model wants to {verb} after reading your files, clipboard or screen. "
                             f"Check the address does not carry your data:\n\n{url[:2000]}", "links",
                             "new link after reading your data")

    def see_image(self, root: Path | None, args: dict[str, Any]) -> tuple[str, str]:
        """view_image for this chat and its sub-agents: (text with size and OCR, data URL for a model that sees)."""
        from src.ui.chat_widgets import image_data_url

        try:
            path = resolve(root, args.get("path"))
            if is_secret(path):
                return "error: that looks like a secrets file and view_image does not open those", ""
            image = load_image_file(path)
        except (OSError, ValueError) as exc:
            return f"error: {exc}", ""
        small = image if max(image.width(), image.height()) <= MAX_OCR_SIDE else image.scaled(
            MAX_OCR_SIDE, MAX_OCR_SIDE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        text = self._ocr(small, empty="no text found in it")
        return f"{path.name}: {image.width()}x{image.height()} px. Text in it (OCR):\n{text}", image_data_url(image)

    def take_image(self) -> str:
        """The screenshot (data URL) of the last screen_map, once: the request loop sends it to a model that sees."""
        shot, self._shot = self._shot, ""
        return shot

    def take_saved(self) -> list[Path]:
        """Files saved since the last call (the chat shows a row for each)."""
        saved, self.saved = self.saved, []
        return saved

    # ------------------------------------------------------------------ thread bridge

    def _on_gui(self, fn: Callable[[Callable[[Any], None]], None], timeout: float = GUI_TIMEOUT_S) -> Any:
        """Run fn(done) on the Qt thread and wait for done(result)."""
        box: dict[str, Any] = {"event": threading.Event(), "result": None}
        self._call.emit((fn, box))
        box["event"].wait(timeout)
        return box["result"]

    def _on_call(self, item: tuple[Callable, dict[str, Any]]) -> None:
        fn, box = item

        def done(result: Any) -> None:
            box["result"] = result
            box["event"].set()

        try:
            fn(done)
        except Exception as exc:  # a tool bug must not leave the worker waiting
            done(f"error: {type(exc).__name__}")

    # ------------------------------------------------------------------ Qt-thread tools

    def _grab_screen(self, done: Callable[[Any], None], primary: bool = False) -> None:
        """Every screen read of a chat comes here; one that shows a browser, reader or chat app arms `outside`."""
        def got(result: Any) -> None:
            if result is not None and computer.visible_apps() & OUTSIDE_APPS:
                self.outside = True  # the page or message on screen could carry instructions, like a fetched page
            done(result)

        grab_screen(self._win, got, primary)

    def _save_file(self, file_name: str, content: str) -> str:
        """Nothing is written unless the user picks a path; the dialog itself confirms overwrites."""
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        path, _filter = QFileDialog.getSaveFileName(self._win._ai_chat, "Save file from Paperduck AI",
                                                    str(Path(downloads) / file_name))
        if not path:
            return "the user cancelled the save"
        try:
            Path(path).write_text(content, encoding="utf-8", newline="")
        except OSError as exc:
            return f"error: could not save ({exc.strerror or 'write failed'})"
        self.saved.append(Path(path))
        return f"saved to {path}"

    def _pet_tool(self, name: str, args: dict[str, Any]) -> str:
        cb = QGuiApplication.clipboard()
        if name == "read_clipboard":
            if cb is not None and clipboard_is_private(cb.mimeData()):
                return "the clipboard holds a copy a password manager marked private; it is not read"
            text = cb.text() if cb is not None else ""
            return text[:MAX_CLIP_CHARS] if text.strip() else "the clipboard has no text"
        if name == "copy_text":
            text = args.get("text")
            if not isinstance(text, str) or not text or cb is None:
                return "error: nothing to copy"
            cb.setText(text)
            return "copied to the clipboard"
        if name == "pet_status":
            rows = []
            for plugin in self._win._state_machine.registry.plugins:
                hud = plugin.get_hud_data()
                bars = ", ".join(f"{b.label} {(b.display_text or f'{b.value:.0f}%').strip()}" for b in hud.bars)
                rows.append(f"{hud.badge_left} [{hud.badge_right}] {bars}. {hud.detail}".strip())
            project = self._win._state_machine.registry.get_plugin("project")
            repo = getattr(project, "repo", None)
            errands = self._win._state_machine.registry.get_plugin("errands")
            waiting = errands.summary() if errands is not None else ""
            return "\n".join([*rows, f"Project Folder: {repo or 'none chosen'}",
                              "Errands waiting:\n" + waiting if waiting else "No errands waiting"])
        if name == "pet_action":
            action = args.get("action")
            if action not in PET_ACTIONS:
                return "error: not an allowed action"
            self._win.trigger_tool_action(action)
            return f"done: {PET_ACTIONS[action]}"
        if name == "run_macro":
            macro = self._win.macros.find(args.get("name") if isinstance(args.get("name"), str) else "")
            if macro is None:
                return "error: no such macro. Saved: " + ", ".join(m.name for m in self._win.macros.macros)
            if any(step in ("ask", "chat") for step, _arg in parse_recipe(macro.recipe)):  # it would use this busy chat
                return "error: that macro uses the AI chat itself, so the user has to run it"
            self._win._macro_runner.run(macro)
            return f"started macro {macro.name}: {macro.recipe}"
        if name == "open_link":
            url = args.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                return "error: web links (http or https) only"
            self._win.trigger_tool_action("web:" + url)
            return "opened in the browser"
        return "error: unknown tool"

    # ------------------------------------------------------------------ worker-thread OCR

    @staticmethod
    def _ocr(image: QImage, boxes: bool = False, empty: str = "no text found on screen") -> str:
        tmp = Path(tempfile.mkdtemp(prefix="paperduck_ocr_"))
        png = tmp / "screen.png"
        try:
            image.save(str(png), "PNG")
            text = ocr_image_file(png, boxes=boxes)
        finally:
            for leftover in (png, png.with_suffix(".ps1")):
                leftover.unlink(missing_ok=True)
            tmp.rmdir()
        if boxes:
            return text.strip()[:MAX_SCREEN_CHARS * 2]
        return text.strip()[:MAX_SCREEN_CHARS] or empty
