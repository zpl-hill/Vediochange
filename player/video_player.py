"""基于 QMediaPlayer 的播放内核（需 PySide6 >= 6.10）。

关键实现：
- Qt 6.10 起 QMediaPlayer 提供 pitchCompensation / pitchCompensationAvailability，
  FFmpeg 后端会做 Time Stretch，变速不变调；但不同后端可能返回 Unavailable，
  所以这里做运行时探测并把状态回报给 UI，绝不硬编码假设。
- >= 4x 自动静音（可关闭），回到低速时恢复用户原本的静音状态。
- Qt 的 FFmpeg 后端在打开音频流时会绑定设备，系统切换默认输出后不会自动跟随。
  这里监听 QMediaDevices.audioOutputsChanged 并低频轮询默认设备 id，
  变化时重建 QAudioOutput，保留播放位置、速率、音量与静音状态。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

from config.logging_setup import get_logger
from config.settings import settings

_log = get_logger("player")

RATES = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 4.0, 8.0)
MUTE_THRESHOLD = 4.0


class VideoPlayer(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playingChanged = Signal(bool)
    errorOccurred = Signal(str)
    rateChanged = Signal(float)
    pitchStatus = Signal(str)          # on | off | unknown
    volumeChanged = Signal(int)
    audioDeviceChanged = Signal(str)   # 新的默认输出设备名
    endReached = Signal()              # 播放到文件末尾（播放列表据此自动下一个）

    def __init__(self, video_widget, parent: QObject | None = None):
        super().__init__(parent)
        self._player = QMediaPlayer(parent)
        self._audio = QAudioOutput(parent)
        self._player.setAudioOutput(self._audio)
        if hasattr(video_widget, "sink"):
            # VideoSurface：纯 Qt 自绘，不产生原生视频窗口，悬浮控制条才不会被盖住
            self._player.setVideoSink(video_widget.sink)
            self._surface = video_widget
        else:  # 兼容传入 QVideoWidget 的旧用法
            self._player.setVideoOutput(video_widget)
            self._surface = None

        self._path = ""
        self._rate = 1.0
        self._user_muted = False
        self._auto_muted = False
        self._auto_mute = bool(settings().get("auto_mute_fast", True))
        self._pitch = "unknown"
        self._volume = int(settings().get("volume", 80))
        self._end_emitted = False

        self._audio.setVolume(self._volume / 100.0)
        # QMediaPlayer 的 position/duration 是 qlonglong，不能直接 signal→Signal(int)，
        # 统一用槽转发并转成 Python int，避免 PySide6 严格类型检查报错。
        self._player.positionChanged.connect(self._forward_position)
        self._player.durationChanged.connect(self._forward_duration)
        self._player.playingChanged.connect(self._forward_playing)
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._setup_audio_device_watch()
        self._detect_pitch()

    # ---------- 音频输出设备热切换 ----------
    def _setup_audio_device_watch(self) -> None:
        self._follow_audio_device = bool(settings().get("follow_default_audio_device", True))
        self._device_id = b""
        self._device_name = ""
        self._devices = QMediaDevices(self)
        try:
            device = QMediaDevices.defaultAudioOutput()
            if not device.isNull():
                self._device_id = bytes(device.id())
                self._device_name = device.description()
                self._audio.setDevice(device)
        except Exception as exc:
            _log.warning("读取默认音频设备失败: %s", exc)

        # 设备列表变化会立刻发信号；但仅改「默认设备」时列表可能不变，
        # 所以再加一个 2 秒轮询兜底（只比较 id，不重建任何对象）。
        self._device_watch = QTimer(self)
        self._device_watch.setInterval(2000)
        self._device_watch.timeout.connect(self._check_default_device)
        self._device_debounce = QTimer(self)
        self._device_debounce.setSingleShot(True)
        self._device_debounce.setInterval(500)
        self._device_debounce.timeout.connect(self._check_default_device)
        try:
            self._devices.audioOutputsChanged.connect(self._on_audio_outputs_changed)
        except Exception as exc:
            _log.warning("无法监听音频设备变化: %s", exc)
        if self._follow_audio_device:
            self._device_watch.start()

    def _on_audio_outputs_changed(self) -> None:
        # 切换设备时 Windows 会连发多次，防抖后只重建一次
        if self._follow_audio_device:
            self._device_debounce.start()

    @staticmethod
    def _current_device() -> tuple[bytes, str]:
        try:
            device = QMediaDevices.defaultAudioOutput()
            if device.isNull():
                return b"", ""
            return bytes(device.id()), device.description()
        except Exception:
            return b"", ""

    def _check_default_device(self) -> None:
        if not self._follow_audio_device:
            return
        device_id, name = self._current_device()
        if not device_id or device_id == self._device_id:
            return
        previous = self._device_id
        self._device_id = device_id
        self._device_name = name
        if not previous or not self._path:
            return  # 首次记录 / 没在播放时不必重建
        self._rebind_audio_output(name)

    def _rebind_audio_output(self, name: str) -> None:
        """替换 QAudioOutput 让 FFmpeg 后端重新打开到新的默认设备。"""
        was_playing = bool(self._player.isPlaying())
        position = int(self._player.position())
        rate = float(self._player.playbackRate() or 1.0)
        try:
            device = QMediaDevices.defaultAudioOutput()
            new_audio = QAudioOutput(self)
            if not device.isNull():
                new_audio.setDevice(device)
            new_audio.setVolume(self._volume / 100.0)
            new_audio.setMuted(True if self._auto_muted else self._user_muted)
            self._player.setAudioOutput(new_audio)
            old_audio, self._audio = self._audio, new_audio
            old_audio.deleteLater()
            if rate > 0:
                self._player.setPlaybackRate(rate)
            if position > 0:
                self._player.setPosition(position)
            if was_playing:
                self._player.play()
        except Exception as exc:
            _log.warning("切换音频输出设备失败: %s", exc)
            return
        _log.info("音频输出已切换到: %s (位置 %d ms, 继续播放=%s)", name, position, was_playing)
        self.audioDeviceChanged.emit(name or "默认设备")

    @property
    def follow_audio_device(self) -> bool:
        return bool(getattr(self, "_follow_audio_device", True))

    def set_follow_audio_device(self, enabled: bool) -> None:
        self._follow_audio_device = bool(enabled)
        settings().set("follow_default_audio_device", self._follow_audio_device)
        if self._follow_audio_device:
            self._device_id, self._device_name = self._current_device()
            self._device_watch.start()
        else:
            self._device_watch.stop()
        _log.info("跟随系统音频输出设备: %s", self._follow_audio_device)

    @property
    def audio_device_name(self) -> str:
        return getattr(self, "_device_name", "")

    def _forward_position(self, value) -> None:
        self.positionChanged.emit(int(value))

    def _forward_duration(self, value) -> None:
        self.durationChanged.emit(int(value))

    def _forward_playing(self, value) -> None:
        self.playingChanged.emit(bool(value))

    def _on_media_status(self, status) -> None:
        try:
            if status == QMediaPlayer.MediaStatus.EndOfMedia:
                if not self._end_emitted:
                    self._end_emitted = True
                    self.endReached.emit()
            else:
                self._end_emitted = False
        except Exception as exc:
            _log.warning("处理媒体状态失败: %s", exc)

    # ---------- 音调补偿 ----------
    def _detect_pitch(self) -> None:
        try:
            if not hasattr(self._player, "setPitchCompensation"):
                self._pitch = "unknown"
            else:
                availability = self._player.pitchCompensationAvailability()
                unavailable = getattr(
                    getattr(QMediaPlayer, "PitchCompensationAvailability", object),
                    "Unavailable", 2,
                )
                is_unavailable = availability == unavailable
                try:
                    is_unavailable = is_unavailable or int(availability) == 2
                except (TypeError, ValueError):
                    pass
                if is_unavailable:
                    self._pitch = "off"
                else:
                    self._player.setPitchCompensation(True)
                    self._pitch = "on" if bool(self._player.pitchCompensation()) else "off"
        except Exception as exc:
            _log.warning("音调补偿探测失败: %s", exc)
            self._pitch = "unknown"
        _log.info("音调补偿状态: %s", self._pitch)
        self.pitchStatus.emit(self._pitch)

    @property
    def pitch_status(self) -> str:
        return self._pitch

    def _on_error(self, _error, message: str) -> None:
        _log.error("播放错误: %s", message)
        self.errorOccurred.emit(message or "播放失败")

    # ---------- 媒体控制 ----------
    def open(self, path: str) -> None:
        self._path = str(path)
        self._end_emitted = False
        self._player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self._player.play()
        _log.info("打开媒体: %s", path)

    def close(self) -> None:
        if getattr(self, "_device_watch", None) is not None:
            self._device_watch.stop()
            self._device_debounce.stop()
        self._player.stop()
        self._player.setSource(QUrl())
        self._path = ""
        if self._surface is not None:
            self._surface.clear()

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def toggle(self) -> None:
        if self._player.isPlaying():
            self._player.pause()
        else:
            self._player.play()

    def stop(self) -> None:
        self._player.stop()

    def seek(self, position_ms: int) -> None:
        if self._player.isSeekable() or self._player.duration() > 0:
            self._player.setPosition(max(0, int(position_ms)))

    def skip(self, delta_ms: int) -> None:
        self.seek(self._player.position() + int(delta_ms))

    @property
    def position(self) -> int:
        return int(self._player.position())

    @property
    def duration(self) -> int:
        return int(self._player.duration())

    @property
    def is_playing(self) -> bool:
        return bool(self._player.isPlaying())

    @property
    def path(self) -> str:
        return self._path

    # ---------- 速度 ----------
    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float) -> None:
        rate = max(RATES[0], min(RATES[-1], float(rate)))
        self._rate = rate
        self._player.setPlaybackRate(rate)
        self._apply_auto_mute()
        self.rateChanged.emit(rate)
        _log.info("播放速率: %sx", rate)

    def faster(self) -> None:
        self._step_rate(1)

    def slower(self) -> None:
        self._step_rate(-1)

    def _step_rate(self, direction: int) -> None:
        try:
            index = RATES.index(self._rate)
        except ValueError:
            index = next((i for i, r in enumerate(RATES) if r >= self._rate), 3)
        index = max(0, min(len(RATES) - 1, index + direction))
        self.set_rate(RATES[index])

    def _apply_auto_mute(self) -> None:
        should_mute = self._auto_mute and self._rate >= MUTE_THRESHOLD
        if should_mute and not self._auto_muted:
            self._auto_muted = True
            self._audio.setMuted(True)
        elif not should_mute and self._auto_muted:
            self._auto_muted = False
            self._audio.setMuted(self._user_muted)

    def set_auto_mute_fast(self, enabled: bool) -> None:
        self._auto_mute = bool(enabled)
        settings().set("auto_mute_fast", self._auto_mute)
        self._apply_auto_mute()

    # ---------- 音量 ----------
    def set_volume(self, value: int) -> None:
        self._volume = max(0, min(100, int(value)))
        self._audio.setVolume(self._volume / 100.0)
        settings().set("volume", self._volume)
        self.volumeChanged.emit(self._volume)

    @property
    def volume(self) -> int:
        return self._volume

    def set_muted(self, muted: bool) -> None:
        self._user_muted = bool(muted)
        if not self._auto_muted:
            self._audio.setMuted(self._user_muted)

    @property
    def muted(self) -> bool:
        return self._user_muted
