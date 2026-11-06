"""路径解析与外部二进制定位。

源码运行时以工程根目录为基准；PyInstaller 打包后区分两类路径：
- resource_path(): 只读资源（language/、内置 presets.json、随包 ffmpeg/）
- data_path():     可写数据（config/、logs/、Screenshots/、输出目录）
这样无论 onedir 还是 onefile，用户数据和日志都不会被写进临时解压目录。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_EXE_SUFFIX = ".exe" if os.name == "nt" else ""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """可写数据根目录：源码运行时是工程根目录，打包后是 exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """只读资源根目录：onefile 时指向 _MEIPASS。"""
    if is_frozen() and getattr(sys, "_MEIPASS", None):
        base = Path(sys._MEIPASS)
    else:
        base = app_root()
    return base.joinpath(*parts)


def data_path(*parts: str) -> Path:
    return app_root().joinpath(*parts)


def ensure_dir(*parts: str) -> Path:
    path = data_path(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_binary(name: str, configured: str | None = None) -> str:
    """按 用户配置 > 随包 ffmpeg/ > PATH 的顺序查找 ffmpeg / ffprobe。

    找不到时原样返回 name，让 subprocess 抛出可诊断的 FileNotFoundError，
    再由错误层翻译成友好提示。
    """
    filename = name if name.lower().endswith((".exe", ".bat", ".cmd")) else name + _EXE_SUFFIX
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(resource_path("ffmpeg", filename))
    candidates.append(data_path("ffmpeg", filename))
    which = shutil.which(name)
    if which:
        candidates.append(Path(which))

    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            continue
    return name
