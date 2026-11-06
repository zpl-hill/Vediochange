"""配置持久化：config/settings.json（JSON，缺失字段自动补默认值）。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .paths import data_path

DEFAULTS: dict[str, Any] = {
    "language": "zh_CN",
    "theme": "dark",
    "ffmpeg_path": "",
    "ffprobe_path": "",
    "encoder": "auto",
    "concurrency": 2,
    "output_dir": "",
    "screenshot_dir": "Screenshots",
    "screenshot_format": "png",
    "volume": 80,
    "auto_mute_fast": True,
    "follow_default_audio_device": True,
    "auto_load_subtitle": True,
    "last_play_position": {},
    "last_file": "",
    "auto_resume_playback": True,
    "window_state": "",
    "overlay_timeout_ms": 2800,
    "overlay_auto_hide": False,  # False = 播放时控制条常驻；True = 沉浸模式自动隐藏
    "overlay_docked": False,     # False = 悬浮在画面上；True = 停靠在画面下方（不遮挡）
    "playlist": [],
    "playlist_index": -1,
    "playlist_auto_next": True,

    "geometry": "",
    "last_preset": "quality.standard",
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_path("config", "settings.json")
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data.update(raw)
        except Exception as exc:  # 配置损坏不能阻止程序启动
            get_logger("settings").warning("读取配置失败，使用默认值: %s", exc)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            get_logger("settings").error("保存配置失败: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any, save: bool = True) -> None:
        self._data[key] = value
        if save:
            self.save()

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)


_instance: Settings | None = None


def settings() -> Settings:
    global _instance
    if _instance is None:
        _instance = Settings()
    return _instance
