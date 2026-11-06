"""当前帧截图：PNG / JPG / BMP，默认写入 Screenshots/。"""
from __future__ import annotations

import time
from pathlib import Path

from config.paths import ensure_dir
from config.settings import settings
from ffmpeg_core.command_builder import build_snapshot_command
from ffmpeg_core.ffmpeg_engine import ffmpeg_binary

from .converter import Plan

SNAPSHOT_FORMATS = ("png", "jpg", "bmp")


def plan_snapshot(src: str, time_s: float, *, fmt: str | None = None) -> Plan:
    fmt = (fmt or settings().get("screenshot_format", "png")).lower().lstrip(".")
    if fmt not in SNAPSHOT_FORMATS:
        fmt = "png"
    folder = ensure_dir(settings().get("screenshot_dir", "Screenshots"))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = f"{Path(src).stem}_{stamp}.{fmt}"
    output = folder / name
    index = 1
    while output.exists():
        output = folder / f"{Path(src).stem}_{stamp}_{index}.{fmt}"
        index += 1
    command = build_snapshot_command(
        src, str(output), max(0.0, time_s), ffmpeg_bin=ffmpeg_binary()
    )
    return Plan(commands=[command], output=str(output))
