@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Build Video Toolkit (onedir)

py -3 -m pip show pyinstaller >nul 2>nul
if errorlevel 1 py -3 -m pip install pyinstaller

py -3 -m PyInstaller --noconfirm --clean --windowed --onedir --name VideoToolkit ^
  --add-data "language;language" ^
  --add-data "config/presets.json;config" ^
  --exclude-module PySide6.QtWebEngineCore ^
  --exclude-module PySide6.QtPdf ^
  --exclude-module numpy ^
  --exclude-module cv2 ^
  --exclude-module tkinter ^
  main.py

echo.
echo Done: dist\VideoToolkit\VideoToolkit.exe
echo Put ffmpeg.exe / ffprobe.exe into dist\VideoToolkit\ffmpeg\ if they are not on PATH.
pause
