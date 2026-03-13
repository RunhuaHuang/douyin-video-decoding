import sys
import time
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

            if wait_mode == "browser_close":
                while True:
                    try:
                        context.storage_state(path=str(account.storage_state_path))
                    except Exception:
                        pass
                    try:
                        if not browser.is_connected():
                            break
                    except Exception:
                        break
                    time.sleep(1)
            else:
                input("完成登录后按回车保存并关闭浏览器...")
                context.storage_state(path=str(account.storage_state_path))
                context.close()
                browser.close()
    except Exception:
        account_manager.mark_relogin_in_progress(account.account_id, False)
        raise

    account_manager.record_login(account.account_id, label=account.label)
    logger(f"登录会话已保存到 `{account.storage_state_path.name}`。后续运行 WebUI 会自动纳入 cookies 池。")


def run() -> None:
    account_label = _resolve_account_label(sys.argv[1:])
    login_with_label(account_label, wait_mode="enter", logger=print)
