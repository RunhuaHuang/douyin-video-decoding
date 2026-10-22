# NAS 视频下载部署

这套 Compose 只安装视频下载所需的运行时：FastAPI、Playwright、Chromium、pandas、Excel 读取和 ffmpeg，不安装本机 Apple Silicon 转写依赖。它适合把 WebUI 和视频下载任务放在 NAS 上运行；转写继续在 Mac 上运行。下载任务按命令手动触发，不会常驻监控输入目录。

## 路径映射和命名

Compose 把 NAS 宿主机的 `/volume5/视频下载` 挂载为容器内的 `/nas/videos`，并通过 `DOUYIN_VIDEO_OUTPUT_DIR=/nas/videos` 将它设成仅下载模式的默认目录。因此 WebUI 的“仅下载视频”模式不填写保存目录时，文件会直接写入 NAS。

- WebUI 表格批量下载：`/volume5/视频下载/<表格文件名>/row0001_<标题>.mp4`。
- 独立下载器：在容器内执行 `douyin-video-dl "链接" -o /nas/videos`，沿用程序原有的 `视频-ID.mp4` / `图文-ID.mp4` 命名。
- Excel 手动批量下载：`/volume5/视频下载/视频下载/<Excel 文件名>/视频-ID.mp4` 或 `图文-ID.mp4`。
- `douyin-dl` 仍沿用标题命名规则；若使用它也请显式传 `-o /nas/videos`。

`tmp/` 和 `output/` 是项目目录旁的持久化目录，分别保存账号池/任务状态和处理结果。它们不会随镜像重建丢失。

## 通过 nasopencode 连接和部署

`nasopencode` 已配置 `nas` SSH 别名（局域网）和 `nas-tailscale`（外网 Tailscale）。先确认连接：

```bash
ssh nas 'echo NAS SSH OK; uname -m; test -d /volume5/视频下载'
```

用现有的项目同步脚本把代码放到 NAS 的 `/home/a/Agents/projects`：

```bash
NAS_HOST_OVERRIDE=nas \
  "/Users/runhua/Runhua MBP/Python/nasopencode/scripts/ocprojectsync.sh" \
  push "/Users/runhua/Runhua MBP/Python/抖音文案提取工具"
```

同步后在 NAS 构建并启动（项目目录名带中文时请保留引号）：

```bash
ssh nas 'cd "/home/a/Agents/projects/抖音文案提取工具/deploy/nas" && sudo docker compose up -d --build'
ssh nas 'sudo docker compose -f "/home/a/Agents/projects/抖音文案提取工具/deploy/nas/docker-compose.yml" ps'
```

浏览器访问 `http://192.168.100.188:8000`；通过 Tailscale 时访问 `http://100.68.215.29:8000`。Compose 已同时绑定本机回环、NAS 局域网地址和当前 `nasopencode` 配置中的 Tailscale 地址。

## 账号会话

NAS 容器是无头 Chromium，不能自动弹出扫码窗口。首次使用前，在 Mac 上完成登录后，将账号池 JSON 文件复制到 NAS 项目目录：

```bash
# 用 tar + SSH 传输，避免 macOS scp 对中文远程路径的兼容问题。
COPYFILE_DISABLE=1 tar --no-xattrs -czf - \
  -C "/Users/runhua/Runhua MBP/Python/抖音文案提取工具/tmp/browser/accounts" . \
  | ssh nas 'sudo docker exec -i douyin-video tar xzf - -C /app/tmp/browser/accounts'
ssh nas 'sudo docker compose -f "/home/a/Agents/projects/抖音文案提取工具/deploy/nas/docker-compose.yml" restart'
```

这些 JSON 是抖音登录会话，按私密文件保存；不要提交到 Git 或放进 Docker 镜像。账号池为空时，先在 Mac 运行 `./login "备注"` 扫码，再复制新增的 JSON。

## 直接下载命令

你直接告诉我某个 Excel 路径后，我会执行同一个命令。也可以手动执行：

```bash
ssh nas 'sudo docker exec douyin-video douyin-batch-download \
  "/volume5/视频下载/目标视频列表/分享总榜.xlsx"'
```

表格下载默认使用 2 个线程；每个线程使用不同的账号 cookies。可通过 `--workers 1` 到 `--workers 8` 调整，线程数不会超过账号池中可用 cookies 数量。例如：

