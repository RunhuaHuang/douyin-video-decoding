@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
uv run --no-sync python login_douyin.py %*
set "_DOUYIN_EXIT_CODE=%errorlevel%"
pause
exit /b %_DOUYIN_EXIT_CODE%
