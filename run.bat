@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if /I "%~1"=="asr" (
    uv run --no-sync --extra asr douyin-launch
) else (
    uv run --no-sync douyin-launch
)
exit /b %errorlevel%
