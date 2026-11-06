@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Video Toolkit - install dependencies
set VTK_SILENT=0
if /i "%~1"=="--silent" set VTK_SILENT=1

echo === Video Toolkit dependency installer ===
echo.

py -3 --version >nul 2>nul
if errorlevel 1 goto nopython
py -3 --version

echo.
echo [1/3] Installing Python packages from requirements.txt ...
py -3 -m pip install -r requirements.txt
if errorlevel 1 goto pipfail

echo.
echo [2/3] Checking ffmpeg.exe / ffprobe.exe ...
if exist "ffmpeg\ffmpeg.exe" goto ffmpegok
where ffmpeg >nul 2>nul
if errorlevel 1 goto noffmpeg
:ffmpegok
if exist "ffmpeg\ffprobe.exe" goto ffprobeok
where ffprobe >nul 2>nul
if errorlevel 1 goto noffprobe
:ffprobeok
echo       ffmpeg: OK
echo       ffprobe: OK

echo.
echo [3/3] Running self-test ...
py -3 main.py --selftest
if errorlevel 1 goto testfail

echo.
echo [OK] All dependencies are ready. Start the app with run.bat
echo      Optional extras: py -3 -m pip install psutil darkdetect
echo.
if "%VTK_SILENT%"=="0" pause
exit /b 0

:nopython
echo [ERROR] Python 3 was not found for the "py -3" launcher.
echo         Install Python 3.10 or newer from https://www.python.org/downloads/
echo         Remember to check "Add python.exe to PATH" during setup.
echo.
if "%VTK_SILENT%"=="0" pause
exit /b 1

:pipfail
echo [ERROR] pip install failed. See the messages above.
echo         Try: py -3 -m pip install PySide6^>=6.10
echo.
if "%VTK_SILENT%"=="0" pause
exit /b 1

:noffmpeg
echo [WARN] ffmpeg.exe was not found.
echo        Copy ffmpeg.exe and ffprobe.exe into the ffmpeg folder,
echo        or add them to PATH, or set ffmpeg_path in config/settings.json.
echo        They are NOT Python packages and cannot be installed by pip.
echo.
if "%VTK_SILENT%"=="0" pause
exit /b 1

:noffprobe
echo [WARN] ffprobe.exe was not found.
echo        Copy ffprobe.exe into the ffmpeg folder, or set ffprobe_path
echo        in config/settings.json.
echo.
if "%VTK_SILENT%"=="0" pause
exit /b 1

:testfail
echo [WARN] Self-test reported a problem. Check the output above
echo        and logs/error.log. Python packages were installed successfully.
echo.
if "%VTK_SILENT%"=="0" pause
exit /b 1
