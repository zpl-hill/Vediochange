"""剪切设置面板：IN/OUT、无损/精确、输出路径与命令预览。

V1.0 原本剪切只能靠时间轴打点后从菜单直接执行，没有可视化设置；
这里补上按需唤起的独立面板，所有命令仍由 command_builder 构造。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from config.i18n import i18n, tr
from config.logging_setup import get_logger
from ffmpeg_core.ffprobe_engine import MediaInfo
from modules.converter import Plan, default_output_path

_log = get_logger("cut")


class CutPanel(QWidget):
    cutRequested = Signal(bool)          # start_now
    currentRequested = Signal(bool)      # True=用当前位置填入点，False=出点
    marksChanged = Signal(float, float)  # 秒；同步回时间轴

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.src = ""
        self.media: MediaInfo | None = None
        self._builder: Callable[[], Plan] | None = None
        self._loading = False
        self._groups: dict[str, QGroupBox] = {}
        self._labels: dict[str, QLabel] = {}
        self._build_ui()
        i18n().changed.connect(self.retranslate)

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self.hint_label.setObjectName("hint")
        root.addWidget(self.hint_label)

        range_box = QGroupBox()
        self._groups["cut.group_range"] = range_box
        form = QFormLayout(range_box)
        form.setContentsMargins(12, 16, 12, 10)
        form.setSpacing(8)

        self.in_spin = QDoubleSpinBox()
        self.in_spin.setRange(0, 1_000_000)
        self.in_spin.setDecimals(3)
        self.in_spin.setSuffix(" s")
        self.out_spin = QDoubleSpinBox()
        self.out_spin.setRange(0, 1_000_000)
        self.out_spin.setDecimals(3)
        self.out_spin.setSuffix(" s")

        self.in_current_btn = QPushButton()
        self.out_current_btn = QPushButton()
        self.in_current_btn.setFixedWidth(110)
        self.out_current_btn.setFixedWidth(110)
        self.in_row = QHBoxLayout()
        self.in_row.addWidget(self.in_spin, 1)
        self.in_row.addWidget(self.in_current_btn)
        self.out_row = QHBoxLayout()
        self.out_row.addWidget(self.out_spin, 1)
        self.out_row.addWidget(self.out_current_btn)

        self._labels["cut.in"] = QLabel()
        self._labels["cut.out"] = QLabel()
        self._labels["cut.mode"] = QLabel()
        self._labels["cut.output"] = QLabel()
        form.addRow(self._labels["cut.in"], self.in_row)
        form.addRow(self._labels["cut.out"], self.out_row)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("", "lossless")
        self.mode_combo.addItem("", "precise")
        form.addRow(self._labels["cut.mode"], self.mode_combo)
        root.addWidget(range_box)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.browse_btn = QPushButton()
        self.browse_btn.setFixedWidth(84)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.browse_btn)
        root.addLayout(output_row)

        self.warn_label = QLabel()
        self.warn_label.setWordWrap(True)
        self.warn_label.setObjectName("hint")
        root.addWidget(self.warn_label)

        preview_box = QGroupBox()
        self._groups["cut.group_command"] = preview_box
        preview_layout = QVBoxLayout(preview_box)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview.setMaximumHeight(130)
        preview_layout.addWidget(self.preview)
        root.addWidget(preview_box, 1)

        buttons = QHBoxLayout()
        self.queue_btn = QPushButton()
        self.run_btn = QPushButton()
        self.run_btn.setObjectName("primary")
        buttons.addWidget(self.queue_btn)
        buttons.addWidget(self.run_btn)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self.in_current_btn.clicked.connect(lambda: self.currentRequested.emit(True))
        self.out_current_btn.clicked.connect(lambda: self.currentRequested.emit(False))
        self.browse_btn.clicked.connect(self._browse)
        self.queue_btn.clicked.connect(lambda: self.cutRequested.emit(False))
        self.run_btn.clicked.connect(lambda: self.cutRequested.emit(True))
        for spin in (self.in_spin, self.out_spin):
            spin.valueChanged.connect(self._on_values_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_values_changed)
        self.output_edit.textChanged.connect(self._on_values_changed)
        self.retranslate()

    # ---------------- 数据 ----------------
    def set_plan_builder(self, builder: Callable[[], Plan]) -> None:
        self._builder = builder
        self.refresh_preview()

    def set_context(self, src: str, media: MediaInfo | None,
                    in_ms: int | None, out_ms: int | None) -> None:
        self.src = src
        self.media = media
        duration = float(media.duration) if media and media.duration else 0.0
        self._loading = True
        self.in_spin.setRange(0, max(1.0, duration))
        self.out_spin.setRange(0, max(1.0, duration))
        start = in_ms / 1000.0 if in_ms is not None else 0.0
        end = out_ms / 1000.0 if out_ms is not None else min(duration or 10.0, start + 10.0)
        self.in_spin.setValue(min(start, max(0.0, duration)))
        self.out_spin.setValue(min(max(end, start + 0.1), max(0.1, duration)))
        if not self.output_edit.text() and src:
            try:
                self.output_edit.setText(default_output_path(src, "mp4", suffix="_cut"))
            except Exception as exc:
                _log.warning("生成剪切输出路径失败: %s", exc)
        self._loading = False
        self.refresh_preview()

    def set_current(self, is_in: bool, seconds: float) -> None:
        self._loading = True
        (self.in_spin if is_in else self.out_spin).setValue(max(0.0, float(seconds)))
        self._loading = False
        self._on_values_changed()

    def values(self) -> tuple[float, float, bool, str]:
        return (
            float(self.in_spin.value()),
            float(self.out_spin.value()),
            self.mode_combo.currentData() == "lossless",
            self.output_edit.text().strip(),
        )

    def _on_values_changed(self, *_args) -> None:
        if self._loading:
            return
        self.marksChanged.emit(self.in_spin.value(), self.out_spin.value())
        self.refresh_preview()

    def refresh_preview(self) -> None:
        if not self.src:
            self.preview.setPlainText(tr("cut.no_media"))
            return
        start, end, _lossless, _dst = self.values()
        if end <= start:
            self.preview.setPlainText(tr("msg.invalid_range"))
            return
        if self._builder is None:
            self.preview.setPlainText("")
            return
        try:
            self.preview.setPlainText(self._builder().preview)
        except Exception as exc:
            _log.warning("剪切命令预览失败: %s", exc)
            self.preview.setPlainText(f"[{exc.__class__.__name__}] {exc}")

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, tr("cut.output"), self.output_edit.text(),
            "MP4 (*.mp4);;MOV (*.mov);;MKV (*.mkv);;所有文件 (*.*)",
        )
        if path:
            self.output_edit.setText(path)

    # ---------------- 国际化 ----------------
    def retranslate(self) -> None:
        for key, box in self._groups.items():
            box.setTitle(tr(key))
        for key, label in self._labels.items():
            label.setText(tr(key))
        self.hint_label.setText(tr("cut.hint"))
        self.warn_label.setText(tr("cut.warn_lossless"))
        self.in_current_btn.setText(tr("cut.use_current"))
        self.out_current_btn.setText(tr("cut.use_current"))
        self.browse_btn.setText(tr("common.browse"))
        self.queue_btn.setText(tr("cut.queue"))
        self.run_btn.setText(tr("cut.run"))
        current = self.mode_combo.currentData() or "lossless"
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        self.mode_combo.addItem(tr("cut.mode.lossless"), "lossless")
        self.mode_combo.addItem(tr("cut.mode.precise"), "precise")
        index = self.mode_combo.findData(current)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        self.mode_combo.blockSignals(False)
