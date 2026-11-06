@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Video Toolkit V1.0

py -3 -c "import PySide6" >nul 2>nul
if errorlevel 1 goto nopyside

rem Hide the console window opened by this script.
rem The program does it itself right after start-up.
rem Keep the console for troubleshooting:  run.bat --console
set VTK_HIDE_CONSOLE=1
py -3 main.py %*
exit /b %errorlevel%

:nopyside
echo [ERROR] PySide6 was not found for the "py -3" launcher.
echo         Install it with:  py -3 -m pip install PySide6^>=6.10
pause
exit /b 1
