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
from app.huitun_support import (
    add_session_arguments, browser_launch_options, browser_user_agent,
    default_output_dir, default_source_dirs, default_state_file, positive_int,
    prepare_anonymous_state_file, prepare_state_file, resolve_proxy, should_block_browser_request,
)
from app.path_utils import portable_name
from app.huitun_storage import publish_local_file, read_manifest, write_manifest

# 路径配置
SOURCE_DIRS = default_source_dirs()
DEST_DIR = default_output_dir()

USER_AGENT = browser_user_agent()
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


def is_permanent_unavailability(reason: str) -> bool:
    """Account restrictions and ambiguous permission notices must be retried."""
    message = str(reason or "").strip()
    for prefix in ("已删除/不可见(", "访问受限("):
        if message.startswith(prefix):
            message = message[len(prefix):].removesuffix(")")
            break
    if any(word in message for word in (
        "拉黑", "权限", "隐私", "私密", "好友", "登录", "验证", "风控", "地区", "暂", "审核",
    )):
        return False
    return any(word in message for word in ("已删除", "已被删除", "不存在", "已下架", "已失效"))


def detail_notice_error(notice: str) -> str:
    label = "已删除/不可见" if is_permanent_unavailability(notice) else "访问受限"
    return f"{label}({notice})"


class WafCoordinator:
    """全局风控与休眠协调器：确保多线程在遇到滑块拦截时协同静默，不跨线程调用 Playwright"""
    _lock = threading.Lock()
    _in_cooldown = False
    _cooldown_event = threading.Event()
    _cooldown_event.set()  # set = 正常运行状态; clear = 全局静默冷却中

    @classmethod
    def trigger_cooldown(cls, initiator_tag: str, state_file: Path, duration: float = 180.0) -> bool:
        """
        触发全局统一静默冷却。
        调用该方法的线程已经在自己的线程内关闭了自己的浏览器。
        该方法负责设置全局事件、计时、检测共享会话文件，并最终恢复事件。
        """
        is_initiator = False
        with cls._lock:
            if not cls._in_cooldown:
                cls._in_cooldown = True
                cls._cooldown_event.clear()
                is_initiator = True
                print(f"\n🚨 [{initiator_tag}] 检测到滑块/验证码风控！启动【全线程统一静默自愈】（休眠 {int(duration)} 秒）...", flush=True)
                print("👉 其他线程将在下一检查点暂停新请求并关闭浏览器。", flush=True)
                print(f"👉 可由人工运行完成验证脚本（同一会话路径；访客模式用 --anonymous-state），保存后提前恢复。会话: {state_file}", flush=True)
                print(f"👉 【无人值守模式】：静默倒计时结束后将自动全员恢复推进。\n", flush=True)

        if not is_initiator:
            cls._cooldown_event.wait()
            return False

        def state_mtime():
            try:
                return state_file.stat().st_mtime_ns
            except OSError:
                return 0

        init_mtime = state_mtime()
        end_time = time.monotonic() + duration
        broken_early = False
        try:
            while time.monotonic() < end_time:
                time.sleep(min(1.0, max(0, end_time - time.monotonic())))
                if state_mtime() != init_mtime and state_file.exists():
                    print("\n⚡ 检测到会话已更新，提前恢复并重新尝试。\n", flush=True)
                    broken_early = True
                    break
        finally:
            with cls._lock:
                cls._in_cooldown = False
                cls._cooldown_event.set()
        if not broken_early:
            print(f"\n⚡ 冷却 {int(duration)} 秒结束，重新尝试解析。\n", flush=True)

        return broken_early

    @classmethod
    def check_and_wait(cls, worker_tag: str, worker: 'TableWorker'):
        """如果全局正处于静默期，在当前线程内安全关闭当前 worker 的浏览器，并等待冷却结束"""
        if not cls._cooldown_event.is_set():
            worker.close_browser()
            cls._cooldown_event.wait()
            # 唤醒错峰机制：线程 2 及以上按 worker_id 阶梯多随机等待，彻底避免多线程在同一瞬间同时打向抖音
            if worker.worker_id > 1:
                stagger_sec = (worker.worker_id - 1) * 2.5 + random.uniform(1.0, 3.0)
                print(f"[{worker_tag}] ⚡ 触发唤醒错峰保护，交错等待 {stagger_sec:.1f} 秒后再请求...", flush=True)
                time.sleep(stagger_sec)

