@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在打开验证窗口...
.\douyin-video-decoding\.venv\Scripts\python.exe verify_douyin_interactive.py
pause
