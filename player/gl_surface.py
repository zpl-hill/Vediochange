"""QOpenGLWidget 直传 NV12 纹理的视频画面（性能优先路径）。

为什么需要这个文件
------------------
player/video_surface.py 用 QVideoSink + QVideoFrame.toImage() 在 GUI 主线程做
NV12→RGB32 转换（4K 实测 16.7ms/帧）+ 软件缩放绘制（8.9ms），单帧 25.6ms，
远超 60fps 的 16.67ms 预算。实测 4K60 只能跑到 35fps，且主线程被占满，
导致整个界面（菜单、控制条、时间轴）都跟着卡。

这里改成：frame.map() 拿 NV12 平面 → glTexSubImage2D 直接上传两个纹理 →
GLSL 做 YUV→RGB。实测 4K60 满帧 60fps，主线程每帧仅约 1.2ms。

公开接口与 video_surface.VideoSurface 完全一致，上层无需改动；上层仍可
通过 settings 的 render_backend=cpu 强制回到软件渲染。

为什么不用 QVideoWidget
----------------------
Qt 6.12 在 Windows 用 D3D11/DirectComposition 渲染视频，会创建 WS_EX_LAYERED
原生子窗口，盖住所有普通 Qt 子控件（悬浮播放控制条完全不可见）。QOpenGLWidget
走 widget 合成路径，实测 EnumChildWindows 为 0 个原生子窗口，不会遮挡。

三个真实踩过的坑（改动本文件前必读）
----------------------------------
1. frame.bits(n) 返回的 memoryview 在 frame.unmap() 之后立即失效。把它存起来
   等 paintGL 再用就是野指针，直接段错误。上传必须在 map/unmap 窗口内完成。
2. QOpenGLTexture.setData() 上传 RG8 纹理时会静默失败：不抛异常、纹理全 0，
   画面渲染成一片绿。必须用裸 glTexSubImage2D 上传。
3. 裸指针要用 shiboken6.VoidPtr 包装；直接传 bytes 会被当成指针地址，崩溃。
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QVector3D,
)
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat, QVideoSink
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from config.logging_setup import get_logger

try:
    import shiboken6
except Exception:  # pragma: no cover - PySide6 一定带 shiboken6
    shiboken6 = None  # type: ignore[assignment]

_log = get_logger("player.gl")

# ---- OpenGL 常量（避免依赖 PyOpenGL） ----
_GL_TEXTURE_2D = 0x0DE1
_GL_TEXTURE0 = 0x84C0
_GL_TEXTURE1 = 0x84C1
_GL_RED = 0x1903
_GL_RG = 0x8227
_GL_UNSIGNED_BYTE = 0x1401
_GL_FLOAT = 0x1406
_GL_COLOR_BUFFER_BIT = 0x4000
_GL_TRIANGLE_STRIP = 0x0005
_GL_UNPACK_ALIGNMENT = 0x0CF5
_GL_UNPACK_ROW_LENGTH = 0x0CF2

_VERTEX_SHADER = """
attribute vec2 aPos;
varying vec2 vTex;
void main() {
    vTex = vec2((aPos.x + 1.0) * 0.5, 1.0 - (aPos.y + 1.0) * 0.5);
    gl_Position = vec4(aPos, 0.0, 1.0);
}
"""

# 通用 YUV→RGB：矩阵系数与范围由 uniform 传入，BT.601/709、limited/full 都能覆盖
_FRAGMENT_SHADER = """
uniform sampler2D texY;
uniform sampler2D texUV;
uniform vec3 uCb;
uniform vec3 uCr;
uniform float uYOffset;
uniform float uYScale;
varying vec2 vTex;
void main() {
    float y = texture2D(texY, vTex).r;
    vec2 uv = texture2D(texUV, vTex).rg;
    float cb = uv.r - 0.5;
    float cr = uv.g - 0.5;
    vec3 luma = vec3((y - uYOffset) * uYScale);
    vec3 rgb = luma + uCb * cb + uCr * cr;
    gl_FragColor = vec4(clamp(rgb, 0.0, 1.0), 1.0);
}
"""

# (uCb, uCr) 系数表：R/G/B 三通道各自的 Cb / Cr 贡献
_MATRIX_BT709 = (QVector3D(0.0, -0.213, 2.112), QVector3D(1.793, -0.533, 0.0))
_MATRIX_BT601 = (QVector3D(0.0, -0.344, 1.772), QVector3D(1.402, -0.714, 0.0))


class GLVideoSurface(QOpenGLWidget):
    """GPU 渲染的视频画面控件，接口与 VideoSurface 保持一致。"""

    # GPU 路径不可用时通知上层（可用于提示用户切到 CPU 渲染）
    renderFailed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("videoSurface")
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)

        self._subtitle = ""
        self._subtitle_enabled = True
        self._subtitle_margin = 104
        self._bottom_inset = 0

        self._has_frame = False
        self._frame_size = (0, 0)
        self._tex_size = (0, 0)
        self._tex_y: QOpenGLTexture | None = None
        self._tex_uv: QOpenGLTexture | None = None
        self._prog: QOpenGLShaderProgram | None = None
        self._locs: dict[str, int] = {}
        self._vbo: QOpenGLBuffer | None = None
        self._ready = False
        self._failed = False

    # ---------------- 公开接口（与 VideoSurface 对齐） ----------------
    def clear(self) -> None:
        self._has_frame = False
        self.update()

    def has_frame(self) -> bool:
        return self._has_frame

    def current_image(self):
        """GPU 路径拿不到原始 QImage，改为抓取当前 framebuffer。"""
        if not self._has_frame or not self.isValid():
            return None
        try:
            return self.grabFramebuffer()
        except Exception as exc:
            _log.warning("抓取画面失败: %s", exc)
            return None

    def set_subtitle(self, text: str) -> None:
        value = text or ""
        if value != self._subtitle:
            self._subtitle = value
            self.update()

    def set_subtitle_enabled(self, enabled: bool) -> None:
        self._subtitle_enabled = bool(enabled)
        self.update()

    def set_subtitle_margin(self, pixels: int) -> None:
        value = max(0, int(pixels))
        if value != self._subtitle_margin:
            self._subtitle_margin = value
            self.update()

    def set_bottom_inset(self, pixels: int) -> None:
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

    @property
    def failed(self) -> bool:
        return self._failed

    # ---------------- 帧接收与上传 ----------------
    def _on_frame(self, frame) -> None:
        if not frame.isValid() or self._failed:
            return
        if not self._ready or not self.isValid():
            # GL 上下文还没准备好：直接丢弃本帧（播放中下一帧马上到）
            return
        if frame.planeCount() < 2:
            return
        if not frame.map(QVideoFrame.MapMode.ReadOnly):
            return
        try:
            y_buf = frame.bits(0)
            uv_buf = frame.bits(1)
            if y_buf is None or uv_buf is None or shiboken6 is None:
                return
            width, height = frame.width(), frame.height()
            if width <= 0 or height <= 0:
                return
            # 必须在 unmap 之前完成上传，否则 memoryview 失效（段错误）
            # 注意：QOpenGLWidget.makeCurrent() 返回 void（PySide6 里是 None），
            # 不能写成 if not makeCurrent(): return，否则每一帧都会被跳过。
            self.makeCurrent()
            try:
                self._upload(y_buf, uv_buf, width, height, frame)
            except Exception as exc:
                self._failed = True
                _log.error("视频帧上传 GPU 失败: %s", exc)
                self.renderFailed.emit(str(exc))
                return
            finally:
                self.doneCurrent()
        finally:
            frame.unmap()
        self._has_frame = True
        self.update()

    def _upload(self, y_buf, uv_buf, width: int, height: int, frame) -> None:
        self._ensure_textures(width, height)
        fn = self.context().functions()
        # GL_UNPACK_ROW_LENGTH 必须显式清零：Qt 内部可能残留非 0 的行距设置。
        # Y 纹理每行正好 W 个 texel，行距错了也看不出来；但 UV 是 RG8，
        # 一旦行距按 texel 算成 W，实际会读到 2W 字节/行，色度整体错位（画面偏蓝紫）。
        fn.glPixelStorei(_GL_UNPACK_ROW_LENGTH, 0)
        fn.glPixelStorei(_GL_UNPACK_ALIGNMENT, 1)
        # VoidPtr 要在调用处直接构造，避免中间变量被提前回收
        fn.glActiveTexture(_GL_TEXTURE0)
        fn.glBindTexture(_GL_TEXTURE_2D, self._tex_y.textureId())
        fn.glTexSubImage2D(
            _GL_TEXTURE_2D, 0, 0, 0, width, height,
            _GL_RED, _GL_UNSIGNED_BYTE, shiboken6.VoidPtr(y_buf),
        )
        fn.glActiveTexture(_GL_TEXTURE1)
        fn.glBindTexture(_GL_TEXTURE_2D, self._tex_uv.textureId())
        fn.glTexSubImage2D(
            _GL_TEXTURE_2D, 0, 0, 0, width // 2, height // 2,
            _GL_RG, _GL_UNSIGNED_BYTE, shiboken6.VoidPtr(uv_buf),
        )
        self._frame_size = (width, height)
        self._colorspace = frame.surfaceFormat()

    def _release_textures(self) -> None:
        for tex in (self._tex_y, self._tex_uv):
            if tex is not None:
                try:
                    tex.destroy()
                except Exception:
                    pass
        self._tex_y = None
        self._tex_uv = None
        self._tex_size = (0, 0)

    def _ensure_textures(self, width: int, height: int) -> None:
        if self._tex_y is not None and self._tex_size == (width, height):
            return
        self._release_textures()
        linear = QOpenGLTexture.Filter.Linear
        tex_y = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
        tex_y.setFormat(QOpenGLTexture.TextureFormat.R8_UNorm)
        tex_y.setSize(width, height)
        tex_y.allocateStorage()
        tex_y.setMinMagFilters(linear, linear)
        tex_uv = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
        tex_uv.setFormat(QOpenGLTexture.TextureFormat.RG8_UNorm)
        tex_uv.setSize(width // 2, height // 2)
        tex_uv.allocateStorage()
        tex_uv.setMinMagFilters(linear, linear)
        self._tex_y, self._tex_uv = tex_y, tex_uv
        self._tex_size = (width, height)
        _log.info("GPU 视频纹理已分配: %dx%d (UV %dx%d)", width, height, width // 2, height // 2)

    # ---------------- GL 初始化与绘制 ----------------
    def initializeGL(self) -> None:
        try:
            fn = self.context().functions()
            fn.glClearColor(0.0, 0.0, 0.0, 1.0)
            prog = QOpenGLShaderProgram(self)
            ok_v = prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERTEX_SHADER)
            ok_f = prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _FRAGMENT_SHADER)
            ok_l = prog.link()
            if not (ok_v and ok_f and ok_l):
                raise RuntimeError(f"着色器编译失败: {prog.log()}")
            self._prog = prog
            # 缓存 uniform 位置，避免每帧做字符串查找
            self._locs = {
                name: prog.uniformLocation(name)
                for name in ("texY", "texUV", "uCb", "uCr", "uYOffset", "uYScale")
            }
            vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            vbo.create()
            vbo.bind()
            import struct

            verts = struct.pack("8f", -1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0)
            vbo.allocate(verts, len(verts))
            vbo.release()
            self._vbo = vbo
            self._ready = True
            _log.info("GPU 视频渲染已就绪 (OpenGL %s)",
                      ".".join(str(v) for v in self.context().format().version()))
        except Exception as exc:
            self._failed = True
            _log.error("GPU 视频渲染初始化失败: %s", exc)
            self.renderFailed.emit(str(exc))

    def paintGL(self) -> None:
        rect = None
        try:
            fn = self.context().functions()
            fn.glClearColor(0.0, 0.0, 0.0, 1.0)
            fn.glClear(_GL_COLOR_BUFFER_BIT)
            if self._ready and self._has_frame and self._tex_y is not None:
                rect = self._draw_video(fn)
        except Exception as exc:
            _log.warning("绘制视频帧失败: %s", exc)
        self._paint_subtitle(rect)

    def _draw_video(self, fn) -> QRect | None:
        logical_w = self.width()
        logical_h = self.height() - self._bottom_inset
        img_w, img_h = self._frame_size
        if img_w <= 0 or img_h <= 0 or logical_w <= 0 or logical_h <= 0:
            return None

        scale = min(logical_w / img_w, logical_h / img_h)
        draw_w, draw_h = img_w * scale, img_h * scale
        rect_x = (logical_w - draw_w) / 2.0
        rect_y = (logical_h - draw_h) / 2.0

        # GL 的视口原点在左下角，且必须是物理像素（4K 屏 200% 缩放时 DPR=2）
        dpr = self.devicePixelRatioF()
        view_x = rect_x * dpr
        view_y = (self.height() - rect_y - draw_h) * dpr
        fn.glViewport(
            int(round(view_x)), int(round(view_y)),
            max(1, int(round(draw_w * dpr))), max(1, int(round(draw_h * dpr))),
        )

        prog = self._prog
        prog.bind()
        cb, cr = self._matrix_for(self._colorspace)
        offset, yscale = self._range_for(self._colorspace)
        loc = self._locs
        # 必须用裸 glUniform*：PySide6 的 QOpenGLShaderProgram.setUniformValue(location, float)
        # 实测不生效（float uniform 保持默认 0，画面被压成全黑只剩色度），
        # 所以这里全部走明确的 GL 调用。
        fn.glUniform1i(loc["texY"], 0)
        fn.glUniform1i(loc["texUV"], 1)
        fn.glUniform3f(loc["uCb"], cb.x(), cb.y(), cb.z())
        fn.glUniform3f(loc["uCr"], cr.x(), cr.y(), cr.z())
        fn.glUniform1f(loc["uYOffset"], float(offset))
        fn.glUniform1f(loc["uYScale"], float(yscale))
        self._tex_y.bind(0)
        self._tex_uv.bind(1)
        self._vbo.bind()
        prog.enableAttributeArray(0)
        prog.setAttributeBuffer(0, _GL_FLOAT, 0, 2, 8)
        fn.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
        self._vbo.release()
        self._tex_uv.release(1)
        self._tex_y.release(0)
        prog.release()

        fn.glViewport(
            0, 0,
            max(1, int(round(logical_w * dpr))),
            max(1, int(round(self.height() * dpr))),
        )
        return QRect(
            int(round(rect_x)), int(round(rect_y)),
            max(1, int(round(draw_w))), max(1, int(round(draw_h))),
        )

    @staticmethod
    def _matrix_for(surface_format) -> tuple[QVector3D, QVector3D]:
        space = None
        try:
            space = surface_format.colorSpace()
        except Exception:
            pass
        if space == QVideoFrameFormat.ColorSpace.ColorSpace_BT709:
            return _MATRIX_BT709
        if space == QVideoFrameFormat.ColorSpace.ColorSpace_BT601:
            return _MATRIX_BT601
        # 未知时按分辨率猜：SD 用 BT.601，HD 及以上用 BT.709
        width = 0
        try:
            width = surface_format.frameSize().width()
        except Exception:
            pass
        return _MATRIX_BT601 if 0 < width <= 720 else _MATRIX_BT709

    @staticmethod
    def _range_for(surface_format) -> tuple[float, float]:
        # 注意 PySide6 的枚举名是 ColorRange_Full / ColorRange_Video / ColorRange_Unknown，
        # 没有 ColorRange_Jpeg（C++ 里才叫 Jpeg），写错会静默走进 except。
        try:
            full = getattr(QVideoFrameFormat.ColorRange, "ColorRange_Full", None)
            if full is not None and surface_format.colorRange() == full:
                return 0.0, 1.0  # full range：Y 已经是 0-1
        except Exception:
            pass
        return 0.0625, 1.164  # limited (tv) range：16-235 → 0-1

    # ---------------- 字幕 ----------------
    def _paint_subtitle(self, rect: QRect | None) -> None:
        if rect is None or not self._subtitle or not self._subtitle_enabled:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._draw_subtitle(painter, rect)
        finally:
            painter.end()

    def _draw_subtitle(self, painter: QPainter, rect: QRect) -> None:
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
        # 悬浮控制条模式下字幕要整体抬高，停靠模式由上层把留白调小
        bottom = rect.bottom() - max(self._subtitle_margin, int(rect.height() * 0.06))
        x = rect.left() + max(6, (rect.width() - box_w) // 2)
        y = max(rect.top() + 4, bottom - box_h)
        painter.save()
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
