"""深色主题 QSS。"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication

DARK_QSS = """
QWidget {
    background-color: #15171c;
    color: #d8dde8;
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 12px;
}
QMainWindow, QDialog, QTabWidget::pane { background-color: #15171c; }
QMenuBar { background-color: #1b1e25; color: #d8dde8; border-bottom: 1px solid #2a2f3a; }
QMenuBar::item { padding: 6px 12px; }
QMenuBar::item:selected { background-color: #2f5fa8; }
QMenu { background-color: #1b1e25; color: #d8dde8; border: 1px solid #333947; }
QMenu::item { padding: 6px 22px; }
QMenu::item:selected { background-color: #2f5fa8; }
QGroupBox {
    border: 1px solid #2a2f3a; border-radius: 8px;
    margin-top: 14px; padding: 10px 12px 8px 12px;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #8fb6f5; }
QLabel { color: #c3cad8; }
QLabel#hint { color: #7f889b; }
QPushButton {
    background-color: #232833; border: 1px solid #363c4a;
    border-radius: 6px; padding: 6px 14px; color: #d8dde8;
}
QPushButton:hover { background-color: #2c3444; border-color: #4a5b7a; }
QPushButton:pressed { background-color: #2f5fa8; }
QPushButton:disabled { color: #5d6472; border-color: #292e38; }
QPushButton#primary { background-color: #2f5fa8; border-color: #3d74c9; color: #ffffff; }
QPushButton#primary:hover { background-color: #3a6fc2; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit {
    background-color: #1d212a; border: 1px solid #363c4a;
    border-radius: 5px; padding: 5px 8px; color: #e2e7f0;
    selection-background-color: #2f5fa8;
}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: #4a5b7a; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background-color: #1d212a; border: 1px solid #363c4a; selection-background-color: #2f5fa8; }
QTabWidget::pane { border: 1px solid #2a2f3a; border-radius: 8px; top: -1px; }
QTabBar::tab { background: #1b1e25; color: #98a1b3; padding: 8px 20px; border: 1px solid #2a2f3a; border-bottom: none; }
QTabBar::tab:selected { background: #232833; color: #dce6f8; border-bottom: 2px solid #3d74c9; }
QTableWidget { background-color: #1a1e26; alternate-background-color: #1f2430; gridline-color: #2a2f3a; border: 1px solid #2a2f3a; border-radius: 6px; }
QHeaderView::section { background-color: #232833; color: #aab4c6; border: none; border-right: 1px solid #2a2f3a; padding: 6px; }
QTableWidget::item:selected { background-color: #2f5fa8; }
QListWidget {
    background-color: #1a1e26; alternate-background-color: #1f2430;
    border: 1px solid #2a2f3a; border-radius: 6px; color: #d8dde8;
    outline: none;
}
QListWidget::item { padding: 5px 8px; border-radius: 4px; }
QListWidget::item:hover { background-color: #232833; }
QListWidget::item:selected { background-color: #2f5fa8; color: #ffffff; }
QCheckBox { color: #c3cad8; }
QSlider::groove:horizontal { height: 4px; background: #333947; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; margin: -4px 0; background: #6f9bea; border-radius: 6px; }
QProgressBar { border: 1px solid #363c4a; border-radius: 5px; text-align: center; background: #1d212a; height: 18px; }
QProgressBar::chunk { background-color: #2f5fa8; border-radius: 4px; }
QScrollBar:vertical { background: #15171c; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a4152; border-radius: 5px; min-height: 24px; }
QStatusBar { background: #1b1e25; color: #98a1b3; border-top: 1px solid #2a2f3a; }
QStatusBar QLabel#mediaBrief { color: #d8dde8; }
QSplitter::handle { background: #2a2f3a; }
QToolTip { background: #232833; color: #e2e7f0; border: 1px solid #3a4152; }
QToolBar { background-color: #1b1e25; border-bottom: 1px solid #2a2f3a; padding: 5px 10px; spacing: 6px; }
QToolBar QToolButton {
    background-color: #232833; border: 1px solid #363c4a; border-radius: 6px;
    padding: 5px 14px; color: #d8dde8;
}
QToolBar QToolButton:hover { background-color: #2c3444; border-color: #4a5b7a; }
QToolBar QToolButton:checked { background-color: #2f5fa8; border-color: #3d74c9; color: #ffffff; }
QToolBar::separator { background: #333947; width: 1px; margin: 4px 8px; }
QDockWidget { background-color: #15171c; color: #d8dde8; border: 1px solid #333947; }
QDockWidget::title {
    background-color: #1b1e25; border-bottom: 1px solid #2a2f3a;
    padding: 7px 12px; text-align: left; color: #aebcd4;
}
QDockWidget::float-button, QDockWidget::close-button { background: #232833; border: 1px solid #363c4a; border-radius: 3px; }
#videoArea { background-color: #000000; }
#playbackOverlay {
    background-color: rgba(19, 22, 29, 0.78);
    border: 1px solid #3a4252; border-radius: 10px;
}
#playbackOverlay:hover { background-color: rgba(19, 22, 29, 0.94); }
/* 控制条内部控件要紧凑，否则整条会撑到 130px 以上、压掉一大块画面 */
#playbackOverlay QPushButton {
    background-color: transparent; border: 1px solid transparent;
    border-radius: 4px; padding: 0px; color: #cfd6e4; font-size: 12px;
}
#playbackOverlay QPushButton:hover { background-color: #2c3444; border-color: #46536b; }
#playbackOverlay QPushButton:pressed { background-color: #2f5fa8; }
#playbackOverlay QLabel { color: #97a1b3; font-size: 11px; }
#playbackOverlay QComboBox {
    padding: 1px 4px; font-size: 11px; border-radius: 4px;
    background-color: #1d212a; border: 1px solid #363c4a;
}
#playbackOverlay QComboBox::drop-down { width: 14px; }
#playbackOverlay QSlider::groove:horizontal {
    height: 3px; background: #2b313d; border-radius: 2px;
}
#playbackOverlay QSlider::handle:horizontal {
    width: 10px; margin: -4px 0; border-radius: 5px; background: #6f8fc4;
}
#playbackOverlay QSlider::handle:horizontal:hover { background: #8fb0e6; }
/* 停靠模式：独占一条画面下方的条带，必须完全不透明且不带圆角，避免透出画面 */
#playbackOverlay[docked="true"] {
    background-color: #13161d;
    border: none; border-top: 1px solid #2a2f3a; border-radius: 0px;
}
#playbackOverlay[docked="true"]:hover { background-color: #161a22; }
"""


def apply_theme(app: QApplication, theme: str = "dark") -> None:
    """darkdetect 是可选依赖；缺失或用户手动选择时以配置为准。"""
    if theme == "auto":
        try:
            import darkdetect
            theme = "dark" if darkdetect.isDark() else "light"
        except Exception:
            theme = "dark"
    if theme == "dark":
        app.setStyleSheet(DARK_QSS)
    else:
        app.setStyleSheet("")
