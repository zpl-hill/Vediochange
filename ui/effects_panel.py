"""字幕 / 水印 / HDR 设置（嵌在输出面板里，不新开浮动窗口）。

所有控件只产出普通 dict，交给 EncodeParams → command_builder 生成 FFmpeg argv。
文件选择走 QFileDialog，路径含中文、空格、冒号时由 escape_filter_path() 转义。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from config.i18n import tr
from ffmpeg_core.ffprobe_engine import MediaInfo
from ffmpeg_core.media_options import (
    HDR_MODES,
    SUBTITLE_MODES,
    TONEMAP_ALGORITHMS,
    WATERMARK_MODES,
    WATERMARK_POSITIONS,
    HdrOptions,
    SubtitleOptions,
    WatermarkOptions,
    auto_font_name,
    default_font,
)
from ffmpeg_core.presets import EncodeParams

_LANGUAGES = (("chi", "中文"), ("eng", "English"), ("jpn", "日本語"),
              ("kor", "한국어"), ("fre", "Français"), ("ger", "Deutsch"),
              ("und", "Undetermined"))
_COLORS = ("white", "black", "red", "yellow", "cyan", "green", "#FFCC00")


class EffectsPanel(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._loading = False
        self._media: MediaInfo | None = None
        self._groups: dict[str, QGroupBox] = {}
        self._labels: dict[str, QLabel] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._build_subtitle())
        root.addWidget(self._build_watermark())
        root.addWidget(self._build_hdr())
        self.retranslate()
        self._refresh_visibility()
        self._wire()

    # ---------------- 构造工具 ----------------
    @staticmethod
    def _combo(items) -> QComboBox:
        combo = QComboBox()
        for value, text in items:
            combo.addItem(text, value)
        return combo

    def _file_row(self, line_edit: QLineEdit, caption: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line_edit, 1)
        button = QPushButton(tr("common.browse"))
        button.setFixedWidth(78)
        button.clicked.connect(lambda: self._pick_file(line_edit, caption))
        layout.addWidget(button)
        row._browse_button = button  # type: ignore[attr-defined]
        return row

    def _pick_file(self, line_edit: QLineEdit, caption: str) -> None:
        current = line_edit.text().strip()
        start = str(Path(current).parent) if current and Path(current).parent.is_dir() else ""
        path, _ = QFileDialog.getOpenFileName(self, caption, start,
                                              "所有文件 (*.*)")
        if path:
            line_edit.setText(path)

    def _row(self, form: QFormLayout, key: str, widget: QWidget) -> QLabel:
        label = QLabel(tr(key))
        form.addRow(label, widget)
        self._labels[key] = label
        return label

    # ---------------- 字幕 ----------------
    def _build_subtitle(self) -> QGroupBox:
        box = QGroupBox()
        form = QFormLayout(box)
        form.setContentsMargins(12, 14, 12, 10)
        self.sub_mode = QComboBox()
        self.sub_path = QLineEdit("")
        self.sub_path.setPlaceholderText(".srt / .ass / .ssa / .vtt")
        self.sub_lang = self._combo(_LANGUAGES)
        self.sub_font = QLineEdit(auto_font_name())
        self.sub_size = QSpinBox()
        self.sub_size.setRange(8, 96)
        self.sub_size.setValue(24)
        self.sub_outline = QSpinBox()
        self.sub_outline.setRange(0, 8)
        self.sub_outline.setValue(2)
        self.sub_force = QCheckBox()
        self.sub_force.setChecked(True)
        self.sub_file_row = self._file_row(self.sub_path, tr("fx.sub.file"))
        self._row(form, "fx.sub.mode", self.sub_mode)
        self.sub_file_label = self._row(form, "fx.sub.file", self.sub_file_row)
        self._row(form, "fx.sub.language", self.sub_lang)
        self.sub_style_label = self._row(form, "fx.sub.font", self.sub_font)
        self.sub_size_label = self._row(form, "fx.sub.size", self.sub_size)
        self.sub_outline_label = self._row(form, "fx.sub.outline", self.sub_outline)
        self.sub_force_label = self._row(form, "fx.sub.force", self.sub_force)
        self.sub_hint = QLabel("")
        self.sub_hint.setObjectName("hint")
        self.sub_hint.setWordWrap(True)
        form.addRow(self.sub_hint)
        self._groups["fx.group_subtitle"] = box
        self._sub_rows = [self.sub_file_label, self.sub_style_label,
                          self.sub_size_label, self.sub_outline_label, self.sub_force_label]
        return box

    # ---------------- 水印 ----------------
    def _build_watermark(self) -> QGroupBox:
        box = QGroupBox()
        form = QFormLayout(box)
        form.setContentsMargins(12, 14, 12, 10)
        self.wm_mode = QComboBox()
        self.wm_image = QLineEdit("")
        self.wm_image.setPlaceholderText(".png / .jpg / .webp（建议透明 PNG）")
        self.wm_text = QLineEdit("")
        self.wm_text.setPlaceholderText("Video Toolkit")
        self.wm_font_path = QLineEdit("")
        self.wm_font_path.setPlaceholderText(tr("fx.wm.font_auto"))
        self.wm_size = QSpinBox()
        self.wm_size.setRange(8, 160)
        self.wm_size.setValue(28)
        self.wm_color = self._combo([(c, c) for c in _COLORS])
        self.wm_opacity = QDoubleSpinBox()
        self.wm_opacity.setRange(0.0, 1.0)
        self.wm_opacity.setSingleStep(0.05)
        self.wm_opacity.setDecimals(2)
        self.wm_opacity.setValue(0.75)
        self.wm_position = QComboBox()
        self.wm_margin = QSpinBox()
        self.wm_margin.setRange(0, 400)
        self.wm_margin.setValue(20)
        self.wm_scale = QSpinBox()
        self.wm_scale.setRange(1, 100)
        self.wm_scale.setValue(15)
        self.wm_box = QCheckBox()
        self.wm_box.setChecked(True)

        self.wm_image_row = self._file_row(self.wm_image, tr("fx.wm.image"))
        self.wm_font_row = self._file_row(self.wm_font_path, tr("fx.wm.font"))
        self._row(form, "fx.wm.mode", self.wm_mode)
        self.wm_image_label = self._row(form, "fx.wm.image", self.wm_image_row)
        self.wm_text_label = self._row(form, "fx.wm.text", self.wm_text)
        self.wm_font_label = self._row(form, "fx.wm.font", self.wm_font_row)
        self.wm_size_label = self._row(form, "fx.wm.size", self.wm_size)
        self.wm_color_label = self._row(form, "fx.wm.color", self.wm_color)
        self._row(form, "fx.wm.opacity", self.wm_opacity)
        self._row(form, "fx.wm.position", self.wm_position)
        self._row(form, "fx.wm.margin", self.wm_margin)
        self.wm_scale_label = self._row(form, "fx.wm.scale", self.wm_scale)
        self.wm_box_label = self._row(form, "fx.wm.box", self.wm_box)
        self.wm_hint = QLabel("")
        self.wm_hint.setObjectName("hint")
        self.wm_hint.setWordWrap(True)
        form.addRow(self.wm_hint)
        self._groups["fx.group_watermark"] = box
        return box

    # ---------------- HDR ----------------
    def _build_hdr(self) -> QGroupBox:
        box = QGroupBox()
        form = QFormLayout(box)
        form.setContentsMargins(12, 14, 12, 10)
        self.hdr_mode = QComboBox()
        self.hdr_algorithm = QComboBox()
        self.hdr_npl = QSpinBox()
        self.hdr_npl.setRange(10, 1000)
        self.hdr_npl.setValue(100)
        self.hdr_desat = QDoubleSpinBox()
        self.hdr_desat.setRange(0.0, 2.0)
        self.hdr_desat.setSingleStep(0.1)
        self.hdr_desat.setDecimals(2)
        self.hdr_depth = self._combo([(10, "10-bit"), (8, "8-bit")])
        self.hdr_info = QLabel("")
        self.hdr_info.setObjectName("hint")
        self.hdr_info.setWordWrap(True)
        self._row(form, "fx.hdr.mode", self.hdr_mode)
        self.hdr_algo_label = self._row(form, "fx.hdr.algorithm", self.hdr_algorithm)
        self.hdr_npl_label = self._row(form, "fx.hdr.npl", self.hdr_npl)
        self.hdr_desat_label = self._row(form, "fx.hdr.desat", self.hdr_desat)
        self.hdr_depth_label = self._row(form, "fx.hdr.depth", self.hdr_depth)
        form.addRow(self.hdr_info)
        self._groups["fx.group_hdr"] = box
        return box

    # ---------------- 信号 / 显隐 ----------------
    def _wire(self) -> None:
        combos = [self.sub_mode, self.sub_lang, self.wm_mode, self.wm_color,
                  self.wm_position, self.hdr_mode, self.hdr_algorithm, self.hdr_depth]
        for combo in combos:
            combo.currentIndexChanged.connect(self._on_change)
        spins = [self.sub_size, self.sub_outline, self.wm_size, self.wm_margin,
                 self.wm_scale, self.hdr_npl]
        for spin in spins:
            spin.valueChanged.connect(self._on_change)
        for spin in (self.wm_opacity, self.hdr_desat):
            spin.valueChanged.connect(self._on_change)
        for edit in (self.sub_path, self.sub_font, self.wm_image, self.wm_text,
                     self.wm_font_path):
            edit.textChanged.connect(self._on_change)
        for check in (self.sub_force, self.wm_box):
            check.toggled.connect(self._on_change)

    def _on_change(self, *_args) -> None:
        if self._loading:
            return
        self._refresh_visibility()
        self.changed.emit()

    def _refresh_visibility(self) -> None:
        sub_mode = str(self.sub_mode.currentData() or "none")
        burn = sub_mode == "burn"
        embed = sub_mode == "embed"
        for row in self._sub_rows:
            key = {self.sub_file_label: "file", self.sub_style_label: "style",
                   self.sub_size_label: "style", self.sub_outline_label: "style",
                   self.sub_force_label: "style"}[row]
            row.setVisible((sub_mode != "none") if key == "file" else burn)
        self.sub_file_row.setVisible(sub_mode != "none")
        self.sub_lang.setVisible(sub_mode != "none")
        self._labels["fx.sub.language"].setVisible(sub_mode != "none")
        self.sub_font.setVisible(burn)
        self.sub_size.setVisible(burn)
        self.sub_outline.setVisible(burn)
        self.sub_force.setVisible(burn)
        self.sub_hint.setText(tr("fx.sub.hint_embed") if embed else tr("fx.sub.hint_burn"))

        wm_mode = str(self.wm_mode.currentData() or "none")
        is_image, is_text = wm_mode == "image", wm_mode == "text"
        for label, widget, show in (
            (self.wm_image_label, self.wm_image_row, is_image),
            (self.wm_text_label, self.wm_text, is_text),
            (self.wm_font_label, self.wm_font_row, is_text),
            (self.wm_size_label, self.wm_size, is_text),
            (self.wm_color_label, self.wm_color, is_text),
            (self.wm_scale_label, self.wm_scale, is_image),
            (self.wm_box_label, self.wm_box, is_text),
        ):
            label.setVisible(show)
            widget.setVisible(show)
        self._labels["fx.wm.opacity"].setVisible(is_image or is_text)
        self.wm_opacity.setVisible(is_image or is_text)
        self._labels["fx.wm.position"].setVisible(is_image or is_text)
        self.wm_position.setVisible(is_image or is_text)
        self._labels["fx.wm.margin"].setVisible(is_image or is_text)
        self.wm_margin.setVisible(is_image or is_text)
        self.wm_hint.setText(
            tr("fx.wm.hint_image") if is_image else
            tr("fx.wm.hint_text") if is_text else tr("fx.wm.hint_none")
        )

        tonemap = str(self.hdr_mode.currentData() or "auto") == "tonemap"
        keep = str(self.hdr_mode.currentData() or "auto") in ("keep", "auto")
        for label, widget in (
            (self.hdr_algo_label, self.hdr_algorithm),
            (self.hdr_npl_label, self.hdr_npl),
            (self.hdr_desat_label, self.hdr_desat),
        ):
            label.setVisible(tonemap)
            widget.setVisible(tonemap)
        self.hdr_depth_label.setVisible(keep)
        self.hdr_depth.setVisible(keep)
        self.hdr_info.setText(self._hdr_note())

    def _hdr_note(self) -> str:
        if self._media is None:
            return tr("fx.hdr.no_media")
        if self._media.is_hdr:
            return tr("fx.hdr.detected", info=self._media.color_text())
        return tr("fx.hdr.sdr_source", info=self._media.color_text())

    # ---------------- 数据 ----------------
    def values(self) -> dict[str, dict]:
        return {
            "subtitle": SubtitleOptions(
                mode=str(self.sub_mode.currentData() or "none"),
                path=self.sub_path.text().strip(),
                language=str(self.sub_lang.currentData() or "chi"),
                font_name=self.sub_font.text().strip() or auto_font_name(),
                font_size=int(self.sub_size.value()),
                outline=int(self.sub_outline.value()),
                force_style=bool(self.sub_force.isChecked()),
            ).to_dict(),
            "watermark": WatermarkOptions(
                mode=str(self.wm_mode.currentData() or "none"),
                image_path=self.wm_image.text().strip(),
                text=self.wm_text.text().strip(),
                font_path=self.wm_font_path.text().strip() or default_font(),
                font_size=int(self.wm_size.value()),
                color=str(self.wm_color.currentData() or "white"),
                opacity=float(self.wm_opacity.value()),
                position=str(self.wm_position.currentData() or "br"),
                margin=int(self.wm_margin.value()),
                scale_percent=int(self.wm_scale.value()),
                box=bool(self.wm_box.isChecked()),
            ).to_dict(),
            "hdr": HdrOptions(
                mode=str(self.hdr_mode.currentData() or "auto"),
                tonemap=str(self.hdr_algorithm.currentData() or "hable"),
                npl=int(self.hdr_npl.value()),
                desat=float(self.hdr_desat.value()),
                bit_depth=int(self.hdr_depth.currentData() or 10),
            ).to_dict(),
        }

    def set_params(self, params: EncodeParams) -> None:
        """从预设恢复（只覆盖预设里实际写过的字段，其余保持用户当前值）。"""
        sub = SubtitleOptions.from_dict(params.subtitle)
        wm = WatermarkOptions.from_dict(params.watermark)
        hdr = HdrOptions.from_dict(params.hdr)
        self._loading = True

        def select(combo: QComboBox, data) -> None:
            index = combo.findData(data)
            if index >= 0:
                combo.setCurrentIndex(index)

        if params.subtitle:
            select(self.sub_mode, sub.mode)
            self.sub_path.setText(sub.path)
            select(self.sub_lang, sub.language)
            self.sub_font.setText(sub.font_name)
            self.sub_size.setValue(sub.font_size)
            self.sub_outline.setValue(sub.outline)
            self.sub_force.setChecked(sub.force_style)
        if params.watermark:
            select(self.wm_mode, wm.mode)
            self.wm_image.setText(wm.image_path)
            self.wm_text.setText(wm.text)
            self.wm_font_path.setText(wm.font_path)
            self.wm_size.setValue(wm.font_size)
            select(self.wm_color, wm.color)
            self.wm_opacity.setValue(wm.opacity)
            select(self.wm_position, wm.position)
            self.wm_margin.setValue(wm.margin)
            self.wm_scale.setValue(wm.scale_percent)
            self.wm_box.setChecked(wm.box)
        if params.hdr:
            select(self.hdr_mode, hdr.mode)
            select(self.hdr_algorithm, hdr.tonemap)
            self.hdr_npl.setValue(hdr.npl)
            self.hdr_desat.setValue(hdr.desat)
            select(self.hdr_depth, hdr.bit_depth)
        self._loading = False
        self._refresh_visibility()

    def set_media(self, media: MediaInfo | None) -> None:
        self._media = media
        self.hdr_info.setText(self._hdr_note())

    # ---------------- 国际化 ----------------
    def _fill_combos(self) -> None:
        self._rebuild(self.sub_mode, [(m, tr(f"fx.sub.mode.{m}")) for m in SUBTITLE_MODES])
        self._rebuild(self.wm_mode, [(m, tr(f"fx.wm.mode.{m}")) for m in WATERMARK_MODES])
        self._rebuild(self.wm_position,
                      [(p, tr(f"fx.wm.pos.{p}")) for p in WATERMARK_POSITIONS],
                      default="br")
        self._rebuild(self.hdr_mode, [(m, tr(f"fx.hdr.mode.{m}")) for m in HDR_MODES])
        self._rebuild(self.hdr_algorithm, [(a, a) for a in TONEMAP_ALGORITHMS])

    @staticmethod
    def _rebuild(combo: QComboBox, items, default=None) -> None:
        first_fill = combo.count() == 0
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for value, text in items:
            combo.addItem(text, value)
        index = combo.findData(default if first_fill and default is not None else current)
        if index < 0 and default is not None:
            index = combo.findData(default)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def retranslate(self) -> None:
        for key, box in self._groups.items():
            box.setTitle(tr(key))
        for key, label in self._labels.items():
            label.setText(tr(key))
        self.sub_force.setText(tr("fx.sub.force"))
        self.wm_box.setText(tr("fx.wm.box"))
        self.wm_font_path.setPlaceholderText(tr("fx.wm.font_auto"))
        for row in (self.sub_file_row, self.wm_image_row, self.wm_font_row):
            button = getattr(row, "_browse_button", None)
            if button is not None:
                button.setText(tr("common.browse"))
        self._fill_combos()
        self._refresh_visibility()
