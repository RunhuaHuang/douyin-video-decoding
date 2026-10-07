# 抖音文案提取工具

本地 Python 工具，链路：读取表格里的抖音链接 → 下载视频（浏览器优先 + 账号池会话）→ ffmpeg 抽音频 → MLX Whisper / SenseVoice 转写 → 写回表格。

三种用法，按需选择：

| 场景 | 入口 |
|---|---|
| 下载单个/少量视频 | 命令行 `uv run douyin-dl <链接>` |
| 人工批量（浏览器界面） | `./run` 打开 WebUI |
| 无人值守批量（脚本/Agent） | WebUI 的 JSON API，见「无头批量」 |

## 前置自检

```bash
cd "/Users/runhua/Runhua MBP/Python/抖音文案提取工具"
uv sync                                  # 依赖
uv run playwright install chromium       # 浏览器兜底必需
uv run douyin-dl --list-accounts         # 账号池：列出可用会话，空则见「登录抖音会话」
uv run pytest                            # 全量测试
```

- ffmpeg：转写和长视频（音视频分离流）合并需要；`brew install ffmpeg`
- 账号池为空时浏览器会话没有登录态，大部分视频会解析失败——先找人工执行 `./login`

## 命令行下载（douyin-dl）

```bash
# 单个链接（jingxuan、/video/、短链、整段分享文案都可以）
uv run douyin-dl "https://www.douyin.com/video/7687432578693284239"

# 多个链接 + 自定义保存目录
uv run douyin-dl "链接1" "链接2" -o /Volumes/电影/test
```

- 默认保存到 `output/downloads/<视频标题>.mp4`；只抓到音频流时保存 `.m4a`
- 长视频（约 5 分钟以上）网页端是音视频分离流，会分别下载视频/音频流后用 ffmpeg 合并成单个 mp4
- 退出码：全部成功 0，任一失败 1；逐条打印 `完成 链接 -> 路径` / `失败 链接: 原因`

## WebUI 批量（人工使用）

```bash
./run        # 启动并自动打开浏览器
# 或 uv run douyin-csv-webui 后手动访问 http://127.0.0.1:8000
```

1. 上传一个或多个 CSV / XLSX；或直接在首页「快速下载」卡片粘贴链接/分享口令（每行一个），可勾选同时转写
2. 选择抖音链接列，填写「视频文本列」列名和模型名（仅下载模式下都不用填）
3. 勾选「仅下载视频（跳过转写）」可只下载不转写，视频保存目录可改任意本机路径（如 NAS）
4. 批次页查看进度、暂停/继续、重试失败项（单个任务或整批）、下载结果

### 仅下载模式说明

- 跳过抽音频和转写；短视频不需要 ffmpeg，长视频合并时需要，没装会退回保存 `.m4a` 纯音频
- 视频保存到 `output/videos/<原表格文件名>/row<行号>_<视频标题>.mp4`
- 反风控、账号池轮换、失败重试等逻辑与完整模式完全一致
- 结果表格仍输出到 `output/spreadsheet/`，只是不含转写文本列

## 无头批量（JSON API，脚本 / Agent 用）

先起服务：`uv run douyin-csv-webui`（监听 `127.0.0.1:8000`）。完整链路四步：

```bash
# 1. 上传表格，拿 batch_id / job_id / 表头
curl -s -F "files=@links.csv" http://127.0.0.1:8000/api/preview
# → {"batch_id":"...", "common_columns":["链接",...], "jobs":[{"job_id":"...", "columns":[...], "row_count":10, ...}]}

# 2. 启动整个批次（所有文件统一配置；也可以只跑单文件 POST /api/run/<job_id>）
curl -s -X POST http://127.0.0.1:8000/api/batches/<batch_id>/run \
  -H "Content-Type: application/json" \
  -d '{"url_column":"链接","download_only":true}'

# 3. 轮询状态（status: uploaded → running → completed / failed / paused；进度看 processed_rows/total_rows）
curl -s http://127.0.0.1:8000/api/batches/<batch_id>
curl -s http://127.0.0.1:8000/api/jobs/<job_id>       # 单文件，含逐行日志 logs

# 4. 下载结果（批次为 zip，单文件为处理后的 CSV）
curl -sO http://127.0.0.1:8000/download/batches/<batch_id>
curl -sO http://127.0.0.1:8000/download/<job_id>
```

启动参数（`/api/run/{job_id}`、`/api/batches/{batch_id}/run`、两者 `/continue` 变体的 JSON body）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `url_column` | str | 必填 | 哪一列是抖音链接 |
| `transcript_column` | str | `视频文本` | 转写写回的列（仅下载模式不用填） |
| `model_names` | str | `FunAudioLLM/SenseVoiceSmall` | 模型名，逗号分隔可多个 |
| `overwrite` | bool | false | 覆盖已有转写文本 |
| `download_only` | bool | false | 仅下载模式 |
| `video_output_dir` | str | 空→`output/videos/` | 仅下载模式的视频保存目录，可填 NAS 挂载路径 |
| `download_workers` | int | 2 | 下载并发（上限 8） |
| `asr_workers` | int | 1 | 转写并发（上限 4） |
| `batch_delay_seconds` | int | 8 | 批间隔秒数（上限 30） |
| `anti_crawl_streak_threshold` | int | 6 | 强风控连续批次切换阈值 |
| `anti_crawl_cooldown_minutes` | int | 60 | 风控冷却时长 |
| `proactive_cooldown_batch_threshold` | int | 20 | 主动冷却触发批次阈值 |
| `proactive_cooldown_minutes` | int | 30 | 主动冷却时长 |

其他控制端点（POST，无 body 或表单）：