class TableWorker:
    """独立的工作者实例，拥有自己的浏览器和 HTTP 客户端"""
    def __init__(self, worker_id: int, dest_dir: Path, state_file: Optional[Path] = None, proxy: Optional[str] = None, output_name: Optional[str] = None):
        self.worker_id = worker_id
        self.dest_dir = dest_dir
        self.output_name = output_name
        self.state_file = state_file or default_state_file()
        self.proxy = proxy
        # 忽略应用层/系统代理设置；操作系统 TUN 接管仍需在代理客户端关闭或分流。
        self.http_client = httpx.Client(
            headers=HEADERS,
            timeout=httpx.Timeout(connect=15.0, read=90.0, write=90.0, pool=90.0),
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=30),
            trust_env=False
        )
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.resolve_count = 0

    def start_browser(self):
        if self.page and not self.page.is_closed():
            return
        if self.context:
            self.page = self.context.new_page()
            return
        if not self.playwright:
            self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(**browser_launch_options(self.proxy, headless=True))
        user_agent = browser_user_agent(self.browser.version)
        self.http_client.headers["User-Agent"] = user_agent
        context_kwargs = {
            "user_agent": user_agent,
            "viewport": {"width": 1280, "height": 720},
            "locale": "zh-CN",
            "service_workers": "block",
        }
        if self.state_file.exists():
            context_kwargs["storage_state"] = str(self.state_file)
        self.context = self.browser.new_context(**context_kwargs)
        self.context.route("**/*", lambda route: (
            route.abort() if should_block_browser_request(route.request.url, route.request.resource_type)
            else route.continue_()
        ))
        self.page = self.context.new_page()
        try:
            self.page.goto("https://www.douyin.com/", wait_until="commit", timeout=10000)
            self.captcha_hit = False
            for _ in range(35):
                try:
                    cur_t = self.page.title() or ""
                    if "验证码" in cur_t or "verify" in self.page.url:
                        self.captcha_hit = True
                        break
                    if "抖音" in cur_t:
                        self.captcha_hit = False
                        break
                except Exception:
                    pass
                self.page.wait_for_timeout(100)
        except Exception:
            self.captcha_hit = False
        self.resolve_count = 0

    def close_browser(self):
        for resource, method in [(self.context, "close"), (self.browser, "close"), (self.playwright, "stop")]:
            if resource:
                try:
                    getattr(resource, method)()
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
                            return None, detail_notice_error(notice)
                        return None, "未捕获到有效视频数据(空详情)"

                    if getattr(self, "captcha_hit", False):
                        return None, "触发验证码风控"

                    cur_title, cur_url = "", ""
                    for _ in range(15):
                        try:
                            cur_title = self.page.title() or ""
                            cur_url = self.page.url or ""
                            if cur_title:
                                break
                        except Exception:
                            pass
                        self.page.wait_for_timeout(100)

                    if "验证码" in cur_title or "verify" in cur_url or "captcha" in cur_url:
                        return None, "触发验证码风控"
                except Exception as e:
                    try:
                        self.page.remove_listener("response", on_response)
                    except Exception:
                        pass
                finally:
                    # 元数据已经捕获，关闭页面以停止播放器预加载；保留 context 复用会话。
                    if self.page:
                        try:
                            self.page.close()
                        except Exception:
                            pass
                        self.page = None

            if attempt < max_retries - 1:
                time.sleep(2.0)
                self.restart_browser()

        return None, "未捕获到视频元数据"

    def download_video_stream(self, cdn_urls: List[str], target_path: Path) -> bool:
        """先在本地完成下载，再通过独立临时文件发布到输出目录。"""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="dy_video_") as local:
            tmp_path = Path(local) / "download.mp4"
            for cdn_url in cdn_urls:
                WafCoordinator.check_and_wait(f"工作线程 {self.worker_id}", self)
                try:
                    with self.http_client.stream("GET", cdn_url) as resp:
                        if resp.status_code != 200:
                            continue
                        with tmp_path.open("wb") as stream:
                            for chunk in resp.iter_bytes(chunk_size=65536):
                                stream.write(chunk)
                    if tmp_path.stat().st_size > 10240:
                        publish_local_file(tmp_path, target_path)
                        return True
                except OSError as exc:
                    print(f"[工作线程 {self.worker_id}] 文件写入失败: {target_path.name}: {exc}", flush=True)
                    return False
                except Exception:
                    continue

        return False

    def synthesize_note_mp4(self, img_urls: List[str], bgm_urls: List[str], target_path: Path) -> bool:
        """图文 note 合成 720x1280 竖屏 MP4（1 秒 1 张图，内嵌原版音频）"""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=f"dy_note_{target_path.stem}_") as tmpdir:
                td = Path(tmpdir)
                local_pngs = []
                for i, img_u in enumerate(img_urls):
                    WafCoordinator.check_and_wait(f"工作线程 {self.worker_id}", self)
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
                    WafCoordinator.check_and_wait(f"工作线程 {self.worker_id}", self)
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
                    publish_local_file(temp_out, target_path)
                    return True
        except Exception as e:
            return False

        return False

    def process_excel(self, excel_path: Path, max_limit: Optional[int] = None) -> int:
        subfolder_name = self.output_name or portable_name(excel_path.stem, fallback="表格")
        folder_dest = self.dest_dir / subfolder_name
        folder_dest.mkdir(parents=True, exist_ok=True)
        completion_flag = folder_dest / "_COMPLETED.flag"
        tag = f"[工作线程 {self.worker_id} | {subfolder_name}]"

        # 旧版标记即使有失败/仅试跑也会写入。始终核对表格和文件，避免永远漏掉失败行。
        completion_flag.unlink(missing_ok=True)

        manifest_path = folder_dest / "manifest.json"
        print(f"\n{tag} 开始处理表格: {excel_path.name}", flush=True)

        manifest = read_manifest(manifest_path)

        try:
            df = pd.read_excel(excel_path)
        except Exception as e:
            print(f"{tag} 读取 Excel 失败: {e}", flush=True)
            return 1

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
            return 1

        total_rows = len(df)
        if max_limit:
            df = df.head(max_limit)

        print(f"{tag} 找到链接列 [{link_col}]，待处理 {len(df)} 行（总计 {total_rows} 行）", flush=True)

        def save_manifest():
            write_manifest(manifest_path, manifest)

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
                fail_count += 1
                print(f"{tag} 第 {idx + 1} 行无法提取视频 ID，请提供完整 video/note 链接。", flush=True)
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
            previous = manifest.get(video_id, {})
            if previous.get("status") == "deleted" and is_permanent_unavailability(previous.get("reason", "")):
                deleted_count += 1
                consecutive_fails = 0
                continue

            # 3. 解析视频元数据
            print(f"{tag} [{idx+1}/{len(df)}] 解析第{rank}名 (ID: {video_id})...", flush=True)
            det, err = self.resolve_video_info(video_id)
            if err and "触发验证码风控" in err:
                for duration in (180.0, 300.0):
                    self.close_browser()
                    manual_ok = WafCoordinator.trigger_cooldown(tag, self.state_file, duration=duration)
                    if self.worker_id > 1:
                        time.sleep(random.uniform(3.0, 5.0))
                    det, err = self.resolve_video_info(video_id)
                    if not err or "触发验证码风控" not in err or manual_ok:
                        break
            if err:
                if is_permanent_unavailability(err):
                    deleted_count += 1
                    consecutive_fails = 0
                    manifest[video_id] = {"status": "deleted", "reason": err}
                    print(f"{tag}   -> ⚠️ {err}", flush=True)
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

            # 正常处理完成后加入随机延迟，降低并发请求峰值。
            time.sleep(random.uniform(2.5, 3.5))

        save_manifest()
        if fail_count == 0 and (max_limit is None or max_limit >= total_rows):
            completion_flag.touch(exist_ok=True)
        print(f"\n{tag} 表格处理完毕: 成功 {success_count} | 跳过 {skip_count} | 已删除 {deleted_count} | 失败 {fail_count}\n", flush=True)
        return fail_count


