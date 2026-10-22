# AGENTS.md — Agent 操作速查

给 AI agent（ZCode / Codex / Claude Code 等）的最短路径手册。人用的完整说明在 README.md。

## 环境

- macOS Apple Silicon / Windows x64 / Linux；建议 Python 3.12，一律走 `uv run ...`，不要用裸 python3
- 工作目录固定为仓库根目录
- 基础依赖仅含下载；转写使用 `uv sync --extra asr`。MLX 仅支持 Apple Silicon，Windows/Linux 使用 SenseVoice（可用 CUDA 优先，否则 CPU）
- Windows 入口：`setup.bat`、`run.bat`、`login.bat "备注"`、`download_videos.bat`；需要转写时 `setup.bat asr` / `run.bat asr`

## 上手前自检（按顺序）

```bash
uv sync --extra dev && uv run playwright install chromium   # 依赖（一次性）
uv run douyin-dl --list-accounts                # 账号池必须有可用会话
uv run pytest                                   # 全量测试应全部通过
```

**账号池为空 → 停下来找人**：让用户在 WebUI 首页账号池点「新增账号」扫码，或人工执行 `./login "备注"`（扫码后自动保存），登录这一步无法自动化。空池状态下浏览器无登录态，绝大多数视频解析会失败。

## 任务 → 最短命令

| 任务 | 做法 |
|---|---|
| 下载视频与图文（独立命名） | `uv run douyin-video-dl "链接1" "链接2" -o <目录>` 或 `./download_videos "表.xlsx" -o <目录>`（命名: `视频-ID.mp4` / `图文-ID.mp4`，图文 1秒1图合成） |
| NAS 手动批量下载 | `ssh nas 'sudo docker exec douyin-video douyin-batch-download "/volume5/视频下载/目标目录/文件.xlsx"'`；输出到 `/volume5/视频下载/视频下载/<文件名>/`，重复执行可断点续接 |
| 下载 1~几个视频 | `uv run douyin-dl "链接1" "链接2" -o <目录>`，默认存 `output/downloads/`，退出码 0/1 对应全成功/有失败 |
| 粘贴链接快速下载 | WebUI 首页「快速下载」卡片，或 `POST /quick-download`（`links` 每行一个链接，`transcribe` 勾选同时转写），直接建批次并跳转批次页 |
| 批量（无人值守） | 见下面 API 四步 |
| 批量（有人在旁边） | `./run` 起 WebUI 让人点，agent 只负责起服务和看日志 |
| 加/换账号会话 | WebUI 账号池「新增账号」按钮（人扫码）或 `./login "备注"`；agent 可用 `GET /api/accounts` 查看池状态 |
| 跑测试 | `uv sync --extra dev && uv run pytest` |

## 灰豚多表下载（Windows / macOS 共用）

- **输入和输出目录由用户每次任务给出**，不要写死用户名、桌面路径或盘符，也不要自动跑默认目录。未提供路径时先询问。工作目录为仓库根目录。
- 先预览：`uv run python batch_download_huitun.py --source "<输入目录>" -o "<输出目录>" --dry-run`。
- 正式执行：去掉 `--dry-run`，可加 `--concurrency 2`；单表也可直接传 `"<表格.xlsx>"`，多个目录重复 `--source`。只扫描目录第一层 `.xlsx`；按月筛选用 `--month 2025-10`，试跑用 `--test 2`。
- 输出 `<输出目录>/<表名>/视频-ID.mp4` / `图文-ID.mp4` 和 `manifest.json`。同一命令可补跑失败/缺失文件，旧完成标记不会阻止核对；有失败或试跑未覆盖整表时不标记整表完成。退出码 `0` 无失败、`1` 有失败、`2` 参数/预检错误。
- 会话：Mac 默认 `tmp/browser/huitun_state.json`，Windows 默认 `D:\抖音视频下载\douyin_state.json`。首次自动复制可用账号池会话；可用 `--account "备注"` 重新指定，原账号池不被修改。独立脚本不做账号自动轮换。
- 已实测旧 Cookie 异常时，可显式使用 `--anonymous-state "独立访客.json"`（不与 `--account` 同用），批量和人工验证助手都传同一路径；此模式只用未登录会话，不从账号池补 Cookie。不要删除或覆盖账号池。新版会重查旧 manifest 中被误记为 deleted 的账号/权限限制，明确删除的作品仍跳过。
- 无可用会话时找人工扫码：`uv run python login_douyin.py`。验证码找人工执行 `uv run python verify_douyin_interactive.py "<本次受限的完整链接>"`。三个脚本自定义 `--state-file` 时必须一致；后台在冷却期检测该文件更新后提前重试。
- 三个脚本的浏览器均支持 `--proxy auto`（默认检测 7890）、`--proxy none` 或自定义 URL；登录、验证和解析使用相同参数。文件下载忽略应用代理；**用户已选择关闭 TUN，不要改系统代理/Clash 配置，也不要宣称代码能绕过 TUN**。
- Mac 启动入口为 `启动批量下载.command`、`扫码登录.command`、`完成验证.command`；agent 优先使用上述 `uv run python ...` 命令以便传参和获取退出码。Windows 保留同名 `.bat`。
- 修改后跑全量测试；已装 Chromium 时用 `DOUYIN_BROWSER_SMOKE=1 uv run pytest`，包含本地代理与浏览器媒体拦截验证，不消耗真实视频下载流量。

