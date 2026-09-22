"""命令行下载抖音视频：douyin-dl <链接或分享文本> [更多链接...] [--out 目录]。

与 WebUI 批量链路共用 DouyinService：浏览器优先、账号池会话、
音视频分离流自动合并。适合单个/少量视频的快速下载和脚本调用。
"""
import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from .browser_service import PlaywrightFallback
from .config import OUTPUT_DIR
from .cookie_accounts import CookieAccountManager
from .douyin_service import DouyinService, ParsedVideo
from .media_service import FfmpegService

DEFAULT_CLI_OUTPUT_DIR = OUTPUT_DIR.parent / "downloads"


def sanitize_title(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80] or "video"


def build_service(
    account_manager: CookieAccountManager,
    service_factory: Callable[[], DouyinService] = DouyinService,
) -> tuple[DouyinService, Optional[str]]:
    """创建已挂载账号池会话的 DouyinService；无账号时也能跑（无 cookie 直连/裸浏览器）。"""
    service = service_factory()
    account = account_manager.select_current_or_first_available_account()
    label = None
    if account is not None:
        service.browser_resolver = PlaywrightFallback(
            storage_state_path=account.storage_state_path,
            request_coordinator=service.request_coordinator,
        )
        label = account.label
    return service, label


def download_one(service: DouyinService, share_text: str, out_dir: Path) -> Path:
    """下载单个视频并返回保存路径；失败抛异常。"""
    video = service.parse_share_text(share_text)
    if video.is_deleted:
        raise RuntimeError("视频已删除")
    if not video.download_urls:
        raise RuntimeError("未解析到可下载地址")

    out_dir.mkdir(parents=True, exist_ok=True)
    # 临时文件名唯一化：固定名会让并发 CLI/多进程互踩半成品，失败残留也分不清归属
    fd, video_name = tempfile.mkstemp(prefix="_cli_", suffix=".mp4", dir=out_dir)
    os.close(fd)
    fd, audio_name = tempfile.mkstemp(prefix="_cli_", suffix=".m4a", dir=out_dir)
    os.close(fd)
    video_path = Path(video_name)
    audio_path = Path(audio_name)
    try:
        # 下载前清掉 mkstemp 建出的空文件，保持“文件存在即下载产物”的原语义
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
        service.download_video(video, video_path, audio_path)

        source = None
        if video_path.exists() and audio_path.exists():
            try:
                streams = FfmpegService().probe_media(video_path).get("streams") or []
                has_video = any(stream.get("codec_type") == "video" for stream in streams)
                has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
            except Exception:
                has_video = False
                has_audio = False
            source = video_path if has_video and has_audio else audio_path
        elif video_path.exists():
            source = video_path
        elif audio_path.exists():
            source = audio_path
        if source is None or not source.exists():
            raise RuntimeError("下载完成但没有产出文件")
        suffix = source.suffix or ".mp4"
        base = sanitize_title(video.title)
        final_path = out_dir / f"{base}{suffix}"
        if final_path.exists():
            # 同名标题（或截断后同名）不覆盖，改用 标题_视频ID
            final_path = out_dir / f"{base}_{video.video_id}{suffix}"
            counter = 2
            while final_path.exists():
                final_path = out_dir / f"{base}_{video.video_id}_{counter}{suffix}"
                counter += 1
        source.replace(final_path)
        return final_path
    finally:
        # 半成品与未选中的下载文件统一清理，失败路径也不残留
        for leftover in (video_path, audio_path):
            leftover.unlink(missing_ok=True)


def main(
    argv: Optional[List[str]] = None,
    *,
    account_manager: Optional[CookieAccountManager] = None,
    service_factory: Callable[[], DouyinService] = DouyinService,
) -> int:
    parser = argparse.ArgumentParser(
        prog="douyin-dl",
        description="下载抖音视频（浏览器优先、账号池会话、音视频分离流自动合并）",
    )
    parser.add_argument("urls", nargs="*", help="抖音链接或整段分享文本（可多个）")
    parser.add_argument(
        "-o",
        "--out",
        default=str(DEFAULT_CLI_OUTPUT_DIR),
        help=f"保存目录（默认 {DEFAULT_CLI_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--list-accounts",
        action="store_true",
        help="列出账号池里的可用会话后退出",
    )
    args = parser.parse_args(argv)

    if not args.urls and not args.list_accounts:
        parser.print_help()
        return 2

    account_manager = account_manager or CookieAccountManager()

    if args.list_accounts:
        accounts = account_manager.list_available_accounts()
        if not accounts:
            print("账号池为空。运行 ./login \"备注\" 添加账号（需要人工扫码）。")
            return 1
        for account in accounts:
            print(f"{account.label}\t{account.storage_state_path}")
        return 0

    available = account_manager.list_available_accounts()
    if available:
        print(f"账号池可用会话: {len(available)} 份（自动选用第一份）")
    else:
        print("警告: 账号池为空，浏览器会话无登录态，部分视频可能解析失败", file=sys.stderr)

    out_dir = Path(args.out).expanduser()
    service, label = build_service(account_manager, service_factory)
    if label:
        print(f"使用账号: {label}")

    succeeded: List[Path] = []
    failed: List[str] = []
    try:
        for url in args.urls:
            try:
                path = download_one(service, url, out_dir)
            except Exception as exc:
                failed.append(f"{url}: {exc}")
                print(f"失败 {url}: {exc}", file=sys.stderr)
            else:
                succeeded.append(path)
                print(f"完成 {url} -> {path} ({path.stat().st_size} 字节)")
    finally:
        service.close()

    print(f"共 {len(succeeded)} 成功 / {len(failed)} 失败，保存在 {out_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
