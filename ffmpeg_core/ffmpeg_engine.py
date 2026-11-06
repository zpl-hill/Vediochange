"""FFmpeg 子进程执行、实时进度解析、GPU 编码器探测。

耗时操作都在后台线程调用本模块，UI 线程只收进度回调，不会卡顿。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from config.errors import FFmpegError
from config.logging_setup import get_logger
from config.paths import find_binary
from config.settings import settings

from .command_builder import NULL_DEVICE, format_command
from .encoder_registry import set_available_encoders

_log = get_logger("ffmpeg")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ProgressCallback = Callable[[int, str], None]


def ffmpeg_binary() -> str:
    return find_binary("ffmpeg", settings().get("ffmpeg_path"))


@dataclass
class RunResult:
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    command: str = ""
    cancelled: bool = False


def _decode(data: bytes | str | None) -> str:
    if not data:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _parse_out_time(value: str) -> float:
    """解析 -progress 的 out_time=HH:MM:SS.ffffff。"""
    try:
        parts = value.strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        pass
    return 0.0


def run_command(
    argv: Sequence[str],
    *,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    duration_s: float = 0.0,
    timeout: float | None = None,
) -> RunResult:
    """同步执行一条命令；调用方负责把它放进后台线程。"""
    command_text = format_command(argv)
    _log.info("执行命令: %s", command_text)
    result = RunResult(command=command_text)
    started = time.monotonic()

    try:
        proc = subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(f"ffmpeg 不可用: {argv[0]}", -1, command_text, str(exc)) from exc
    except OSError as exc:
        raise FFmpegError(f"无法启动 ffmpeg: {exc}", -1, command_text, str(exc)) from exc

    stderr_chunks: list[bytes] = []

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in iter(proc.stderr.readline, b""):
            stderr_chunks.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, name="vtk-stderr", daemon=True)
    stderr_thread.start()

    last_percent = -1
    fields: dict[str, str] = {}
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        line = _decode(raw).strip()
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
            if key in ("out_time", "out_time_us", "out_time_ms") and on_progress and duration_s > 0:
                if key == "out_time":
                    elapsed = _parse_out_time(value)
                elif key == "out_time_us":
                    elapsed = float(value or 0) / 1_000_000
                else:
                    elapsed = float(value or 0) / 1_000_000
                if elapsed > 0:
                    percent = min(99, int(elapsed / duration_s * 100))
                    if percent != last_percent:
                        last_percent = percent
                        on_progress(percent, f"{percent}%")
        if line.startswith("progress=") and on_progress and line == "progress=end":
            on_progress(99, "99%")

        if cancel_event is not None and cancel_event.is_set():
            _log.warning("任务被取消，终止 ffmpeg: %s", command_text[:120])
            _terminate(proc)
            result.cancelled = True
            break
        if timeout is not None and time.monotonic() - started > timeout:
            _log.error("ffmpeg 执行超时: %.1fs", timeout)
            _terminate(proc)
            result.stderr = f"执行超时（{timeout:.0f} 秒）"
            break

    try:
        proc.stdout.close()
    except OSError:
        pass
    stderr_thread.join(timeout=2.0)
    try:
        result.returncode = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        result.returncode = proc.wait()

    result.stderr = _decode(b"".join(stderr_chunks)) if stderr_chunks else result.stderr
    result.stdout = fields.get("progress", "")
    if result.returncode != 0 and not result.cancelled:
        _log.error("ffmpeg 返回 %s: %s", result.returncode, result.stderr.strip()[:500])
    else:
        _log.info("命令完成，用时 %.1fs", time.monotonic() - started)
    return result


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_commands(
    commands: Sequence[Sequence[str]],
    *,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    duration_s: float = 0.0,
    timeout: float | None = None,
) -> RunResult:
    """按顺序执行多条命令（2-pass）。总进度按命令数加权。"""
    total = max(1, len(commands))
    last = RunResult()
    for index, argv in enumerate(commands):
        def callback(percent: int, text: str, i: int = index) -> None:
            if on_progress:
                on_progress(int((i + percent / 100.0) / total * 100), text)

        last = run_command(argv, on_progress=callback, cancel_event=cancel_event,
                           duration_s=duration_s, timeout=timeout)
        if last.returncode != 0 or last.cancelled:
            return last
    if on_progress:
        on_progress(100, "100%")
    return last


def run_capture_bytes(argv: Sequence[str], *, timeout: float = 30.0) -> bytes:
    """执行命令并原样返回 stdout 二进制（用于逐帧抽帧的 PNG 管道）。"""
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(f"ffmpeg 不可用: {argv[0]}", -1, format_command(argv), str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("抽帧超时", -1, format_command(argv), str(exc)) from exc
    if completed.returncode != 0:
        raise FFmpegError(
            "抽帧失败", completed.returncode, format_command(argv),
            _decode(completed.stderr)[:500],
        )
    return completed.stdout


@dataclass
class EncoderCapabilities:
    available_encoders: set[str] = field(default_factory=set)
    vendors: dict[str, bool] = field(default_factory=dict)
    av1_vendors: dict[str, bool] = field(default_factory=dict)
    gpu_names: list[str] = field(default_factory=list)

    def preferred_vendor(self) -> str:
        for vendor in ("nvidia", "intel", "amd"):
            if self.vendors.get(vendor):
                return vendor
        return "cpu"

    def is_available(self, vendor: str) -> bool:
        return vendor == "cpu" or bool(self.vendors.get(vendor))

    def has_encoder(self, name: str) -> bool:
        return name in self.available_encoders

    def av1_hardware_vendor(self) -> str:
        """选真正能编 AV1 的硬件；没有就返回 cpu（用 libaom-av1/libsvtav1）。"""
        for vendor in ("nvidia", "intel", "amd"):
            if self.vendors.get(vendor) and self.av1_vendors.get(vendor):
                return vendor
        return "cpu"

    def summary(self) -> str:
        active = [v.upper() for v in ("nvidia", "intel", "amd") if self.vendors.get(v)]
        av1 = [v.upper() for v in ("nvidia", "intel", "amd") if self.av1_vendors.get(v)]
        gpu = " / ".join(self.gpu_names[:2]) if self.gpu_names else "-"
        base = f"GPU: {gpu} | 硬件编码: {', '.join(active) if active else '无，使用 CPU'}"
        cpu_av1 = "libsvtav1" if self.has_encoder("libsvtav1") else "libaom-av1"
        return f"{base} | AV1: {cpu_av1}{' + ' + ', '.join(av1) if av1 else ''}"


_cached_caps: EncoderCapabilities | None = None


def _list_encoders(binary: str) -> set[str]:
    try:
        completed = subprocess.run(
            [binary, "-hide_banner", "-encoders"], capture_output=True,
            timeout=15, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names: set[str] = set()
    for line in _decode(completed.stdout).splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and "." in parts[0]:
            names.add(parts[1])
    return names


def _test_encoder(binary: str, vcodec: str, timeout: float = 8.0,
                  size: str = "256x256") -> bool:
    """编入 ≠ 硬件存在：实际编一帧来验证（这台 AMD 机器上 nvenc/qsv 会编入但不可用）。

    AV1 硬件编码器对最小尺寸有要求：256x256 时 av1_amf 会 Init 失败，
    所以 AV1 探测统一用 640x360。
    """
    argv = [binary, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", f"color=c=black:s={size}:r=25:d=0.2",
            "-c:v", vcodec, "-frames:v", "1", "-f", "null", NULL_DEVICE]
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout,
                                   creationflags=_CREATE_NO_WINDOW)
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _gpu_names() -> list[str]:
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join '|'"],
            capture_output=True, timeout=6, creationflags=_CREATE_NO_WINDOW,
        )
        text = _decode(completed.stdout).strip()
        return [n.strip() for n in text.split("|") if n.strip()]
    except Exception:
        return []


def detect_capabilities(*, refresh: bool = False) -> EncoderCapabilities:
    """探测编码器与可用硬件；结果缓存在进程内，避免每次打开设置都卡几秒。"""
    global _cached_caps
    if _cached_caps is not None and not refresh:
        return _cached_caps

    binary = ffmpeg_binary()
    encoders = _list_encoders(binary)
    set_available_encoders(encoders)  # 供 command_builder 挑选 CPU AV1 编码器
    caps = EncoderCapabilities(available_encoders=encoders, gpu_names=_gpu_names())
    for vendor, candidate, av1 in (
        ("nvidia", "h264_nvenc", "av1_nvenc"),
        ("intel", "h264_qsv", "av1_qsv"),
        ("amd", "h264_amf", "av1_amf"),
    ):
        ok = candidate in encoders and _test_encoder(binary, candidate)
        caps.vendors[vendor] = ok
        _log.info("硬件编码 %s (%s): %s", vendor, candidate, "可用" if ok else "不可用")
        caps.av1_vendors[vendor] = (
            ok and av1 in encoders and _test_encoder(binary, av1, size="640x360")
        )
        _log.info("AV1 硬件编码 %s (%s): %s", vendor, av1,
                  "可用" if caps.av1_vendors[vendor] else "不可用")
    caps.vendors["cpu"] = "libx264" in encoders or not encoders
    _cached_caps = caps
    return caps
