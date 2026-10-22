"""扫码登录：uv run python login_douyin.py [--state-file 路径] [--proxy auto|none|URL]."""

import argparse
import sys

from app.huitun_session import capture_session, is_authenticated
from app.huitun_support import add_session_arguments, positive_int, resolve_proxy


def run_login(argv=None):
    parser = argparse.ArgumentParser(description="抖音扫码登录助手（Windows / macOS）")
    add_session_arguments(parser)
    parser.add_argument("--timeout", type=positive_int, default=300, help="等待人工扫码的秒数")
    args = parser.parse_args(argv)
    try:
        proxy = resolve_proxy(args.proxy)
    except ValueError as exc:
        parser.error(str(exc))
    return capture_session(args.state_file.expanduser().resolve(), proxy, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(run_login())
