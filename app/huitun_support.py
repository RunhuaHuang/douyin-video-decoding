"""Shared paths, sessions and browser policy for the standalone Huitun tools."""

import argparse
import json
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .account_storage import write_storage_state
from .config import PROJECT_ROOT
from .cookie_accounts import CookieAccountManager


def default_output_dir(platform=None, home=None):
    platform = platform or sys.platform
    home = home or Path.home()
    if platform == "win32":
        return Path("D:/抖音视频下载")
    return home / ("Movies" if platform == "darwin" else "Videos") / "抖音视频下载"


def default_source_dirs():
    desktop = Path.home() / "Desktop"
    return [desktop / "灰豚数据/带货视频榜-总销售额排序-每月", desktop / "带货视频榜-分类"]


def default_state_file(platform=None):
    if (platform or sys.platform) == "win32":
        return default_output_dir("win32") / "douyin_state.json"
    return PROJECT_ROOT / "tmp/browser/huitun_state.json"


def browser_user_agent(version="128.0.0.0", platform=None):
    platform = platform or sys.platform
    system = {
        "win32": "Windows NT 10.0; Win64; x64",
        "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    }.get(platform, "X11; Linux x86_64")
    return f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"


def add_session_arguments(parser):
    parser.add_argument("--state-file", type=Path, default=default_state_file(), help="三个脚本共用的会话文件")
    parser.add_argument("--proxy", default="auto", help="浏览器代理：auto 检测本机 7890；none 禁用；或 http://主机:端口")


def resolve_proxy(value):
    if value.lower() == "none":
        return None
    if value.lower() == "auto":
        try:
            with socket.create_connection(("127.0.0.1", 7890), timeout=0.3):
                return "http://127.0.0.1:7890"
        except OSError:
            return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise ValueError("--proxy 请使用 auto、none 或带协议的代理地址")
    return value


def browser_launch_options(proxy, *, headless):
    options = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    if proxy:
        options["proxy"] = {"server": proxy}
    else:
        # Chromium otherwise inherits the OS proxy even when HTTPX does not.
        options["args"].append("--no-proxy-server")
    return options


def read_state(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(state, dict) and isinstance(state.get("cookies"), list) and state["cookies"]:
        return state
    return None


def save_state(path, state):
    if not write_storage_state(path, json.dumps(state, ensure_ascii=False)):
        raise RuntimeError(f"会话已被删除，不能覆盖: {path}")


def prepare_state_file(path, account_label=None):
    """Seed a dedicated session from the account pool without modifying that pool."""
    path = path.expanduser().resolve()
    if not account_label and read_state(path):
        return path
    manager = CookieAccountManager()
    if account_label:
        account = next((item for item in manager.list_available_accounts()
                        if account_label in {item.account_id, item.label}), None)
    else:
        account = manager.select_current_or_first_available_account()
    state = read_state(account.storage_state_path) if account else None
    if not state:
        raise RuntimeError("没有可用会话，请先由人工执行扫码登录脚本，或通过 --account 指定可用账号。")
    save_state(path, state)
    print(f"已复用账号池会话: {account.label} -> {path}", flush=True)
    return path


def prepare_anonymous_state_file(path):
    """Validate an explicitly chosen guest state without consulting the account pool."""
    path = path.expanduser().resolve()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取独立访客会话: {path}") from exc
    if (not isinstance(state, dict)
            or not isinstance(state.get("cookies"), list)
            or not isinstance(state.get("origins"), list)
            or any(not isinstance(cookie, dict) for cookie in state["cookies"])):
        raise ValueError("独立访客会话格式无效，需包含 cookies 和 origins 数组")
    login_names = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt", "uid_tt_ss"}
    if any(cookie.get("name") in login_names and cookie.get("value") for cookie in state["cookies"]):
        raise ValueError("独立访客会话包含登录凭据，请使用未登录窗口的会话")
    return path


def should_block_browser_request(url, resource_type):
    if resource_type in {"image", "media", "font"}:
        return True
    parsed = urlsplit(url)
    host, path = (parsed.hostname or "").lower(), parsed.path.lower()
    # MediaSource players use fetch/XHR; resource_type alone misses those bytes.
    return (
        any(host == domain or host.endswith("." + domain) for domain in ("douyinvod.com", "bytevod.com"))
        or path.endswith((".mp4", ".m4a", ".mp3", ".webm", ".m3u8", ".mpd", ".m4s", ".ts"))
        or "media-video-" in path or "media-audio-" in path
        or "/aweme/v1/play" in path
        or "mime_type=video" in parsed.query.lower()
        or "mime_type=audio" in parsed.query.lower()
    )


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number
