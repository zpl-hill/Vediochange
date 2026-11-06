"""格式转换：把「预设 + 手动覆盖 + 源媒体信息」变成最终命令计划。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings
from ffmpeg_core.command_builder import (
    build_two_pass_commands,
    build_video_command,
    estimate_output_size_mb,
    format_command,
    resolve_vcodec,
)
from ffmpeg_core.ffmpeg_engine import ffmpeg_binary
from ffmpeg_core.ffprobe_engine import MediaInfo, probe
from ffmpeg_core.presets import EncodeParams


@dataclass
class Plan:
    commands: list[list[str]]
    output: str
    duration_s: float = 0.0
    estimated_mb: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def preview(self) -> str:
        return "\n\n".join(format_command(cmd) for cmd in self.commands)

    @property
    def first_command(self) -> str:
        return format_command(self.commands[0]) if self.commands else ""


def default_output_path(src: str, container: str = "mp4", *, suffix: str = "_vtk") -> str:
    source = Path(src)
    configured = settings().get("output_dir", "")
    folder = Path(configured) if configured else source.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = source.parent
    candidate = folder / f"{source.stem}{suffix}.{container}"
    index = 1
    while candidate.exists():
        candidate = folder / f"{source.stem}{suffix}_{index}.{container}"
        index += 1
    return str(candidate)


def plan_conversion(
    src: str,
    dst: str,
    params: EncodeParams,
    *,
    media: MediaInfo | None = None,
    vendor: str = "cpu",
    target_size_mb: float = 0.0,
    start_s: float | None = None,
    end_s: float | None = None,
    force_single_pass: bool = False,
) -> Plan:
    info = media or probe(src)
    clip_duration = None
    duration = info.duration
    if start_s is not None and end_s is not None and end_s > start_s:
        clip_duration = end_s - start_s
        duration = clip_duration

    _, is_hardware = resolve_vcodec(params.codec_family, vendor)
    kwargs = dict(
        ffmpeg_bin=ffmpeg_binary(),
        vendor=vendor,
        src_width=info.width,
        src_height=info.height,
        src_fps=info.fps,
        duration_s=duration,
        target_size_mb=target_size_mb,
        seek_s=start_s,
        clip_duration=clip_duration,
        color_info=info.color_info(),
    )
    two_pass = (params.two_pass or params.rate_mode == "target_size") and not force_single_pass
    if two_pass:
        first, second = build_two_pass_commands(src, dst, params, **kwargs)
        commands = [first, second]
    else:
        commands = [build_video_command(src, dst, params, **kwargs)]

    estimated = estimate_output_size_mb(
        params, duration_s=duration, is_hardware=is_hardware,
        target_size_mb=target_size_mb, src_width=info.width,
        src_height=info.height, src_fps=info.fps,
    )
    return Plan(commands=commands, output=dst, duration_s=duration, estimated_mb=estimated)
