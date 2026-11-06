"""音频提取：MP3 / AAC / M4A / WAV / FLAC / OGG，通过 ffmpeg -vn。"""
from __future__ import annotations

from ffmpeg_core.command_builder import build_audio_command
from ffmpeg_core.ffmpeg_engine import ffmpeg_binary
from ffmpeg_core.ffprobe_engine import MediaInfo

from .converter import Plan, default_output_path

AUDIO_FORMATS = ("mp3", "aac", "m4a", "wav", "flac", "ogg")


def plan_audio(
    src: str,
    dst: str | None,
    *,
    fmt: str = "mp3",
    bitrate_kbps: int = 192,
    samplerate: int = 48000,
    channels: int = 2,
    media: MediaInfo | None = None,
) -> Plan:
    fmt = fmt.lower().lstrip(".")
    if fmt not in AUDIO_FORMATS:
        raise ValueError(f"不支持的音频格式: {fmt}")
    output = dst or default_output_path(src, fmt, suffix="_audio")
    command = build_audio_command(
        src, output, ffmpeg_bin=ffmpeg_binary(), fmt=fmt,
        bitrate_kbps=bitrate_kbps, samplerate=samplerate, channels=channels,
    )
    duration = media.duration if media else 0.0
    estimated = bitrate_kbps * duration / 8192.0 if duration else 0.0
    return Plan(commands=[command], output=output,
                duration_s=duration, estimated_mb=estimated)
