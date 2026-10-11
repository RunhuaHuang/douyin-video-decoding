@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if /I "%~1"=="asr" (
    uv run --extra asr douyin-launch
) else (
    uv run douyin-launch
)
exit /b %errorlevel%
