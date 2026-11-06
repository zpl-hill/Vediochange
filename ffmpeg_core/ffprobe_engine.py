"""媒体信息读取：全部通过 ffprobe.exe，不把媒体文件读进内存。

200GB / 24 小时级文件只读取容器元数据，Python 侧只保存路径与少量数值。
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from config.errors import MediaProbeError
from config.logging_setup import get_logger
from config.paths import find_binary
from config.settings import settings

_log = get_logger("ffprobe")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class MediaInfo:
    path: str = ""
    filename: str = ""
    container: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    vcodec_long: str = ""
    acodec: str = ""
    bitrate_kbps: int = 0
    # 分轨码率：ffprobe 的 stream.bit_rate，缺失时按容器/标签回算
    vbitrate_kbps: int = 0
    abitrate_kbps: int = 0
    vbitrate_estimated: bool = False
    abitrate_estimated: bool = False
    achannels: int = 0
    size_bytes: int = 0
    nb_frames: int = 0
    pix_fmt: str = ""
    has_video: bool = False
    has_audio: bool = False
    # HDR / 色彩信息（全部来自 ffprobe）
    color_space: str = ""
    color_transfer: str = ""
    color_primaries: str = ""
    color_range: str = ""
    bits_per_raw_sample: str = ""
    master_display: str = ""
    max_cll: str = ""
    is_hdr: bool = False
    hdr_type: str = "SDR"
    raw_streams: list[dict] = field(default_factory=list, repr=False)

    def color_info(self) -> dict:
        """交给 command_builder 的源色彩描述。"""
        return {
            "color_space": self.color_space,
            "color_transfer": self.color_transfer,
            "color_primaries": self.color_primaries,
            "pix_fmt": self.pix_fmt,
            "master_display": self.master_display,
            "max_cll": self.max_cll,
            "is_hdr": self.is_hdr,
        }

    def color_text(self) -> str:
        from .media_options import hdr_label
        parts = [hdr_label(self.color_transfer, self.color_primaries,
                           self.color_space, self.pix_fmt)]
        detail = " / ".join(x for x in (
            self.color_space, self.color_transfer or self.color_primaries,
            self.pix_fmt,
        ) if x)
        if detail:
            parts.append(detail)
        if self.max_cll:
            parts.append(f"MaxCLL {self.max_cll}")
        return " · ".join(parts)

    def duration_text(self) -> str:
        return format_time(self.duration)

    def size_text(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"

    def resolution_text(self) -> str:
        return f"{self.width} × {self.height}" if self.width and self.height else "-"

    def fps_text(self) -> str:
        return f"{self.fps:.3f} fps" if self.fps else "-"

    def bitrate_text(self) -> str:
        return rate_text(self.bitrate_kbps)

    def vbitrate_text(self) -> str:
        text = rate_text(self.vbitrate_kbps)
        return f"{text} (≈)" if text != "-" and self.vbitrate_estimated else text

    def abitrate_text(self) -> str:
        text = rate_text(self.abitrate_kbps)
        return f"{text} (≈)" if text != "-" and self.abitrate_estimated else text


def rate_text(kbps: int) -> str:
    """统一的码率文案：>=1000 kbps 用 Mbps，保留三位有效数字。"""
    kbps = int(kbps or 0)
    if kbps <= 0:
        return "-"
    if kbps >= 1000:
        return f"{kbps / 1000:.2f} Mbps"
    return f"{kbps} kbps"


def format_time(seconds: float, *, with_ms: bool = False) -> str:
    seconds = max(0.0, float(seconds or 0))
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    text = f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
    if with_ms:
        text += f".{int((seconds - int(seconds)) * 1000):03d}"
    return text


def parse_rate(value: str | float | int | None) -> float:
    if value in (None, "", "0/0", "N/A"):
        return 0.0
    text = str(value)
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_TAG_RATE_KEYS = ("BPS", "BPS-eng", "bps", "bps-eng")
_TAG_BYTES_KEYS = ("NUMBER_OF_BYTES", "NUMBER_OF_BYTES-eng")


def _stream_bitrate(stream: dict, duration: float) -> int:
    """单条流的码率（kbps）。

    MP4/MOV 一般直接给 stream.bit_rate；MKV/WebM 通常没有，
    退到 mkvmerge 风格标签（BPS / NUMBER_OF_BYTES）按流自身时长换算。
    """
    raw = stream.get("bit_rate")
    if raw not in (None, "", "N/A"):
        kbps = _to_int(raw) // 1000
        if kbps > 0:
            return kbps
    tags = stream.get("tags") or {}
    for key, tag in tags.items():
        if str(key).split("-")[0].upper() != "BPS" and key not in _TAG_RATE_KEYS:
            continue
        kbps = _to_int(tag) // 1000
        if kbps > 0:
            return kbps
    span = _to_float(stream.get("duration")) or duration
    for key, tag in tags.items():
        if key in _TAG_BYTES_KEYS and span > 0:
            kbps = int(_to_int(tag) * 8 / span / 1000)
            if kbps > 0:
                return kbps
    return 0


# ffmpeg 自封装的 MKV 既没有 stream.bit_rate 也没有 BPS 标签，
# 只能按「编码 + 声道数」给个常见档位，仅用于总码率里减掉音频部分。
_AUDIO_KBPS_HINTS = {
    "aac": (96, 128, 256),
    "mp3": (96, 128, 224),
    "opus": (64, 96, 160),
    "vorbis": (96, 128, 192),
    "ac3": (192, 384, 448),
    "eac3": (192, 384, 640),
    "dts": (768, 1536, 1536),
    "flac": (600, 800, 1200),
    "alac": (600, 800, 1200),
    "truehd": (1000, 2000, 4000),
    "pcm_s16le": (705, 1411, 4233),
    "pcm_s24le": (1058, 2117, 6350),
}


def _assumed_audio_kbps(codec: str, channels: int) -> int:
    hints = _AUDIO_KBPS_HINTS.get((codec or "").lower())
    if not hints:
        return 0
    index = 0 if channels <= 1 else (1 if channels <= 2 else 2)
    return hints[index]


def _probe_audio_kbps(path: Path, binary: str, index: int = 0) -> int:
    """没有分轨码率时，用前 64 个音频包实测一次（只读约 1.5 秒数据）。

    MKV / WebM 里 ffprobe 拿不到 stream.bit_rate，按编码猜会有 10% 以上偏差，
    实测更可靠；失败就返回 0 交给上层用经验值兜底。
    """
    argv = [binary, "-v", "error", "-select_streams", f"a:{index}",
            "-read_intervals", "%+#64",
            "-show_entries", "packet=size,duration_time",
            "-print_format", "json", str(path)]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_CREATE_NO_WINDOW,
        )
        packets = (json.loads(completed.stdout or "{}") or {}).get("packets") or []
    except Exception as exc:
        _log.debug("音频码率实测跳过: %s", exc)
        return 0
    total_bytes = sum(_to_int(p.get("size")) for p in packets)
    span = sum(_to_float(p.get("duration_time")) for p in packets)
    if total_bytes <= 0 or span <= 0:
        return 0
    return int(total_bytes * 8 / span / 1000)


def _numerator(value) -> str:
    """ffprobe 把 mastering display 写成分数；分子就是 x265 需要的整数。"""
    text = str(value if value is not None else "0")
    return text.split("/", 1)[0].strip() or "0"


def _probe_color_details(path: Path, binary: str) -> dict:
    """读第一帧的 HDR SEI 侧数据。

    FFmpeg 9 在部分容器（尤其 MP4）里只在 frame side_data 暴露
    mastering display / content light level，stream 层看不到，必须补探一次。
    -read_intervals %+#1 只解一帧，开销可以忽略。
    """
    argv = [binary, "-v", "error", "-print_format", "json",
            "-read_intervals", "%+#1", "-show_frames", str(path)]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=12, creationflags=_CREATE_NO_WINDOW,
        )
        data = json.loads(completed.stdout or "{}")
        frames = data.get("frames") or []
    except Exception as exc:
        _log.debug("HDR 侧数据探测跳过: %s", exc)
        return {}
    if not frames:
        return {}
    frame = frames[0]
    result = {
        "color_space": frame.get("color_space") or "",
        "color_transfer": frame.get("color_transfer") or "",
        "color_primaries": frame.get("color_primaries") or "",
    }
    for side in frame.get("side_data_list") or []:
        kind = str(side.get("side_data_type") or "")
        if "Mastering display metadata" in kind:
            try:
                result["master_display"] = (
                    f"G({_numerator(side.get('green_x'))},{_numerator(side.get('green_y'))})"
                    f"B({_numerator(side.get('blue_x'))},{_numerator(side.get('blue_y'))})"
                    f"R({_numerator(side.get('red_x'))},{_numerator(side.get('red_y'))})"
                    f"WP({_numerator(side.get('white_point_x'))},{_numerator(side.get('white_point_y'))})"
                    f"L({_numerator(side.get('max_luminance'))},{_numerator(side.get('min_luminance'))})"
                )
            except Exception as exc:
                _log.debug("解析 mastering display 失败: %s", exc)
        elif "Content light level" in kind:
            result["max_cll"] = f"{_to_int(side.get('max_content'))},{_to_int(side.get('max_average'))}"
    return result


def probe(path: str | Path, *, ffprobe_bin: str | None = None, timeout: int = 40) -> MediaInfo:
    source = Path(path)
    if not source.is_file():
        raise MediaProbeError(f"文件不存在: {source}")

    binary = ffprobe_bin or find_binary("ffprobe", settings().get("ffprobe_path"))
    argv = [binary, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(source)]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise MediaProbeError(f"ffprobe 不可用: {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError("ffprobe 读取超时，文件可能位于慢速网络盘") from exc

    if completed.returncode != 0:
        raise MediaProbeError(f"ffprobe 读取失败: {completed.stderr.strip()[:300]}")
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe 输出不是合法 JSON") from exc

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    info = MediaInfo(
        path=str(source), filename=source.name,
        container=fmt.get("format_name", ""),
        duration=_to_float(fmt.get("duration")),
        size_bytes=_to_int(fmt.get("size")) or source.stat().st_size,
        bitrate_kbps=_to_int(fmt.get("bit_rate")) // 1000,
        raw_streams=streams,
    )

    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            info.vcodec = stream.get("codec_name", "")
            info.vcodec_long = stream.get("codec_long_name", "")
            info.width = _to_int(stream.get("width"))
            info.height = _to_int(stream.get("height"))
            info.pix_fmt = stream.get("pix_fmt", "")
            info.color_space = stream.get("color_space") or ""
            info.color_transfer = stream.get("color_transfer") or ""
            info.color_primaries = stream.get("color_primaries") or ""
            info.color_range = stream.get("color_range") or ""
            info.bits_per_raw_sample = str(stream.get("bits_per_raw_sample") or "")
            for side in stream.get("side_data_list") or []:
                if "Content light level" in str(side.get("side_data_type") or ""):
                    info.max_cll = f"{_to_int(side.get('max_content'))},{_to_int(side.get('max_average'))}"
            info.fps = parse_rate(stream.get("avg_frame_rate")) or parse_rate(stream.get("r_frame_rate"))
            info.nb_frames = _to_int(stream.get("nb_frames"))
            info.vbitrate_kbps = _stream_bitrate(stream, info.duration)
            if not info.duration:
                info.duration = _to_float(stream.get("duration"))
        elif kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.acodec = stream.get("codec_name", "")
            info.achannels = _to_int(stream.get("channels"))
            info.abitrate_kbps = _stream_bitrate(stream, info.duration)

    if not info.duration:
        info.duration = _to_float(fmt.get("duration"))
    if not info.nb_frames and info.fps and info.duration:
        info.nb_frames = int(info.fps * info.duration)
    if not info.bitrate_kbps and info.size_bytes and info.duration:
        info.bitrate_kbps = int(info.size_bytes * 8 / info.duration / 1000)
    if info.has_video and not info.vbitrate_kbps and info.bitrate_kbps:
        # MKV/WebM 常常不给分轨码率：实测音频比特率，再从总码率里扣掉
        if info.has_audio and not info.abitrate_kbps:
            measured = _probe_audio_kbps(source, binary)
            if measured > 0:
                info.abitrate_kbps = measured
                info.abitrate_estimated = True
        audio = 0
        if info.has_audio:
            audio = info.abitrate_kbps or _assumed_audio_kbps(info.acodec, info.achannels)
        approx = info.bitrate_kbps - audio
        if approx > 0:
            info.vbitrate_kbps = approx
            info.vbitrate_estimated = True

    from .media_options import hdr_label, is_hdr_source
    info.is_hdr = is_hdr_source(
        info.color_transfer, info.color_primaries, info.color_space, info.pix_fmt
    )
    if info.is_hdr or "bt2020" in info.color_space.lower():
        details = _probe_color_details(source, binary)
        info.color_space = info.color_space or details.get("color_space", "")
        info.color_transfer = info.color_transfer or details.get("color_transfer", "")
        info.color_primaries = info.color_primaries or details.get("color_primaries", "")
        info.master_display = details.get("master_display", "")
        info.max_cll = info.max_cll or details.get("max_cll", "")
        info.is_hdr = is_hdr_source(
            info.color_transfer, info.color_primaries, info.color_space, info.pix_fmt
        )
    info.hdr_type = hdr_label(info.color_transfer, info.color_primaries,
                              info.color_space, info.pix_fmt)

    _log.info("探测完成: %s | %s | %s | %s | %s", info.filename, info.duration_text(),
              info.resolution_text(), info.fps_text(), info.hdr_type)
    return info
