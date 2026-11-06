"""播放控制层：两种摆放方式，都保证「不挡画面」或「可隐藏」。

摆放方式（overlay_docked）：
- 悬浮（默认）：覆盖在画面底部，半透明；播放中鼠标静止一段时间自动淡出，
  鼠标一动立刻回来，暂停时始终常驻。
- 停靠：占据画面下方一条独立条带，视频等比缩到上方，任何情况下都不遮挡画面。

历史坑（两次踩同一处，改动前务必读完）：
1) 早期用「光标位于画面底部热区」强制保持可见 → 点完播放鼠标自然停在控制条上，
   隐藏倒计时被无限续期，控制条永远不消失。
2) 后来把热区改成「只在已隐藏时唤出」，看起来没问题，实际更糟：控制条淡出隐藏后
   光标仍停在原处（也就是热区里），200ms 后轮询立刻把它唤回来，形成
   「淡出 → 立刻弹出」的死循环，用户看到的就是「怎么都隐藏不了」。
   **静止的光标绝不能唤出控制条**，只有光标真的动了（≥3px）才唤出。
现在：`_should_hide_later()` 只判断 自动隐藏 + 播放中 + 有媒体 + 没按住鼠标；
唤出只依赖 `_check_pointer()` 的位移判断。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QVBoxLayout, QWidget

from config.settings import settings


class PlaybackOverlay(QWidget):
    """作为视频区的子 widget 手动定位（不加入任何布局）。"""

    # 停靠模式下占用高度变化（显示 / 隐藏 / 切换模式），宿主据此重排视频区
    spaceChanged = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("playbackOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)

        self._auto_hide = bool(settings().get("overlay_auto_hide", False))
        self._docked = bool(settings().get("overlay_docked", False))
        self._has_media = False
        self._playing = False
        self._pressed = False
        self._force_hide = False
        self._anim = None
        self._effect = None
        self._host: QWidget | None = None
        self._last_cursor: QPoint | None = None
        try:
            self._effect = QGraphicsOpacityEffect(self)
            self._effect.setOpacity(1.0)
            self.setGraphicsEffect(self._effect)
        except Exception:
            self._effect = None

        self._timeout_ms = max(800, int(settings().get("overlay_timeout_ms", 2800)))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._timeout_ms)
        self._timer.timeout.connect(self._fade_out)

        # 光标轮询兜底：即使鼠标事件被别的控件吃掉，也能在光标移动时唤出控制条
        self._watch = QTimer(self)
        self._watch.setInterval(200)
        self._watch.timeout.connect(self._check_pointer)

        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(10, 4, 10, 5)
        self.content.setSpacing(4)

        self._apply_docked_style()

    # ---------------- 组装 ----------------
    def add_controls(self, layout) -> None:
        self.content.addLayout(layout)

    def add_timeline(self, widget: QWidget) -> None:
        self.content.addWidget(widget)

    def attach(self, host: QWidget) -> None:
        """安装应用级鼠标监听并启动光标轮询。host 为主窗口。"""
        self._host = host
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._watch.start()
        self.poke(force=True)

    # ---------------- 状态 ----------------
    @property
    def auto_hide(self) -> bool:
        return self._auto_hide

    @property
    def is_docked(self) -> bool:
        return self._docked

    def set_has_media(self, value: bool) -> None:
        self._has_media = bool(value)
        self.poke(force=True)

    def set_playing(self, value: bool) -> None:
        """暂停 / 未播放时控制条常驻；播放中才可能按超时隐藏。"""
        self._playing = bool(value)
        self.poke(force=True)

    def set_auto_hide(self, value: bool) -> None:
        """切换常驻 / 播放时自动隐藏，并记住用户选择。"""
        self._auto_hide = bool(value)
        settings().set("overlay_auto_hide", self._auto_hide)
        self.poke(force=True)

    def toggle_auto_hide(self) -> bool:
        self.set_auto_hide(not self._auto_hide)
        return self._auto_hide

    def set_docked(self, value: bool) -> None:
        """切换「悬浮在画面上」/「停靠在画面下方」。"""
        value = bool(value)
        if value == self._docked:
            return
        self._docked = value
        settings().set("overlay_docked", value)
        self._apply_docked_style()
        self.poke(force=True)
        self.spaceChanged.emit()

    def _apply_docked_style(self) -> None:
        self.setProperty("docked", "true" if self._docked else "false")
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    # ---------------- 可见性 ----------------
    def _area(self) -> QWidget | None:
        return self.parentWidget()

    def _cursor_in_area(self) -> bool:
        area = self._area()
        return area is not None and area.isVisible() and \
            area.rect().contains(area.mapFromGlobal(QCursor.pos()))

    def _should_hide_later(self) -> bool:
        # 只有「播放中 + 自动隐藏 + 正在拖拽之外」才允许淡出；
        # 暂停时永远常驻，鼠标停在控制条上也不再阻止隐藏（这是旧版的最大问题）。
        return self._auto_hide and self._playing and self._has_media and not self._pressed

    def hide_now(self) -> None:
        """用户主动隐藏（控制条上的「隐藏」按钮）：无视常驻设置，立即淡出。

        鼠标只要移动就会重新出现，所以不会出现「藏起来再也找不回」的情况。
        """
        self._timer.stop()
        self._force_hide = True
        self._fade_out()

    def poke(self, *, force: bool = False) -> None:
        """鼠标活动 / 播放状态变化：立即显示并重置隐藏倒计时。"""
        if (not force and not self._auto_hide and self.isVisible()
                and (self._effect is None or self._effect.opacity() >= 1.0)):
            return
        if self._anim is not None:
            try:
                self._anim.finished.disconnect(self._on_fade_finished)
            except (RuntimeError, TypeError):
                pass
            self._anim.stop()
            self._anim = None
        self._force_hide = False
        self._set_opacity(1.0)
        was_hidden = not self.isVisible()
        if was_hidden:
            self.show()
        # QStackedLayout.setCurrentIndex() 会把视频控件 raise 到最上层，
        # 所以强制刷新状态时必须把控制条重新压回顶部。
        if was_hidden or force:
            self.raise_()
        if self._should_hide_later():
            self._timer.start()
        else:
            self._timer.stop()

    def _set_opacity(self, value: float) -> None:
        if self._effect is not None:
            self._effect.setOpacity(value)

    def _fade_out(self) -> None:
        if not self._force_hide and not self._should_hide_later():
            return
        if self._effect is None:
            self.hide()
            return
        anim = QPropertyAnimation(self._effect, b"opacity", self)
        anim.setDuration(220)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self._on_fade_finished)
        self._anim = anim
        anim.start()

    def _on_fade_finished(self) -> None:
        self._anim = None
        # 动画期间用户可能又动了鼠标 / 切回常驻，此时不能真的隐藏
        if self._force_hide or self._should_hide_later():
            self._force_hide = False
            self.hide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._docked:
            self.spaceChanged.emit()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self._docked:
            self.spaceChanged.emit()

    # ---------------- 唤醒 ----------------
    def _check_pointer(self) -> None:
        """只有光标**真的移动**才唤出控制条（或重置隐藏倒计时）。

        绝不能用「光标是否位于底部热区」来判断，哪怕只在隐藏后才生效也不行：
        控制条淡出时鼠标通常还停在原处（画面上点完播放、或停靠条上点完按钮），
        热区判断会让它在 200ms 后立刻弹回来，形成「淡出 → 立刻弹出」的死循环。
        真正移动鼠标时位移一定 ≥3px，这里的判断已经覆盖了唤出需求。
        """
        if self._host is None or not self._host.isVisible():
            return
        pos = QCursor.pos()
        if not self._cursor_in_area():
            self._last_cursor = pos
            return
        prev = self._last_cursor
        # 视频重绘 / 状态栏刷新会让 Windows 重复投递同位置 WM_MOUSEMOVE，
        # 必须按位移去重，否则静止的光标也会让倒计时无限续期。
        if prev is not None and (pos - prev).manhattanLength() < 3:
            return
        self._last_cursor = pos
        self.poke()

    # ---------------- 鼠标 ----------------
    def enterEvent(self, event) -> None:
        self._set_opacity(1.0)
        if self.isVisible():
            self.poke()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._should_hide_later():
            self._timer.start()
        super().leaveEvent(event)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind in (
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.Wheel,
        ):
            # 按住鼠标（拖进度条 / 拖音量）期间不淡出，否则拖到一半控制条会消失
            if kind == QEvent.Type.MouseButtonPress and self._is_own(obj):
                self._pressed = True
                self._timer.stop()
            elif kind == QEvent.Type.MouseButtonRelease and self._pressed:
                self._pressed = False
                self.poke()
            elif kind == QEvent.Type.MouseMove and self._pressed:
                self._timer.stop()
            self._check_pointer()
        return False

    def _is_own(self, obj) -> bool:
        """事件是否发生在控制条（含子控件）上。"""
        widget = obj if isinstance(obj, QWidget) else None
        while widget is not None:
            if widget is self:
                return True
            widget = widget.parentWidget()
        return False
