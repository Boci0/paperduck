"""Painting of the pet window: the vector duck, HUD, hint card and toasts (mixed into PetWindow)."""

from __future__ import annotations


from PyQt6.QtCore import QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen

from src.engine.models import PetState
from src.graphics.duck import render_pose
from src.ui.theme import (
    HAIRLINE_RGBA,
    HOVER_RGBA,
    SLEEP_HEX,
    TEXT_DIM_HEX,
    TEXT_HEX,
    paint_panel,
    pet_accent,
    qcolor,
    ui_font,
)




class PaintMixin:
    """Methods of PetWindow; state and Qt signals live on PetWindow itself."""

    # ------------------------------------------------------------------
    # Painting (Vector Duck + HUD)
    # ------------------------------------------------------------------

    def _current_qimage(self) -> QImage:
        """The current pose rendered at the sprite's device pixel size (sharp at any pet size and DPI)."""
        dpr = self.devicePixelRatioF() or 1.0
        px = max(16, round(self._sprite_rect.width() * dpr))
        key = (self._anim.active_clip_name, self._anim.frame_index, self._anim.facing_left, px)
        cached = self._image_cache.get(key)
        if cached is None:
            if len(self._image_cache) >= 96:  # about 10 MB at 4x on a 125% display
                self._image_cache.clear()
            cached = render_pose(self._anim.current_frame(), px, mirrored=self._anim.facing_left)
            cached.setDevicePixelRatio(px / self._sprite_rect.width())
            self._image_cache[key] = cached
        return cached

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

        # Rendered at device pixels already, so this is a 1:1 blit
        painter.drawImage(self._sprite_rect.topLeft(), self._current_qimage())
        errands = self._errands()
        if errands is not None and errands.errands:
            self._paint_errand_tag(painter, len(errands.errands))

        if self._hint is not None:
            self._paint_hint(painter)
        elif self._is_hud_visible():
            self._paint_hud(painter)

        if self._toast_steps > 0 and self._toast_text:
            self._paint_toast(painter)

        painter.end()

    def _paint_hud(self, painter: QPainter) -> None:
        """Folded-paper card matching PetContextMenu: dog-eared panel, rounded bar tracks, Segoe UI text."""
        r = self._hud_rect
        hud = self._state_machine.get_hud_data()
        sleeping = self._state_machine.state == PetState.SLEEPING
        plugin = self._state_machine.active_plugin
        title = plugin.display_name if plugin is not None else hud.badge_left

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        paint_panel(painter, r)

        # Header: name + colored status dot, like the menu header card
        header = QRect(r.left() + 10, r.top() + 5, r.width() - 20, 14)
        status_text = "DND" if sleeping else hud.badge_right
        # Short codes (OK, DND, ERR) stay upper case; words read like the menu's "Online"
        status = f"● {status_text if len(status_text) <= 3 else status_text.title()}"
        painter.setFont(ui_font(7))
        painter.setPen(QColor(SLEEP_HEX) if sleeping else pet_accent(hud.badge_right_color))
        painter.drawText(header, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, status)
        status_w = painter.fontMetrics().horizontalAdvance(status) + 6

        painter.setFont(ui_font(8.5, bold=True))
        painter.setPen(QColor(TEXT_HEX))
        title_w = header.width() - status_w
        painter.drawText(
            QRect(header.left(), header.top(), title_w, header.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, title_w),
        )

        painter.setPen(QPen(qcolor(HAIRLINE_RGBA), 1))
        painter.drawLine(r.left() + 10, r.top() + 21, r.right() - 10, r.top() + 21)

        for idx, bar in enumerate(hud.bars[:3]):
            row = QRect(r.left() + 10, r.top() + 25 + idx * 11, r.width() - 20, 10)
            painter.setFont(ui_font(6.5))
            painter.setPen(QColor(TEXT_DIM_HEX))
            painter.drawText(
                QRect(row.left(), row.top(), 34, row.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                bar.label,
            )
            # Rounded track with a filled run; any nonzero value shows at least a dot of fill
            track = QRectF(row.left() + 36, row.center().y() - 2, row.width() - 68, 4)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(HOVER_RGBA))
            painter.drawRoundedRect(track, 2, 2)
            if bar.clamped_value > 0:
                painter.setBrush(pet_accent(bar.color_hex))
                painter.drawRoundedRect(QRectF(track.left(), track.top(), max(4.0, track.width() * bar.clamped_value / 100.0), 4), 2, 2)
            painter.setPen(QColor(TEXT_HEX))
            painter.drawText(
                QRect(row.right() - 28, row.top(), 28, row.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                bar.formatted_value.strip(),
            )

        if hud.detail:
            detail_rect = QRect(r.left() + 10, r.top() + 58, r.width() - 20, 11)
            painter.setFont(ui_font(6.5))
            painter.setPen(QColor(TEXT_DIM_HEX))
            painter.drawText(
                detail_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(hud.detail, Qt.TextElideMode.ElideRight, detail_rect.width()),
            )
        painter.restore()

    def _paint_hint(self, painter: QPainter) -> None:
        """Hint card in the HUD style: the suggestion plus a dim how-to line."""
        r = self._hint_rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        paint_panel(painter, r)
        center = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        painter.setFont(ui_font(8.5, bold=True))
        painter.setPen(QColor(TEXT_HEX))
        painter.drawText(QRect(r.left() + 8, r.top() + 5, r.width() - 16, 16), center, self._hint.text)
        painter.setFont(ui_font(6.5))
        painter.setPen(QColor(TEXT_DIM_HEX))
        painter.drawText(QRect(r.left() + 8, r.top() + 22, r.width() - 16, 12), center,
                         self._hint.data if self._hint.rule == "tip" or self._hint.rule.startswith("errand")
                         else "Click: do it   Right-click: mute")
        painter.restore()

    def _paint_errand_tag(self, painter: QPainter, count: int) -> None:
        """A small folded paper tag on a string at the duck's neck: errands it is carrying (the count)."""
        s = self._sprite_rect
        neck_x = s.left() + (s.width() * 0.36 if self._anim.facing_left else s.width() * 0.64)
        neck = QPointF(neck_x, s.top() + s.height() * 0.5)
        tag = QRectF(neck.x() - 8 + (-10 if self._anim.facing_left else 10), neck.y() + 6, 16, 13)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(TEXT_DIM_HEX), 1))
        painter.drawLine(neck, QPointF(tag.center().x(), tag.top()))
        paint_panel(painter, tag.toRect(), radius=3, fold=False)
        painter.setFont(ui_font(6.5, bold=True))
        painter.setPen(QColor(TEXT_HEX))
        painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, str(count))
        painter.restore()

    def _paint_toast(self, painter: QPainter) -> None:
        progress = 1.0 - (self._toast_steps / max(1, self._toast_max_steps))
        rise_px = int(progress * 8)
        alpha = max(80, min(255, int(255 * (self._toast_steps / max(1, self._toast_max_steps)) * 1.35)))

        pill_w = max(68, min(self.width() - 12, len(self._toast_text) * 7 + 16))
        pill_h = 18
        pill_x = (self.width() - pill_w) // 2
        min_y = (self._hud_rect.bottom() + 3) if self._is_hud_visible() else 4
        pill_y = max(min_y, self._sprite_rect.top() - 22 - rise_px)
        pill = QRect(pill_x, pill_y, pill_w, pill_h)

        painter.save()
        painter.setOpacity(alpha / 255)
        paint_panel(painter, pill, radius=pill_h / 2, fold=False)
        painter.setPen(pet_accent(self._toast_color))
        painter.setFont(ui_font(7.5, bold=True))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, self._toast_text)
        painter.restore()
