"""中英文切换：language/<lang>.json，切换时发信号让各面板重刷文本。"""
from __future__ import annotations

import json

from PySide6.QtCore import QObject, Signal

from .logging_setup import get_logger
from .paths import resource_path

SUPPORTED = ("zh_CN", "en_US")
_DEFAULT = "zh_CN"


class I18n(QObject):
    changed = Signal()

    def __init__(self, language: str = _DEFAULT):
        super().__init__()
        self.lang = language if language in SUPPORTED else _DEFAULT
        self._strings: dict[str, str] = {}
        self._fallback: dict[str, str] = {}
        self.reload()

    @staticmethod
    def _read(language: str) -> dict[str, str]:
        path = resource_path("language", f"{language}.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}
        except Exception as exc:
            get_logger("i18n").warning("语言文件读取失败 %s: %s", path, exc)
            return {}

    def reload(self) -> None:
        self._strings = self._read(self.lang)
        self._fallback = self._read(_DEFAULT) if self.lang != _DEFAULT else {}

    def set_language(self, language: str) -> None:
        if language not in SUPPORTED or language == self.lang:
            return
        self.lang = language
        self.reload()
        get_logger("i18n").info("界面语言切换为 %s", language)
        self.changed.emit()

    def t(self, key: str, **kwargs) -> str:
        text = self._strings.get(key) or self._fallback.get(key) or key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text


_instance: I18n | None = None


def i18n() -> I18n:
    global _instance
    if _instance is None:
        _instance = I18n()
    return _instance


def tr(key: str, **kwargs) -> str:
    return i18n().t(key, **kwargs)