```bash
ssh nas 'sudo docker exec douyin-video douyin-batch-download --workers 2 \
  "/volume5/视频下载/目标视频列表/分享总榜.xlsx"'
```

容器会把宿主机路径自动映射为容器路径，实际输出为 `/volume5/视频下载/视频下载/分享总榜/`。任务目录里的 `.douyin-download-progress.json` 记录本次状态；重新执行同一命令时，已经存在且有效的 `视频-ID.mp4` / `图文-ID.mp4` 会跳过，剩余项目继续下载。退出码为 `0` 表示全部完成，`1` 表示仍有失败项，直接再次执行即可重试。

下载过程中命令会输出 `PROGRESS` JSON 行，agent 可以直接读取当前处理数、成功数、失败数和百分比。也可以在另一个终端查询：

```bash
ssh nas 'sudo docker exec douyin-video douyin-batch-download --status \
  "/volume5/视频下载/目标视频列表/分享总榜.xlsx"'
```

在 NAS 容器内运行独立命名下载器：

```bash
ssh nas 'sudo docker exec douyin-video douyin-video-dl \
  "https://www.douyin.com/video/视频ID" -o /nas/videos'
```

多个链接或表格也可继续传给同一个命令。WebUI 批量任务则选择“仅下载视频”；如果填写保存目录，填写容器内路径 `/nas/videos`，不要填写宿主机路径 `/volume5/视频下载`。

## 更新和检查

### 新版灰豚冷却下载入口

镜像包含 `batch_download_huitun.py` 及共享会话模块。此入口使用全线程协同冷却：验证码后关闭浏览器，按 180 秒、300 秒冷却重试；会话文件更新可提前恢复。并发按表格分配，单表使用一个工作线程。

输入、输出由每次任务指定；单表可用 `--output-name` 指定输出子目录。先预览，再去掉 `--dry-run` 下载：

```bash
ssh nas 'sudo docker exec douyin-video uv run --no-project --python /opt/venv/bin/python python /app/batch_download_huitun.py \
  --source "/nas/videos/目标视频列表/点赞星图总榜.xlsx" \
  -o "/nas/videos/视频下载" --output-name "点赞总榜-星图" \
  --state-file /app/tmp/browser/huitun-zan-xingtu.json --proxy none --dry-run'
```

首次正式运行从账号池复制可用会话，也可通过 `--account` 指定。续跑会核对已有文件并跳过，输出目录保存 `manifest.json`。日志应重定向到本次任务独立文件，后台运行可使用 `docker exec -d`。

若已确认旧 Cookie 导致空白页，灰豚入口改用 `--anonymous-state /app/tmp/browser/huitun-guest.json`，替代账号模式的 `--state-file` / `--account`。它不会复制账号池，也会拒绝包含登录凭据的文件；人工验证时也传同一个 `--anonymous-state`。部署验证准备的访客状态与旧登录状态分开保存，不会自动开启下载任务。

旧版将账号拉黑、权限限制等提示记入 `deleted` 的记录，新版会在续跑时重查，不需要清空整个 manifest；已有有效视频继续跳过。

基础镜像仓库不可达时，可用 `deploy/nas/Dockerfile.update` 基于已有运行镜像离线更新。先为旧镜像打备份标签，以 `--build-arg BASE_IMAGE=<备份标签>` 构建；在构建目录的 `wheels/` 中放置缺失依赖的 wheel，并按锁文件核对哈希，构建时禁用索引并检查全部运行依赖。系统库或浏览器依赖改变时仍须走完整 Dockerfile。容器代码不挂载宿主机目录，必须重建/切换镜像后才生效。

代码更新后重新同步并重建：

```bash
NAS_HOST_OVERRIDE=nas \
  "/Users/runhua/Runhua MBP/Python/nasopencode/scripts/ocprojectsync.sh" \
  push "/Users/runhua/Runhua MBP/Python/抖音文案提取工具"
ssh nas 'cd "/home/a/Agents/projects/抖音文案提取工具/deploy/nas" && sudo docker compose up -d --build'
```

只读检查：

```bash
ssh nas 'sudo docker compose -f "/home/a/Agents/projects/抖音文案提取工具/deploy/nas/docker-compose.yml" ps'
ssh nas 'sudo docker logs douyin-video --tail 80'
ssh nas 'test -w /volume5/视频下载 && echo video-target-writable'
```
