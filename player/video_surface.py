"""Qt 自绘视频画面：QVideoSink + QVideoFrame.toImage() 画到普通 QWidget。

为什么不用 QVideoWidget：
    Qt 6.12 在 Windows 上用 D3D11 / DirectComposition 渲染视频，会创建一个
    WS_EX_LAYERED 原生子窗口。原生子窗口会盖住所有普通（alien）Qt 子控件，
    实测连 Tool / Frameless / WindowStaysOnTopHint 顶层窗口都压不过它 ——
    悬浮播放控制条在播放时完全不可见。改用 QVideoSink 纯 Qt 绘制后，
    播放时不再产生任何原生子 HWND，悬浮层就能正常显示，视频尺寸也不受影响。

代价：每帧做一次像素格式转换（NV12 → RGB）与缩放，CPU 开销高于硬件合成；
     用 drawImage 直接缩放，避免额外的中间 QImage 拷贝。

外挂字幕也在这里画：和控制条一样都是普通 Qt 绘制，永远压在画面之上，
且位置避开底部控制条，不会被遮住。
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen,
)
from PySide6.QtMultimedia import QVideoSink
from PySide6.QtWidgets import QWidget


class VideoSurface(QWidget):
    """可放进 QStackedLayout 的普通视频画面控件，暴露 sink 给 QMediaPlayer。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("videoSurface")
        self.setStyleSheet("background-color: #000000;")
        self.setAutoFillBackground(True)
        self._frame: QImage | None = None
        self._subtitle = ""
        self._subtitle_enabled = True
        # 字幕距画面底部的最小留白：悬浮控制条要留出条高，停靠模式只需一点点
        self._subtitle_margin = 104
        # 停靠模式下画面底部要让出的像素（控制条独占条带），QStackedLayout 会忽略
        # layout 的 contentsMargins，所以只能在这里自己扣掉，否则控制条还会压在画面上
        self._bottom_inset = 0
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)

    def _on_frame(self, frame) -> None:
        # 回调在 GUI 线程；toImage() 内部完成 map / 格式转换 / unmap
        try:
            if not frame.isValid():
                return
            image = frame.toImage()
        except Exception:
            return
        if image.isNull():
            return
        self._frame = image
        self.update()

    def clear(self) -> None:
        self._frame = None
        self.update()

    def has_frame(self) -> bool:
        return self._frame is not None

    def current_image(self) -> QImage | None:
        return self._frame

    # ---------------- 外挂字幕 ----------------
    def set_subtitle(self, text: str) -> None:
        value = text or ""
        if value != self._subtitle:
            self._subtitle = value
            self.update()

    def set_subtitle_enabled(self, enabled: bool) -> None:
        self._subtitle_enabled = bool(enabled)
        self.update()

    def set_subtitle_margin(self, pixels: int) -> None:
        """字幕距画面底部的留白：悬浮控制条模式下需要更大，停靠模式下更小。"""
        value = max(0, int(pixels))
        if value != self._subtitle_margin:
            self._subtitle_margin = value
            self.update()

    def set_bottom_inset(self, pixels: int) -> None:
        """底部预留高度（像素）：画面只在上方剩余区域等比显示，绝不被控制条盖住。"""
        value = max(0, int(pixels))
        if value != self._bottom_inset:
            self._bottom_inset = value
            self.update()

    @property
    def bottom_inset(self) -> int:
        return self._bottom_inset

    @property
    def subtitle_enabled(self) -> bool:
        return self._subtitle_enabled

    def _draw_subtitle(self, painter: QPainter, rect: QRect) -> None:
        if not self._subtitle or not self._subtitle_enabled:
            return
        lines = [line for line in self._subtitle.splitlines() if line.strip()]
        if not lines:
            return
        size = max(13, min(44, int(rect.height() * 0.042)))
        font = QFont()
        font.setFamily("Microsoft YaHei")
        font.setPixelSize(size)
        font.setBold(True)
        fm = QFontMetrics(font)
        widths = [fm.horizontalAdvance(line) for line in lines]
        text_width = max(widths) if widths else 0
        line_height = fm.height() + 4
        box_w = min(rect.width() - 12, text_width + 30)
        box_h = line_height * len(lines) + 14
        # 悬浮控制条高约 118px，字幕往上挪，两者互不遮挡；
        # 停靠模式由主窗口把留白调小，字幕可以贴近画面底部
        bottom = rect.bottom() - max(self._subtitle_margin, int(rect.height() * 0.06))
        x = rect.left() + max(6, (rect.width() - box_w) // 2)
        y = max(rect.top() + 4, bottom - box_h)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(QRect(x, y, box_w, box_h), QColor(0, 0, 0, 115))
        path = QPainterPath()
        for index, line in enumerate(lines):
            lx = x + 15 + (max(0, text_width - fm.horizontalAdvance(line)) / 2)
            ly = y + 7 + fm.ascent() + index * line_height
            path.addText(lx, ly, font, line)
        painter.strokePath(
            path,
            QPen(QColor(0, 0, 0, 230), max(2.0, size * 0.12),
                 Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin),
        )
        painter.fillPath(path, QColor(255, 255, 255))
        painter.restore()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        image = self._frame
        if image is None:
            return
        iw, ih = image.width(), image.height()
        w = self.width()
        h = self.height() - self._bottom_inset
        if iw <= 0 or ih <= 0 or w <= 0 or h <= 0:
            return
        scale = min(w / iw, h / ih)
        dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))
        rect = QRect((w - dw) // 2, (h - dh) // 2, dw, dh)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(rect, image)
        self._draw_subtitle(painter, rect)