## 无头批量四步（服务需在跑：`uv run douyin-csv-webui`）

```bash
curl -s -F "files=@links.csv" http://127.0.0.1:8000/api/preview        # 拿 batch_id/job_id/columns
curl -s -X POST http://127.0.0.1:8000/api/batches/<bid>/run -H "Content-Type: application/json" \
     -d '{"url_column":"链接","download_only":true}'                   # 启动（参数表见 README）
curl -s http://127.0.0.1:8000/api/batches/<bid>                        # 轮询 status/processed_rows
curl -sO http://127.0.0.1:8000/download/batches/<bid>                  # 完成后取 zip
```

- status 流转：`uploaded → running → completed / failed / paused`；行级状态是中文（成功/失败/已删除/非视频/无法下载/待重试），看 `/api/jobs/<id>` 的 `logs`
- 长任务先干别的，隔段时间再轮询；`paused` 时可 `POST /jobs/<id>/resume` 或 `POST /batches/<id>/resume`
- 批次有失败行时可 `POST /batches/<id>/retry-failed` 一键重试所有失败项（单文件对应 `POST /jobs/<id>/retry-failed`）
- 转写模式把 `download_only` 去掉并给 `model_names`（默认 SenseVoiceSmall；首次跑模型要下载，慢）

## 关键行为（改代码/排查前先知道）

- **浏览器优先**：`app/config.py` 的 `DOUYIN_BROWSER_FIRST=True`，解析和下载直接走 Playwright + 账号池会话，不尝试直连接口（直连基本 403）
- **分流视频**：约 5 分钟以上的长视频网页端是音视频分离流（URL 路径带 `media-video-` / `media-audio-`），下载层会分别取两条流再用 ffmpeg 合并；音频流 URL 的 query 带 `mime_type=video_mp4`，分类必须看路径特征（`app/browser_service.py` 的 `_is_audio_like_url`）
- 反风控/cookies 轮换逻辑都在 `app/pipeline.py`，任务实际执行与其渲染分离，改下载行为不要动 pipeline
- 修改下载/解析相关代码后：`uv sync --extra dev && uv run pytest` 必跑；有真实链接时用 `douyin-dl` 各验一条长视频和一条短视频

## 输出位置

CLI 下载 `output/downloads/`；批量视频 `output/videos/<表名>/`；结果表格 `output/spreadsheet/`；会话 `tmp/browser/accounts/`。`output/`、`tmp/` 均被 gitignore，不要往里放需要提交的东西。

结果表格命名 `<原名去扩展名>.<任务ID前8位>.processed.csv`（`csv_service.build_processed_output_name`，job_id 为空时退化为 `<原名去扩展名>.processed.csv`），同名输入各写各的、互不覆盖；续跑定位已有产物用 `csv_service.find_latest_processed_path` 按同名前缀取 mtime 最新一份。
