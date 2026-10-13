@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
where uv >nul 2>nul
if errorlevel 1 (
    echo Install uv first: winget install --id astral-sh.uv -e
    exit /b 1
)
if /I "%~1"=="asr" (
    uv sync --no-editable --python 3.12 --extra asr
) else (
    uv sync --no-editable --python 3.12
)
if errorlevel 1 exit /b 1
uv run --no-sync playwright install chromium
if errorlevel 1 exit /b 1
where ffmpeg >nul 2>nul
if errorlevel 1 goto :missing_ffmpeg
where ffprobe >nul 2>nul
if errorlevel 1 goto :missing_ffmpeg
echo Setup complete. Run run.bat, or run.bat asr for transcription.
exit /b 0
:missing_ffmpeg
echo Install FFmpeg: winget install --id Gyan.FFmpeg -e
echo Then open a new terminal and run setup.bat again.
exit /b 1
