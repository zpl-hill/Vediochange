"""编码器可用性注册表：`ffmpeg -encoders` 的结果在进程内缓存一次。

FFmpeg 的编译选项差异很大 —— 本机 9.0.2 essentials 有 libaom-av1 却
没有 libsvtav1，所以 CPU AV1 不能写死编码器名，必须按实际 `-encoders`
列表挑选。detect_capabilities() 会把探测结果注入这里，避免重复起子进程。
"""
from __future__ import annotations

import os
import subprocess
from typing import Iterable

from config.logging_setup import get_logger
from config.paths import find_binary
from config.settings import settings

_log = get_logger("encoders")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_CPU_CANDIDATES: dict[str, tuple[str, ...]] = {
    "h264": ("libx264", "h264_mf"),
    "hevc": ("libx265", "hevc_mf"),
    "av1": ("libsvtav1", "libaom-av1", "librav1e", "av1_mf"),
}
_FALLBACK: dict[str, str] = {"h264": "libx264", "hevc": "libx265", "av1": "libaom-av1"}

_encoders: set[str] | None = None


def set_available_encoders(names: Iterable[str]) -> None:
    global _encoders
    _encoders = set(names)


def available_encoders(*, refresh: bool = False) -> set[str]:
    global _encoders
    if _encoders is not None and not refresh:
        return _encoders
    try:
        binary = find_binary("ffmpeg", settings().get("ffmpeg_path"))
        completed = subprocess.run(
            [binary, "-hide_banner", "-encoders"], capture_output=True,
            timeout=15, creationflags=_CREATE_NO_WINDOW,
        )
        text = completed.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("读取编码器列表失败，使用保守默认值: %s", exc)
        _encoders = set(_FALLBACK.values())
        return _encoders
    names: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and "." in parts[0]:
            names.add(parts[1])
    _encoders = names
    return names


def pick_cpu_encoder(family: str) -> str:
    """按编译进 FFmpeg 的库挑选 CPU 编码器，未知族回退 H.264。"""
    candidates = _CPU_CANDIDATES.get(family, _CPU_CANDIDATES["h264"])
    names = available_encoders()
    for name in candidates:
        if name in names:
            return name
    return _FALLBACK.get(family, "libx264")


def has_encoder(name: str) -> bool:
    return name in available_encoders()


def av1_cpu_name() -> str:
    return pick_cpu_encoder("av1")


def av1_summary() -> str:
    names = available_encoders()
    found = [n for n in ("libsvtav1", "libaom-av1", "librav1e",
                         "av1_nvenc", "av1_qsv", "av1_amf", "av1_mf") if n in names]
    return ", ".join(found) if found else "无"
