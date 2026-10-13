@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===================================================
echo 抖音带货视频双表并发轮询下载器（灰豚数据）
echo 来源目录: C:\Users\cuhksz\Desktop\灰豚数据\带货视频榜-总销售额排序-每月
echo 目标目录: D:\抖音视频下载
echo 命名格式: 视频-视频ID.mp4 / 图文-视频ID.mp4
echo 并发表格: 2 张表同时下载，一张完成接续下一张
echo ===================================================
.\douyin-video-decoding\.venv\Scripts\python.exe batch_download_huitun.py --concurrency 2 %*
pause
