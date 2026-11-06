"""主窗口：播放优先布局 + 按需浮动工具面板 + 悬浮播放控件。

布局原则：
- 视频区就是 centralWidget，占满整个窗口，任何工具面板都不参与主窗口布局；
- 播放控制条与时间轴是视频区的覆盖子控件，不改变视频尺寸；
- 输出设置 / 剪辑 / 队列 / 媒体信息是浮动 QDockWidget，默认隐藏，点命令才出现。
"""
from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import (
    QByteArray, QEvent, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal,
)
from PySide6.QtGui import QAction, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QDockWidget, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSlider,
    QStackedLayout, QStatusBar, QToolBar, QVBoxLayout, QWidget,
)

from config.errors import VTKError, report
from config.i18n import i18n, tr
from config.logging_setup import get_logger
from config.settings import settings
from ffmpeg_core.command_builder import build_gif_command
from ffmpeg_core.ffmpeg_engine import EncoderCapabilities, detect_capabilities, ffmpeg_binary
from ffmpeg_core.ffprobe_engine import MediaInfo, format_time, probe
from ffmpeg_core.jobs import JobQueue
from modules.batch import job_from_plan
from modules.converter import Plan, default_output_path
from modules.cutter import plan_cut
from modules.extract_audio import AUDIO_FORMATS, plan_audio
from modules.merge import plan_merge
from modules.screenshot import plan_snapshot
from player.frame_extractor import FrameExtractor
from player.playlist import Playlist
from player.subtitle_track import SUPPORTED as SUBTITLE_EXTS
from player.subtitle_track import SubtitleTrack, find_sidecar
from player.timeline import Timeline
from player.video_player import RATES, VideoPlayer
from player.video_surface import VideoSurface
from ui.cut_panel import CutPanel
from ui.info_panel import InfoPanel
from ui.output_panel import OutputPanel
from ui.playback_overlay import PlaybackOverlay
from ui.playlist_panel import PlaylistPanel
from ui.queue_panel import QueuePanel
from ui.shortcuts import ShortcutsDialog, install_shortcuts

_log = get_logger("ui")

VIDEO_FILTER = (
    "视频文件 (*.mp4 *.mov *.mkv *.avi *.wmv *.m4v *.webm *.ts *.flv *.mpeg *.mpg);;"
    "所有文件 (*.*)"
)
SUBTITLE_FILTER = "字幕文件 (*.srt *.vtt *.ass *.ssa);;所有文件 (*.*)"
_PITCH_KEYS = {"on": "player.pitch_on", "off": "player.pitch_off", "unknown": "player.pitch_unknown"}
_TOOL_KEYS = ("compress", "cut", "queue", "list", "info")


class _CapsSignals(QObject):
    ready = Signal(object)


class _CapsRunnable(QRunnable):
    def __init__(self, signals: _CapsSignals):
        super().__init__()
        self.signals = signals

    def run(self) -> None:
        try:
            caps = detect_capabilities(refresh=True)
        except Exception as exc:
            _log.warning("硬件编码检测失败: %s", exc)
            caps = None
        try:
            self.signals.ready.emit(caps)
        except RuntimeError:
            # 用户在检测完成前关闭了窗口，信号源已被销毁
            pass


class _MergeDialog(QDialog):
    """拖拽调整合并顺序。"""

    def __init__(self, files: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("merge.title"))
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        hint = QLabel(tr("merge.hint"))
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        layout.addWidget(hint)
        self.list_widget = QListWidget()
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        for path in files:
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def ordered_files(self) -> list[str]:
        return [self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.list_widget.count())]


