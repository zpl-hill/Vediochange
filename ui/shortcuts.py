"""全局快捷键注册与「快捷键一览」对话框。

设计要点：方向键 / 单字符键必须避开数值输入框，否则用户在输出面板里
调分辨率或码率时按左右键会误触发跳转（QShortcut 会吞掉事件）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QHBoxLayout, QLineEdit, QPlainTextEdit, QSlider, QSpinBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
)

from config.i18n import tr

_INPUT_WIDGETS = (
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QSlider,
)

GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("sc.group.navigate", (
        ("← / →", "sc.seek5"),
        ("Shift+← / →", "sc.seek1"),
        ("Ctrl+← / →", "sc.seek30"),
        ("Home / End", "sc.ends"),
        ("PgUp / PgDn", "sc.file_nav"),
    )),
    ("sc.group.playback", (
        ("Space", "sc.toggle"),
        ("+ / -", "sc.rate_step"),
        ("[ / ]", "sc.rate"),
        (", / .", "sc.frame"),
        ("F / F11", "sc.fullscreen"),
        ("H", "sc.overlay"),
        ("Ctrl+H", "sc.dock_overlay"),
        ("V", "sc.subtitle"),
        ("Esc", "sc.escape"),
    )),
    ("sc.group.audio", (
        ("↑ / ↓", "sc.volume"),
        ("M", "sc.mute"),
    )),
    ("sc.group.edit", (
        ("Ctrl+O", "sc.open"),
        ("Ctrl+S", "sc.snapshot"),
        ("I / O", "sc.marks"),
    )),
    ("sc.group.tools", (
        ("Ctrl+1", "sc.tool_compress"),
        ("Ctrl+2", "sc.tool_cut"),
        ("Ctrl+3", "sc.tool_queue"),
        ("Ctrl+4", "sc.tool_list"),
        ("Ctrl+5", "sc.tool_info"),
        ("F1", "sc.help"),
    )),
)


def _typing_focus() -> bool:
    widget = QApplication.focusWidget()
    return isinstance(widget, _INPUT_WIDGETS)


def install_shortcuts(win) -> None:
    """把快捷键挂到主窗口。win 需实现 shortcuts.py 中引用的那些方法。"""
    win._shortcuts = list(getattr(win, "_shortcuts", []))

    def add(key: str, handler, *, guard: bool = True, app_wide: bool = False) -> None:
        shortcut = QShortcut(QKeySequence(key), win)
        shortcut.setContext(
            Qt.ShortcutContext.ApplicationShortcut if app_wide
            else Qt.ShortcutContext.WindowShortcut
        )

        def call() -> None:
            if guard and _typing_focus():
                return
            handler()

        shortcut.activated.connect(call)
        win._shortcuts.append(shortcut)

    add("Left", lambda: win.player.skip(-5_000))
    add("Right", lambda: win.player.skip(5_000))
    add("Shift+Left", lambda: win.player.skip(-1_000))
    add("Shift+Right", lambda: win.player.skip(1_000))
    add("Ctrl+Left", lambda: win.player.skip(-30_000))
    add("Ctrl+Right", lambda: win.player.skip(30_000))
    add("Home", lambda: win.player.seek(0))
    add("End", lambda: win.player.seek(win.player.duration))
    add("PgUp", lambda: win._step_file(-1))
    add("PgDown", lambda: win._step_file(1))
    # 调速：+ 与 = 覆盖「按 Shift」和「不按 Shift」两种键盘布局，
    # 小键盘 + / - 单独注册，避免 NumLock 状态下失效。
    for key in ("+", "=", "Num++"):
        add(key, win.player.faster)
    for key in ("-", "Num+-"):
        add(key, win.player.slower)
    add("Up", lambda: win.change_volume(5))
    add("Down", lambda: win.change_volume(-5))
    add("M", win.toggle_mute)
    add("V", win.toggle_subtitle)
    add("H", win.toggle_overlay, app_wide=True)
    add("F", win.toggle_fullscreen)
    add("F11", win.toggle_fullscreen, guard=False, app_wide=True)
    add("Esc", win.escape_pressed, guard=False, app_wide=True)
    # F1 由 Help 菜单的 QAction 承担（ApplicationShortcut），避免重复注册


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("sc.title"))
        self.resize(520, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels([tr("sc.key"), tr("sc.action")])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 170)
        self._fill()
        layout.addWidget(self.table, 1)

        note = QTableWidgetItem(tr("sc.note"))
        note_row = self.table.rowCount()
        self.table.insertRow(note_row)
        self.table.setSpan(note_row, 0, 1, 2)
        self.table.setItem(note_row, 0, note)
        self.table.item(note_row, 0).setFlags(Qt.ItemFlag.ItemIsEnabled)

        row = QHBoxLayout()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)

    def _fill(self) -> None:
        for group_key, rows in GROUPS:
            group_row = self.table.rowCount()
            self.table.insertRow(group_row)
            item = QTableWidgetItem(tr(group_key))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            self.table.setItem(group_row, 0, item)
            self.table.setSpan(group_row, 0, 1, 2)
            for keys, action_key in rows:
                row = self.table.rowCount()
                self.table.insertRow(row)
                key_item = QTableWidgetItem(keys)
                key_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                action_item = QTableWidgetItem(tr(action_key))
                action_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, 0, key_item)
                self.table.setItem(row, 1, action_item)
