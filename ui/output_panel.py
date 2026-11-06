"""输出设置面板：三层预设 + 手动覆盖分辨率/码率等 + 命令预览 + 极限压缩向导。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from config.i18n import i18n, tr
from config.logging_setup import get_logger
from config.settings import settings
from ffmpeg_core.ffprobe_engine import MediaInfo
from ffmpeg_core.ffmpeg_engine import EncoderCapabilities
from ffmpeg_core.presets import EncodeParams, Preset, apply_strategy, preset_library
from modules.converter import Plan, default_output_path, plan_conversion
from ui.effects_panel import EffectsPanel

_log = get_logger("output")

_CONTAINERS = ("mp4", "mov", "mkv", "avi", "webm", "ts")
_CODECS = (("h264", "H.264 / AVC"), ("hevc", "H.265 / HEVC"), ("av1", "AV1"))
_SCALES = ("none", "fit", "pad_black", "pad_blur", "fill_crop", "stretch")
_RATE_MODES = ("crf", "bitrate", "target_size")
_PRESETS = ("ultrafast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
_PROFILES = ("", "baseline", "main", "high", "high10")
_ACODECS = (("aac", "AAC"), ("libmp3lame", "MP3"), ("flac", "FLAC"),
            ("pcm_s16le", "PCM WAV"), ("copy", "Copy"), ("none", "None"))
_SAMPLERATES = (44100, 48000, 96000)
_ENCODERS = ("auto", "cpu", "nvidia", "intel", "amd")


class OutputPanel(QWidget):
    paramsChanged = Signal()
    queueRequested = Signal()
    convertRequested = Signal()
    extremeRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = preset_library()
        self.src: str = ""
        self.media: MediaInfo | None = None
        self.caps: EncoderCapabilities | None = None
        self._current_preset: Preset | None = None
        self._loading = False
        self._groups: dict[str, QGroupBox] = {}
        self._form_labels: dict[str, QLabel] = {}
        # 先记住用户上次的预设：构建 UI 时会自动选中第一条并写回配置，
        # 如果之后再读 settings 就永远拿不回用户的选择了。
        self._saved_preset_id = str(settings().get("last_preset", ""))
        self._build_ui()
        self._load_presets()
        initial = self.library.get(self._saved_preset_id) or (
            self.library.presets[0] if self.library.presets else None
        )
        if initial:
            self._apply_preset(initial)
        i18n().changed.connect(self.retranslate)

    # ---------------- UI ----------------
    @staticmethod
    def _combo(items) -> QComboBox:
        combo = QComboBox()
        for value, text in items:
            combo.addItem(text, value)
        return combo

    @staticmethod
    def _spin(value: int, maximum: int = 100000, minimum: int = 0) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    def _grid_group(self, key: str, rows: list[tuple[str, QWidget]]) -> QGroupBox:
        box = QGroupBox(tr(key))
        grid = QGridLayout(box)
        grid.setContentsMargins(12, 16, 12, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        for index, (label_key, widget) in enumerate(rows):
            row, col = divmod(index, 2)
            label = QLabel(tr(label_key))
            grid.addWidget(label, row, col * 2)
            grid.addWidget(widget, row, col * 2 + 1)
            self._form_labels[label_key] = label
        self._groups[key] = box
        return box

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form = QWidget()
        form.setMinimumWidth(720)
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(4, 4, 10, 4)

        # 预设库
        self.category_combo = QComboBox()
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(280)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.category_combo, 1)
        preset_row.addWidget(self.preset_combo, 2)
        preset_box = QGroupBox(tr("output.group_preset"))
        preset_layout = QVBoxLayout(preset_box)
        preset_layout.addLayout(preset_row)
        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)
        self.note_label.setObjectName("hint")
        self.disclaimer_label = QLabel(self.library.disclaimer_text())
        self.disclaimer_label.setWordWrap(True)
        self.disclaimer_label.setObjectName("hint")
        preset_layout.addWidget(self.note_label)
        preset_layout.addWidget(self.disclaimer_label)
        self._groups["output.group_preset"] = preset_box
        form_layout.addWidget(preset_box)

        # 视频参数
        self.container_combo = self._combo([(c, c.upper()) for c in _CONTAINERS])
        self.codec_combo = self._combo(_CODECS)
        self.width_spin = self._spin(0, 7680)
        self.height_spin = self._spin(0, 4320)
        self.scale_combo = self._combo([])
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(0, 240)
        self.fps_spin.setDecimals(3)
        self.fps_spin.setValue(0)
        self.rate_mode_combo = self._combo([])
        self.crf_spin = self._spin(23, 51)
        self.bitrate_spin = self._spin(8000, 200000, 100)
        self.maxrate_spin = self._spin(0, 300000)
        self.bufsize_spin = self._spin(0, 600000)
        self.target_size_spin = QDoubleSpinBox()
        self.target_size_spin.setRange(1, 200000)
        self.target_size_spin.setDecimals(1)
        self.target_size_spin.setValue(100)
        self.enc_preset_combo = self._combo([(p, p) for p in _PRESETS])
        self.profile_combo = self._combo([(p, p or "Auto") for p in _PROFILES])
        from PySide6.QtWidgets import QLineEdit
        self.level_edit = QLineEdit("")
        self.level_edit.setPlaceholderText("4.1 / 5.1 / Auto")
        self.gop_spin = self._spin(0, 600)

        video_box = self._grid_group("output.group_video", [
            ("output.container", self.container_combo),
            ("output.vcodec", self.codec_combo),
            ("output.width", self.width_spin),
            ("output.height", self.height_spin),
            ("output.scale_mode", self.scale_combo),
            ("output.fps", self.fps_spin),
            ("output.rate_mode", self.rate_mode_combo),
            ("output.crf", self.crf_spin),
            ("output.bitrate", self.bitrate_spin),
            ("output.maxrate", self.maxrate_spin),
            ("output.bufsize", self.bufsize_spin),
            ("output.target_size", self.target_size_spin),
            ("output.enc_preset", self.enc_preset_combo),
            ("output.profile", self.profile_combo),
            ("output.level", self.level_edit),
            ("output.gop", self.gop_spin),
        ])
        form_layout.addWidget(video_box)

        # 音频参数
        self.acodec_combo = self._combo(_ACODECS)
        self.abitrate_spin = self._spin(128, 640, 16)
        self.samplerate_combo = self._combo([(s, str(s)) for s in _SAMPLERATES])
        self.channels_spin = self._spin(2, 8, 1)
        form_layout.addWidget(self._grid_group("output.group_audio", [
            ("output.acodec", self.acodec_combo),
            ("output.abitrate", self.abitrate_spin),
            ("output.samplerate", self.samplerate_combo),
            ("output.channels", self.channels_spin),
        ]))

        # 字幕 / 水印 / HDR
        self.effects = EffectsPanel()
        form_layout.addWidget(self.effects)

        # 编码器与高级
        self.encoder_combo = self._combo([])
        self.two_pass_check = QCheckBox()
        self.faststart_check = QCheckBox()
        self.faststart_check.setChecked(True)
        form_layout.addWidget(self._grid_group("output.group_action", [
            ("output.encoder", self.encoder_combo),
            ("output.two_pass", self.two_pass_check),
            ("output.faststart", self.faststart_check),
        ]))
        form_layout.addStretch(1)
        scroll.setWidget(form)
        root.addWidget(scroll, 3)

        # 命令预览
        preview_box = QGroupBox(tr("output.command"))
        preview_layout = QVBoxLayout(preview_box)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(120)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        preview_layout.addWidget(self.preview)
        info_row = QHBoxLayout()
        self.estimate_label = QLabel(f"{tr('output.estimate')}: -")
        self.copy_btn = QPushButton()
        info_row.addWidget(self.estimate_label)
        info_row.addStretch(1)
        info_row.addWidget(self.copy_btn)
        preview_layout.addLayout(info_row)
        self._groups["output.command"] = preview_box
        root.addWidget(preview_box, 1)

        # 操作按钮
        buttons = QHBoxLayout()
        self.reset_btn = QPushButton()
        self.extreme_btn = QPushButton()
        self.queue_btn = QPushButton()
        self.convert_btn = QPushButton()
        self.convert_btn.setObjectName("primary")
        self.extreme_btn.setToolTip(tr("output.extreme_hint"))
        for button in (self.reset_btn, self.extreme_btn, self.queue_btn, self.convert_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self._fill_i18n_combos()
        self.retranslate()

        self.category_combo.currentIndexChanged.connect(self._on_category_changed)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.reset_btn.clicked.connect(self._on_reset)
        self.copy_btn.clicked.connect(self._copy_command)
        self.queue_btn.clicked.connect(self.queueRequested)
        self.convert_btn.clicked.connect(self.convertRequested)
        self.extreme_btn.clicked.connect(self._on_extreme)

        widgets = [
            self.container_combo, self.codec_combo, self.scale_combo, self.rate_mode_combo,
            self.enc_preset_combo, self.profile_combo, self.acodec_combo,
            self.samplerate_combo, self.encoder_combo,
        ]
        for combo in widgets:
            combo.currentIndexChanged.connect(self._on_param_changed)
        spins = [
            self.width_spin, self.height_spin, self.crf_spin, self.bitrate_spin,
            self.maxrate_spin, self.bufsize_spin, self.gop_spin,
            self.abitrate_spin, self.channels_spin,
        ]
        for spin in spins:
            spin.valueChanged.connect(self._on_param_changed)
        for spin in (self.fps_spin, self.target_size_spin):
            spin.valueChanged.connect(self._on_param_changed)
        self.level_edit.textChanged.connect(self._on_param_changed)
        self.two_pass_check.toggled.connect(self._on_param_changed)
        self.faststart_check.toggled.connect(self._on_param_changed)
        self.effects.changed.connect(self._on_param_changed)

    # ---------------- 预设 ----------------
    def _load_presets(self) -> None:
        self._loading = True
        self.category_combo.clear()
        for value, key in (("all", "cat.all"), ("platform", "cat.platform"),
                           ("device", "cat.device"), ("quality", "cat.quality")):
            self.category_combo.addItem(tr(key), value)
        self._on_category_changed()
        self._loading = False

    def _on_category_changed(self) -> None:
        if self._loading:
            return
        category = self.category_combo.currentData()
        previous = self.preset_combo.currentData()
        self._loading = True
        self.preset_combo.clear()
        presets = [p for p in self.library.presets if category in (None, "all") or p.category == category]
        for preset in presets:
            self.preset_combo.addItem(preset.label(i18n().lang), preset.id)
        if previous:
            index = self.preset_combo.findData(previous)
            if index >= 0:
                self.preset_combo.setCurrentIndex(index)
        self._loading = False
        if self.preset_combo.count() and self._current_preset is None:
            self._apply_preset(self.library.get(self.preset_combo.currentData()))

    def _on_preset_changed(self) -> None:
        if self._loading:
            return
        preset = self.library.get(self.preset_combo.currentData())
        if preset:
            self._apply_preset(preset)

    def _apply_preset(self, preset: Preset | None) -> None:
        if preset is None:
            return
        self._current_preset = preset
        # 预设不在当前分类过滤结果里时，自动切回「全部」，保证下拉框与参数始终一致
        if self.preset_combo.findData(preset.id) < 0:
            all_index = self.category_combo.findData("all")
            if all_index >= 0:
                self._loading = True
                self.category_combo.setCurrentIndex(all_index)
                self._loading = False
                self._on_category_changed()
        self._loading = True
        index = self.preset_combo.findData(preset.id)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)
        self._loading = False
        params = preset.params
        if preset.strategy:
            w = self.media.width if self.media and self.media.width else 1920
            h = self.media.height if self.media and self.media.height else 1080
            fps = self.media.fps if self.media and self.media.fps else 30.0
            params = apply_strategy(params, preset.strategy, w, h, fps)
        self._fill(params)
        self.note_label.setText(preset.description(i18n().lang))
        settings().set("last_preset", preset.id)
        self.refresh_preview()

    def _on_reset(self) -> None:
        if self._current_preset:
            self._apply_preset(self._current_preset)

    def _on_extreme(self) -> None:
        preset = self.library.get("quality.extreme")
        if preset:
            index = self.category_combo.findData("quality")
            if index >= 0:
                self.category_combo.setCurrentIndex(index)
            self._apply_preset(preset)
        self.extremeRequested.emit()

    def _fill(self, params: EncodeParams) -> None:
        self._loading = True

        def select(combo: QComboBox, data) -> None:
            index = combo.findData(data)
            if index >= 0:
                combo.setCurrentIndex(index)

        select(self.container_combo, params.container)
        select(self.codec_combo, params.codec_family)
        select(self.scale_combo, params.scale_mode)
        select(self.rate_mode_combo, params.rate_mode)
        select(self.enc_preset_combo, params.preset)
        select(self.profile_combo, params.profile)
        select(self.acodec_combo, params.acodec)
        select(self.samplerate_combo, params.samplerate)
        self.width_spin.setValue(int(params.width))
        self.height_spin.setValue(int(params.height))
        self.fps_spin.setValue(float(params.fps))
        self.crf_spin.setValue(int(params.crf))
        self.bitrate_spin.setValue(int(params.bitrate_kbps))
        self.maxrate_spin.setValue(int(params.maxrate_kbps))
        self.bufsize_spin.setValue(int(params.bufsize_kbps))
        self.target_size_spin.setValue(float(params.target_size_mb or 100))
        self.gop_spin.setValue(int(params.gop))
        self.level_edit.setText(str(params.level or ""))
        self.abitrate_spin.setValue(int(params.abitrate_kbps))
        self.channels_spin.setValue(int(params.channels))
        self.two_pass_check.setChecked(bool(params.two_pass))
        self.faststart_check.setChecked(bool(params.faststart))
        self.effects.set_params(params)
        self._loading = False

    # ---------------- 参数 / 命令 ----------------
    def _on_param_changed(self, *_args) -> None:
        if not self._loading:
            self.refresh_preview()
            self.paramsChanged.emit()

    def current_params(self) -> EncodeParams:
        return EncodeParams(
            container=str(self.container_combo.currentData() or "mp4"),
            codec_family=str(self.codec_combo.currentData() or "h264"),
            vcodec="libx264",
            profile=str(self.profile_combo.currentData() or ""),
            level=self.level_edit.text().strip(),
            width=int(self.width_spin.value()),
            height=int(self.height_spin.value()),
            scale_mode=str(self.scale_combo.currentData() or "fit"),
            fps=float(self.fps_spin.value()),
            rate_mode=str(self.rate_mode_combo.currentData() or "crf"),
            crf=int(self.crf_spin.value()),
            bitrate_kbps=int(self.bitrate_spin.value()),
            maxrate_kbps=int(self.maxrate_spin.value()),
            bufsize_kbps=int(self.bufsize_spin.value()),
            preset=str(self.enc_preset_combo.currentData() or "medium"),
            gop=int(self.gop_spin.value()),
            pix_fmt="yuv420p",
            acodec=str(self.acodec_combo.currentData() or "aac"),
            abitrate_kbps=int(self.abitrate_spin.value()),
            samplerate=int(self.samplerate_combo.currentData() or 48000),
            channels=int(self.channels_spin.value()),
            faststart=bool(self.faststart_check.isChecked()),
            two_pass=bool(self.two_pass_check.isChecked()),
            target_size_mb=float(self.target_size_spin.value()),
            **self.effects.values(),
        )

    def target_size_mb(self) -> float:
        return float(self.target_size_spin.value())

    def resolved_vendor(self) -> str:
        choice = str(self.encoder_combo.currentData() or "auto")
        if choice == "auto":
            vendor = self.caps.preferred_vendor() if self.caps else "cpu"
        elif self.caps and not self.caps.is_available(choice):
            vendor = "cpu"
        else:
            vendor = choice
        # H.264 能用不代表这块 GPU 能编 AV1（老卡 / 驱动差异）：
        # 探测结果确认没有 AV1 硬件编码时回退 CPU，避免任务直接失败。
        if (vendor != "cpu" and str(self.codec_combo.currentData() or "") == "av1"
                and self.caps is not None
                and not self.caps.av1_vendors.get(vendor, False)):
            vendor = "cpu"
        return vendor

    def set_context(self, src: str, media: MediaInfo | None, caps: EncoderCapabilities | None = None) -> None:
        self.src = src
        self.media = media
        self.effects.set_media(media)
        if caps is not None:
            self.caps = caps
        if self._current_preset and self._current_preset.strategy:
            self._apply_preset(self._current_preset)
        else:
            self.refresh_preview()

    def set_caps(self, caps: EncoderCapabilities) -> None:
        self.caps = caps
        index = self.encoder_combo.findData("auto")
        if index >= 0:
            self.encoder_combo.setItemText(index, f"{tr('encoder.auto')} · {caps.preferred_vendor().upper()}")
        for vendor in ("nvidia", "intel", "amd"):
            idx = self.encoder_combo.findData(vendor)
            if idx >= 0:
                base = tr(f"encoder.{vendor}")
                self.encoder_combo.setItemText(idx, base if caps.is_available(vendor) else f"{base} (—)")
        self.refresh_preview()

    def build_plan(
        self,
        start_s: float | None = None,
        end_s: float | None = None,
        src: str | None = None,
        media: MediaInfo | None = None,
    ) -> Plan:
        source = src or self.src
        if not source:
            raise ValueError(tr("msg.no_file"))
        info = media or (self.media if source == self.src else None)
        if info is None:
            from ffmpeg_core.ffprobe_engine import probe
            info = probe(source)
        params = self.current_params()
        dst = default_output_path(source, params.container)
        return plan_conversion(
            source, dst, params, media=info, vendor=self.resolved_vendor(),
            target_size_mb=self.target_size_mb(), start_s=start_s, end_s=end_s,
        )

    def refresh_preview(self) -> None:
        if not self.src or self.media is None:
            self.preview.setPlainText(tr("info.no_media"))
            self.estimate_label.setText(f"{tr('output.estimate')}: -")
            return
        try:
            plan = self.build_plan()
            self.preview.setPlainText(plan.preview)
            self.estimate_label.setText(f"{tr('output.estimate')}: {plan.estimated_mb:.1f} MB")
        except Exception as exc:
            _log.warning("命令预览生成失败: %s", exc)
            self.preview.setPlainText(f"[{exc.__class__.__name__}] {exc}")

    def _copy_command(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.preview.toPlainText())

    # ---------------- 国际化 ----------------
    def _fill_i18n_combos(self) -> None:
        self._rebuild(self.scale_combo, [(s, tr(f"scale.{s}")) for s in _SCALES])
        self._rebuild(self.rate_mode_combo, [(m, tr(f"output.mode.{m}")) for m in _RATE_MODES])
        self._rebuild(self.encoder_combo, [(e, tr(f"encoder.{e}")) for e in _ENCODERS])

    @staticmethod
    def _rebuild(combo: QComboBox, items) -> None:
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for value, text in items:
            combo.addItem(text, value)
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def retranslate(self) -> None:
        for key, box in self._groups.items():
            box.setTitle(tr(key))
        for key, label in self._form_labels.items():
            label.setText(tr(key))
        self.reset_btn.setText(tr("output.reset"))
        self.copy_btn.setText(tr("output.copy_cmd"))
        self.queue_btn.setText(tr("output.add_queue"))
        self.convert_btn.setText(tr("output.convert_now"))
        self.extreme_btn.setText(tr("output.extreme"))
        self.extreme_btn.setToolTip(tr("output.extreme_hint"))
        self.two_pass_check.setText(tr("output.two_pass"))
        self.faststart_check.setText(tr("output.faststart"))
        self.disclaimer_label.setText(self.library.disclaimer_text(i18n().lang))
        self.effects.retranslate()
        if self._current_preset:
            self.note_label.setText(self._current_preset.description(i18n().lang))
        self._fill_i18n_combos()
        saved_category = self.category_combo.currentData()
        saved_preset = self.preset_combo.currentData()
        self._loading = True
        self.category_combo.clear()
        for value, key in (("all", "cat.all"), ("platform", "cat.platform"),
                           ("device", "cat.device"), ("quality", "cat.quality")):
            self.category_combo.addItem(tr(key), value)
        idx = self.category_combo.findData(saved_category)
        if idx >= 0:
            self.category_combo.setCurrentIndex(idx)
        self._loading = False
        self._on_category_changed()
        idx = self.preset_combo.findData(saved_preset)
        if idx >= 0:
            self._loading = True
            self.preset_combo.setCurrentIndex(idx)
            self._loading = False
        self.refresh_preview()
