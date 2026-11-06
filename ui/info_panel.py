"""视频信息面板：数值全部来自 ffprobe。"""
from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QLabel, QVBoxLayout, QWidget

from config.i18n import tr
from ffmpeg_core.ffprobe_engine import MediaInfo

_FIELDS = (
    ("info.filename", "filename"),
    ("info.duration", "duration"),
    ("info.fps", "fps"),
    ("info.resolution", "resolution"),
    ("info.vcodec", "vcodec"),
    ("info.acodec", "acodec"),
    ("info.bitrate", "bitrate"),
    ("info.vbitrate", "vbitrate"),
    ("info.abitrate", "abitrate"),
    ("info.size", "size"),
    ("info.format", "container"),
    ("info.frames", "frames"),
    ("info.color", "color"),
    ("info.hdr", "hdr"),
)


class InfoPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._media: MediaInfo | None = None
        self._keys: dict[str, QLabel] = {}
        self._labels: dict[str, QLabel] = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for key, field in _FIELDS:
            label = QLabel(tr(key))
            value = QLabel("-")
            value.setTextInteractionFlags(value.textInteractionFlags() | value.textInteractionFlags().TextSelectableByMouse)
            value.setWordWrap(True)
            form.addRow(label, value)
            self._labels[key] = label
            self._keys[field] = value
        layout.addLayout(form)
        layout.addStretch(1)
        self.set_media(None)

    def set_media(self, info: MediaInfo | None) -> None:
        self._media = info
        if info is None:
            for value in self._keys.values():
                value.setText("-")
            self._keys["filename"].setText(tr("info.no_media"))
            return
        values = {
            "filename": info.filename,
            "duration": info.duration_text(),
            "fps": info.fps_text(),
            "resolution": info.resolution_text(),
            "vcodec": f"{info.vcodec or '-'}  {info.vcodec_long or ''}".strip(),
            "acodec": info.acodec or "-",
            "bitrate": info.bitrate_text(),
            "vbitrate": info.vbitrate_text(),
            "abitrate": info.abitrate_text(),
            "size": info.size_text(),
            "container": info.container or "-",
            "frames": f"{info.nb_frames:,}" if info.nb_frames else "-",
            "color": info.color_text(),
            "hdr": "HDR10 / HLG" if info.is_hdr else "SDR",
        }
        for field, value in values.items():
            self._keys[field].setText(str(value))

    def retranslate(self) -> None:
        for key, label in self._labels.items():
            label.setText(tr(key))
        if self._media is None:
            self._keys["filename"].setText(tr("info.no_media"))
