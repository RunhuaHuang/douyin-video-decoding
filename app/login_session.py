import sys
import time
import json
import threading
from pathlib import Path
from typing import Callable

from .browser_service import CHROME_USER_AGENT
from .config import PLAYWRIGHT_INSTALL_HINT, ensure_directories
from .cookie_accounts import CookieAccountManager


def _resolve_account_label(argv: list[str]) -> str:
    if argv:
        value = " ".join(str(item) for item in argv).strip()
        if value:
            return value

    while True:
        value = input("请输入这份 cookies 的备注（例如 主号 / 小号A）: ").strip()
        if value:
            return value
        print("备注不能为空，请重新输入。")


def _storage_state_has_cookies(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, list):
        return False
    return any(isinstance(cookie, dict) and str(cookie.get("name") or "").strip() for cookie in cookies)


def _persist_storage_state_once(context, target_path: Path) -> bool:
    try:
        context.storage_state(path=str(target_path))
        return _storage_state_has_cookies(target_path)
    except Exception:
        return False


def _cookies_look_authenticated(cookies: list[dict]) -> bool:
    names = {str(cookie.get("name") or "").strip() for cookie in cookies if isinstance(cookie, dict)}
    return (
        ("sessionid" in names or "sessionid_ss" in names)
        and ("uid_tt" in names or "uid_tt_ss" in names or "sid_tt" in names)
    )


def _context_has_authenticated_cookies(context) -> bool:
    try:
        cookies = list(context.cookies(["https://www.douyin.com/"]))
    except Exception:
        try:
            cookies = list(context.cookies())
        except Exception:
            cookies = []
    return _cookies_look_authenticated(cookies)


def login_with_label(
    account_label: str,
    *,
    wait_mode: str = "enter",
    logger: Callable[[str], None] = print,
) -> None:
    ensure_directories()
    account_manager = CookieAccountManager()
    account = account_manager.prepare_account(account_label)
    account_manager.mark_relogin_in_progress(account.account_id, True)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        account_manager.mark_relogin_in_progress(account.account_id, False)
        raise RuntimeError(f"未安装 Playwright Python 依赖，请先运行 `{PLAYWRIGHT_INSTALL_HINT}`") from exc

    logger("将打开一个 Chromium 窗口用于登录抖音。")
    if wait_mode == "browser_close":
        logger("登录完成后直接关闭该浏览器窗口，程序会自动保存会话。")
    else:
        logger("登录或完成验证码后，回到终端按回车保存会话。")
    logger(f"当前备注: {account.label}")
    logger(f"会话文件会保存到: {account.storage_state_path}")
    logger("登录窗口会强制从未登录状态开始，不会复用当前账号池里的旧会话。")
    login_recorded = False

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context(
                user_agent=CHROME_USER_AGENT,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://www.douyin.com/",
                },
            )
            try:
                context.clear_cookies()
            except Exception:
                pass
            try:
                context.add_init_script(
                    """
                    (() => {
                      try { window.localStorage.clear(); } catch (error) {}
                      try { window.sessionStorage.clear(); } catch (error) {}
                    })();
                    """
                )
            except Exception:
                pass
            page = context.new_page()
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            account.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_storage_path = account.storage_state_path.with_suffix(".tmp.json")
            temp_storage_path.unlink(missing_ok=True)

            if wait_mode == "browser_close":
                saved = False
                closed_event = threading.Event()

                def persist_state(*_args) -> None:
                    nonlocal saved
                    if saved:
                        closed_event.set()
                        return
                    saved = _persist_storage_state_once(context, temp_storage_path)
                    closed_event.set()

                try:
                    page.on("close", persist_state)
                except Exception:
                    pass
                try:
                    context.on("close", persist_state)
                except Exception:
                    pass
                try:
                    browser.on("disconnected", persist_state)
                except Exception:
                    pass

                while True:
                    if _context_has_authenticated_cookies(context):
                        persist_state()
                        break
                    if closed_event.wait(0.2):
                        break
                    try:
                        page_closed = bool(getattr(page, "is_closed", lambda: False)())
                    except Exception:
                        page_closed = True
                    try:
                        pages = list(getattr(context, "pages", []))
                    except Exception:
                        pages = []
                    try:
                        browser_connected = bool(getattr(browser, "is_connected", lambda: False)())
                    except Exception:
                        browser_connected = False

                    if page_closed or not pages or not browser_connected:
                        persist_state()
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                input("完成登录后按回车保存并关闭浏览器...")
                _persist_storage_state_once(context, temp_storage_path)
                context.close()
                browser.close()

            if _storage_state_has_cookies(temp_storage_path):
                temp_storage_path.replace(account.storage_state_path)
                account_manager.record_login(account.account_id, label=account.label)
                login_recorded = True
                logger(f"登录会话已保存到 `{account.storage_state_path.name}`。后续运行 WebUI 会自动纳入 cookies 池。")
            else:
                temp_storage_path.unlink(missing_ok=True)
                account_manager.mark_relogin_in_progress(account.account_id, False)
                logger("未检测到有效登录 cookies，已保留原有会话文件。")
                return
    except Exception:
        account_manager.mark_relogin_in_progress(account.account_id, False)
        raise

    if not login_recorded:
        account_manager.record_login(account.account_id, label=account.label)
        logger(f"登录会话已保存到 `{account.storage_state_path.name}`。后续运行 WebUI 会自动纳入 cookies 池。")


def run() -> None:
    account_label = _resolve_account_label(sys.argv[1:])
    login_with_label(account_label, wait_mode="enter", logger=print)
