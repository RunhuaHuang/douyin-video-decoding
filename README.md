# 抖音文案提取工具

一个独立的本地 Python Web 项目，只保留这一条链路：

1. 从 CSV 或 XLSX 中读取你指定的抖音链接列
2. 解析抖音链接并下载视频
3. 用 FFmpeg 抽取音频
4. 用 MLX Whisper 或 SenseVoice 转写
5. 把转写文本写回同一行的指定视频文本列

## 运行要求

- macOS Apple Silicon
- `ffmpeg` 已安装并可在终端直接运行
- Python 3.9+
- `uv` 已安装

如果没装 `ffmpeg`：

```bash
brew install ffmpeg
```

## 安装依赖

```bash
cd "/Users/runhua/Desktop/文本转录/douyin-creator-toolkit/抖音文案提取工具"
uv sync
uv run playwright install chromium
```

## 启动 WebUI

```bash
uv run douyin-csv-webui
```

启动后打开：

```text
http://127.0.0.1:8000
```

## 登录抖音会话

程序不会自动帮你“登录抖音账号”。如果你想让浏览器兜底和直连接口复用同一份稳定会话，先执行一次：

```bash
uv run douyin-csv-login
```

执行后会打开一个 Chromium 窗口：

1. 在窗口里登录抖音，或者完成验证码
2. 完成后回到终端按回车
3. 程序会把会话保存到 `tmp/browser/douyin-storage-state.json`

后续再运行：

```bash
uv run douyin-csv-webui
```

WebUI 和浏览器兜底都会自动复用这份已保存的会话。

## 快捷命令

在项目目录里可以直接使用：

```bash
./run
```

它会先强制重新登录抖音、刷新会话，再启动 WebUI，并自动打开浏览器访问：

```text
http://127.0.0.1:8000
```

如果你想重新登录抖音并替换当前 cookies / 浏览器状态：

```bash
./login
```

它会先清除 `tmp/browser/douyin-storage-state.json`，再打开登录窗口让你重新登录。

## 使用方式

1. 一次上传一个或多个 CSV / XLSX 文件
2. 统一选择哪一列是抖音链接列
3. 填写要写回的“视频文本列”列名
4. 填写一个或多个模型名
5. 点击开始执行
6. 在批次页查看每个文件的进度，并分别下载当前结果文件

## 说明

- 你可以把整段抖音分享文本放进单元格，不一定非要纯 URL。
- 支持一次性上传多个文件，程序会按照上传顺序逐个处理。
- 如果“视频文本列”不存在，程序会自动新建这一列。
- 默认不会覆盖已有文本；如果要覆盖，勾选页面上的复选框。
- 如果填写多个模型，每个模型会写入一个单独的输出列，方便横向对比效果。
- SenseVoice 首次运行除了主模型外，还会额外下载一个 VAD 模型。
- 输出 CSV 会额外写入 `处理状态`、`失败原因`、`尝试次数`。
- 如果输入是 XLSX，输出也会保持为 XLSX。
- 删除视频会直接写入 `已删除`，不再重复重试。
- 连续两次命中 `HTTP 403 Forbidden x2, 浏览器兜底失败: 浏览器兜底下载失败: HTTP 403 x2` 会直接标记为 `无法下载`，不再重复重试。
- 图文 `note` 链接会直接标记为 `非视频`，不参与重试。
- 即使原始链接写着 `/video/`，如果浏览器最终落到 `/note/` 页面，也会按 `非视频` 处理。
- 连续两次命中 `返回内容不是有效视频, HTTP 404 Not Found, 浏览器兜底失败: 浏览器兜底下载失败: 返回内容不是有效媒体, HTTP 404` 也会直接标记为 `非视频`。
- 程序会先扫完整个 CSV，再分批重试失败项；默认每批最多处理 10 行。
- 单文件页面支持暂停、继续、重试失败项；批次页面支持按文件查看状态并下载当前结果。
- 下载层会优先尝试多个直链地址和多组浏览器请求头；如果仍然被拦截，会自动切到 Playwright 浏览器会话兜底。
- 输出文件保存在 `output/spreadsheet/`。
- 上传和中间文件会放在 `tmp/spreadsheets/`。

## 默认模型

默认使用：

```text
FunAudioLLM/SenseVoiceSmall
```

你也可以在页面上手动改成别的 MLX Whisper 或 SenseVoice 模型名。

建议对比：

```text
mlx-community/whisper-large-v3-turbo
mlx-community/whisper-large-v3-mlx
FunAudioLLM/SenseVoiceSmall
```

通常 `large-v3-mlx` 会比 `turbo` 更稳一些，但速度更慢。`SenseVoiceSmall` 在中文口语上往往值得一起对比。

## 测试

```bash
uv run pytest
```
