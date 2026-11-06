"""逐帧浏览：FFmpeg 按帧号抽帧 → PNG 管道 → QImage。

QMediaPlayer 没有原生 nextFrame()，且 setPosition 只有毫秒精度，
所以逐帧走 FFmpeg：t = frame_no / fps，用 -ss 精确 seek 后取一帧。
长视频(24h+)也只会 seek 到附近关键帧，不会从头解码。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage

from config.errors import FFmpegError
from config.logging_setup import get_logger
from ffmpeg_core.command_builder import build_frame_png_command
from ffmpeg_core.ffmpeg_engine import ffmpeg_binary, run_capture_bytes

_log = get_logger("frames")


class _FrameSignals(QObject):
    ready = Signal(int, object, int)   # frame_no, png bytes, sequence
    failed = Signal(str)


class _FrameRunnable(QRunnable):
    def __init__(self, src: str, frame_no: int, fps: float, sequence: int, signals: _FrameSignals):
        super().__init__()
        self.src = src
        self.frame_no = frame_no
        self.fps = fps
        self.sequence = sequence
        self.signals = signals

    def run(self) -> None:
        try:
            argv = build_frame_png_command(
                self.src, self.frame_no, self.fps, ffmpeg_bin=ffmpeg_binary()
            )
            data = run_capture_bytes(argv, timeout=40)
            self.signals.ready.emit(self.frame_no, data, self.sequence)
        except FFmpegError as exc:
            _log.warning("抽帧失败 frame=%s: %s", self.frame_no, exc)
            self.signals.failed.emit(str(exc))
        except Exception as exc:
            _log.exception("抽帧异常 frame=%s", self.frame_no)
            self.signals.failed.emit(str(exc))


class FrameExtractor(QObject):
    frameReady = Signal(int, object)   # frame_no, QImage
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _FrameSignals()
        self._signals.ready.connect(self._on_ready)
        self._signals.failed.connect(self.failed)
        self._sequence = 0

    def request(self, src: str, frame_no: int, fps: float) -> None:
        if not src or fps <= 0 or frame_no < 0:
            return
        self._sequence += 1
        self._pool.clear()  # 连续按 . 时丢弃还没开始的旧请求
        self._pool.start(_FrameRunnable(src, int(frame_no), fps, self._sequence, self._signals))

    def _on_ready(self, frame_no: int, data: bytes, sequence: int) -> None:
        if sequence != self._sequence:
            return
        image = QImage()
        if not image.loadFromData(data):
            self.failed.emit("无法解码抽帧结果")
            return
        self.frameReady.emit(frame_no, image)

    @staticmethod
    def frame_at(position_ms: int, fps: float) -> int:
        return max(0, int(round(position_ms / 1000.0 * fps)))
