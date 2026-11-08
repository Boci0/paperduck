"""Multi-tool actions of the pet window: drops, wheels, updates, snips and OCR (mixed into PetWindow)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from PyQt6.QtCore import QDateTime, QPoint, QProcess, QStandardPaths, QTimer, QUrl
from PyQt6.QtGui import QColor, QCursor, QDesktopServices, QGuiApplication, QImage
from PyQt6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMenu

from src.engine.models import PetState
from src.tools.file_forge import ForgeMode, ForgeWheelOption, get_wheel_options_for_paths
from src.ui.ai_tool_host import show_quietly
from src.tools.web_drop import WebImageJob, html_image_src, is_web_image_source, strip_metadata
from src.ui.context_menu import style_menu
from src.ui.drop_wheel import RadialDropWheel
from src.ui.quick_tools import OcrJob, SnipSession, open_folder_in_background
from src.ui.tool_wheel import WEB_SHORTCUTS, ToolWheel, ToolWheelOption
from src.updater import APP_DIR, FROZEN, FROZEN_EXE, RELEASES_URL, UpdateJob, is_installed


from src.ui.pet_helpers import clamp_position_to_screens


class ToolsMixin:
    """Methods of PetWindow; state and Qt signals live on PetWindow itself."""

    # ------------------------------------------------------------------
    # Interactive Multi-Tool Actions
    # ------------------------------------------------------------------

    def trigger_feed(self) -> None:
        if self._state_machine.refresh_metrics():
            self._anim.play_oneshot("eating")
            self._schedule_next_anim_tick()
            cpu = int(round(self._state_machine.stats.cpu_usage))
            self._show_toast(f"CPU {cpu}%", QColor("#38B764"))
            self._pin_hud(36)

    def trigger_play(self) -> None:
        running = self._state_machine.toggle_pomodoro()
        if running:
            self._anim.set_state("focus", force=True)
            self._schedule_next_anim_tick()
            self._show_toast("FOCUS!", QColor("#FFCD75"))
        else:
            self._sync_animation_to_pet_state(self._state_machine.state)
            self._schedule_next_anim_tick()
            self._show_toast("PAUSED", QColor("#8A9AF2"))

    def trigger_pet(self) -> None:
        if self._state_machine.state == PetState.SLEEPING:
            self._show_toast("DND...", QColor("#8A9AF2"))
            return
        plugin = self._state_machine.cycle_tool()
        self._anim.play_oneshot("spin")
        self._schedule_next_anim_tick()
        badge = plugin.tool_id.upper()[:6] if plugin is not None else "TOOL"
        self._show_toast(badge, QColor("#73EFF7"))
        self._pin_hud(36)

    def trigger_select_tool(self, tool_id: str) -> None:
        if self._state_machine.select_tool(tool_id):
            self._show_toast(tool_id.upper()[:6], QColor("#73EFF7"))
            self._pin_hud(36)

    def trigger_drop_files(
        self,
        paths: Sequence[Path | str],
        *,
        mode: ForgeMode | None = None,
    ) -> bool:
        """Feed local files or folders into FileForge with eating animation."""
        if self._state_machine.submit_files(paths, mode=mode):
            self._anim.play_oneshot("eating")
            self._schedule_next_anim_tick()
            self._show_toast("FORGING..", QColor("#FFCD75"))
            return True
        forge = self._state_machine.forge_plugin
        if forge is not None and forge.busy:
            self._show_toast("BUSY!", QColor("#EF7D57"))
        return False

    @property
    def drop_wheel(self) -> RadialDropWheel | None:
        return self._drop_wheel

    def _on_wheel_option_dropped(
        self,
        paths: list[Path | str],
        option: ForgeWheelOption,
    ) -> None:
        self._hovered = False
        self._drag_paths = []
        self._dispatch_drop(paths, option.mode)

    def _on_wheel_cancelled(self) -> None:
        self._hovered = False
        self._drag_paths = []
        self._pending_web_source = None
        self._show_toast("SKIPPED", QColor("#8A9AF2"))

    def _dispatch_drop(self, paths: Sequence[Path | str], mode: ForgeMode | None) -> None:
        source, self._pending_web_source = self._pending_web_source, None
        if source is not None:
            self._start_web_fetch(source, mode or ForgeMode.PNG)
        elif paths:
            self.trigger_drop_files(paths, mode=mode)

    @staticmethod
    def _web_drop_source(mime) -> str | QImage | None:
        """The dragged <img>'s URL, else raw image data, else the first http(s)/data: URL."""
        if mime is None:
            return None
        # A linked image (YouTube avatars, thumbnails) drags the page URL first; the <img> is the picture
        src = html_image_src(mime.html()) if mime.hasHtml() else None
        if src is not None:
            return src
        if mime.hasUrls():
            for url in mime.urls():
                text = url.toString()
                if not url.isLocalFile() and is_web_image_source(text):
                    return text
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage) and not image.isNull():
                return image
        return None

    @staticmethod
    def _web_drop_dir() -> Path:
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        for out_dir in (Path(downloads) / "Paperduck" if downloads else None, Path.home() / ".paperduck" / "drops"):
            if out_dir is None:
                continue
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                return out_dir
            except OSError:
                continue
        return Path.home() / ".paperduck" / "drops"

    def check_for_updates(self, *, install: bool = True) -> None:
        """Ask GitHub for a newer release; `install` also applies it (installed copies only)."""
        job = UpdateJob()
        job.finished.connect(lambda status, tag, j=job, i=install: self._on_update_done(j, status, tag, i))
        if install:  # a slow mirror takes minutes for the 38 MB standalone bundle: show it moving
            job.progress.connect(lambda pct: self._show_toast(f"GET {pct}%", QColor("#8A9AF2")))
        self._update_jobs.append(job)
        job.start(install=install)
        if install:
            self._show_toast("CHECKING", QColor("#8A9AF2"))

    def _on_update_done(self, job: UpdateJob | None, status: str, tag: str, install: bool) -> None:
        if job in self._update_jobs:
            self._update_jobs.remove(job)
        if status == "installed":
            # A frozen build is only staged: the swap after restart can still fail, and the old build then says so
            self._show_toast("RESTARTING" if FROZEN else "UPDATED", QColor("#38B764"))
            if self._on_notification is not None:
                self._on_notification(f"{'Installing' if FROZEN else 'Updated to'} {tag}, restarting")
            QTimer.singleShot(1500, self.restart)
        elif status == "ready":  # the startup check already downloaded and verified it
            self._show_toast(f"NEW {tag}", QColor("#FFCD75"))
            if self._on_notification is not None:
                self._on_notification(f"Paperduck {tag} is downloaded: Settings > Check for Updates installs it")
        elif status == "available":
            self._show_toast(f"NEW {tag}", QColor("#FFCD75"))
            if self._on_notification is not None:
                self._on_notification(f"Paperduck {tag} is available: Settings > Check for Updates")
            if install and not is_installed():
                QDesktopServices.openUrl(QUrl(RELEASES_URL))  # git checkout: update by hand
        elif install:  # the silent startup check never toasts "up to date" or errors
            # On error `tag` carries the reason; signature failures mean the release was refused
            refused = {"bad signature": "BAD SIG", "unsigned release": "UNSIGNED",
                       "version mismatch": "BAD VERSION"}.get(tag)
            self._show_toast("UP TO DATE" if status == "current" else refused or "UPDATE ERR",
                             QColor("#38B764" if status == "current" else "#E05A47"))
            if refused and self._on_notification is not None:
                self._on_notification(f"Update refused ({tag}): it is not a release signed by the Paperduck key")
            elif status == "error" and tag != "needs installer" and self._on_notification is not None:
                self._on_notification(f"Update failed: {tag}")  # the reason, also in the log (Settings > Copy Log)
            if tag == "needs installer":
                self._show_toast("RUN INSTALLER", QColor("#FFCD75"))
                if self._on_notification is not None:
                    self._on_notification("This update changes libraries: run install.ps1 from the releases page")
                QDesktopServices.openUrl(QUrl(RELEASES_URL))

    def restart(self) -> None:
        self.trigger_quit()  # saves state.json before the new copy loads it
        staged = APP_DIR.with_name(APP_DIR.name + ".new") / FROZEN_EXE
        if FROZEN and staged.is_file():  # the staged build swaps itself in once this process has exited
            QProcess.startDetached(str(staged), ["--finish-update", str(os.getpid())], str(APP_DIR.parent))
        elif FROZEN:
            QProcess.startDetached(sys.executable, [], str(APP_DIR.parent))
        else:
            QProcess.startDetached(sys.executable, ["-m", "src.main"], str(APP_DIR))

    def _start_web_fetch(self, source: str | QImage, mode: ForgeMode) -> None:
        job = WebImageJob()
        job.finished.connect(lambda path, err, j=job, m=mode: self._on_web_fetch_done(j, path, err, m))
        self._web_jobs.append(job)
        job.start(source, self._web_drop_dir())
        self._anim.play_oneshot("eating")
        self._schedule_next_anim_tick()
        self._show_toast("FETCHING", QColor("#FFCD75"))

    def _on_web_fetch_done(self, job: WebImageJob, path: object, error: str, mode: ForgeMode) -> None:
        if job in self._web_jobs:
            self._web_jobs.remove(job)
        job.deleteLater()
        if self._is_shutting_down:
            return
        if not isinstance(path, Path):
            toast = {"not an image": "NOT IMAGE", "too large": "TOO BIG"}.get(error, "WEB ERR")
            self._show_toast(toast, QColor("#EF7D57"))
            return
        if mode == ForgeMode.PNG:
            self.recent.add(path)
            self._show_toast("SAVED PNG", QColor("#38B764"))
            self._show_drop_folder(path.parent)
            return
        if self.trigger_drop_files([path], mode=mode):
            self._web_intermediate = path
        else:
            self.recent.add(path)  # forge busy: keep the clean PNG as the result

    def _collect_forge_outputs(self) -> None:
        forge = self._state_machine.forge_plugin
        outputs = forge.pop_finished_outputs() if forge is not None else None
        if outputs is None:
            return
        for out in reversed(outputs):
            self.recent.add(out)
        if outputs:
            self._show_drop_folder(outputs[0].parent)
        intermediate, self._web_intermediate = self._web_intermediate, None
        if intermediate is None:
            return
        if outputs and intermediate not in outputs:
            try:
                intermediate.unlink(missing_ok=True)
            except OSError:
                self.recent.add(intermediate)
        else:
            self.recent.add(intermediate)  # conversion failed: the clean PNG is still useful

    def reveal_recent(self, index: int) -> None:
        entries = self.recent.entries()
        if not 0 <= index < len(entries):
            self._show_toast("GONE", QColor("#EF7D57"))
            return
        try:
            # Windows paths cannot contain quotes, so wrapping is safe
            subprocess.Popen(f'explorer /select,"{entries[index][0]}"')
        except OSError:
            self._show_toast("OPEN ERR", QColor("#EF7D57"))

    def _show_drop_folder(self, folder: Path) -> None:
        """Reveal where a drop's output landed, unless that folder is the visible desktop."""
        desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        if desktop and os.path.normcase(str(folder)) == os.path.normcase(str(Path(desktop))):
            return
        open_folder_in_background(folder)

    def open_output_folder(self, folder: Path) -> None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except OSError:
            self._show_toast("OPEN ERR", QColor("#EF7D57"))

    @staticmethod
    def snips_dir() -> Path:
        pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        return Path(pictures) / "Paperduck" if pictures else Path.home() / ".paperduck" / "snips"

    def _spawn_drop_wheel(
        self,
        paths: Sequence[Path | str],
        local_cursor: QPoint,
        options: Sequence[ForgeWheelOption] | None = None,
    ) -> None:
        if self._drop_wheel is None:
            self._drop_wheel = RadialDropWheel(
                on_option_dropped=self._on_wheel_option_dropped,
                on_cancelled=self._on_wheel_cancelled,
                always_on_top=self._always_on_top,
            )
        if options is None:
            options = get_wheel_options_for_paths(paths)
        sprite_global = self.mapToGlobal(self.sprite_center_point())
        ideal_x = sprite_global.x() - int(RadialDropWheel.CENTER_X)
        ideal_y = sprite_global.y() - int(RadialDropWheel.CENTER_Y)
        wx, wy = clamp_position_to_screens(
            ideal_x,
            ideal_y,
            RadialDropWheel.WHEEL_WIDTH,
            RadialDropWheel.WHEEL_HEIGHT,
            self.available_screen_rects(),
        )
        self._drop_wheel.open_for_paths(
            paths,
            options,
            window_top_left=QPoint(wx, wy),
            initial_global_cursor=self.mapToGlobal(local_cursor),
        )

    @property
    def tool_wheel(self) -> ToolWheel | None:
        return self._tool_wheel

    def tool_wheel_options(self) -> list[ToolWheelOption]:
        """Slices the user chose in Settings > Customize Wheel."""
        return self.wheel.visible(self.all_tool_wheel_options())

    def _wheel_toast(self, text: str) -> None:
        good = text in ("SHOWN", "ADDED", "REMOVED")
        self._show_toast(text, QColor("#38B764" if good else "#8A9AF2" if text in ("HIDDEN", "CANCEL") else "#EF7D57"))

    def _customize_wheel(self, key: str) -> None:
        if key.startswith("toggle:"):
            self._wheel_toast(self.wheel.toggle(key.removeprefix("toggle:"), self.all_tool_wheel_options()))
        elif key == "add_web":
            label, ok = QInputDialog.getText(self, "Paperduck - Add Website", "Slice name:")
            url, ok2 = (QInputDialog.getText(self, "Paperduck - Add Website", "URL:", text="https://")
                        if ok and label.strip() else ("", False))
            self._wheel_toast(self.wheel.add_custom(label, url) if ok2 else "CANCEL")
        elif key == "add_app":
            path, _flt = QFileDialog.getOpenFileName(self, "Paperduck - Add App or File to Wheel")
            self._wheel_toast(self.wheel.add_custom(Path(path).stem, path) if path else "CANCEL")
        elif key.startswith("remove:") and key.removeprefix("remove:").isdigit():
            self._wheel_toast("REMOVED" if self.wheel.remove_custom(int(key.removeprefix("remove:"))) else "GONE")

    def all_tool_wheel_options(self) -> list[ToolWheelOption]:
        """Slices clockwise from 12 o'clock: everyday quick tools, then web shortcuts."""
        self.wheel.macros = [m.name for m in self.macros.macros]  # every wheel lookup passes through here
        pomo = self._state_machine.pomodoro_plugin
        focus_sub = "pause" if pomo is not None and pomo.running else f"{pomo.work_minutes if pomo else 25}m"
        options = [
            ToolWheelOption("OCR", "copy text", "#41A6F6", "snip:text"),
            ToolWheelOption("Clips", "history", "#73EFF7", "pet:clips"),
            ToolWheelOption("Search", "clipboard", "#38B764", "pet:search"),
            ToolWheelOption("Focus", focus_sub, "#FFCD75", "pomodoro:toggle"),
        ]
        if sys.platform == "win32":
            options.append(ToolWheelOption("Mute", "speakers", "#8A9AF2", "pet:mute"))
        options.append(ToolWheelOption("Ask AI", "chat", "#B48EF2", "ai:chat"))
        options.extend(
            ToolWheelOption(label, "open", color, f"web:{url}") for label, url, color in WEB_SHORTCUTS
        )
        return options

    def open_tool_wheel(self, global_cursor: QPoint | None = None) -> ToolWheel:
        if self._tool_wheel is None:
            self._tool_wheel = ToolWheel(
                on_selected=self._on_tool_wheel_selected,
                always_on_top=self._always_on_top,
            )
        sprite_global = self.mapToGlobal(self.sprite_center_point())
        wx, wy = clamp_position_to_screens(
            sprite_global.x() - int(ToolWheel.CENTER_X),
            sprite_global.y() - int(ToolWheel.CENTER_Y),
            ToolWheel.WHEEL_WIDTH,
            ToolWheel.WHEEL_HEIGHT,
            self.available_screen_rects(),
        )
        self._tool_wheel.open_tools(
            self.tool_wheel_options(),
            window_top_left=QPoint(wx, wy),
            initial_global_cursor=global_cursor,
        )
        return self._tool_wheel

    def _on_hold_timeout(self) -> None:
        if self._press_global_pos is None or self._did_drag:
            return
        self._wheel_holding = True
        self._click_timer.stop()
        self._anim.play_oneshot("spin")
        self._schedule_next_anim_tick()
        self.open_tool_wheel(QCursor.pos())

    def _on_tool_wheel_selected(self, option: ToolWheelOption) -> None:
        if option.callback_id == "pomodoro:toggle":
            self.trigger_play()
        else:
            self.trigger_tool_action(option.callback_id)

    def _open_clip_history(self, global_pos: QPoint) -> None:
        vault = self._state_machine.registry.get_plugin("clipvault")
        entries = list(getattr(vault, "entries", ()))
        pins = list(getattr(vault, "pins", ()))
        if not entries and not pins:
            self._show_toast("NO CLIPS", QColor("#EF7D57"))
            return
        menu = style_menu(QMenu(self))
        for idx, entry in enumerate(entries):
            act = menu.addAction(entry.preview.replace("&", "&&"))
            act.triggered.connect(lambda _c=False, i=idx: self.trigger_tool_action(f"clipvault:restore:{i}"))
        if pins:
            menu.addSeparator()
            for idx, entry in enumerate(pins):
                act = menu.addAction(f"Pinned: {entry.preview}".replace("&", "&&"))
                act.triggered.connect(lambda _c=False, i=idx: self.trigger_tool_action(f"clipvault:pinned:{i}"))
        menu.exec(global_pos)
        menu.deleteLater()

    def _clipboard_local_paths(self) -> list[Path]:
        app = QApplication.instance()
        if app is None:
            return []
        cb = app.clipboard()
        if cb is None:
            return []
        mime = cb.mimeData()
        if mime is not None and mime.hasUrls():
            urls = [
                Path(u.toLocalFile())
                for u in mime.urls()
                if u.isLocalFile() and u.toLocalFile()
            ]
            valid = [p for p in urls if p.exists()]
            if valid:
                return valid
        raw_txt = (cb.text() or "").strip().strip('"')
        if raw_txt and len(raw_txt) <= 1024:
            try:
                candidate = Path(raw_txt)
                if candidate.exists():
                    return [candidate]
            except OSError:
                pass
        return []

    # ------------------------------------------------------------------
    # Screen Snip / OCR / Color Pick
    # ------------------------------------------------------------------

    def trigger_snip(self, mode: str) -> None:
        if mode == "ai":  # Ctrl+Shift+Alt+A opens the chat bar
            self.open_ai_chat()
            return
        if mode == "watch":  # Alt+` or Ctrl+Shift+Alt+W: Watch lens on the screen now
            self.watch_now()
            return
        if self._snip_session is not None:
            return
        if mode == "ask" and self._ai_chat is not None:
            self._ai_chat.hide()  # keep the bar out of the frozen grab
        # Hide Paperduck and let the context menu fade so neither lands in the frozen grab
        self.hide()
        QTimer.singleShot(180, lambda: self._begin_snip(mode))

    def _begin_snip(self, mode: str) -> None:
        if self._is_shutting_down:
            return
        if self._snip_session is not None:  # a macro's region snip opened during the 180ms hide delay
            show_quietly(self)
            return
        self._snip_session = SnipSession(mode, self._on_snip_result)

    def _on_snip_result(self, mode: str, result: object | None) -> None:
        self._snip_session = None
        show_quietly(self)
        if mode == "ask":  # the chat's "Screen region" attachment
            if self._ai_chat is not None:
                if isinstance(result, QImage):
                    self._ai_chat.attach_image(result, "Screen region")
                self._ai_chat.open_near(self._sprite_global_rect())
            return
        if result is None:
            self._show_toast("CANCEL", QColor("#8A9AF2"))
            return
        cb = QGuiApplication.clipboard()
        if mode == "color" and isinstance(result, str):
            if cb is not None:
                cb.setText(result)
            self._show_toast(result, QColor("#73EFF7"))
            return
        if not isinstance(result, QImage):
            return
        if mode == "text":
            job = OcrJob(self)
            job.finished.connect(lambda text, j=job: self._on_ocr_finished(j, text))
            self._ocr_jobs.append(job)
            job.start(result)
            self._show_toast("READING", QColor("#FFCD75"))
            return
        if cb is not None:
            cb.setImage(result)
        saved = self._save_snip_png(result)
        if saved is not None:
            self.recent.add(saved)
        self._show_toast("SNIPPED" if saved else "COPIED", QColor("#38B764"))

    @staticmethod
    def _save_snip_png(image: QImage) -> Path | None:
        """Save to Pictures/Paperduck, else ~/.paperduck/snips; never raise into Qt handlers."""
        pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        stamp = QDateTime.currentDateTime().toString("yyyyMMdd_HHmmss_zzz")
        # OneDrive-redirected or Controlled Folder Access Pictures dirs can refuse writes
        for out_dir in (Path(pictures) / "Paperduck" if pictures else None, Path.home() / ".paperduck" / "snips"):
            if out_dir is None:
                continue
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            target = out_dir / f"snip_{stamp}.png"
            if strip_metadata(image).save(str(target), "PNG"):
                return target
        return None

    def _on_ocr_finished(self, job: OcrJob, text: str) -> None:
        if job in self._ocr_jobs:
            self._ocr_jobs.remove(job)
        job.deleteLater()
        if self._is_shutting_down:
            return
        if not text:
            self._show_toast("NO TEXT", QColor("#EF7D57"))
            return
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        self._show_toast("TEXT OK", QColor("#38B764"))