def worker_thread_loop(worker_id, task_queue, dest_dir, max_limit, state_file, proxy, results, output_name=None):
    worker = None
    try:
        worker = TableWorker(worker_id, dest_dir, state_file, proxy, output_name)
        while True:
            try:
                excel_file = task_queue.get_nowait()
            except queue.Empty:
                break

            try:
                results.put(worker.process_excel(excel_file, max_limit=max_limit))
            except Exception as e:
                results.put(1)
                print(f"[工作线程 {worker_id}] 处理表格 {excel_file.name} 异常: {e}", flush=True)
            finally:
                task_queue.task_done()
    except Exception as e:
        results.put(1)
        print(f"[工作线程 {worker_id}] 初始化失败: {e}", flush=True)
    finally:
        if worker:
            worker.close()
        print(f"[工作线程 {worker_id}] 全部任务结束，已退出并清理资源。", flush=True)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="灰豚 Excel 批量下载（Windows / macOS）")
    parser.add_argument("inputs", nargs="*", type=Path, help="Excel 文件或包含 Excel 的目录，可指定多个")
    parser.add_argument("--source", action="append", type=Path, default=[], help="输入目录/文件，可重复指定")
    parser.add_argument("-o", "--output", type=Path, default=DEST_DIR, help=f"视频输出根目录（默认 {DEST_DIR}）")
    parser.add_argument("--output-name", help="单表任务的输出子目录名（默认使用表名）")
    parser.add_argument("--test", type=positive_int, default=None, help="每个表格仅测试前 N 行，不标记整表完成")
    parser.add_argument("--month", type=str, default=None, help="指定月份文件名匹配，例如 2025-10")
    parser.add_argument("--concurrency", type=positive_int, default=2, help="同时下载的表格数量（默认 2 个）")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--account", help="从现有账号池复制指定账号的会话（备注或账号 ID）")
    access.add_argument("--anonymous-state", type=Path, help="使用独立未登录会话；完全不读取或复制账号池")
    parser.add_argument("--dry-run", action="store_true", help="仅列出输入、输出和任务，不启动浏览器或下载")
    add_session_arguments(parser)
    args = parser.parse_args(argv)

    files = []
    sources = args.inputs + args.source
    if not sources:
        sources = [path for path in SOURCE_DIRS if path.is_dir()]
    for source in sources:
        source = source.expanduser().resolve()
        if source.is_dir():
            files.extend(sorted(path for path in source.iterdir()
                                if path.is_file() and path.suffix.lower() == ".xlsx" and not path.name.startswith("~$")))
        elif source.is_file() and source.suffix.lower() == ".xlsx":
            files.append(source)
        else:
            parser.error(f"输入不存在或不是 xlsx 文件/目录: {source}")
    files = list(dict.fromkeys(files))

    if args.month:
        files = [f for f in files if args.month in f.name]
    if not files:
        parser.error("没有找到匹配的 Excel；请通过 --source 输入目录（或直接传入 xlsx 文件），并用 -o 指定输出目录。")
    if args.output_name is not None:
        if len(files) != 1:
            parser.error("--output-name 仅支持单表任务")
        if not args.output_name or args.output_name in {".", ".."} or portable_name(args.output_name) != args.output_name:
            parser.error("--output-name 必须是有效的单层目录名")
    names = {}
    for path in files:
        name = portable_name(path.stem, fallback="表格").casefold()
        if name in names:
            parser.error(f"两个表格的输出目录重名，请先改名或分开运行: {names[name]} / {path}")
        names[name] = path
    dest_dir = args.output.expanduser().resolve()
    state_file = (args.anonymous_state or args.state_file).expanduser().resolve()

    print(f"=======================================================")
    print(f"共发现 {len(files)} 个待处理表格任务，准备进入队列轮询调度:")
    for i, f in enumerate(files[:15], 1):
        print(f"  {i}. {f.name} ({f.parent.name})")
    if len(files) > 15:
        print(f"  ... 以及后续 {len(files) - 15} 个分类表格")
    print(f"并发表格数: {args.concurrency}（同时下载 {args.concurrency} 张表，完成一张接续下一张）")
    print(f"保存目录: {dest_dir}")
    print(f"会话文件: {state_file}")
    if args.anonymous_state:
        print("会话模式: 独立访客（不使用账号池）")
    print(f"命名规范: 视频-视频ID.mp4 / 图文-视频ID.mp4")
    print(f"=======================================================\n", flush=True)
    if args.dry_run:
        for path in files:
            print(f"{path} -> {dest_dir / (args.output_name or portable_name(path.stem, fallback='表格'))}")
        return 0
    if not shutil.which("ffmpeg"):
        parser.error("缺少 ffmpeg；Mac 请运行 brew install ffmpeg，Windows 请安装 FFmpeg 并加入 PATH。")
    try:
        proxy = resolve_proxy(args.proxy)
        state_file = (prepare_anonymous_state_file(state_file) if args.anonymous_state
                      else prepare_state_file(state_file, args.account))
    except (ValueError, RuntimeError, OSError) as e:
        parser.error(str(e))
    print(f"浏览器解析: {'使用代理' if proxy else '直连'}；文件下载: 忽略系统代理（请关闭 TUN，或配置 DIRECT 分流）。", flush=True)

    task_queue = queue.Queue()
    results = queue.Queue()
    for f in files:
        task_queue.put(f)

    threads = []
    concurrency = min(args.concurrency, len(files))
    for i in range(1, concurrency + 1):
        t = threading.Thread(
            target=worker_thread_loop,
            args=(i, task_queue, dest_dir, args.test, state_file, proxy, results, args.output_name),
            name=f"TableWorker-{i}",
            daemon=False
        )
        threads.append(t)
        t.start()
        # 错开启动，避免并发初始化竞争
        time.sleep(1.0)

    for t in threads:
        t.join()

    failures = 0
    while not results.empty():
        failures += results.get_nowait()
    print(f"\n全部表格处理结束，失败项: {failures}。重复同一命令可补跑失败/缺失文件。", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
