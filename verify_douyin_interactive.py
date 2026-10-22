"""人工验证码助手；使用与批量下载相同的会话文件和浏览器代理。"""

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from app.huitun_session import capture_session
from app.huitun_support import (
    add_session_arguments, positive_int, prepare_anonymous_state_file, prepare_state_file, resolve_proxy,
)


def run_interactive_verify(argv=None):
    parser = argparse.ArgumentParser(description="抖音验证码人工验证助手（Windows / macOS）")
    parser.add_argument("url", nargs="?", help="本次触发验证码的视频或图文完整链接")
    add_session_arguments(parser)
    parser.add_argument("--anonymous-state", type=Path, help="使用独立访客会话，不从账号池补入登录状态")
    parser.add_argument("--timeout", type=positive_int, default=300, help="等待人工验证的秒数")
    args = parser.parse_args(argv)
    if not args.url and sys.stdin.isatty():
        args.url = input("请粘贴触发验证码的抖音完整链接: ").strip()
    if not args.url:
        parser.error("请传入本次触发验证码的完整抖音链接")
    url = urlsplit(args.url)
    if url.scheme not in {"http", "https"} or not (url.hostname == "douyin.com" or (url.hostname or "").endswith(".douyin.com")):
        parser.error("请提供抖音页面的完整 http(s) 链接")
    try:
        proxy = resolve_proxy(args.proxy)
        state_file = (prepare_anonymous_state_file(args.anonymous_state) if args.anonymous_state
                      else prepare_state_file(args.state_file))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.error(str(exc))
    return capture_session(state_file, proxy, verify_url=args.url, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(run_interactive_verify())
