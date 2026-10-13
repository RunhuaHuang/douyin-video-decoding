@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
uv run --no-sync douyin-csv-login %*
exit /b %errorlevel%
