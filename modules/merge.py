"""视频合并：concat demuxer + 拖拽排序后的文件列表。"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Sequence

from config.logging_setup import get_logger
from config.paths import ensure_dir
from ffmpeg_core.command_builder import build_merge_command, write_concat_list
from ffmpeg_core.ffmpeg_engine import ffmpeg_binary
from ffmpeg_core.ffprobe_engine import probe
from ffmpeg_core.presets import EncodeParams

from .converter import Plan

_log = get_logger("merge")


def plan_merge(
    files: Sequence[str],
    dst: str,
    *,
    reencode: bool = False,
    params: EncodeParams | None = None,
    vendor: str = "cpu",
) -> Plan:
    if len(files) < 2:
        raise ValueError("合并至少需要两个文件")
    list_path = ensure_dir("logs") / f"concat_{uuid.uuid4().hex[:8]}.txt"
    write_concat_list(files, list_path)

    first_info = None
    total_duration = 0.0
    for item in files:
        try:
            info = probe(item)
            total_duration += info.duration
            if first_info is None:
                first_info = info
        except Exception as exc:
            _log.warning("合并前探测失败 %s: %s", item, exc)

    command = build_merge_command(
        str(list_path), dst, ffmpeg_bin=ffmpeg_binary(),
        reencode=reencode, params=params, vendor=vendor,
        src_width=first_info.width if first_info else 0,
        src_height=first_info.height if first_info else 0,
        src_fps=first_info.fps if first_info else 0.0,
        duration_s=total_duration,
    )
    return Plan(commands=[command], output=dst, duration_s=total_duration,
                extra={"concat_list": str(list_path)})