| 端点 | 作用 |
|---|---|
| `POST /jobs/{id}/pause` `/resume` | 暂停/继续（批次：`/batches/{id}/pause` 等） |
| `POST /jobs/{id}/skip-cooldown` | 跳过当前风控冷却 |
| `POST /jobs/{id}/switch-cookie` | 强制切换 cookies（当前批结束后生效） |
| `POST /jobs/{id}/retry-failed` | 重试失败项 |
| `POST /batches/{id}/retry-failed` | 批次级一键重试所有失败项 |
| `GET /api/accounts` | 账号池快照（可用会话、预警状态） |
| `GET /health` | ffmpeg / MLX / 模型就绪状态 |

WebUI 表单端点：`POST /quick-download`（字段 `links` 每行一个链接、`transcribe` 勾选同时转写）直接建批次跑粘贴的链接，等价于 WebUI 版 `douyin-dl`。

`/continue` 变体（`POST /api/jobs/{id}/continue`、`POST /api/batches/{id}/continue`）沿用已有输出文件续跑，参数同上。

## 登录抖音会话（需要人工）

程序不会自动登录。推荐直接在 WebUI 操作：首页账号池点「新增账号」，输入备注后会在本机打开 Chromium 窗口，扫码登录完成后关闭窗口即保存（与 WebUI 里每个账号的「重新登录」同一流程）。也可以在终端执行：

```bash
./login "主号"
```

会打开 Chromium：登录抖音或完成验证码 → 回终端按回车 → 会话保存到 `tmp/browser/accounts/<备注>.json`。之后 WebUI、CLI 和浏览器兜底会自动轮换使用这些会话。备注已存在则覆盖并清除预警标记。

## 输出位置

| 内容 | 位置 |
|---|---|
| CLI 下载的视频 | `output/downloads/` |
| 仅下载模式的视频 | `output/videos/<表格文件名>/row<行号>_<标题>.mp4` |
| 结果表格（含转写列） | `output/spreadsheet/`，命名 `<原名去扩展名>.<任务ID前8位>.processed.csv` |
| 上传与中间文件 | `tmp/spreadsheets/` |
| 账号池会话 | `tmp/browser/accounts/` |

## 说明

- 单元格里可以放整段抖音分享文本，不一定要纯 URL。
- 图文 `note` 链接直接标记 `非视频`；即使链接写着 `/video/`，最终落到 `/note/` 页也按 `非视频` 处理。
- 明确删除的视频写 `已删除`；连续两次 403 或 404 类失败直接标记 `无法下载`/`非视频`，不再重复重试。
- 下载层默认**浏览器优先**：直接用账号池会话走 Playwright 解析和下载，跳过直连接口尝试（直连近年基本都被 403）；浏览器不可用时自动退回直连链路。想恢复旧的「直连优先」，把 `app/config.py` 的 `DOUYIN_BROWSER_FIRST` 改为 `False`。
- 默认每处理 30 个内部批次自动切换下一份可用 cookies；主任务阶段连续 6 个批次命中强风控会提前切换，重试阶段放宽到 20。
- 弱风控信号只参与冷却计数；强冷却（验证码挑战页）不计入弱阈值但参与 cookies 预警/切换。
- 多次达到强信号切换阈值的账号标记「预警」，仍会轮流使用避免任务停摆；是否剔除由人在 WebUI 决定。
- 删除账号后，旧浏览器不能再写回该账号；重新添加同名备注会使用新的账号 ID。账号信息损坏时会退出可用池，可通过「重新登录」修复，损坏原件保留为 `*.corrupt` 备份。
- 默认不覆盖已有转写文本；多个模型各写一列方便对比。
- 输出结果统一为 `*.processed.csv`，并额外写入 `处理状态`、`失败原因`、`尝试次数`；XLSX 输入会先读取后输出为 CSV。同名输入的产物靠 `<任务ID前8位>` 后缀区分、互不覆盖；续跑/下载时按同名前缀取 mtime 最新的一份。多工作表 XLSX 会直接报错并列出全部 sheet 名，请先拆成单表文件。
- 先扫完整个表格再分批重试失败项，默认每批 10 行。
- 续跑优先使用当前任务最新的结果；批次重试暂停后恢复，会继续重试剩余文件。批次与其中的单个任务不能同时运行。

## 默认模型

默认 `FunAudioLLM/SenseVoiceSmall`。可对比：

```text
mlx-community/whisper-large-v3-turbo
mlx-community/whisper-large-v3-mlx
FunAudioLLM/SenseVoiceSmall
```

通常 `large-v3-mlx` 更稳但更慢；`SenseVoiceSmall` 中文口语自然，首次运行会额外下载 VAD 模型。

## 测试

```bash
uv run pytest
```

## NAS 视频下载部署

项目包含面向 NAS 的视频下载 Compose，默认把 NAS 宿主机 `/volume5/视频下载` 映射为容器 `/nas/videos`，并将它设为仅下载模式的默认保存目录。完整的 SSH、账号会话复制、启动和更新步骤见 [`deploy/nas/README.md`](deploy/nas/README.md)。

本机默认仍监听 `127.0.0.1:8000`；NAS Compose 通过 `DOUYIN_WEBUI_HOST=0.0.0.0` 和 `DOUYIN_WEBUI_PORT=8000` 对外提供 WebUI。视频保存目录也可以用 `DOUYIN_VIDEO_OUTPUT_DIR` 覆盖，留空时保持本机的 `output/videos/` 默认值。

以后需要下载 NAS 上的某个 Excel 时，直接执行 `douyin-batch-download`；它会把 `视频链接` 列下载到 `/volume5/视频下载/视频下载/<Excel 文件名>/`，重复执行会跳过已完成文件并继续失败或中断的行。

下载命令会输出 `PROGRESS` JSON 进度行；运行中的任务也可用 `douyin-batch-download --status <Excel路径>` 查询。
