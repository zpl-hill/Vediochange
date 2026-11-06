"""播放列表：已选中文件的清单，双击即播，可调整顺序。

只负责显示与发信号，真实的列表数据在 MainWindow 持有的 PlayerPlaylist 里，
避免「界面列表」和「实际播放顺序」两份状态互相打架。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from config.i18n import tr


class PlaylistPanel(QWidget):
    addRequested = Signal()
    playRequested = Signal(int)
    removeRequested = Signal(list)
    clearRequested = Signal()
    moveRequested = Signal(int, int)
    autoNextChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._count = 0

        self.title_label = QLabel()
        self.title_label.setObjectName("hint")
        self.auto_next = QCheckBox()
        self.auto_next.setChecked(True)

        head = QHBoxLayout()
        head.addWidget(self.title_label)
        head.addStretch(1)
        head.addWidget(self.auto_next)

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setUniformItemSizes(True)
        self.list.itemDoubleClicked.connect(lambda item: self._emit_play(item))
        self.list.itemActivated.connect(lambda item: self._emit_play(item))

        self.add_btn = QPushButton()
        self.remove_btn = QPushButton()
        self.up_btn = QPushButton()
        self.down_btn = QPushButton()
        self.clear_btn = QPushButton()

        buttons = QHBoxLayout()
        for button in (self.add_btn, self.remove_btn, self.up_btn, self.down_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.clear_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addLayout(head)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)

        self.add_btn.clicked.connect(self.addRequested)
        self.remove_btn.clicked.connect(self._emit_remove)
        self.clear_btn.clicked.connect(self.clearRequested)
        self.up_btn.clicked.connect(lambda: self._emit_move(-1))
        self.down_btn.clicked.connect(lambda: self._emit_move(1))
        self.auto_next.toggled.connect(self.autoNextChanged)
        self.retranslate()

    # ---------------- 数据 ----------------
    def set_items(self, items: list[str], index: int = -1) -> None:
        self._count = len(items)
        self.list.blockSignals(True)
        self.list.clear()
        if not items:
            placeholder = QListWidgetItem(tr("playlist.empty"))
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(placeholder)
        else:
            current_font = QFont(self.list.font())
            current_font.setBold(True)
            for row, path in enumerate(items):
                item = QListWidgetItem(f"{row + 1:02d}.  {Path(path).name}")
                item.setToolTip(path)
                if row == index:
                    item.setFont(current_font)
                    item.setText(f"▶  {row + 1:02d}.  {Path(path).name}")
                self.list.addItem(item)
            if 0 <= index < self.list.count():
                self.list.setCurrentRow(index)
                self.list.scrollToItem(self.list.item(index))
        self.list.blockSignals(False)
        self._update_labels()
        self._update_buttons()

    def selected_rows(self) -> list[int]:
        if not self._count:
            return []
        return sorted({self.list.row(item) for item in self.list.selectedItems()})

    def current_row(self) -> int:
        if not self._count:
            return -1
        return self.list.currentRow()

    def set_auto_next(self, value: bool) -> None:
        self.auto_next.blockSignals(True)
        self.auto_next.setChecked(bool(value))
        self.auto_next.blockSignals(False)

    def select_row(self, row: int) -> None:
        """只移动界面选中行（用于上下移动后继续调整同一项）。"""
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
            self._update_buttons()

    # ---------------- 交互 ----------------
    def _emit_play(self, item: QListWidgetItem | None) -> None:
        if item is None or not self._count:
            return
        row = self.list.row(item)
        if row >= 0:
            self.playRequested.emit(row)

    def _emit_remove(self) -> None:
        rows = self.selected_rows()
        if rows:
            self.removeRequested.emit(rows)

    def _emit_move(self, delta: int) -> None:
        row = self.current_row()
        if row >= 0:
            self.moveRequested.emit(row, delta)

    def _update_buttons(self) -> None:
        row = self.current_row()
        has = self._count > 0
        self.remove_btn.setEnabled(has and bool(self.selected_rows()))
        self.up_btn.setEnabled(has and row > 0)
        self.down_btn.setEnabled(has and 0 <= row < self._count - 1)
        self.clear_btn.setEnabled(has)

    def _update_labels(self) -> None:
        self.title_label.setText(tr("playlist.count", n=self._count))

    def retranslate(self) -> None:
        self.add_btn.setText(tr("playlist.add"))
        self.remove_btn.setText(tr("playlist.remove"))
        self.clear_btn.setText(tr("playlist.clear"))
        self.up_btn.setText(tr("playlist.up"))
        self.down_btn.setText(tr("playlist.down"))
        self.auto_next.setText(tr("playlist.auto_next"))
        self.auto_next.setToolTip(tr("playlist.auto_next_hint"))
        self._update_labels()
        if not self._count:
            self.list.clear()
            placeholder = QListWidgetItem(tr("playlist.empty"))
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(placeholder)
