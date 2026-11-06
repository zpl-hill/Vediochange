"""日志：所有功能记录到 logs/app.log，所有 ERROR 额外写入 logs/error.log。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .paths import ensure_dir

_ROOT = "vtk"
_initialized = False


def setup_logging(console_level: int = logging.INFO) -> logging.Logger:
    global _initialized
    logger = logging.getLogger(_ROOT)
    if _initialized:
        return logger

    log_dir = ensure_dir("logs")
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    app_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    app_handler.setLevel(logging.DEBUG)
    app_handler.setFormatter(fmt)

    error_handler = RotatingFileHandler(
        log_dir / "error.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(fmt)

    logger.addHandler(app_handler)
    logger.addHandler(error_handler)
    logger.addHandler(console)
    logger.info("Video Toolkit 日志初始化完成")
    _initialized = True
    return logger


def get_logger(name: str = "app") -> logging.Logger:
    if not _initialized:
        setup_logging()
    full = _ROOT if name in ("", _ROOT) else f"{_ROOT}.{name}"
    return logging.getLogger(full)
