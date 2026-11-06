"""播放用外挂字幕：SRT / WebVTT / ASS·SSA 的轻量解析与按时间取词。

只做「把当前时间点的文字显示到画面上」这一件事；输出侧的烧录 / 封装
仍然全部交给 FFmpeg（见 ffmpeg_core/command_builder.py）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from config.logging_setup import get_logger

_log = get_logger("subtitle")

SUPPORTED = (".srt", ".vtt", ".ass", ".ssa")

_SRT_TIME = re.compile(
    r"(\d{1,3}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(\d{1,3}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})"
)
_HTML_TAG = re.compile(r"<[^>]+>")
_ASS_TAG = re.compile(r"\{[^}]*\}")


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _to_seconds(value: str) -> float:
    text = value.strip().replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def _clean(text: str) -> str:
    text = _ASS_TAG.sub("", text)
    text = _HTML_TAG.sub("", text)
    text = text.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        match = _SRT_TIME.search(block)
        if not match:
            continue
        start, end = _to_seconds(match.group(1)), _to_seconds(match.group(2))
        body = block[match.end():]
        cleaned = _clean(body)
        if cleaned and end > start:
            cues.append(Cue(start, end, cleaned))
    return cues


def _parse_ass(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for line in text.replace("\r\n", "\n").splitlines():
        if not line.lower().startswith("dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) < 10:
            continue
        start, end = _to_seconds(fields[1]), _to_seconds(fields[2])
        cleaned = _clean(fields[9])
        if cleaned and end > start:
            cues.append(Cue(start, end, cleaned))
    return cues


class SubtitleTrack:
    def __init__(self):
        self.path = ""
        self.cues: list[Cue] = []
        self._last_index = -1
        self._last_text = ""

    @property
    def count(self) -> int:
        return len(self.cues)

    @property
    def loaded(self) -> bool:
        return bool(self.cues)

    def clear(self) -> None:
        self.path = ""
        self.cues = []
        self._last_index = -1
        self._last_text = ""

    def load(self, path: str | Path) -> int:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"字幕文件不存在: {source}")
        if source.suffix.lower() not in SUPPORTED:
            raise ValueError(f"不支持的字幕格式: {source.suffix}")
        text = _read_text(source)
        cues = _parse_ass(text) if source.suffix.lower() in (".ass", ".ssa") else _parse_srt(text)
        cues.sort(key=lambda c: c.start)
        self.path, self.cues = str(source), cues
        self._last_index, self._last_text = -1, ""
        _log.info("外挂字幕已加载: %s (%d 条)", source.name, len(cues))
        return len(cues)

    def cue_at(self, seconds: float) -> str:
        """返回当前时间应显示的文字；用游标缓存避免每帧线性扫描。"""
        if not self.cues:
            return ""
        t = max(0.0, float(seconds))
        index = self._last_index
        if 0 <= index < len(self.cues) and self.cues[index].start <= t < self.cues[index].end:
            return self._last_text
        # 大部分情况是顺序播放，先向后找几格
        for step in range(index + 1, min(len(self.cues), index + 6)):
            cue = self.cues[step]
            if cue.start <= t < cue.end:
                self._last_index, self._last_text = step, cue.text
                return cue.text
        for step, cue in enumerate(self.cues):
            if cue.start <= t < cue.end:
                self._last_index, self._last_text = step, cue.text
                return cue.text
            if cue.start > t:
                break
        self._last_index, self._last_text = -1, ""
        return ""


def find_sidecar(video_path: str | Path) -> str:
    """自动找同名外挂字幕（也支持 video.zh.srt / video.chi.ass 这类命名）。"""
    source = Path(video_path)
    for suffix in SUPPORTED:
        candidate = source.with_suffix(suffix)
        if candidate.is_file():
            return str(candidate)
    for candidate in sorted(source.parent.glob(f"{glob_escape(source.stem)}.*")):
        if candidate.suffix.lower() in SUPPORTED:
            return str(candidate)
    return ""


def glob_escape(text: str) -> str:
    """glob 模式里 [ ] 会被当作字符集，需要转义。"""
    out = ""
    for char in text:
        out += f"[{char}]" if char in "[]" else char
    return out
