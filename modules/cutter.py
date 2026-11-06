"""视频剪切：默认无损 -c copy，精确模式走重编码（帧级）。"""
from __future__ import annotations

from ffmpeg_core.command_builder import build_cut_command, resolve_vcodec
from ffmpeg_core.ffmpeg_engine import ffmpeg_binary
from ffmpeg_core.ffprobe_engine import MediaInfo
from ffmpeg_core.presets import EncodeParams, estimate_size_mb

from .converter import Plan, default_output_path


def plan_cut(
    src: str,
    dst: str | None,
    start_s: float,
    end_s: float,
    *,
    lossless: bool = True,
    params: EncodeParams | None = None,
    media: MediaInfo | None = None,
    vendor: str = "cpu",
) -> Plan:
    if end_s <= start_s:
        raise ValueError("结束时间必须大于开始时间")
    from ffmpeg_core.ffprobe_engine import probe

    info = media or probe(src)
    output = dst or default_output_path(
        src, params.container if params else "mp4", suffix="_cut"
    )
    command = build_cut_command(
        src, output, start_s, end_s,
        lossless=lossless,
        ffmpeg_bin=ffmpeg_binary(),
        params=None if lossless else (params or EncodeParams(rate_mode="crf", crf=23)),
        vendor=vendor,
        src_width=info.width, src_height=info.height, src_fps=info.fps,
        duration_s=end_s - start_s,
        color_info=info.color_info(),
    )
    estimated = 0.0
    if not lossless and params:
        _, is_hardware = resolve_vcodec(params.codec_family, vendor)
        from ffmpeg_core.presets import crf_to_bitrate_kbps

        video = (params.bitrate_kbps if is_hardware or params.rate_mode != "crf"
                 else crf_to_bitrate_kbps(params.crf, info.width, info.height, info.fps))
        estimated = estimate_size_mb(video, params.abitrate_kbps, end_s - start_s)
    return Plan(commands=[command], output=output,
                duration_s=end_s - start_s, estimated_mb=estimated)