class MainWindow(QMainWindow):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.media: MediaInfo | None = None
        self.caps: EncoderCapabilities | None = None
        self.current_frame = 0
        self._last_frame_image: QImage | None = None
        self._pitch = "unknown"
        self._actions: dict[str, QAction] = {}
        self._menus: dict[str, QAction] = {}
        self._tool_actions: dict[str, QAction] = {}
        self._extra_actions: dict[str, QAction] = {}
        self._docks: dict[str, QDockWidget] = {}
        self._dock_titles: dict[str, str] = {}
        self._shortcuts: list = []
        self._fullscreen = False
        self.playlist = Playlist()
        self.subtitle_track = SubtitleTrack()
        self.setAcceptDrops(True)
        self.resize(1220, 880)

        self._build_ui()
        self._build_menus()
        install_shortcuts(self)
        self._connect()
        self._restore_state()
        self._detect_gpu()
        self._resume_last_file()
        i18n().changed.connect(self._retranslate_menus)
        self.statusBar().showMessage(tr("status.ready"))

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        video_area = QWidget()
        video_area.setObjectName("videoArea")
        video_area.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        video_area.setMouseTracking(True)
        self.video_area = video_area
        self.setCentralWidget(video_area)

        self.video_widget = VideoSurface()
        self.frame_label = QLabel()
        self.frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame_label.setStyleSheet("background-color: #000000;")
        self.hint_label = QLabel(tr("player.no_media"))
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setObjectName("hint")
        self.stack = QStackedLayout(video_area)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(self.video_widget)
        self.stack.addWidget(self.frame_label)
        self.stack.addWidget(self.hint_label)
        self.stack.setCurrentIndex(2)

        self._build_overlay()
        self._build_toolbar()
        self._build_panels()

        self.player = VideoPlayer(self.video_widget, self)
        self.frames = FrameExtractor(self)

        status = QStatusBar()
        self.setStatusBar(status)
        self.status_label = QLabel("")
        self.media_label = QLabel("")
        self.media_label.setObjectName("mediaBrief")
        # QStatusBar 的永久控件之间没有间距，靠内边距和 GPU 那串拉开
        self.media_label.setContentsMargins(0, 0, 16, 0)
        self.gpu_label = QLabel("")
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(self.media_label)
        status.addPermanentWidget(self.gpu_label)
        self._retranslate_menus()
        QTimer.singleShot(0, self._position_overlay)

    def _build_toolbar(self) -> None:
        bar = QToolBar("tools", self)
        bar.setObjectName("mainToolbar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self.toolbar = bar
        for index, key in enumerate(_TOOL_KEYS, start=1):
            action = QAction(tr(f"tool.{key}"), self)
            action.setCheckable(True)
            action.setShortcut(QKeySequence(f"Ctrl+{index}"))
            # 浮动面板是独立顶层窗口，ApplicationShortcut 保证在那里也能切换
            action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            action.setToolTip(tr(f"tool.{key}_hint"))
            action.triggered.connect(partial(self.toggle_tool, key))
            bar.addAction(action)
            self._tool_actions[key] = action
        bar.addSeparator()
        fullscreen = QAction(tr("tool.fullscreen"), self)
        fullscreen.triggered.connect(self.toggle_fullscreen)
        bar.addAction(fullscreen)
        keys_action = QAction(tr("tool.shortcuts"), self)
        keys_action.triggered.connect(self.show_shortcuts)
        bar.addAction(keys_action)
        overlay = QAction(tr("tool.overlay"), self)
        overlay.setCheckable(True)
        overlay.setChecked(not self.overlay.auto_hide)
        # H 快捷键由 shortcuts.py 统一注册（那里有输入焦点守卫，打字不会误触发）
        overlay.triggered.connect(self.toggle_overlay)
        bar.addAction(overlay)
        self._extra_actions = {
            "tool.fullscreen": fullscreen,
            "tool.shortcuts": keys_action,
            "tool.overlay": overlay,
        }

    def _build_panels(self) -> None:
        self.output_panel = OutputPanel()
        self.cut_panel = CutPanel()
        self.info_panel = InfoPanel()
        self.playlist_panel = PlaylistPanel()
        self.queue = JobQueue()
        self.queue_panel = QueuePanel(self.queue)

        self._create_dock("compress", "tab.output", self.output_panel, (820, 660))
        self._create_dock("cut", "tool.cut", self.cut_panel, (480, 480))
        self._create_dock("queue", "tab.queue", self.queue_panel, (780, 440))
        self._create_dock("list", "tab.playlist", self.playlist_panel, (420, 560))
        self._create_dock("info", "tab.info", self.info_panel, (520, 460))

    def _create_dock(self, key: str, title_key: str, widget: QWidget, size: tuple[int, int]) -> None:
        dock = QDockWidget(tr(title_key), self)
        dock.setObjectName(f"toolDock_{key}")
        dock.setWidget(widget)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        dock.resize(*size)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        dock.setFloating(True)
        dock.hide()
        dock.visibilityChanged.connect(partial(self._sync_tool_action, key))
        self._docks[key] = dock
        self._dock_titles[key] = title_key

    def _build_overlay(self) -> None:
        self.overlay = PlaybackOverlay(self.video_area)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(3)
        self.play_btn = QPushButton("▶")
        self.pause_btn = QPushButton("||")
        self.stop_btn = QPushButton("■")
        self.prev_btn = QPushButton("|<")
        self.next_btn = QPushButton(">|")
        self.rewind_btn = QPushButton("<<")
        self.forward_btn = QPushButton(">>")
        for button, key in (
            (self.play_btn, "player.play"), (self.pause_btn, "player.pause"),
            (self.stop_btn, "player.stop"), (self.prev_btn, "player.prev_file"),
            (self.next_btn, "player.next_file"), (self.rewind_btn, "player.rewind"),
            (self.forward_btn, "player.forward"),
        ):
            button.setToolTip(tr(key))
            button.setFixedSize(28, 22)
            controls.addWidget(button)
        self.speed_combo = QComboBox()
        for rate in RATES:
            self.speed_combo.addItem(f"{rate:g}x", rate)
        self.speed_combo.setCurrentIndex(self.speed_combo.findData(1.0))
        self.speed_combo.setFixedSize(60, 22)
        self.speed_combo.setToolTip(tr("player.speed"))
        controls.addWidget(QLabel(tr("player.speed")))
        controls.addWidget(self.speed_combo)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(settings().get("volume", 80)))
        self.volume_slider.setFixedSize(78, 20)
        controls.addWidget(QLabel(tr("player.volume")))
        controls.addWidget(self.volume_slider)
        self.fullscreen_btn = QPushButton(tr("player.fullscreen"))
        self.fullscreen_btn.setToolTip("F / F11 / Esc")
        self.fullscreen_btn.setFixedHeight(22)
        controls.addWidget(self.fullscreen_btn)
        controls.addStretch(1)
        self.frame_info_label = QLabel("")
        self.time_label = QLabel("00:00 / 00:00")
        controls.addWidget(self.frame_info_label)
        controls.addWidget(self.time_label)
        # 常驻模式下也能一键收起控制条；鼠标一动就会重新出现，不会「找不回来」
        self.hide_btn = QPushButton("▾")
        self.hide_btn.setToolTip(tr("player.hide_bar"))
        self.hide_btn.setFixedSize(24, 22)
        self.hide_btn.clicked.connect(self._hide_overlay_now)
        controls.addWidget(self.hide_btn)
        self.overlay.add_controls(controls)

        self.timeline = Timeline()
        self.timeline.setToolTip(tr("timeline.hint"))
        self.overlay.add_timeline(self.timeline)

        for widget in (self.video_area, self.video_widget, self.frame_label,
                       self.hint_label, self.overlay):
            widget.setMouseTracking(True)
            widget.installEventFilter(self)
        # 应用级鼠标监听 + 光标轮询：保证隐藏后鼠标一动必定唤出
        self.overlay.attach(self)

    def _hide_overlay_now(self) -> None:
        self.overlay.hide_now()
        self.statusBar().showMessage(tr("status.overlay_hidden"), 4000)

    def _position_overlay(self) -> None:
        if not hasattr(self, "overlay"):
            return
        area = self.video_area
        docked = self.overlay.is_docked
        # 控制条整体压缩后约 78px（按钮行 22 + 时间轴 46 + 间距与边距）
        height = max(78, self.overlay.sizeHint().height())
        height = min(height, max(72, area.height() - 60))
        if docked:
            # 停靠：占画面下方一条独立条带，画面只在上方剩余区域显示
            self.overlay.setGeometry(0, max(0, area.height() - height), area.width(), height)
            reserved = height if self.overlay.isVisible() else 0
        else:
            width = max(320, area.width() - 20)
            self.overlay.setGeometry(10, max(6, area.height() - height - 8), width, height)
            reserved = 0
        # QStackedLayout 会忽略 contentsMargins，只能让画面/图片自己让出底部条带
        self.video_widget.set_bottom_inset(reserved)
        # 悬浮模式下字幕必须整体抬到控制条上方，否则会被半透明条压住
        self.video_widget.set_subtitle_margin(14 if docked else height + 14)
        for label in (self.frame_label, self.hint_label):
            label.setContentsMargins(0, 0, 0, reserved)
        if self._last_frame_image is not None and self.stack.currentIndex() == 1:
            self._show_pixmap(self._last_frame_image)
        self.overlay.raise_()

    # ---------------- 菜单与快捷键 ----------------
    def _build_menus(self) -> None:
        menubar = self.menuBar()
        self._menus["menu.file"] = menubar.addMenu(tr("menu.file"))
        self._menus["menu.edit"] = menubar.addMenu(tr("menu.edit"))
        self._menus["menu.tools"] = menubar.addMenu(tr("menu.tools"))
        self._menus["menu.help"] = menubar.addMenu(tr("menu.help"))

        def add(menu_key: str, key: str, shortcut: str, handler) -> QAction:
            action = QAction(tr(key), self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(handler)
            self._menus[menu_key].addAction(action)
            self._actions[key] = action
            return action

        add("menu.file", "action.open", "Ctrl+O", self._open_file)
        add("menu.file", "action.open_files", "", self._add_files)
        self._menus["menu.file"].addSeparator()
        add("menu.file", "action.load_subtitle", "", self._pick_subtitle)
        self._menus["menu.file"].addSeparator()
        add("menu.file", "action.merge", "", self._merge_files)
        self._menus["menu.file"].addSeparator()
        add("menu.file", "action.exit", "Alt+F4", self.close)

        # V 快捷键由 shortcuts.py 注册（带输入焦点守卫），这里只挂菜单项
        subtitle_toggle = add("menu.edit", "action.toggle_subtitle", "", self.toggle_subtitle)
        subtitle_toggle.setCheckable(True)
        subtitle_toggle.setChecked(True)
        add("menu.edit", "action.mark_in", "I", self._mark_in)
        add("menu.edit", "action.mark_out", "O", self._mark_out)
        add("menu.edit", "action.clear_marks", "", self.timeline.clear_marks)
        self._menus["menu.edit"].addSeparator()
        add("menu.edit", "action.cut_lossless", "", partial(self._cut_selection, True))
        add("menu.edit", "action.cut_precise", "", partial(self._cut_selection, False))
        self._menus["menu.edit"].addSeparator()
        add("menu.edit", "action.screenshot", "Ctrl+S", self._take_snapshot)

        add("menu.tools", "action.extract_audio", "", self._extract_audio)
        add("menu.tools", "action.export_gif", "", self._export_gif)
        self._menus["menu.tools"].addSeparator()
        add("menu.tools", "action.recheck_gpu", "", self._detect_gpu)
        self._menus["menu.tools"].addAction(self._extra_actions["tool.overlay"])
        dock_action = add("menu.tools", "action.dock_overlay", "Ctrl+H", self._toggle_dock_overlay)
        dock_action.setCheckable(True)
        dock_action.setChecked(self.overlay.is_docked)
        dock_action.setToolTip(tr("action.dock_overlay_hint"))
        # 工具栏上也放一个，避免「控制条挡画面」时还要翻菜单
        self.toolbar.addAction(dock_action)
        follow_action = add("menu.tools", "action.follow_audio_device", "", self._toggle_follow_audio)
        follow_action.setCheckable(True)
        follow_action.setChecked(self.player.follow_audio_device)
        lang_menu = self._menus["menu.tools"].addMenu(tr("action.language"))
        for code, label in (("zh_CN", "简体中文"), ("en_US", "English")):
            action = QAction(label, self)
            action.triggered.connect(partial(self._switch_language, code))
            lang_menu.addAction(action)
        self._menus["menu.tools"].addSeparator()
        for key in _TOOL_KEYS:
            self._menus["menu.tools"].addAction(self._tool_actions[key])

        shortcuts_action = add("menu.help", "action.shortcuts", "F1", self.show_shortcuts)
        shortcuts_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        add("menu.help", "action.about", "", self._about)

        for key, handler in (
            (",", partial(self._step_frame, -1)), (".", partial(self._step_frame, 1)),
            ("[", self.player.slower), ("]", self.player.faster),
            ("Space", self.player.toggle),
        ):
            from PySide6.QtGui import QShortcut
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

    def show_shortcuts(self) -> None:
        ShortcutsDialog(self).exec()

    def change_volume(self, delta: int) -> None:
        value = max(0, min(100, self.player.volume + delta))
        self.player.set_volume(value)
        self.statusBar().showMessage(f"{tr('player.volume')}: {value}%", 1600)

    def toggle_mute(self) -> None:
        muted = not self.player.muted
        self.player.set_muted(muted)
        self.statusBar().showMessage(tr("status.muted" if muted else "status.unmuted"), 1600)

    def toggle_overlay(self, _checked: bool = False) -> None:
        """H / 工具栏：切换「常驻显示」与「播放时自动隐藏」。"""
        auto = self.overlay.toggle_auto_hide()
        action = self._extra_actions.get("tool.overlay")
        if action is not None:
            action.setChecked(not auto)
            action.setToolTip(tr("tool.overlay_hint"))
        self.statusBar().showMessage(
            tr("status.overlay_auto" if auto else "status.overlay_pinned"), 4000
        )

    def _toggle_dock_overlay(self, checked: bool = False) -> None:
        """Ctrl+H：控制条在「悬浮」与「停靠画面下方」之间切换。"""
        self.overlay.set_docked(bool(checked))
        action = self._actions.get("action.dock_overlay")
        if action is not None:
            action.setChecked(self.overlay.is_docked)
        self._position_overlay()
        self.statusBar().showMessage(
            tr("status.overlay_docked" if self.overlay.is_docked else "status.overlay_floating"),
            5000,
        )

    def toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            self.menuBar().hide()
            self.toolbar.hide()
            self.statusBar().hide()
            self.showFullScreen()
        else:
            self.showNormal()
            self.menuBar().show()
            self.toolbar.show()
            self.statusBar().show()
        QTimer.singleShot(80, self._position_overlay)

    def escape_pressed(self) -> None:
        if self._fullscreen:
            self.toggle_fullscreen()
            return
        for dock in self._docks.values():
            if dock.isVisible():
                dock.hide()

    # ---------------- 浮动工具面板 ----------------
    def toggle_tool(self, key: str) -> None:
        self.show_tool(key)

    def show_tool(self, key: str, show: bool | None = None) -> None:
        dock = self._docks.get(key)
        if dock is None:
            return
        visible = (not dock.isVisible()) if show is None else bool(show)
        if not visible:
            dock.hide()
            return
        dock.setFloating(True)
        self._place_dock(dock)
        if key == "compress":
            self.output_panel.refresh_preview()
        elif key == "cut":
            self._sync_cut_context()
        dock.show()
        dock.raise_()

    def _place_dock(self, dock: QDockWidget) -> None:
        if dock.property("vtk_placed"):
            return
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        geo = self.frameGeometry()
        width, height = dock.width(), dock.height()
        x = geo.x() + max(24, geo.width() - width - 30)
        y = geo.y() + 96
        if available is not None:
            x = max(available.left() + 8, min(x, available.right() - width - 8))
            y = max(available.top() + 8, min(y, available.bottom() - height - 8))
        dock.move(int(x), int(y))
        dock.setProperty("vtk_placed", True)

    def _sync_tool_action(self, key: str, visible: bool) -> None:
        action = self._tool_actions.get(key)
        if action is not None:
            action.setChecked(bool(visible))

    def _update_queue_badge(self) -> None:
        action = self._tool_actions.get("queue")
        if action is None:
            return
        count = len(self.queue.jobs())
        action.setText(f"{tr('tool.queue')} ({count})" if count else tr("tool.queue"))

    # ---------------- 信号连接 ----------------
    def _connect(self) -> None:
        self.play_btn.clicked.connect(self.player.play)
        self.pause_btn.clicked.connect(self.player.pause)
        self.stop_btn.clicked.connect(self.player.stop)
        self.prev_btn.clicked.connect(partial(self._step_file, -1))
        self.next_btn.clicked.connect(partial(self._step_file, 1))
        self.rewind_btn.clicked.connect(partial(self.player.skip, -10_000))
        self.forward_btn.clicked.connect(partial(self.player.skip, 10_000))
        self.speed_combo.currentIndexChanged.connect(
            lambda: self.player.set_rate(float(self.speed_combo.currentData() or 1.0))
        )
        self.volume_slider.valueChanged.connect(self.player.set_volume)
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        self.player.volumeChanged.connect(self._on_volume_changed)
        self.player.rateChanged.connect(self._on_rate_changed)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playingChanged.connect(self._on_playing_changed)
        self.player.pitchStatus.connect(self._on_pitch)
        self._on_pitch(self.player.pitch_status)  # 状态在播放器构造时已发出，这里补一次同步
        self.player.errorOccurred.connect(
            lambda msg: report(self, VTKError(msg), tr("player.play"), i18n().lang)
        )
        self.player.audioDeviceChanged.connect(self._on_audio_device_changed)
        self.player.endReached.connect(self._on_end_reached)

        self.timeline.seekRequested.connect(self.player.seek)
        self.frames.frameReady.connect(self._on_frame_ready)
        self.frames.failed.connect(lambda msg: self.statusBar().showMessage(msg, 6000))
        self.overlay.spaceChanged.connect(self._position_overlay)

        self.playlist_panel.addRequested.connect(self._playlist_add)
        self.playlist_panel.playRequested.connect(self._playlist_play)
        self.playlist_panel.removeRequested.connect(self._playlist_remove)
        self.playlist_panel.clearRequested.connect(self._playlist_clear)
        self.playlist_panel.moveRequested.connect(self._playlist_move)
        self.playlist_panel.autoNextChanged.connect(self.set_playlist_auto_next)

        self.output_panel.queueRequested.connect(partial(self._queue_current, False))
        self.output_panel.convertRequested.connect(partial(self._queue_current, True))
        self.output_panel.extremeRequested.connect(partial(self._queue_current, True))
        self.cut_panel.cutRequested.connect(self._cut_from_panel)
        self.cut_panel.currentRequested.connect(self._fill_current_mark)
        self.cut_panel.marksChanged.connect(self._sync_marks_to_timeline)
        self.cut_panel.set_plan_builder(self._build_cut_plan)

        self.queue_panel.addRequested.connect(self._add_files)
        self.queue_panel.removeRequested.connect(self._remove_selected_job)
        self.queue_panel.clearRequested.connect(self._clear_queue)
        self.queue_panel.startRequested.connect(self.queue.start)
        self.queue_panel.stopRequested.connect(self.queue.cancel_all)
        self.queue_panel.concurrencyChanged.connect(self.queue.set_concurrency)
        self.queue.job_changed.connect(lambda _job: self._update_queue_badge())
        self.queue.queue_finished.connect(
            lambda: self.statusBar().showMessage(tr("status.done_all"), 6000)
        )

    # ---------------- 状态与恢复 ----------------
    def _restore_state(self) -> None:
        geometry = str(settings().get("geometry", "") or "")
        if geometry:
            try:
                self.restoreGeometry(QByteArray.fromHex(geometry.encode("ascii")))
            except Exception:
                pass
        state = str(settings().get("window_state", "") or "")
        if state:
            try:
                self.restoreState(QByteArray.fromHex(state.encode("ascii")))
            except Exception:
                pass
        # 启动永远以播放为主：面板默认隐藏，但保留用户上次摆放的位置 / 尺寸
        for dock in self._docks.values():
            dock.hide()
            dock.setFloating(True)
        self._restore_playlist()

    def _restore_playlist(self) -> None:
        """恢复上次的播放清单；已不存在的文件自动跳过。"""
        raw = settings().get("playlist", []) or []
        items = [p for p in raw if isinstance(p, str) and Path(p).is_file()] if isinstance(raw, list) else []
        index = int(settings().get("playlist_index", -1) or -1)
        self.playlist = Playlist(items, index)
        self.playlist_panel.set_auto_next(bool(settings().get("playlist_auto_next", True)))
        self._refresh_playlist()

    def _refresh_playlist(self) -> None:
        self.playlist_panel.set_items(self.playlist.items, self.playlist.index)

    def _sync_playlist_current(self, path: str) -> None:
        """播放某个文件时，让清单里的当前项跟着走（不在清单里就补进去）。"""
        if not self.playlist.contains(path):
            self.playlist.add([path])
        self.playlist.select_path(path)
        self._refresh_playlist()

    def _resume_last_file(self) -> None:
        if not bool(settings().get("auto_resume_playback", True)):
            return
        path = str(settings().get("last_file", "") or "")
        if not path:
            # 兼容旧配置：V1.0 只记播放位置，没有 last_file 字段
            remembered = dict(settings().get("last_play_position", {}) or {})
            if remembered:
                path = str(next(reversed(list(remembered.keys()))))
        if not path or not Path(path).is_file():
            return
        QTimer.singleShot(250, partial(self._safe_resume, path))

    def _safe_resume(self, path: str) -> None:
        try:
            probe(path)
        except Exception as exc:
            _log.warning("恢复上次视频失败，跳过: %s", exc)
            return
        self.open_media(path)
        self.statusBar().showMessage(tr("status.resumed", name=Path(path).name), 6000)

    def _detect_gpu(self) -> None:
        self.gpu_label.setText("GPU: ...")
        self._caps_signals = _CapsSignals()
        self._caps_signals.ready.connect(self._on_caps)
        QThreadPool.globalInstance().start(_CapsRunnable(self._caps_signals))

    def _on_caps(self, caps) -> None:
        if caps is None:
            self.gpu_label.setText("GPU: CPU")
            return
        self.caps = caps
        self.output_panel.set_caps(caps)
        self.output_panel.set_context(self.media.path if self.media else "", self.media, caps)
        self.gpu_label.setText(caps.summary())

    def _on_pitch(self, status: str) -> None:
        self._pitch = status
        self._update_status()

    def _update_status(self) -> None:
        self.status_label.setText(tr(_PITCH_KEYS.get(self._pitch, "player.pitch_unknown")))

    def _retranslate_menus(self) -> None:
        self.setWindowTitle(f"{tr('app.title')} · {tr('app.subtitle')}")
        for key, menu in self._menus.items():
            menu.setTitle(tr(key))
        for key, action in self._actions.items():
            action.setText(tr(key))
        for key, action in self._extra_actions.items():
            action.setText(tr(key))
        overlay_action = self._extra_actions.get("tool.overlay")
        if overlay_action is not None:
            overlay_action.setToolTip(tr("tool.overlay_hint"))
            overlay_action.setChecked(not self.overlay.auto_hide)
        for key, action in self._tool_actions.items():
            action.setText(tr(f"tool.{key}"))
            action.setToolTip(tr(f"tool.{key}_hint"))
        for key, dock in self._docks.items():
            dock.setWindowTitle(tr(self._dock_titles.get(key, key)))
        dock_action = self._actions.get("action.dock_overlay")
        if dock_action is not None:
            dock_action.setChecked(self.overlay.is_docked)
            dock_action.setToolTip(tr("action.dock_overlay_hint"))
        self._update_queue_badge()
        if hasattr(self, "hint_label"):
            self.hint_label.setText(tr("player.no_media"))
        if hasattr(self, "fullscreen_btn"):
            self.fullscreen_btn.setText(tr("player.fullscreen"))
        if hasattr(self, "status_label"):
            self._update_status()
            self._update_media_brief(self.media)
        if hasattr(self, "timeline"):
            self.timeline.setToolTip(tr("timeline.hint"))
            self.timeline.update()
        if hasattr(self, "playlist_panel"):
            self.playlist_panel.retranslate()

    def _switch_language(self, code: str) -> None:
        settings().set("language", code)
        i18n().set_language(code)

    # ---------------- 媒体 ----------------
    def _open_file(self) -> None:
        """Ctrl+O：支持多选，全部进播放列表，从第一个开始播。"""
        paths, _ = QFileDialog.getOpenFileNames(self, tr("action.open"), "", VIDEO_FILTER)
        if paths:
            self._open_paths(paths)

    def _open_paths(self, paths: list[str]) -> None:
        """把一批文件加入播放列表并播放第一个（用于多选打开 / 拖入多个文件）。"""
        paths = [str(p) for p in paths if p]
        if not paths:
            return
        first = paths[0]
        self.playlist.add(paths)
        if self.playlist.select_path(first) < 0:
            return
        self._refresh_playlist()
        self.open_media(first)
        if len(paths) > 1:
            self.statusBar().showMessage(
                tr("status.playlist_added", n=len(paths), total=len(self.playlist)), 6000
            )
            # 一次选了多个文件：顺手把播放列表面板打开，让用户看到清单
            self.show_tool("list", True)

    def open_media(self, path: str) -> None:
        try:
            info = probe(path)
        except Exception as exc:
            report(self, exc, tr("action.open"), i18n().lang)
            return
        self.media = info
        self.current_frame = 0
        self._last_frame_image = None
        self.subtitle_track.clear()
        self.video_widget.set_subtitle("")
        if bool(settings().get("auto_load_subtitle", True)):
            sidecar = find_sidecar(path)
            if sidecar:
                self._load_subtitle(sidecar, quiet=True)
        self.player.open(path)
        self.stack.setCurrentIndex(0)
        self.overlay.set_has_media(True)
        self.timeline.set_duration(int(info.duration * 1000))
        self.info_panel.set_media(info)
        self._update_media_brief(info)
        self.output_panel.set_context(path, info, self.caps)
        self._sync_cut_context()
        self._sync_playlist_current(path)
        settings().set("last_file", path)
        self.statusBar().showMessage(tr("status.opened", name=info.filename), 6000)

        remembered = (settings().get("last_play_position", {}) or {}).get(path)
        if remembered and 0 < int(remembered) < info.duration * 1000:
            QTimer.singleShot(400, partial(self.player.seek, int(remembered)))

    def _update_media_brief(self, info: MediaInfo | None) -> None:
        """状态栏常驻的「一眼信息」：分辨率 / 帧率 / 视频码率。"""
        label = getattr(self, "media_label", None)
        if label is None:
            return
        if info is None or not info.has_video:
            label.setText("")
            label.setToolTip("")
            return
        parts = [info.resolution_text()]
        if info.fps:
            parts.append(f"{info.fps:.3f} fps")
        if info.vbitrate_kbps:
            parts.append(f"{tr('status.vbitrate_short')} {info.vbitrate_text()}")
        label.setText("  ·  ".join(parts))
        label.setToolTip(f"{tr('info.vbitrate')}: {info.vbitrate_text()}\n"
                         f"{tr('info.abitrate')}: {info.abitrate_text()}\n"
                         f"{tr('info.bitrate')}: {info.bitrate_text()}")

    # ---------------- 播放列表 ----------------
    def _playlist_add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, tr("playlist.add"), "", VIDEO_FILTER)
        if paths:
            self._open_paths(paths)

    def _playlist_play(self, index: int) -> None:
        path = self.playlist.select(index)
        if not path:
            return
        self._refresh_playlist()
        if self.media and self.media.path == path:
            self.player.seek(0)
            self.player.play()
            return
        self.open_media(path)

    def _playlist_remove(self, rows: list[int]) -> None:
        playing = self.playlist.current
        self.playlist.remove(list(rows))
        self._refresh_playlist()
        current = self.playlist.current
        # 只删掉了别人：继续播当前视频；删掉了正在播的那一项：跟到新的当前项
        if current and current != playing and self.media is not None:
            self.open_media(current)

    def _playlist_clear(self) -> None:
        self.playlist.clear()
        self._refresh_playlist()
        self.statusBar().showMessage(tr("status.playlist_cleared"), 3000)

    def _playlist_move(self, row: int, delta: int) -> None:
        if not self.playlist.move(row, delta):
            return
        self._refresh_playlist()
        self.playlist_panel.select_row(row + delta)

    def _step_file(self, delta: int) -> None:
        """上一个 / 下一个文件（播放条 |< >| 、PageUp / PageDown）。"""
        index = self.playlist.next_index() if delta > 0 else self.playlist.prev_index()
        if index is None:
            self.statusBar().showMessage(
                tr("status.playlist_end" if delta > 0 else "status.playlist_start"), 3000
            )
            return
        self._playlist_play(index)

    def _on_end_reached(self) -> None:
        if not bool(settings().get("playlist_auto_next", True)):
            return
        index = self.playlist.next_index()
        if index is not None:
            QTimer.singleShot(500, partial(self._playlist_play, index))

    def set_playlist_auto_next(self, value: bool) -> None:
        settings().set("playlist_auto_next", bool(value))

    def _on_duration(self, duration_ms: int) -> None:
        if duration_ms > 0:
            self.timeline.set_duration(duration_ms)

    def _on_position(self, position_ms: int) -> None:
        self.timeline.set_position(position_ms)
        self.time_label.setText(
            f"{format_time(position_ms / 1000)} / {format_time(self.player.duration / 1000)}"
        )
        if self.subtitle_track.loaded:
            self.video_widget.set_subtitle(self.subtitle_track.cue_at(position_ms / 1000.0))
        if self.media and self.media.fps and self.stack.currentIndex() == 0:
            self.current_frame = FrameExtractor.frame_at(position_ms, self.media.fps)

    def _on_playing_changed(self, playing: bool) -> None:
        if playing:
            self._last_frame_image = None
            self.frame_info_label.setText("")
            self.stack.setCurrentIndex(0)
        # 默认常驻；只有用户切到沉浸模式时，播放中才会自动隐藏
        self.overlay.set_playing(bool(playing))

    def _on_rate_changed(self, rate: float) -> None:
        index = self.speed_combo.findData(rate)
        if index >= 0:
            self.speed_combo.blockSignals(True)
            self.speed_combo.setCurrentIndex(index)
            self.speed_combo.blockSignals(False)
        # + / - / [ / ] 调速后立即反馈，避免用户以为按键无效
        self.statusBar().showMessage(tr("status.rate", rate=f"{rate:g}"), 1800)

    # ---------------- 字幕 / 音频设备 ----------------
    def _pick_subtitle(self) -> None:
        start = str(Path(self.media.path).parent) if self.media else ""
        path, _ = QFileDialog.getOpenFileName(
            self, tr("action.load_subtitle"), start, SUBTITLE_FILTER
        )
        if path:
            self._load_subtitle(path)

    def _load_subtitle(self, path: str, *, quiet: bool = False) -> None:
        try:
            count = self.subtitle_track.load(path)
        except Exception as exc:
            if not quiet:
                report(self, exc, tr("action.load_subtitle"), i18n().lang)
            else:
                _log.warning("自动加载字幕失败: %s", exc)
            return
        self.video_widget.set_subtitle_enabled(True)
        action = self._actions.get("action.toggle_subtitle")
        if action is not None:
            action.setChecked(True)
        self.statusBar().showMessage(
            tr("status.subtitle_loaded", name=Path(path).name, n=count), 4000
        )

    def toggle_subtitle(self, enabled: bool | None = None) -> None:
        action = self._actions.get("action.toggle_subtitle")
        if enabled is None:  # 快捷键 V：切换
            enabled = not (action.isChecked() if action else self.video_widget.subtitle_enabled)
        self.video_widget.set_subtitle_enabled(bool(enabled))
        if action is not None:
            action.setChecked(bool(enabled))
        self.statusBar().showMessage(
            tr("status.subtitle_on" if enabled else "status.subtitle_off"), 2200
        )

    def _toggle_follow_audio(self, checked: bool) -> None:
        self.player.set_follow_audio_device(bool(checked))
        self.statusBar().showMessage(
            tr("status.audio_follow_on" if checked else "status.audio_follow_off"), 3500
        )

    def _on_audio_device_changed(self, name: str) -> None:
        self.statusBar().showMessage(tr("status.audio_switched", name=name), 5000)

    def _on_volume_changed(self, value: int) -> None:
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(int(value))
        self.volume_slider.blockSignals(False)

    def _step_frame(self, delta: int) -> None:
        if not self.media or not self.media.fps:
            return
        self.player.pause()
        total = self.media.nb_frames or int(self.media.duration * self.media.fps)
        self.current_frame = max(0, min(max(1, total) - 1, self.current_frame + delta))
        self.timeline.set_position(int(self.current_frame / self.media.fps * 1000))
        self.frames.request(self.media.path, self.current_frame, self.media.fps)

    def _on_frame_ready(self, frame_no: int, image: QImage) -> None:
        self._last_frame_image = image
        self._show_pixmap(image)
        self.stack.setCurrentIndex(1)
        self.overlay.poke(force=True)  # 切页后把悬浮控制条重新置顶
        self.frame_info_label.setText(tr("player.frame", n=frame_no))

    def _show_pixmap(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        self.frame_label.setPixmap(pixmap.scaled(
            self.frame_label.contentsRect().size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def eventFilter(self, obj, event) -> bool:
        # 鼠标唤醒统一由 PlaybackOverlay 的应用级过滤器 + 光标轮询负责
        if (event.type() == QEvent.Type.MouseButtonDblClick
                and obj is not self.overlay):
            self.toggle_fullscreen()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._last_frame_image is not None and self.stack.currentIndex() == 1:
            self._show_pixmap(self._last_frame_image)
        self._position_overlay()

    # ---------------- 标记与任务 ----------------
    def _mark_in(self) -> None:
        self.timeline.set_in(self.player.position)
        self.output_panel.refresh_preview()
        if self._docks["cut"].isVisible():
            self._sync_cut_context()

    def _mark_out(self) -> None:
        self.timeline.set_out(self.player.position)
        self.output_panel.refresh_preview()
        if self._docks["cut"].isVisible():
            self._sync_cut_context()

    def _sync_cut_context(self) -> None:
        if self.media:
            self.cut_panel.set_context(self.media.path, self.media,
                                       self.timeline.in_ms, self.timeline.out_ms)
        else:
            self.cut_panel.set_context("", None, None, None)

    def _sync_marks_to_timeline(self, start_s: float, end_s: float) -> None:
        self.timeline.set_in(int(start_s * 1000))
        self.timeline.set_out(int(end_s * 1000))
        self.output_panel.refresh_preview()

    def _fill_current_mark(self, is_in: bool) -> None:
        self.cut_panel.set_current(is_in, self.player.position / 1000.0)

    def _build_cut_plan(self) -> Plan:
        if not self.media:
            raise ValueError(tr("msg.no_file"))
        start, end, lossless, dst = self.cut_panel.values()
        if end <= start:
            raise ValueError(tr("msg.invalid_range"))
        return plan_cut(
            self.media.path, dst or None, start, end,
            lossless=lossless, params=self.output_panel.current_params(),
            media=self.media, vendor=self.output_panel.resolved_vendor(),
        )

    def _clip_range(self) -> tuple[float, float] | None:
        if self.timeline.in_ms is None or self.timeline.out_ms is None:
            QMessageBox.information(self, tr("app.title"), tr("msg.no_in_out"))
            return None
        start, end = self.timeline.in_ms / 1000.0, self.timeline.out_ms / 1000.0
        if end <= start or (self.media and end > self.media.duration + 0.5):
            QMessageBox.information(self, tr("app.title"), tr("msg.invalid_range"))
            return None
        return start, end

    def _submit(self, plan: Plan, *, switch_to_queue: bool = False) -> None:
        self.queue.add(job_from_plan(plan, name=Path(plan.output).name))
        self._update_queue_badge()
        self.statusBar().showMessage(tr("msg.task_queued", name=Path(plan.output).name), 5000)
        if switch_to_queue:
            self.show_tool("queue", True)

    def _queue_current(self, start_now: bool) -> None:
        if not self.media:
            QMessageBox.information(self, tr("app.title"), tr("msg.no_file"))
            return
        try:
            plan = self.output_panel.build_plan()
        except Exception as exc:
            report(self, exc, tr("output.convert_now"), i18n().lang)
            return
        self._submit(plan)
        if start_now:
            self.queue.start()

    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, tr("action.open_files"), "", VIDEO_FILTER)
        if paths:
            self._add_paths(paths)

    def _add_paths(self, paths: list[str]) -> None:
        added = 0
        for path in paths:
            try:
                info = probe(path)
                plan = self.output_panel.build_plan(src=path, media=info)
            except Exception as exc:
                _log.warning("加入队列失败 %s: %s", path, exc)
                continue
            self.queue.add(job_from_plan(plan, name=Path(path).name))
            added += 1
        if added:
            self._update_queue_badge()
            self.statusBar().showMessage(f"+{added}", 4000)

    def _remove_selected_job(self) -> None:
        job_id = self.queue_panel.selected_job_id()
        if job_id:
            self.queue.remove(job_id)
            self.queue_panel.refresh_rows()
            self._update_queue_badge()

    def _clear_queue(self) -> None:
        self.queue.clear_all()
        self.queue_panel.refresh_rows()
        self._update_queue_badge()

    def _cut_from_panel(self, start_now: bool) -> None:
        if not self.media:
            QMessageBox.information(self, tr("app.title"), tr("msg.no_file"))
            return
        start, end, _lossless, _dst = self.cut_panel.values()
        if end <= start:
            QMessageBox.information(self, tr("app.title"), tr("msg.invalid_range"))
            return
        try:
            plan = self._build_cut_plan()
        except Exception as exc:
            report(self, exc, tr("tool.cut"), i18n().lang)
            return
        self._submit(plan)
        if start_now:
            self.queue.start()

    def _cut_selection(self, lossless: bool) -> None:
        """菜单快捷路径：保持 V1.0 的「打点后一键剪切」行为。"""
        if not self.media:
            QMessageBox.information(self, tr("app.title"), tr("msg.no_file"))
            return
        clip = self._clip_range()
        if not clip:
            return
        try:
            plan = plan_cut(
                self.media.path, None, clip[0], clip[1],
                lossless=lossless, params=self.output_panel.current_params(),
                media=self.media, vendor=self.output_panel.resolved_vendor(),
            )
        except Exception as exc:
            report(self, exc, tr("action.cut_lossless"), i18n().lang)
            return
        self._submit(plan)
        self.queue.start()

    def _merge_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, tr("action.merge"), "", VIDEO_FILTER)
        if len(paths) < 2:
            return
        dialog = _MergeDialog(list(paths), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        files = dialog.ordered_files()
        try:
            dst = default_output_path(files[0], "mp4", suffix="_merged")
            plan = plan_merge(files, dst)
        except Exception as exc:
            report(self, exc, tr("action.merge"), i18n().lang)
            return
        self._submit(plan)
        self.queue.start()

    def _extract_audio(self) -> None:
        if not self.media:
            QMessageBox.information(self, tr("app.title"), tr("msg.no_file"))
            return
        fmt, ok = QInputDialog.getItem(
            self, tr("action.extract_audio"), tr("audio.pick_format"),
            list(AUDIO_FORMATS), 0, False,
        )
        if not ok:
            return
        try:
            plan = plan_audio(self.media.path, None, fmt=fmt, media=self.media)
        except Exception as exc:
            report(self, exc, tr("action.extract_audio"), i18n().lang)
            return
        self._submit(plan)
        self.queue.start()

    def _export_gif(self) -> None:
        if not self.media:
            QMessageBox.information(self, tr("app.title"), tr("msg.no_file"))
            return
        clip = self._clip_range() or (self.player.position / 1000.0,
                                      min(self.media.duration, self.player.position / 1000.0 + 5.0))
        try:
            dst = default_output_path(self.media.path, "gif", suffix="_clip")
            command = build_gif_command(
                self.media.path, dst, ffmpeg_bin=ffmpeg_binary(),
                start_s=clip[0], duration_s=max(0.5, clip[1] - clip[0]),
            )
            self._submit(Plan(commands=[command], output=dst, duration_s=clip[1] - clip[0]))
            self.queue.start()
        except Exception as exc:
            report(self, exc, tr("action.export_gif"), i18n().lang)

    def _take_snapshot(self) -> None:
        if not self.media:
            QMessageBox.information(self, tr("app.title"), tr("msg.no_file"))
            return
        time_s = (self.current_frame / self.media.fps if self.stack.currentIndex() == 1
                  else self.player.position / 1000.0)
        try:
            plan = plan_snapshot(self.media.path, time_s)
        except Exception as exc:
            report(self, exc, tr("action.screenshot"), i18n().lang)
            return
        self._submit(plan, switch_to_queue=False)
        self.queue.start()
        self.statusBar().showMessage(tr("msg.screenshot_saved", path=plan.output), 6000)

    def _about(self) -> None:
        QMessageBox.about(self, tr("action.about"), tr("msg.about"))

    # ---------------- 拖放 / 关闭 ----------------
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        subtitle = next(
            (p for p in paths if Path(p).is_file()
             and Path(p).suffix.lower() in SUBTITLE_EXTS), None
        )
        if subtitle:
            self._load_subtitle(subtitle)
            event.acceptProposedAction()
            return
        files = [p for p in paths if Path(p).is_file()]
        if files:
            # 拖入多个文件按「播放列表」处理：全部加入清单，从第一个开始播
            self._open_paths(files)
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        try:
            if hasattr(self, "overlay"):
                self.overlay._watch.stop()
                app = QApplication.instance()
                if app is not None:
                    app.removeEventFilter(self.overlay)
            self.queue.cancel_all()
            settings().set("geometry", bytes(self.saveGeometry()).hex(), save=False)
            settings().set("window_state", bytes(self.saveState()).hex(), save=False)
            if self.media:
                settings().set("last_file", self.media.path, save=False)
                if self.player.position > 0:
                    positions = dict(settings().get("last_play_position", {}) or {})
                    positions[self.media.path] = self.player.position
                    settings().set("last_play_position", positions, save=False)
            settings().set("playlist", self.playlist.items, save=False)
            settings().set("playlist_index", self.playlist.index, save=False)
            settings().save()
            self.player.close()
        except Exception as exc:
            _log.warning("退出清理异常: %s", exc)
        super().closeEvent(event)
