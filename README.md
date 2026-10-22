# 抖音文案提取工具

本地 Python 工具，链路：读取表格里的抖音链接 → 下载视频（浏览器优先 + 账号池会话）→ ffmpeg 抽音频 → MLX Whisper / SenseVoice 转写 → 写回表格。

三种用法，按需选择：

| 场景 | 入口 |
|---|---|
| 下载单个/少量视频 | 命令行 `uv run douyin-dl <链接>` |
| 人工批量（浏览器界面） | Mac：`./run`；Windows：`run.bat` |
| 无人值守批量（脚本/Agent） | WebUI 的 JSON API，见「无头批量」 |

## 前置自检

```bash
cd "/Users/runhua/Runhua MBP/Python/抖音文案提取工具"
uv sync --extra dev                      # 依赖（含测试工具）
uv run playwright install chromium       # 浏览器兜底必需
uv run douyin-dl --list-accounts         # 账号池：列出可用会话，空则见「登录抖音会话」
uv run pytest                            # 全量测试
```

- ffmpeg：转写和长视频（音视频分离流）合并需要；`brew install ffmpeg`
- 账号池为空时浏览器会话没有登录态，大部分视频会解析失败——先找人工执行 `./login`

基础安装只包含下载和 WebUI 依赖。需要转写时运行 `uv sync --extra asr`，并用 `uv run --extra asr douyin-csv-webui` 启动；Mac 的 `./run` 会自动启用此依赖组。MLX Whisper 仅在 Apple Silicon Mac 安装，Windows/Linux 使用 SenseVoice。Python 最低 3.10，建议统一使用 3.12。

## Windows 安装与使用

目标环境为 Windows 10/11 x64。把项目放在可写目录（例如 `D:\DouyinTool`；也支持中文和空格），不要复制 Mac 的 `.venv`。在 PowerShell 或 CMD 中安装：

```powershell
winget install --id astral-sh.uv -e
winget install --id Gyan.FFmpeg -e
```

安装后重新打开终端，进入项目根目录：

```powershell
.\setup.bat                 # 安装 Python 3.12、下载依赖和 Chromium，检查 FFmpeg/FFprobe
.\login.bat "主号"          # 人工扫码，检测到登录后自动保存
.\run.bat                   # 启动 WebUI 并自动打开浏览器；Ctrl+C 停止
```

也可以直接在 WebUI 首页点击「新增账号」扫码。下载时选择「仅下载视频」，或用命令行：

```powershell
uv run douyin-dl "抖音链接" -o "D:\视频下载"
.\download_videos.bat "链接表.xlsx" -o "D:\视频下载"
```

`download_videos.bat` 支持图文合成 MP4；图文合成、长视频音视频合并和转写都需要 FFmpeg。上述脚本会自动切换到项目根目录，设置 Python UTF-8 模式并传递退出码。端口已占用时启动器会报错，请先停止旧服务，或在 PowerShell 设置 `$env:DOUYIN_WEBUI_PORT = "8001"` 后重启。

需要文案转写时：

```powershell
.\setup.bat asr
.\run.bat asr
```

