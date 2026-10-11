@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
uv run douyin-video-dl %*
exit /b %errorlevel%
