"""字幕 / 水印 / HDR 的任务级选项与 FFmpeg 滤镜转义工具。

这些选项不是「编码参数」，但和 EncodeParams 一起写进预设 JSON，方便复用、
导入导出。所有处理仍然只生成 FFmpeg argv，不在 Python 里改像素。

Windows 路径在 FFmpeg 滤镜里要过两层解析（滤镜链 + 滤镜参数），冒号、
反斜杠、单引号、方括号、逗号、分号都必须转义，否则含空格或中文盘符的
路径会直接报 "No such filter" 或 "Unable to parse option value"。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

SUBTITLE_MODES = ("none", "burn", "embed")
WATERMARK_MODES = ("none", "image", "text")
WATERMARK_POSITIONS = ("tl", "tc", "tr", "ml", "mc", "mr", "bl", "bc", "br")
HDR_MODES = ("auto", "keep", "tonemap", "sdr")
TONEMAP_ALGORITHMS = ("hable", "mobius", "reinhard", "linear", "gamma", "clip")

_HDR_TRANSFERS = ("smpte2084", "smpte2086", "arib-std-b67", "hlg")
_HDR_PRIMARIES = ("bt2020", "bt2020m")


@dataclass
class SubtitleOptions:
    """mode: none=不处理 / burn=烧录进画面 / embed=作为软字幕轨道封装。"""

    mode: str = "none"
    path: str = ""
    language: str = "chi"
    font_name: str = "Microsoft YaHei"
    font_size: int = 24
    outline: int = 2
    force_style: bool = True
    encoding: str = "utf-8"

    def active(self) -> bool:
        return self.mode in ("burn", "embed") and bool(self.path)

    @classmethod
    def from_dict(cls, data: Any) -> "SubtitleOptions":
        return _from_dict(cls, data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WatermarkOptions:
    """mode: none / image=图片叠加 / text=drawtext 文字。"""

    mode: str = "none"
    image_path: str = ""
    text: str = ""
    font_path: str = ""
    font_size: int = 28
    color: str = "white"
    opacity: float = 0.75
    position: str = "br"
    margin: int = 20
    scale_percent: int = 15
    box: bool = True

    def active(self) -> bool:
        if self.mode == "image":
            return bool(self.image_path)
        if self.mode == "text":
            return bool(self.text.strip())
        return False

    @classmethod
    def from_dict(cls, data: Any) -> "WatermarkOptions":
        return _from_dict(cls, data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HdrOptions:
    """mode: auto=跟随源 / keep=保留 HDR10 / tonemap=转 SDR / sdr=按普通 8bit 输出。"""

    mode: str = "auto"
    tonemap: str = "hable"
    npl: int = 100
    desat: float = 0.0
    bit_depth: int = 10
    max_cll: str = ""
    master_display: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "HdrOptions":
        return _from_dict(cls, data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _from_dict(cls, data: Any):
    """宽松合并：预设里只写覆盖字段，其余保持默认；非法值自动回退。"""
    if not isinstance(data, dict):
        return cls()
    allowed = {f.name for f in fields(cls)}
    values: dict[str, Any] = {}
    for name in allowed:
        if name not in data or data[name] is None:
            continue
        default = getattr(cls, name)
        raw = data[name]
        try:
            if isinstance(default, bool):
                value = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, float):
                value = float(raw)
            elif isinstance(default, int):
                value = int(float(raw))
            else:
                value = str(raw)
        except (TypeError, ValueError):
            continue
        values[name] = value
    return cls(**values)


def is_hdr_source(color_transfer: str, color_primaries: str, color_space: str, pix_fmt: str) -> bool:
    """按 ffprobe 的色彩标记判断 HDR。

    部分容器（尤其 MP4）在 FFmpeg 9 里不暴露 color_transfer / color_primaries，
    只留下 bt2020nc + 10bit，这种情况也按 HDR 处理。
    """
    transfer = (color_transfer or "").strip().lower()
    primaries = (color_primaries or "").strip().lower()
    if any(t in transfer for t in _HDR_TRANSFERS):
        return True
    if any(p in primaries for p in _HDR_PRIMARIES):
        return True
    wide = "bt2020" in (color_space or "").lower()
    high_bit = any(tag in (pix_fmt or "").lower() for tag in ("10le", "10be", "12le", "p010", "p016"))
    return wide and high_bit


def hdr_label(color_transfer: str, color_primaries: str, color_space: str, pix_fmt: str) -> str:
    if not is_hdr_source(color_transfer, color_primaries, color_space, pix_fmt):
        return "SDR"
    transfer = (color_transfer or "").lower()
    if "arib-std-b67" in transfer or "hlg" in transfer:
        return "HLG"
    if "smpte2084" in transfer or "2086" in transfer:
        return "HDR10 (PQ)"
    return "HDR (BT.2020)"


# ---------------- 转义 ----------------
def escape_filter_path(path: str | Path) -> str:
    """把本地路径转成可安全嵌入 FFmpeg 滤镜参数（引号内）的字符串。"""
    text = Path(path).as_posix().replace("\\", "/")
    # 顺序有讲究：先处理反斜杠，再逐个转义滤镜分隔符
    for char in (":", "'", "[", "]", ",", ";", "="):
        text = text.replace(char, "\\" + char)
    return text


def escape_drawtext(text: str) -> str:
    """drawtext 的 text 参数：只转义滤镜层分隔符。

    配合 expansion=none 使用：FFmpeg 9 里 `\\%` 和 `%%` 都会报 "Stray %"，
    而禁用展开后 % / { } 都是普通字符，不能再给它们加反斜杠。
    """
    out = str(text).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    out = out.replace("\\", r"\\").replace(":", r"\:")
    # 单引号在 filtergraph 里要用两个单引号表示；反斜杠转义在
    # -filter_complex 下会把滤镜链截断，导致 "[vout] does not exist"。
    return out.replace("'", "''")


def overlay_position(position: str, margin: int, *, for_drawtext: bool = False) -> tuple[str, str]:
    """九宫格位置 → overlay / drawtext 的 x, y 表达式。"""
    m = max(0, int(margin))
    main_w, main_h = ("w", "h") if for_drawtext else ("W", "H")
    overlay_w, overlay_h = ("tw", "th") if for_drawtext else ("w", "h")
    vertical = {"t": str(m), "m": f"({main_h}-{overlay_h})/2", "b": f"{main_h}-{overlay_h}-{m}"}
    horizontal = {"l": str(m), "c": f"({main_w}-{overlay_w})/2", "r": f"{main_w}-{overlay_w}-{m}"}
    key = position if position in WATERMARK_POSITIONS else "br"
    return horizontal[key[1]], vertical[key[0]]


def default_font() -> str:
    """优先找支持中文的 Windows 字体；找不到就交给 FFmpeg 内置默认字体。"""
    for candidate in (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ):
        if candidate.is_file():
            return str(candidate)
    return ""


def auto_font_name() -> str:
    return "Microsoft YaHei" if Path("C:/Windows/Fonts/msyh.ttc").is_file() else "Arial"
