import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

STATE_FILE = Path(r"D:\抖音视频下载\douyin_state.json")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

def run_interactive_verify():
    print("=" * 60)
    print("【抖音验证码人工验证助手】")
    print("正在启动可视化 Chromium 浏览器窗口...")
    print("👉 请在弹出的浏览器中，手动拖动滑块完成拼图验证！")
    print("👉 完成后窗口会自动检测并保存，您也可以在完成后直接关闭浏览器窗口。")
    print("=" * 60, flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        context_kwargs = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 800},
            "locale": "zh-CN",
        }
        if STATE_FILE.exists():
            context_kwargs["storage_state"] = str(STATE_FILE)

        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        # 打开目前触发了风控的视频页面
        target_url = "https://www.douyin.com/video/7667889682693290106"
        try:
            page.goto(target_url, timeout=30000)
        except Exception as e:
            print("页面初次加载提示:", e)

        print("\n页面已打开，等待验证码加载与用户拖动滑块...")
        # 强制等待至少 4 秒，确保验证码 iframe 或验证码中间页完全渲染出来
        time.sleep(4.0)

        max_wait = 300  # 最多等待 5 分钟
        start_time = time.time()
        verified = False

        while time.time() - start_time < max_wait:
            if page.is_closed():
                print("\n检测到用户已关闭浏览器窗口，正在保存当前 Cookie 凭据...")
                verified = True
                break

            try:
                title = page.title() or ""
                url = page.url or ""

                has_captcha = False
                for frame in page.frames:
                    if "verifycenter" in frame.url or "captcha" in frame.url:
                        has_captcha = True
                        break

                # 必须明确不是验证码中间页，且没有验证码 frame，且页面包含抖音视频内容
                if not has_captcha and "验证码中间页" not in title and "验证码" not in title and len(title) > 0 and title != "blank":
                    print(f"\n🎉 检测到滑块验证已成功完成！当前页面: 《{title}》")
                    time.sleep(2.0)
                    verified = True
                    break
            except Exception:
                pass

            time.sleep(1.5)
            remaining = int(max_wait - (time.time() - start_time))
            if remaining % 15 == 0:
                print(f"等待完成滑块验证中... (剩余 {remaining} 秒，完成后可直接关闭窗口)", flush=True)

        if verified:
            try:
                context.storage_state(path=str(STATE_FILE))
                print(f"✅ 会话凭证已成功保存至: {STATE_FILE}")
            except Exception as e:
                print(f"保存凭证异常: {e}")
            time.sleep(2.0)
        else:
            print("⚠️ 未检测到完成验证，或等待超时。")

        try:
            browser.close()
        except Exception:
            pass

if __name__ == "__main__":
    run_interactive_verify()
