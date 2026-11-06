"""Video Toolkit V1.0 — 入口。

    py -3 main.py              启动图形界面
    py -3 main.py --console    启动图形界面并保留控制台窗口（排查问题用）
    py -3 main.py --selftest   无界面自检（预设 / 命令 / ffprobe / 实际转码）
"""
from __future__ import annotations

import os
import sys


def _configure_dpi() -> None:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")


def _hide_console() -> None:
    """把 run.bat 带出来的那个黑框藏起来。

    只在 run.bat 启动时生效（它设置 VTK_HIDE_CONSOLE=1），所以从已有终端
    手动 `py -3 main.py` 不会把用户自己的终端一起藏掉。日志照常写文件，
    控制台只是不可见，不是销毁，排查时加 --console 就能看到。
    """
    if os.name != "nt" or "--console" in sys.argv:
        return
    if os.environ.get("VTK_HIDE_CONSOLE") != "1":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def main() -> int:
    if "--selftest" in sys.argv:
        from selftest import run
        return run()

    _hide_console()
    _configure_dpi()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except AttributeError:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("Video Toolkit")
    app.setApplicationVersion("1.0")
    app.setOrganizationName("Fred Zhao")

    from config.logging_setup import setup_logging
    from config.settings import settings

    setup_logging()

    from config.errors import report
    from config.i18n import i18n
    from ui.theme import apply_theme

    language = str(settings().get("language", "zh_CN"))
    if i18n().lang != language:
        i18n().set_language(language)
    apply_theme(app, str(settings().get("theme", "dark")))

    from ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    def excepthook(exc_type, exc_value, exc_tb) -> None:
        import traceback

        from config.logging_setup import get_logger

        get_logger("unhandled").error(
            "未捕获异常:\n%s", "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )
        try:
            report(None, exc_value, "程序异常", i18n().lang)
        except Exception:
            pass

    sys.excepthook = excepthook
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
