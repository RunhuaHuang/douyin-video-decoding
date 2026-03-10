from .browser_service import CHROME_USER_AGENT
from .config import PLAYWRIGHT_INSTALL_HINT, PLAYWRIGHT_STORAGE_STATE_PATH, ensure_directories


def run() -> None:
    ensure_directories()

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(f"未安装 Playwright Python 依赖，请先运行 `{PLAYWRIGHT_INSTALL_HINT}`") from exc

    print("将打开一个 Chromium 窗口用于登录抖音。")
    print("登录或完成验证码后，回到终端按回车保存会话。")
    print(f"会话文件会保存到: {PLAYWRIGHT_STORAGE_STATE_PATH}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context_kwargs = {
            "user_agent": CHROME_USER_AGENT,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1440, "height": 900},
            "extra_http_headers": {
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.douyin.com/",
            },
        }
        if PLAYWRIGHT_STORAGE_STATE_PATH.exists():
            context_kwargs["storage_state"] = str(PLAYWRIGHT_STORAGE_STATE_PATH)

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        input("完成登录后按回车保存并关闭浏览器...")
        PLAYWRIGHT_STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(PLAYWRIGHT_STORAGE_STATE_PATH))
        context.close()
        browser.close()

    print("登录会话已保存。后续运行 WebUI 会自动复用这份会话。")