选择默认的 `FunAudioLLM/SenseVoiceSmall`，首次使用需要下载模型和 VAD 模型。程序自动选择可用的 CUDA、MPS 或 CPU；GPU 加载/运行失败后回退 CPU。Windows 上 MPS 不可用；NVIDIA 加速还需要匹配驱动的 CUDA 版 PyTorch，普通安装不保证提供 CUDA。需要自定义 GPU 包时按 [PyTorch 官方安装说明](https://pytorch.org/get-started/locally/) 配置当前 `.venv`，随后用 `uv run --no-sync douyin-launch` 启动，避免重新同步替换自定义包。

Windows 上不显示 MLX 模型预设；手动输入 MLX 模型时会提示改用 SenseVoice。纯下载无需安装任何转写模型。

- 账号备注、上传文件和 CLI 视频标题会处理 Windows 保留名称（如 `CON`、`NUL`）和非法字符。
- 结果 CSV 默认带 UTF-8 BOM，便于 Excel 读取中文；写入时若被 Excel 占用，会保留原结果并提示关闭文件后重试。
- 避免把项目放在多层嵌套的长路径下。可在 WebUI 指定较短的输出目录；Windows 长路径策略及网络盘权限仍由系统管理。
- 账号会话可以从原机器的 `tmp/browser/accounts/` 复制，但换设备或网络后可能需要重新扫码；Chromium 需在 Windows 重新安装。

仓库提供 Windows/macOS/Linux CI 测试，涵盖跨进程账号锁、Chromium 工作线程和本地图文合成。CI 不使用真实抖音账号；正式部署仍需在目标机器验证扫码、短视频、长视频和图文下载。

## 灰豚 Excel 批量下载（Windows / macOS）

本次稳定性修复、运行方式和 Windows 更新步骤见 [2026-10 稳定性说明](docs/stability-2026-10.md)。长期批量任务建议从 `--concurrency 1` 开始。

`batch_download_huitun.py` 是独立的多表下载入口，支持视频与图文，不做文案转写。输入和输出目录按每次任务指定，中文、空格和外接盘路径均可。仍使用现有 WebUI 时，继续使用上面的 WebUI 入口。

在仓库根目录运行（两种系统使用相同的 Python 命令）：

```bash
# 先预览：不启动浏览器、不下载、不创建输出目录
uv run python batch_download_huitun.py --source "/输入目录" -o "/输出目录" --dry-run

# 正式下载：默认两张表并发；重复运行会核对现有文件并补跑失败/缺失项
uv run python batch_download_huitun.py --source "/输入目录" -o "/输出目录" --concurrency 2

# 单个表格、多个输入目录、指定月份或小范围试跑
uv run python batch_download_huitun.py "/输入/榜单.xlsx" -o "/输出目录" --test 2
uv run python batch_download_huitun.py --source "/目录一" --source "/目录二" -o "/输出目录" --month 2025-10
```

目录只扫描直接包含的 `.xlsx`，忽略 Excel 的 `~$` 临时文件；多目录中存在同名输出表时会报错，避免互相覆盖。输出为 `<输出目录>/<表名>/视频-ID.mp4` 或 `图文-ID.mp4`，同时保存 `manifest.json`。有失败行或仅试跑部分行时不写完成标记；旧版留下的 `_COMPLETED.flag` 也不会阻止补跑。退出码 `0` 表示本次范围没有失败项，`1` 表示有失败，`2` 表示参数/预检错误。

视频先下载到本机临时目录，完成后通过输出目录内的独立临时文件替换目标文件；本机需保留至少一条视频大小的空闲空间。共享盘出现短暂的资源忙或超时会有限重试，清理临时文件失败不会中断整表。若记录文件持续不可读或内容损坏，任务会停止并保留原记录，避免将已有进度覆盖为空。持续断网仍需先恢复共享盘连接后续跑。

Mac 可用 `./启动批量下载.command --source "/输入目录" -o "/输出目录"`；Windows 对应 `启动批量下载.bat`。所有启动脚本会切换到仓库根目录，不再依赖嵌套的 `douyin-video-decoding/.venv` 路径。只需下载依赖和 Chromium，并安装 FFmpeg；不需要安装 ASR。

未指定输出时，Mac 默认 `~/Movies/抖音视频下载`，Windows 保留 `D:\抖音视频下载`。未指定输入时，尝试当前用户桌面的 `灰豚数据/带货视频榜-总销售额排序-每月` 和 `带货视频榜-分类`；agent 应始终使用用户本次提供的输入、输出参数。

### 会话与人工验证

Mac 默认共享会话为仓库的 `tmp/browser/huitun_state.json`；Windows 保留 `D:\抖音视频下载\douyin_state.json`。三个脚本均支持 `--state-file`；自定义时必须传入同一文件。批量下载首次运行可以自动复制现有账号池中的可用会话，也可以用 `--account "备注"` 指定账号；复制和后续人工验证不会修改原账号池文件，不会自动轮换账号。

旧登录 Cookie 可能导致作品直达页空白，即使账号首页仍显示已登录。已确认需要访客模式时，批量入口可显式使用 `--anonymous-state "独立访客.json"`：只读取该未登录会话，不从账号池补入 Cookie，且不能与 `--account` 同用。访客文件必须包含 `cookies`、`origins` 数组，可以是全新的空状态，也可以是未登录浏览器保存的会话；含登录凭据时会拒绝启动。人工验证助手也支持相同 `--anonymous-state` 参数。先备份旧会话，再使用独立路径，不要删除账号池。

“你已拉黑对方”、权限/隐私限制以及“因作品权限或已被删除”等含糊提示会保留为可重试失败。旧 manifest 中误标为 `deleted` 的这类记录会在续跑时重新解析；只有明确删除、下架或不存在的作品继续跳过。

```bash
# 尚无可用会话时，由人扫码；成功后自动保存
uv run python login_douyin.py --proxy auto

# 遇到验证码时，使用日志里的实际 video/note 链接，由人完成验证
uv run python verify_douyin_interactive.py "https://www.douyin.com/video/实际ID" --proxy auto
```

Mac 也提供 `扫码登录.command`、`完成验证.command`，Windows 对应同名 `.bat`。验证助手不再打开写死的视频；未传链接时，可在交互终端粘贴。关闭窗口或超时且未确认成功时，会保留原会话。下载任务在冷却期间检测到共享会话文件更新后，会提前重试。

### 代理行为

三个脚本使用相同的 `--proxy` 参数：`auto` 检测本机 `127.0.0.1:7890`，`none` 禁用浏览器代理，也可指定 `http://127.0.0.1:其他端口`。扫码、验证与下载解析应使用同一设置。解析浏览器拦截常见媒体 CDN、媒体路径及通过 fetch/XHR 请求的媒体，拿到详情后关闭页面，保留浏览器会话供下次解析复用。

文件下载使用 `trust_env=False` 忽略应用层/系统代理设置。**若希望视频不消耗代理节点流量，请关闭 TUN／虚拟网卡，或在代理客户端配置 DIRECT 分流；Python 参数不能绕过操作系统的 TUN 路由。** 本工具不会修改系统代理或 Clash 配置。验证码冷却是有界重试，不保证平台一定解除限制。

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
uv sync --extra dev
uv run pytest
```

## NAS 视频下载部署

项目包含面向 NAS 的视频下载 Compose。部署时使用 `nasopencode` 中已经配置好的 SSH 别名连接 NAS，再把本仓库同步到 NAS 后构建容器。

固定路径映射如下：

| 用途 | Mac / NAS 宿主机 | 容器内 |
|---|---|---|
| 项目目录 | `/home/a/Agents/projects/抖音文案提取工具` | `/app` |
| 视频存储 | `/volume5/视频下载` | `/nas/videos` |
| 账号池、任务状态 | 项目目录下的 `tmp/` | `/app/tmp` |
| WebUI 结果 | 项目目录下的 `output/` | `/app/output` |

### 连接 NAS

`nasopencode` 的 `config/nas.env` 应该已经配置 `nas`（局域网）和 `nas-tailscale`（Tailscale）两个 SSH 别名。先做只读检查：

```bash
ssh nas 'echo NAS SSH OK; uname -m; test -d "/volume5/视频下载"'
# 不在局域网时使用：
ssh nas-tailscale 'echo NAS SSH OK; uname -m'
```

### 首次部署或更新代码

不要直接在 NAS 手动复制零散文件。使用 `ocprojectsync.sh` 将整个项目同步到 NAS，再由 Compose 构建镜像：

```bash
NAS_HOST_OVERRIDE=nas \
  "/Users/runhua/Runhua MBP/Python/nasopencode/scripts/ocprojectsync.sh" \
  push "/Users/runhua/Runhua MBP/Python/抖音文案提取工具"

ssh nas 'cd "/home/a/Agents/projects/抖音文案提取工具/deploy/nas" && sudo docker compose up -d --build'
ssh nas 'sudo docker compose -f "/home/a/Agents/projects/抖音文案提取工具/deploy/nas/docker-compose.yml" ps'
```

Compose 会启动 `douyin-video` 容器，默认监听 NAS 的 `8000` 端口：局域网访问 `http://192.168.100.188:8000`，Tailscale 访问 `http://100.68.215.29:8000`。容器是无头 Chromium，视频下载可以直接运行，但不能在 NAS 上弹出扫码窗口。

### 复制账号会话

先在 Mac 上人工扫码登录并确认账号池可用：

```bash
./login "主号"
uv run douyin-dl --list-accounts
```

然后用 `tar + SSH` 将账号 JSON 复制到容器；中文路径下不要改用普通 `scp`：

```bash
COPYFILE_DISABLE=1 tar --no-xattrs -czf - \
  -C "/Users/runhua/Runhua MBP/Python/抖音文案提取工具/tmp/browser/accounts" . \
  | ssh nas 'sudo docker exec -i douyin-video tar xzf - -C /app/tmp/browser/accounts'
ssh nas 'sudo docker compose -f "/home/a/Agents/projects/抖音文案提取工具/deploy/nas/docker-compose.yml" restart'
```

账号 JSON 是登录凭据，不要提交 Git、写入 Docker 镜像或粘贴到聊天中。账号池为空时，先在 Mac 完成扫码，再执行复制步骤。

### 手动启动 Excel 下载

NAS 任务不会自动监控目录。指定 Excel 后，程序读取其中的 `视频链接` 列，将结果写到 `/volume5/视频下载/视频下载/<Excel 文件名>/`，重复执行会跳过已有完整文件并继续剩余行：

```bash
ssh nas 'sudo docker exec douyin-video douyin-batch-download \
  "/volume5/视频下载/目标视频列表/分享总榜.xlsx"'
```

默认使用 2 个下载线程，每个线程使用不同 cookies；可按账号池和 NAS 负载调整为 1～8：

```bash
ssh nas 'sudo docker exec douyin-video douyin-batch-download --workers 2 \
  "/volume5/视频下载/目标视频列表/分享总榜.xlsx"'
```

查询任务进度、容器健康状态和日志：

```bash
ssh nas 'sudo docker exec douyin-video douyin-batch-download --status \
  "/volume5/视频下载/目标视频列表/分享总榜.xlsx"'
ssh nas 'sudo docker compose -f "/home/a/Agents/projects/抖音文案提取工具/deploy/nas/docker-compose.yml" ps'
ssh nas 'sudo docker logs douyin-video --tail 80'
```

单链接下载器在容器内使用 `/nas/videos`，不要把 NAS 宿主机路径传给容器命令：

```bash
ssh nas 'sudo docker exec douyin-video douyin-video-dl \
  "https://www.douyin.com/video/视频ID" -o /nas/videos'
```

更完整的 Compose 参数和故障排查见 [`deploy/nas/README.md`](deploy/nas/README.md)。
