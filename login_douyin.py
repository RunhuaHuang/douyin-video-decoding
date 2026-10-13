import sys
import time
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

STATE_FILE = Path(r"D:\抖音视频下载\douyin_state.json")
BACKUP_STATE_FILE = Path(r"C:\Users\cuhksz\Desktop\抖音视频下载\douyin-video-decoding\tmp\browser\storage_state.json")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

def is_authenticated(cookies: list[dict]) -> bool:
    names = {str(c.get("name", "")).strip() for c in cookies}
    return "sessionid" in names or "sessionid_ss" in names

def run_login():
    print("=" * 65)
    print("【抖音账号扫码登录助手】")
    print("正在启动可视化浏览器窗口，请使用【抖音 App 扫一扫】登录...")
    print("=" * 65, flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 850},
            locale="zh-CN"
        )
        page = context.new_page()

        print("正在打开抖音首页...", flush=True)
        try:
            page.goto("https://www.douyin.com/", timeout=30000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print("页面跳转提示:", e)

        # 尝试点击页面右上角的“登录”按钮以弹出二维码
        try:
            login_btn = page.locator("text='登录'").first
            if login_btn.is_visible():
                login_btn.click()
                print("已自动点击登录按钮弹出登录弹窗。", flush=True)
        except Exception:
            pass

        print("\n👉 请在弹出的浏览器中，使用手机【抖音 App -> 右上角扫一扫】扫码登录！")
        print("等待登录成功中（最多等待 300 秒）...\n", flush=True)

        max_wait = 300
        start_time = time.time()
        logged_in = False

        while time.time() - start_time < max_wait:
            if page.is_closed():
                print("检测到浏览器窗口已被关闭。")
                break

            cookies = context.cookies()
            if is_authenticated(cookies):
                print("\n🎉 检测到已成功登录抖音账号！")
                logged_in = True
                break

            time.sleep(1.0)
            elapsed = int(time.time() - start_time)
            if elapsed > 0 and elapsed % 15 == 0:
                remaining = max_wait - elapsed
                print(f"等待扫码登录中... (剩余 {remaining} 秒)", flush=True)

        if logged_in:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(STATE_FILE))
            print(f"✅ 登录凭据已成功保存至: {STATE_FILE}")
            try:
                BACKUP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(BACKUP_STATE_FILE))
            except Exception:
                pass
            time.sleep(2.0)
        else:
            # 即使没检测到 sessionid，如果用户关闭了窗口或操作了，也尝试保存当前 cookies
            cookies = context.cookies()
            if len(cookies) > 5:
                context.storage_state(path=str(STATE_FILE))
                print("已保存当前浏览器会话。")

        try:
            browser.close()
        except Exception:
            pass

if __name__ == "__main__":
    run_login()
