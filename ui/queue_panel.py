"""批量任务队列面板：等待 / 处理中 / 完成 / 失败，支持 1/2/4/8 并发。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from config.i18n import tr
from ffmpeg_core.jobs import Job, JobQueue, JobStatus

_STATUS_COLORS = {
    JobStatus.PENDING: "#98a1b3",
    JobStatus.RUNNING: "#6f9bea",
    JobStatus.DONE: "#37c98b",
    JobStatus.FAILED: "#ff6b6b",
    JobStatus.CANCELLED: "#8b93a7",
}


class QueuePanel(QWidget):
    addRequested = Signal()
    removeRequested = Signal()
    clearRequested = Signal()
    startRequested = Signal()
    stopRequested = Signal()
    concurrencyChanged = Signal(int)

    def __init__(self, queue: JobQueue, parent: QWidget | None = None):
        super().__init__(parent)
        self.queue = queue
        self._rows: dict[str, int] = {}

        self.table = QTableWidget(0, 4)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 90), (2, 140), (3, 260)):
            self.table.setColumnWidth(col, width)
        self.table.verticalHeader().setVisible(False)

        toolbar = QHBoxLayout()
        self.add_btn = QPushButton()
        self.remove_btn = QPushButton()
        self.clear_btn = QPushButton()
        self.start_btn = QPushButton()
        self.start_btn.setObjectName("primary")
        self.stop_btn = QPushButton()
        self.concurrency_label = QLabel()
        self.concurrency = QComboBox()
        for value in (1, 2, 4, 8):
            self.concurrency.addItem(str(value), value)
        self.concurrency.setCurrentIndex(self.concurrency.findData(queue._pool.maxThreadCount()))
        for button in (self.add_btn, self.remove_btn, self.clear_btn, self.start_btn, self.stop_btn):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.concurrency_label)
        toolbar.addWidget(self.concurrency)

        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self.table, 1)

        self.add_btn.clicked.connect(self.addRequested)
        self.remove_btn.clicked.connect(self.removeRequested)
        self.clear_btn.clicked.connect(self.clearRequested)
        self.start_btn.clicked.connect(self.startRequested)
        self.stop_btn.clicked.connect(self.stopRequested)
        self.concurrency.currentIndexChanged.connect(
            lambda: self.concurrencyChanged.emit(int(self.concurrency.currentData() or 2))
        )
        queue.job_changed.connect(self.on_job_changed)
        self.retranslate()

    def selected_job_id(self) -> str | None:
        row = self.table.currentRow()
        for job_id, index in self._rows.items():
            if index == row:
                return job_id
        return None

    def on_job_changed(self, job: Job) -> None:
        row = self._rows.get(job.id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._rows[job.id] = row
            self.table.setItem(row, 0, QTableWidgetItem(job.name))
            self.table.setItem(row, 1, QTableWidgetItem(""))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            self.table.setCellWidget(row, 2, bar)
            self.table.setItem(row, 3, QTableWidgetItem(""))
        status_item = self.table.item(row, 1)
        if status_item:
            status_item.setText(tr(job.status_key))
            status_item.setForeground(QColor(_STATUS_COLORS.get(job.status, "#d8dde8")))
        bar = self.table.cellWidget(row, 2)
        if isinstance(bar, QProgressBar):
            bar.setValue(job.progress)
        message_item = self.table.item(row, 3)
        if message_item:
            message_item.setText(job.message or job.output)

    def refresh_rows(self) -> None:
        self.table.setRowCount(0)
        self._rows.clear()
        for job in self.queue.jobs():
            self.on_job_changed(job)

    def retranslate(self) -> None:
        self.add_btn.setText(tr("queue.add"))
        self.remove_btn.setText(tr("queue.remove"))
        self.clear_btn.setText(tr("queue.clear"))
        self.start_btn.setText(tr("queue.start"))
        self.stop_btn.setText(tr("queue.stop"))
        self.concurrency_label.setText(tr("queue.concurrency"))
        self.table.setHorizontalHeaderLabels([
            tr("queue.col.file"), tr("queue.col.status"),
            tr("queue.col.progress"), tr("queue.col.message"),
        ])
        self.refresh_rows()
