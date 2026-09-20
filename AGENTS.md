# AGENTS.md — Agent 操作速查

给 AI agent（ZCode / Codex / Claude Code 等）的最短路径手册。人用的完整说明在 README.md。

## 环境

- macOS Apple Silicon；Python 一律走 `uv run ...`，不要用裸 python3
- 工作目录固定为仓库根目录

## 上手前自检（按顺序）

```bash
uv sync && uv run playwright install chromium   # 依赖（一次性）
uv run douyin-dl --list-accounts                # 账号池必须有可用会话
uv run pytest                                   # 全量测试应全部通过
```

**账号池为空 → 停下来找人**：让用户人工执行 `./login "备注"`（扫码 + 回车），这一步无法自动化。空池状态下浏览器无登录态，绝大多数视频解析会失败。

## 任务 → 最短命令

| 任务 | 做法 |
|---|---|
| 下载 1~几个视频 | `uv run douyin-dl "链接1" "链接2" -o <目录>`，默认存 `output/downloads/`，退出码 0/1 对应全成功/有失败 |
| 批量（无人值守） | 见下面 API 四步 |
| 批量（有人在旁边） | `./run` 起 WebUI 让人点，agent 只负责起服务和看日志 |
| 加/换账号会话 | 人来做（`./login`）；agent 只能用 `GET /api/accounts` 查看池状态 |
| 跑测试 | `uv run pytest` |

## 无头批量四步（服务需在跑：`uv run douyin-csv-webui`）

```bash
curl -s -F "files=@links.csv" http://127.0.0.1:8000/api/preview        # 拿 batch_id/job_id/columns
curl -s -X POST http://127.0.0.1:8000/api/batches/<bid>/run -H "Content-Type: application/json" \
     -d '{"url_column":"链接","download_only":true}'                   # 启动（参数表见 README）
curl -s http://127.0.0.1:8000/api/batches/<bid>                        # 轮询 status/processed_rows
curl -sO http://127.0.0.1:8000/download/batches/<bid>                  # 完成后取 zip
```

- status 流转：`uploaded → running → completed / failed / paused`；行级状态是中文（成功/失败/已删除/非视频/无法下载/待重试），看 `/api/jobs/<id>` 的 `logs`
- 长任务先干别的，隔段时间再轮询；`paused` 时可 `POST /jobs/<id>/resume`
- 转写模式把 `download_only` 去掉并给 `model_names`（默认 SenseVoiceSmall；首次跑模型要下载，慢）

## 关键行为（改代码/排查前先知道）

- **浏览器优先**：`app/config.py` 的 `DOUYIN_BROWSER_FIRST=True`，解析和下载直接走 Playwright + 账号池会话，不尝试直连接口（直连基本 403）
- **分流视频**：约 5 分钟以上的长视频网页端是音视频分离流（URL 路径带 `media-video-` / `media-audio-`），下载层会分别取两条流再用 ffmpeg 合并；音频流 URL 的 query 带 `mime_type=video_mp4`，分类必须看路径特征（`app/browser_service.py` 的 `_is_audio_like_url`）
- 反风控/cookies 轮换逻辑都在 `app/pipeline.py`，任务实际执行与其渲染分离，改下载行为不要动 pipeline
- 修改下载/解析相关代码后：`uv run pytest` 必跑；有真实链接时用 `douyin-dl` 各验一条长视频和一条短视频

## 输出位置

CLI 下载 `output/downloads/`；批量视频 `output/videos/<表名>/`；结果表格 `output/spreadsheet/`；会话 `tmp/browser/accounts/`。`output/`、`tmp/` 均被 gitignore，不要往里放需要提交的东西。
