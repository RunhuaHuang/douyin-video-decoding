@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在打开抖音扫码登录窗口...
.\douyin-video-decoding\.venv\Scripts\python.exe login_douyin.py
pause
