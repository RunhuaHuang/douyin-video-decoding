"""Human-operated login/verification for both Windows and macOS."""

import time
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from .huitun_support import browser_launch_options, browser_user_agent, save_state


def is_authenticated(cookies):
    return any(cookie.get("name") in {"sessionid", "sessionid_ss"} and cookie.get("value")
               for cookie in cookies if isinstance(cookie, dict))


def verification_complete(page, detail_seen):
    url = urlsplit(page.url)
    title = page.title() or ""
    if not (url.hostname == "douyin.com" or (url.hostname or "").endswith(".douyin.com")):
        return False
    if not title or "验证码" in title or any(token in page.url.lower() for token in ("verify", "captcha")):
        return False
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if any(token in frame.url.lower() for token in ("verifycenter", "captcha")):
            # Douyin keeps an invisible rmc-nocaptcha iframe on normal video
            # pages. Only an actually displayed challenge blocks completion.
            if frame.frame_element().is_visible():
                return False
    return detail_seen or page.locator("video").count() > 0


def capture_session(state_file, proxy, *, verify_url=None, timeout=300):
    """Save only a confirmed result while the browser context is still alive."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**browser_launch_options(proxy, headless=False))
        try:
            options = {
                "user_agent": browser_user_agent(browser.version),
                "viewport": {"width": 1280, "height": 850},
                "locale": "zh-CN",
            }
            if verify_url:
                options["storage_state"] = str(state_file)
            context = browser.new_context(**options)
            page = context.new_page()
            detail_seen = False

            def on_response(response):
                nonlocal detail_seen
                if "aweme/v1/web/aweme/detail" in response.url:
                    try:
                        detail_seen = bool(response.json().get("aweme_detail"))
                    except Exception:
                        pass

            page.on("response", on_response)
            try:
                page.goto(verify_url or "https://www.douyin.com/", wait_until="commit", timeout=15000)
            except Exception as exc:
                print(f"页面加载提示: {exc}", flush=True)
            if not verify_url:
                try:
                    page.get_by_text("登录", exact=True).first.click(timeout=3000)
                except Exception:
                    pass
            print("请在浏览器中手动完成验证码。" if verify_url else "请使用抖音 App 扫码登录。", flush=True)
            print(f"最多等待 {timeout} 秒；确认成功后自动保存。提前关闭窗口不会覆盖原会话。", flush=True)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if page.is_closed() or not browser.is_connected():
                    break
                try:
                    complete = (verification_complete(page, detail_seen) if verify_url
                                else is_authenticated(context.cookies()))
                    if complete:
                        # storage_state visits iframe origins in a temporary tab.
                        # Polling it steals focus every second during a CAPTCHA;
                        # collect the full state only after the user is finished.
                        state = context.storage_state()
                        if state.get("cookies"):
                            save_state(state_file, state)
                            print(f"会话已保存: {state_file}", flush=True)
                            return 0
                except PlaywrightError:
                    # Completing a challenge navigates the page. Its old execution
                    # context can disappear between the URL/title/DOM reads.
                    # Keep this browser alive and check the destination next time.
                    if page.is_closed() or not browser.is_connected():
                        break
                    complete = False
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    if page.is_closed() or not browser.is_connected():
                        break
                    raise
            print("未确认完成，原会话保持不变。", flush=True)
            return 1
        finally:
            try:
                browser.close()
            except Exception:
                pass
