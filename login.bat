@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
uv run douyin-csv-login %*
exit /b %errorlevel%
