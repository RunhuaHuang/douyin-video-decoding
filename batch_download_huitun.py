import os
import re
import sys
import time
import json
import random
import queue
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import httpx
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

# 路径配置
SOURCE_DIRS = [
    Path(r"C:\Users\cuhksz\Desktop\灰豚数据\带货视频榜-总销售额排序-每月"),
    Path(r"C:\Users\cuhksz\Desktop\带货视频榜-分类"),
]
DEST_DIR = Path(r"D:\抖音视频下载")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.douyin.com/",
}

def extract_video_id(url: str) -> Optional[str]:
    """从抖音链接中提取纯数字 ID"""
    if not url:
        return None
    match = re.search(r"/(?:video|note)/(\d+)", str(url))
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{18,21})\b", str(url))
    return match.group(1) if match else None

class WafCoordinator:
    """全局风控与休眠协调器：确保多线程在遇到滑块拦截时协同静默，不跨线程调用 Playwright"""
    _lock = threading.Lock()
    _in_cooldown = False
    _cooldown_event = threading.Event()
    _cooldown_event.set()  # set = 正常运行状态; clear = 全局静默冷却中

    @classmethod
    def trigger_cooldown(cls, initiator_tag: str, duration: float = 180.0) -> bool:
        """
        触发全局统一静默冷却。
        调用该方法的线程已经在自己的线程内关闭了自己的浏览器。
        该方法负责设置全局事件、计时、30s检测 douyin_state.json，并最终恢复事件。
        """
        is_initiator = False
        with cls._lock:
            if not cls._in_cooldown:
                cls._in_cooldown = True
                cls._cooldown_event.clear()
                is_initiator = True
                print(f"\n🚨 [{initiator_tag}] 检测到滑块/验证码风控！启动【全线程统一静默自愈】（休眠 {int(duration)} 秒）...", flush=True)
                print(f"👉 所有后台浏览器将切断连接（完全零流量以让风控自然消退）。", flush=True)
                print(f"👉 若在电脑前可随时双击桌面【完成抖音验证.bat】加速解除；若不在电脑前，静默倒计时结束后将自动恢复。\n", flush=True)

        if not is_initiator:
            cls._cooldown_event.wait()
            return False

        state_p = Path(r"D:\抖音视频下载\douyin_state.json")
        init_mtime = state_p.stat().st_mtime if state_p.exists() else 0
        end_time = time.time() + duration
        broken_early = False

        while time.time() < end_time:
            time.sleep(30.0)
            cur_mtime = state_p.stat().st_mtime if state_p.exists() else 0
            if cur_mtime > init_mtime:
                print(f"\n⚡ [风控自愈系统] 检测到用户已手动完成滑块验证！立即全员提前唤醒，恢复满速下载！\n", flush=True)
                broken_early = True
                break

        with cls._lock:
            cls._in_cooldown = False
            cls._cooldown_event.set()
            if not broken_early:
                print(f"\n⚡ [风控自愈系统] 静默期已满 {int(duration)} 秒，服务端频控已充分消退，全员唤醒并重启浏览器！\n", flush=True)

        return broken_early

    @classmethod
    def check_and_wait(cls, worker_tag: str, worker: 'TableWorker'):
        """如果全局正处于静默期，在当前线程内安全关闭当前 worker 的浏览器，并等待冷却结束"""
        if not cls._cooldown_event.is_set():
            worker.close_browser()
            cls._cooldown_event.wait()

