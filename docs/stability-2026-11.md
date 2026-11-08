# 2026-11 跨平台下载稳定性更新

适用 Windows/macOS/Linux 的 `batch_download_huitun.py` 及优先榜单入口。
本次以统一参数入口替换写死 Windows 用户路径、7890 端口的批量脚本；保留优先榜单配置中的既有输出目录。媒体库服务与工作槽源码同时同步，但 Windows 下载表格不需要搭建 NAS 服务。

## Windows 更新与续跑（PowerShell）

先让 Windows 当前批次退出再更新，避免运行途中混用代码；Mac/NAS 已在跑的独立部署无需更新或重启。

```powershell
git pull --ff-only
uv sync --extra dev
uv run playwright install chromium
```

如 git 提示本地改动或分支分叉，保留修改，不执行 `reset --hard`。保留原输出目录、manifest、账号池与会话文件。需要系统已安装 FFmpeg 并加入 PATH。

先用实际输入、原输出根目录预览：

```powershell
uv run python batch_download_huitun.py --source "D:\表格输入" -o "D:\原视频输出" --dry-run
```

此入口扫描指定目录的第一层 xlsx；子目录请重复传 `--source`。默认输出为 `<输出根目录>/<表名>`；若以前输出子目录名不同，单表加 `--output-name "原子目录名"`，先核对预览，避免将已有文件当成缺失。

采用家中已验证的访客、无应用代理方式（新建独立文件，不覆盖原登录态）：

```powershell
uv run python -c "import json; from pathlib import Path; p=Path('tmp/windows-guest.json'); p.parent.mkdir(parents=True,exist_ok=True); p.exists() or p.write_text(json.dumps(dict(cookies=[],origins=[])),encoding='utf-8')"
uv run python batch_download_huitun.py --source "D:\表格输入" -o "D:\原视频输出" --anonymous-state "tmp/windows-guest.json" --proxy none --concurrency 2
```

若 PowerShell 的引号处理有差异，可手工新建该 JSON，内容为 `{"cookies":[],"origins":[]}`。
访客不保证所有作品可访问；验证码仍需人工。希望用登录态时省略 `--anonymous-state` 并使用原 `--state-file`，无需删除账号池。

需要代理的网络可显式传 `--proxy http://127.0.0.1:实际端口`，也可保留 `--proxy auto` 自动检测本机7890。`none` 明确禁止浏览器继承系统代理；文件传输忽略应用代理。两者均不能绕过操作系统 TUN 或软路由分流，本程序不修改这些设置。

## 修复内容与边界

- 详情接口之外增加精选页 `RENDER_DATA.app.videoDetail` 回退，核对目标作品 ID，覆盖视频、图文，拒绝推荐作品错配。
- 批量表格入口也使用独立解析子进程；单作品90秒总超时，回收该进程及 Chromium 子进程，错误保留为失败可重试，继续下一作品。下载和发布仍由父进程执行。
- 访客批量槽各用独立临时会话，30分钟在作品边界回收浏览器并清空工作会话；原访客文件、登录文件、账号池不改。登录模式不会定时清除认证Cookie。
- 图文图片先统一画布再合成，图片缺失不发布；不会自动批量替换历史文件，历史图文仍可能缺页。
- 空详情、404、含糊权限提示不直接判永久删除；旧 manifest 中误记的受限条目可重新尝试。
- 保留已完成媒体与断点记录；明确旧库目录索引存在时，断链或校验失败不退回重新下载。
- 优先榜单入口保持原输出路径与有界补跑；新批量并发按表格分配，并非旧版同表双进程协作。单表串行，最多两张表同时处理。

文件下载网络仍受实际网络、平台风控与作品权限影响，无法保证 Windows 网络与家庭网络表现相同。

## 验证

```powershell
uv sync --extra dev
$env:DOUYIN_BROWSER_SMOKE="1"
uv run pytest
```

测试涵盖本机代理、浏览器请求拦截、解析超时后的下一任务、子进程树回收、图文合成与断点续跑。GitHub Actions 使用 Windows/macOS/Linux 三平台；测试不依赖真实账号或下载真实作品。
