"""专业时间轴：拖拽定位、滚轮缩放（全长 → 1 分钟 → 10 秒 → 1 秒）、IN/OUT 标记。"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QWidget

from config.i18n import tr
from ffmpeg_core.ffprobe_engine import format_time

BG = QColor("#15171c")
TRACK = QColor("#262a34")
TRACK_PLAYED = QColor("#2f5fa8")
TICK = QColor("#3a3f4c")
TEXT = QColor("#929bad")
MARK = QColor("#37c98b")
MARK_RANGE = QColor(55, 201, 139, 38)
CURSOR = QColor("#ff6b6b")
HOVER = QColor("#c9d2e3")

_TICKS = (1_000, 2_000, 5_000, 10_000, 15_000, 30_000, 60_000,
          120_000, 300_000, 600_000, 900_000, 1_800_000, 3_600_000, 7_200_000)

# 紧凑布局常量：控制条整体要窄，时间轴压到 46px 仍保留刻度与 IN/OUT 标签
_TRACK_TOP = 16.0
_TRACK_H = 9.0
_TICK_LEN = 4.0
_TICK_TEXT_Y = 41.0
_LABEL_Y = 12.0
_PLAYHEAD_TOP = 2.0
_PLAYHEAD_BOTTOM = _TRACK_TOP + _TRACK_H + 2.0
_HEIGHT = 46


class Timeline(QWidget):
    seekRequested = Signal(int)
    hoverTime = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.duration_ms = 0
        self.position_ms = 0
        self.in_ms: int | None = None
        self.out_ms: int | None = None
        self.view_start = 0.0
        self.view_span = 0.0
        self._dragging = False
        self._hover_x = -1.0
        self._left = 40
        self._right_pad = 16
        self.setMinimumHeight(_HEIGHT)
        self.setMaximumHeight(_HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ---------- 外部接口 ----------
    def set_duration(self, duration_ms: int) -> None:
        self.duration_ms = max(0, int(duration_ms))
        self.reset_zoom()

    def reset_zoom(self) -> None:
        self.view_start = 0.0
        self.view_span = float(self.duration_ms or 1)
        self.update()

    def set_position(self, position_ms: int, follow: bool = True) -> None:
        self.position_ms = max(0, int(position_ms))
        if follow and self.view_span and self.view_span < self.duration_ms:
            if not (self.view_start <= self.position_ms <= self.view_start + self.view_span):
                self.view_start = max(0.0, min(
                    self.duration_ms - self.view_span,
                    self.position_ms - self.view_span / 2.0,
                ))
        self.update()

    def set_in(self, position_ms: int | None) -> None:
        self.in_ms = None if position_ms is None else max(0, int(position_ms))
        self.update()

    def set_out(self, position_ms: int | None) -> None:
        self.out_ms = None if position_ms is None else max(0, int(position_ms))
        self.update()

    def clear_marks(self) -> None:
        self.in_ms = None
        self.out_ms = None
        self.update()

    def zoom(self, factor: float, focus_ratio: float = 0.5) -> None:
        if not self.duration_ms:
            return
        focus = self.view_start + focus_ratio * self.view_span
        span = max(1_000.0, min(float(self.duration_ms), self.view_span * factor))
        self.view_span = span
        self.view_start = max(0.0, min(self.duration_ms - span, focus - focus_ratio * span))
        self.update()

    @property
    def visible_range_text(self) -> str:
        if not self.duration_ms:
            return "00:00"
        return f"{format_time(self.view_start / 1000)} – {format_time((self.view_start + self.view_span) / 1000)}"

    # ---------- 绘制 ----------
    def _track_width(self) -> float:
        return max(1.0, self.width() - self._left - self._right_pad)

    def _x_to_ms(self, x: float) -> float:
        ratio = (x - self._left) / self._track_width()
        return self.view_start + max(0.0, min(1.0, ratio)) * self.view_span

    def _ms_to_x(self, ms: float) -> float:
        if not self.view_span:
            return float(self._left)
        return self._left + (ms - self.view_start) / self.view_span * self._track_width()

    def _tick_step(self) -> int:
        target = self.view_span / 8.0
        return next((step for step in _TICKS if step >= target), _TICKS[-1])

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), BG)

        left = float(self._left)
        right = left + self._track_width()
        track_top, track_h = _TRACK_TOP, _TRACK_H
        painter.fillRect(QRectF(left, track_top, right - left, track_h), TRACK)

        # 已播放区间
        if self.view_span > 0:
            played_right = min(right, self._ms_to_x(self.position_ms))
            if played_right > left:
                painter.fillRect(QRectF(left, track_top, played_right - left, track_h), TRACK_PLAYED)

        # IN / OUT 区间
        if self.in_ms is not None and self.out_ms is not None and self.out_ms > self.in_ms:
            x1 = max(left, self._ms_to_x(self.in_ms))
            x2 = min(right, self._ms_to_x(self.out_ms))
            if x2 > x1:
                painter.fillRect(QRectF(x1, track_top, x2 - x1, track_h), QColor(MARK_RANGE))

        # 刻度
        step = self._tick_step()
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(TICK, 1))
        first = int(self.view_start // step) * step
        t = float(first)
        while t <= self.view_start + self.view_span + step:
            x = self._ms_to_x(t)
            if left <= x <= right:
                painter.drawLine(
                    QPointF(x, track_top + track_h),
                    QPointF(x, track_top + track_h + _TICK_LEN),
                )
                painter.setPen(TEXT)
                painter.drawText(QPointF(x + 2, _TICK_TEXT_Y), format_time(t / 1000))
                painter.setPen(QPen(TICK, 1))
            t += step

        # 起止时间
        painter.setPen(TEXT)
        painter.drawText(QPointF(3, _LABEL_Y), format_time(self.view_start / 1000))
        end_text = format_time((self.view_start + self.view_span) / 1000)
        painter.drawText(QPointF(right - 40, _LABEL_Y), end_text)

        # 标记
        for value, label in ((self.in_ms, tr("timeline.in")), (self.out_ms, tr("timeline.out"))):
            if value is None:
                continue
            x = self._ms_to_x(value)
            if not (left <= x <= right):
                continue
            painter.setPen(QPen(MARK, 2))
            painter.drawLine(QPointF(x, _PLAYHEAD_TOP), QPointF(x, _PLAYHEAD_BOTTOM))
            painter.fillRect(QRectF(x, 1, 22, 12), QColor(55, 201, 139, 60))
            painter.drawText(QPointF(x + 2, 11), label)

        # 播放头
        if self.duration_ms and self.view_span:
            x = self._ms_to_x(self.position_ms)
            if left <= x <= right:
                painter.setPen(QPen(CURSOR, 2))
                painter.drawLine(QPointF(x, _PLAYHEAD_TOP), QPointF(x, _PLAYHEAD_BOTTOM))
                painter.fillRect(QRectF(x - 4, 1, 8, 5), CURSOR)

        # 悬停提示
        if self._hover_x >= left:
            painter.setPen(QPen(HOVER, 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(self._hover_x, track_top), QPointF(self._hover_x, track_top + track_h))
        painter.end()

    # ---------- 交互 ----------
    def _emit_seek(self, x: float) -> None:
        if self.duration_ms > 0:
            self.seekRequested.emit(int(self._x_to_ms(x)))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._emit_seek(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        self._hover_x = x
        if self._dragging:
            self._emit_seek(x)
        if self.duration_ms:
            self.hoverTime.emit(int(self._x_to_ms(x)))
        self.update()

    def mouseReleaseEvent(self, _event) -> None:
        self._dragging = False

    def leaveEvent(self, _event) -> None:
        self._hover_x = -1.0
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self.duration_ms:
            return
        x = event.position().x()
        ratio = max(0.0, min(1.0, (x - self._left) / self._track_width()))
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        self.zoom(factor, ratio)
        event.accept()