class TableWorker:
    """独立的工作者实例，拥有自己的浏览器和 HTTP 客户端"""
    def __init__(self, worker_id: int, dest_dir: Path):
        self.worker_id = worker_id
        self.dest_dir = dest_dir
        self.http_client = httpx.Client(
            headers=HEADERS,
            timeout=httpx.Timeout(connect=15.0, read=90.0, write=90.0, pool=90.0),
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=30)
        )
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.resolve_count = 0

    def start_browser(self):
        if self.page and not self.page.is_closed():
            return
        if not self.playwright:
            self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ]
        )
        context_kwargs = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 720},
            "locale": "zh-CN",
        }
        state_file = Path(r"D:\抖音视频下载\douyin_state.json")
        if state_file.exists():
            context_kwargs["storage_state"] = str(state_file)
        self.context = self.browser.new_context(**context_kwargs)
        self.page = self.context.new_page()
        # 拦截图片和字体以提升解析效率
        self.page.route("**/*", lambda route: (
            route.abort() if route.request.resource_type in ["image", "media", "font"]
            else route.continue_()
        ))
        try:
            self.page.goto("https://www.douyin.com/", wait_until="commit", timeout=8000)
            self.page.wait_for_timeout(1500)
        except Exception:
            pass
        self.resolve_count = 0

    def close_browser(self):
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def restart_browser(self):
        self.close_browser()
        self.start_browser()

    def close(self):
        self.close_browser()
        try:
            self.http_client.close()
        except Exception:
            pass

    def resolve_video_info(self, video_id: str, max_retries: int = 2) -> Tuple[Optional[dict], Optional[str]]:
        WafCoordinator.check_and_wait(f"工作线程 {self.worker_id}", self)
        self.resolve_count += 1
        if self.resolve_count > 100:
            self.restart_browser()

        urls_to_try = [
            f"https://www.douyin.com/video/{video_id}",
            f"https://www.douyin.com/note/{video_id}"
        ]

        for attempt in range(max_retries):
            for target_url in urls_to_try:
                WafCoordinator.check_and_wait(f"工作线程 {self.worker_id}", self)
                detail_box = []

                def on_response(res):
                    if "aweme/v1/web/aweme/detail" in res.url:
                        try:
                            detail_box.append(res.json())
                        except Exception:
                            pass

                try:
                    self.start_browser()
                    self.page.on("response", on_response)
                    try:
                        self.page.goto(target_url, wait_until="commit", timeout=10000)
                    except Exception:
                        pass

                    for _ in range(55):
                        if detail_box:
                            break
                        self.page.wait_for_timeout(100)

                    self.page.remove_listener("response", on_response)

                    if detail_box:
                        data = detail_box[0]
                        det = data.get("aweme_detail")
                        if det:
                            return det, None
                        filter_det = data.get("filter_detail") or {}
                        notice = filter_det.get("detail_msg") or filter_det.get("notice")
                        if notice:
                            return None, f"已删除/不可见({notice})"
                        return None, "未捕获到有效视频数据(空详情)"

                    try:
                        cur_title = self.page.title() or ""
                        cur_url = self.page.url or ""
                    except Exception:
                        cur_title, cur_url = "", ""
                    if "验证码" in cur_title or "verify" in cur_url or "captcha" in cur_url:
                        return None, "触发验证码风控"
                except Exception as e:
                    try:
                        self.page.remove_listener("response", on_response)
                    except Exception:
                        pass

            if attempt < max_retries - 1:
                time.sleep(2.0)
                self.restart_browser()

        return None, "未捕获到视频元数据"

    def download_video_stream(self, cdn_urls: List[str], target_path: Path) -> bool:
        """下载普通视频流"""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_name(f"{target_path.stem}.tmp")

        for cdn_url in cdn_urls:
            try:
                with self.http_client.stream("GET", cdn_url) as resp:
                    if resp.status_code == 200:
                        with open(tmp_path, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=65536):
                                f.write(chunk)
                        if tmp_path.exists() and tmp_path.stat().st_size > 10240:
                            tmp_path.replace(target_path)
                            return True
            except Exception:
                pass
            finally:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)

        return False

    def synthesize_note_mp4(self, img_urls: List[str], bgm_urls: List[str], target_path: Path) -> bool:
        """图文 note 合成 720x1280 竖屏 MP4（1 秒 1 张图，内嵌原版音频）"""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=f"dy_note_{target_path.stem}_") as tmpdir:
                td = Path(tmpdir)
                local_pngs = []
                for i, img_u in enumerate(img_urls):
                    try:
                        r = self.http_client.get(img_u)
                        if r.status_code == 200:
                            raw_img = td / f"img_{i:03d}.webp"
                            raw_img.write_bytes(r.content)
                            png_img = td / f"img_{i:03d}.png"
                            subprocess.run(
                                ["ffmpeg", "-y", "-i", str(raw_img), str(png_img)],
                                check=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=20,
                            )
                            local_pngs.append(png_img)
                    except Exception:
                        pass

                if not local_pngs:
                    return False

                num_imgs = len(local_pngs)
                dur = float(num_imgs)

                # 下载 BGM
                bgm_file = None
                for bgm_u in bgm_urls:
                    try:
                        r_a = self.http_client.get(bgm_u)
                        if r_a.status_code == 200 and len(r_a.content) > 1024:
                            bgm_file = td / "bgm.m4a"
                            bgm_file.write_bytes(r_a.content)
                            break
                    except Exception:
                        pass

                temp_out = td / "note_out.mp4"
                cmd = ["ffmpeg", "-y"]
                if num_imgs == 1:
                    cmd.extend(["-loop", "1", "-i", str(local_pngs[0].resolve())])
                else:
                    concat_txt = td / "concat.txt"
                    with open(concat_txt, "w", encoding="utf-8") as f:
                        for p in local_pngs:
                            f.write(f"file '{p.name}'\n")
                            f.write("duration 1.0\n")
                        f.write(f"file '{local_pngs[-1].name}'\n")
                    cmd.extend(["-f", "concat", "-safe", "0", "-i", str(concat_txt)])

                if bgm_file and bgm_file.exists():
                    cmd.extend(["-i", str(bgm_file)])
                else:
                    cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])

                cmd.extend([
                    "-vf", "fps=30,scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:black",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k",
                    "-t", str(dur),
                    str(temp_out),
                ])
                subprocess.run(
                    cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=90,
                )

                if temp_out.exists() and temp_out.stat().st_size > 10240:
                    tmp_final = target_path.with_name(f"{target_path.stem}.tmp")
                    shutil.copyfile(temp_out, tmp_final)
                    tmp_final.replace(target_path)
                    return True
        except Exception as e:
            return False

        return False

    def process_excel(self, excel_path: Path, max_limit: Optional[int] = None):
        subfolder_name = excel_path.stem
        folder_dest = self.dest_dir / subfolder_name
        folder_dest.mkdir(parents=True, exist_ok=True)
        completion_flag = folder_dest / "_COMPLETED.flag"
        tag = f"[工作线程 {self.worker_id} | {subfolder_name}]"

        if completion_flag.exists():
            print(f"\n{tag} 该表格之前已全部处理完成（检测到 _COMPLETED.flag），立即跳过！", flush=True)
            return

        manifest_path = folder_dest / "manifest.json"
        print(f"\n{tag} 开始处理表格: {excel_path.name}", flush=True)

        manifest = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}

        try:
            df = pd.read_excel(excel_path)
        except Exception as e:
            print(f"{tag} 读取 Excel 失败: {e}", flush=True)
            return

        link_col = None
        for col in ["播放地址", "视频链接", "链接", "url", "URL"]:
            if col in df.columns:
                link_col = col
                break
        if not link_col:
            for col in df.columns:
                samples = df[col].dropna().astype(str).head(10)
                if any("douyin.com" in s for s in samples):
                    link_col = col
                    break

        if not link_col:
            print(f"{tag} 未找到链接列，跳过！", flush=True)
            return

        total_rows = len(df)
        if max_limit:
            df = df.head(max_limit)

        print(f"{tag} 找到链接列 [{link_col}]，待处理 {len(df)} 行（总计 {total_rows} 行）", flush=True)

        def save_manifest():
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        success_count = 0
        skip_count = 0
        deleted_count = 0
        fail_count = 0
        consecutive_fails = 0

        for idx, row in df.iterrows():
            rank = row.get("排名", idx + 1)
            raw_url = str(row.get(link_col, "")).strip()
            video_id = extract_video_id(raw_url)

            if not video_id:
                continue

            # 1. 检查物理文件是否已存在（无论是 视频-{video_id}.mp4 还是 图文-{video_id}.mp4）
            vid_target = folder_dest / f"视频-{video_id}.mp4"
            note_target = folder_dest / f"图文-{video_id}.mp4"

            if vid_target.exists() and vid_target.stat().st_size > 10240:
                skip_count += 1
                consecutive_fails = 0
                manifest[video_id] = {"status": "success", "file": vid_target.name, "type": "video"}
                continue
            if note_target.exists() and note_target.stat().st_size > 10240:
                skip_count += 1
                consecutive_fails = 0
                manifest[video_id] = {"status": "success", "file": note_target.name, "type": "note"}
                continue

            # 2. 检查 manifest 是否已确认原片已删除/不可见
            if manifest.get(video_id, {}).get("status") == "deleted":
                deleted_count += 1
                consecutive_fails = 0
                continue

            # 3. 解析视频元数据
            print(f"{tag} [{idx+1}/{len(df)}] 解析第{rank}名 (ID: {video_id})...", flush=True)
            det, err = self.resolve_video_info(video_id)

            if err:
                if "已删除" in err:
                    deleted_count += 1
                    consecutive_fails = 0
                    manifest[video_id] = {"status": "deleted", "reason": err}
                    print(f"{tag}   -> ⚠️ {err}", flush=True)
                elif "触发验证码风控" in err:
                    # 遇到风控：立即协同全线程统一静默自愈（首轮 180 秒静默），并重试当前视频
                    manual_ok = WafCoordinator.trigger_cooldown(tag, duration=180.0)
                    self.start_browser()
                    det, err = self.resolve_video_info(video_id)

                    if err and "触发验证码风控" in err and not manual_ok:
                        # 若首轮静默后频控仍在衰减中，触发二次深度静默（300 秒）
                        manual_ok = WafCoordinator.trigger_cooldown(tag, duration=300.0)
                        self.start_browser()
                        det, err = self.resolve_video_info(video_id)

                    # 如果经历了两轮静默（总计 8 分钟全域零流量）依然报错，再跳入下一行防止永久挂起
                    if err:
                        fail_count += 1
                        consecutive_fails += 1
                        manifest[video_id] = {"status": "failed", "reason": err}
                        print(f"{tag}   -> ❌ 多轮静默后仍受限，跳过该行: {err}", flush=True)
                        if (idx + 1) % 5 == 0:
                            save_manifest()
                        continue
                else:
                    fail_count += 1
                    consecutive_fails += 1
                    manifest[video_id] = {"status": "failed", "reason": err}
                    print(f"{tag}   -> ❌ 解析失败: {err}", flush=True)
                    if consecutive_fails >= 5:
                        print(f"{tag} ⚠️ 检测到连续 {consecutive_fails} 次解析失败，冷却 20 秒并重启浏览器...", flush=True)
                        self.close_browser()
                        time.sleep(20.0)
                        self.start_browser()
                        consecutive_fails = 0
                    else:
                        time.sleep(random.uniform(1.0, 2.0))
                if (idx + 1) % 5 == 0:
                    save_manifest()
                continue

            consecutive_fails = 0
            ok = False

            # 4. 判断类型：图文 (aweme_type == 68 或含 images) 还是 视频
            aweme_type = det.get("aweme_type")
            images = det.get("images")
            is_note = (aweme_type == 68) or (bool(images) and isinstance(images, list) and len(images) > 0)

            if is_note:
                # 图文 note
                img_urls = []
                if isinstance(images, list):
                    for img_item in images:
                        if isinstance(img_item, dict) and img_item.get("url_list"):
                            img_urls.append(img_item["url_list"][-1])
                bgm_urls = det.get("music", {}).get("play_url", {}).get("url_list", [])

                if not img_urls:
                    fail_count += 1
                    manifest[video_id] = {"status": "failed", "reason": "未找到图文图片链接"}
                    print(f"{tag}   -> ❌ 图文未找到有效图片链接", flush=True)
                    continue

                ok = self.synthesize_note_mp4(img_urls, bgm_urls, note_target)
                if ok:
                    success_count += 1
                    size_mb = note_target.stat().st_size / (1024 * 1024)
                    manifest[video_id] = {"status": "success", "file": note_target.name, "type": "note", "size_mb": round(size_mb, 2)}
                    print(f"{tag}   -> 🖼️✅ 图文合成成功: {note_target.name} ({len(img_urls)}图, {size_mb:.1f} MB)", flush=True)
                else:
                    fail_count += 1
                    manifest[video_id] = {"status": "failed", "reason": "图文合成失败"}
                    print(f"{tag}   -> ❌ 图文合成失败", flush=True)

            else:
                # 普通视频
                play_addr = det.get("video", {}).get("play_addr", {})
                cdn_urls = play_addr.get("url_list", [])
                if not cdn_urls:
                    fail_count += 1
                    manifest[video_id] = {"status": "failed", "reason": "未找到可用视频流"}
                    print(f"{tag}   -> ❌ 未找到可用视频流", flush=True)
                    continue

                ok = self.download_video_stream(cdn_urls, vid_target)
                if ok:
                    success_count += 1
                    size_mb = vid_target.stat().st_size / (1024 * 1024)
                    manifest[video_id] = {"status": "success", "file": vid_target.name, "type": "video", "size_mb": round(size_mb, 2)}
                    print(f"{tag}   -> 🎬✅ 视频下载成功: {vid_target.name} ({size_mb:.1f} MB)", flush=True)
                else:
                    fail_count += 1
                    manifest[video_id] = {"status": "failed", "reason": "视频流写入失败"}
                    print(f"{tag}   -> ❌ 视频流写入失败", flush=True)

            if (idx + 1) % 5 == 0 or ok:
                save_manifest()

            # 正常处理完成后的随机微抖动延迟，平滑流量（防止并发请求瞬时峰值触发风控）
            time.sleep(random.uniform(1.2, 2.2))

        save_manifest()
        completion_flag.touch(exist_ok=True)
        print(f"\n{tag} 表格处理完毕: 成功 {success_count} | 跳过 {skip_count} | 已删除 {deleted_count} | 失败 {fail_count}\n", flush=True)


