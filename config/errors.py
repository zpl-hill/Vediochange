"""统一异常：友好提示框 + 写入 logs/error.log。"""
from __future__ import annotations

from .logging_setup import get_logger

_log = get_logger("error")


class VTKError(Exception):
    """Video Toolkit 基础异常。"""


class BinaryNotFoundError(VTKError):
    """ffmpeg / ffprobe 不可用。"""


class MediaProbeError(VTKError):
    """ffprobe 读取媒体信息失败。"""


class PresetError(VTKError):
    """预设数据非法。"""


class FFmpegError(VTKError):
    """ffmpeg 进程返回非零退出码。"""

    def __init__(self, message: str, returncode: int = -1, command: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.command = command
        self.stderr = stderr


_FRIENDLY_RULES: tuple[tuple[str, str, str], ...] = (
    ("No such file or directory", "找不到输入文件，请确认文件仍然存在且路径不含特殊字符。",
     "Input file not found. Check that the file still exists."),
    ("Invalid data found", "文件可能已损坏或不是受支持的视频格式。",
     "The file seems corrupted or is not a supported media format."),
    ("Unknown encoder", "当前 FFmpeg 不支持所选编码器，请改用 CPU 或切换硬件编码。",
     "Selected encoder is unavailable in this FFmpeg build. Try CPU or another device."),
    ("Unrecognized option", "FFmpeg 版本与参数不匹配，请更新 FFmpeg 或关闭高级选项。",
     "An FFmpeg option was not recognized. Update FFmpeg or disable advanced options."),
    ("Permission denied", "没有写入权限，请换一个输出目录。", "Permission denied. Choose another output folder."),
    ("No space left", "磁盘空间不足，请清理后重试。", "Not enough disk space."),
    ("Conversion failed", "转码失败，可在命令预览里复制命令手动排查。",
     "Encoding failed. Copy the command from the preview to investigate."),
    ("not found", "未找到 ffmpeg / ffprobe，请在设置中指定路径或把它放入 ffmpeg/ 目录。",
     "ffmpeg / ffprobe not found. Set the path in settings or put them in ffmpeg/."),
)


def friendly(exc: BaseException, lang: str = "zh_CN") -> str:
    """把底层异常翻译成用户能看懂的话。"""
    raw = str(exc) or exc.__class__.__name__
    index = 2 if lang == "en_US" else 1
    lowered = raw.lower()
    for needle, zh, en in _FRIENDLY_RULES:
        if needle.lower() in lowered:
            return en if index == 2 else zh
    if isinstance(exc, FileNotFoundError):
        return ("未找到 ffmpeg / ffprobe，请检查设置中的路径。" if lang != "en_US"
                else "ffmpeg / ffprobe not found. Check the configured path.")
    return raw


def report(parent, exc: BaseException, context: str = "", lang: str = "zh_CN") -> str:
    """记录 ERROR 日志并弹出友好提示框；无 GUI 环境下只记日志。"""
    message = friendly(exc, lang)
    _log.error("%s | %s | %s: %s", context or "-", exc.__class__.__name__, message, exc)
    try:
        from PySide6.QtWidgets import QMessageBox

        title = "Video Toolkit" if lang != "en_US" else "Video Toolkit"
        QMessageBox.critical(parent, title, f"{context}\n\n{message}" if context else message)
    except Exception:
        pass
    return message