def worker_thread_loop(worker_id: int, task_queue: queue.Queue, dest_dir: Path, max_limit: Optional[int]):
    worker = TableWorker(worker_id, dest_dir)
    try:
        while True:
            try:
                excel_file = task_queue.get_nowait()
            except queue.Empty:
                break

            try:
                worker.process_excel(excel_file, max_limit=max_limit)
            except Exception as e:
                print(f"[工作线程 {worker_id}] 处理表格 {excel_file.name} 异常: {e}", flush=True)
            finally:
                task_queue.task_done()
    finally:
        worker.close()
        print(f"[工作线程 {worker_id}] 全部任务结束，已退出并清理资源。", flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="灰豚数据带货视频双线程轮询批量下载器")
    parser.add_argument("--test", type=int, default=None, help="每个表格仅测试前 N 个视频")
    parser.add_argument("--month", type=str, default=None, help="指定月份文件名匹配，例如 2025-10")
    parser.add_argument("--concurrency", type=int, default=2, help="同时下载的表格数量（默认 2 个）")
    args = parser.parse_args()

    files = []
    for s_dir in SOURCE_DIRS:
        if s_dir.exists():
            found = sorted(list(s_dir.glob("*.xlsx")))
            files.extend(found)

    if not files:
        print("未在源目录中找到 Excel 文件！")
        return

    if args.month:
        files = [f for f in files if args.month in f.name]

    print(f"=======================================================")
    print(f"共发现 {len(files)} 个待处理表格任务，准备进入队列轮询调度:")
    for i, f in enumerate(files[:15], 1):
        print(f"  {i}. {f.name} ({f.parent.name})")
    if len(files) > 15:
        print(f"  ... 以及后续 {len(files) - 15} 个分类表格")
    print(f"并发表格数: {args.concurrency}（同时下载 {args.concurrency} 张表，完成一张接续下一张）")
    print(f"保存目录: {DEST_DIR}")
    print(f"命名规范: 视频-视频ID.mp4 / 图文-视频ID.mp4")
    print(f"=======================================================\n", flush=True)

    task_queue = queue.Queue()
    for f in files:
        task_queue.put(f)

    threads = []
    concurrency = min(args.concurrency, len(files))
    for i in range(1, concurrency + 1):
        t = threading.Thread(
            target=worker_thread_loop,
            args=(i, task_queue, DEST_DIR, args.test),
            name=f"TableWorker-{i}",
            daemon=False
        )
        threads.append(t)
        t.start()
        # 错开启动，避免并发初始化竞争
        time.sleep(1.0)

    for t in threads:
        t.join()

    print("\n🎉 所有表格视频已全部处理完成！", flush=True)


if __name__ == "__main__":
    main()
