# -*- coding: utf-8 -*-
"""
灰豚数据 / 抖音带货视频 3进程全员合力推进多表格批量下载器
特点：
1. 【3进程合力推单表】：3 个独立工作进程协同分担 1 个 Excel 表格中的全部待下载视频，以 3 倍速度集中攻克，逐一完结并归档。
2. 【智能断点续传 & 优先攻坚】：优先调度已有存量进度的表格（如已推进过半的表格），迅速生成 _COMPLETED.flag 减少在途中间态。
3. 【两轮集中查漏补缺】：每张表跑完首轮后，全员自动协同执行最多 2 轮集中重试，力挽每一个可下载项后才封板归档。
4. 【集中清单持久化】：主监控线程统一汇总并原子写入 manifest.json，彻底杜绝多进程文件写冲突。
5. 【宽带零代理直连下载】：视频二进制流强制走本地千兆宽带直连落盘，零代理流量开销。
6. 【全局静默自愈 & 错峰唤醒】：遭遇验证码滑块频控时全员统一切断连接休眠 180 秒，心跳脉冲守护杜绝假死误杀，唤醒错峰交错请求。
7. 【磁盘 5GB 熔断安全守卫】：每轮严格检查 D 盘剩余空间。
"""

import os
import sys
import time
import json
import random
import shutil
import uuid
import subprocess
import threading
import multiprocessing as mp
from pathlib import Path
from typing import Optional, List, Dict, Tuple

import pandas as pd
import httpx
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

# 引入 GitHub 最新稳定模块 (app.huitun_storage & app.huitun_support)
_root_dir = Path(__file__).resolve().parent
_repo_dir = _root_dir / "douyin-video-decoding"
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))
if _repo_dir.exists() and str(_repo_dir) not in sys.path:
    sys.path.append(str(_repo_dir))

from app.huitun_storage import publish_local_file, read_manifest, write_manifest
from app.huitun_support import should_block_browser_request, browser_user_agent

try:
    from update_summary_excel import update_summary_excel
except Exception:
    update_summary_excel = None

# ==================== 全局配置 ====================

SOURCE_DIRS = [
    Path(r"C:\Users\cuhksz\Desktop\灰豚数据\带货视频榜-总销售额排序-每月"),
    Path(r"C:\Users\cuhksz\Desktop\带货视频榜-总销售额排序-每月"),
    Path(r"C:\Users\cuhksz\Desktop\带货视频榜-分类"),
    Path(r"C:\Users\cuhksz\Desktop\抖音视频下载")
]

DEST_DIR = Path(r"D:\抖音视频下载")
DEST_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.douyin.com/",
    "Accept": "*/*",
}

CLASH_PROXY_URL = "http://127.0.0.1:7890"

def is_clash_running() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 7890), timeout=0.5):
            return True
    except Exception:
        return False

def check_disk_space_gb(drive_letter: str = "D") -> Optional[float]:
    try:
        total, used, free = shutil.disk_usage(f"{drive_letter}:\\")
        return free / (1024 ** 3)
    except Exception:
        return None

def extract_video_id(url_or_id: str) -> Optional[str]:
    import re
    if not url_or_id:
        return None
    url_or_id = str(url_or_id).strip()
    if url_or_id.isdigit() and len(url_or_id) >= 15:
        return url_or_id
    match = re.search(r"/(?:video|note)/(\d+)", url_or_id)
    return match.group(1) if match else None

def is_permanent_unavailability(reason: str) -> bool:
    """准确区分永久删除与临时受限（永久删除/作者设私/删除不再重试，临时风控/登录才需重试）"""
    message = str(reason or "").strip()
    for prefix in ("已删除/不可见(", "访问受限("):
        if message.startswith(prefix):
            message = message[len(prefix):].removesuffix(")")
            break
    # 明确为作者删除、作品权限限制、设为私密、隐私设置等永久不可见状态
    if any(phrase in message for phrase in (
        "因作品权限或已被删除",
        "作品已被作者删除或设为私密",
        "已被作者删除",
        "由于对方隐私设置",
        "页面404不存在",
        "已删除",
        "已被删除",
        "不存在",
        "已下架",
        "已失效"
    )):
        return True
    # 仅真正的临时频控、验证码、登录限制需要重试
    if any(word in message for word in ("验证", "风控", "登录", "频控", "超时")):
        return False
    return False

def detail_notice_error(notice: str) -> str:
    label = "已删除/不可见" if is_permanent_unavailability(notice) else "访问受限"
    return f"{label}({notice})"

def update_worker_heartbeat(worker_id: int):
    try:
        hb_path = Path(rf"D:\抖音视频下载\.worker_{worker_id}.heartbeat")
        hb_path.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass

def get_worker_heartbeat(worker_id: int) -> float:
    try:
        hb_path = Path(rf"D:\抖音视频下载\.worker_{worker_id}.heartbeat")
        if hb_path.exists():
            val = float(hb_path.read_text(encoding="utf-8").strip())
            if val > 0:
                return val
    except Exception:
        pass
    return time.time()

STATE_FILE = Path(r"D:\抖音视频下载\douyin_state.json")

def sanitize_storage_state(state_file: Path = STATE_FILE) -> dict:
    """
    自动深度净化并清洗 storage_state:
    1. 采用严格白名单机制：仅保留最高置信度的登录核心认证会话 Cookies (sessionid, sid_guard, uid_tt 等)
    2. 彻底剔除所有风控/指纹/遥测/失败残留 (bd_ticket_guard, secsdk, s_v_web_id, fpk, web_sign, n_mh 等)
    3. 清空所有 LocalStorage 缓存，防止任何风控标记跨请求堆积污染
    4. 自动原子写回磁盘，保证文件永久纯净无瑕 (<5KB)
    """
    if not state_file.exists():
        return {}
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}

    ALLOWED_COOKIE_NAMES = {
        "sessionid", "sessionid_ss",
        "sid_guard", "sid_tt",
        "uid_tt", "uid_tt_ss",
        "passport_csrf_token", "passport_csrf_token_default",
        "passport_auth_status", "passport_auth_status_ss",
        "is_staff_user", "has_biz_token", "is_dbsc",
        "enter_pc_once", "hevc_supported",
        "msToken", "ttwid"
    }

    clean_cookies = []
    for c in raw.get("cookies", []):
        name = c.get("name", "").strip()
        if name in ALLOWED_COOKIE_NAMES:
            clean_c = {
                "name": name,
                "value": c.get("value", ""),
                "domain": c.get("domain", ".douyin.com"),
                "path": c.get("path", "/"),
                "httpOnly": c.get("httpOnly", False),
                "secure": c.get("secure", False),
                "sameSite": c.get("sameSite", "Lax")
            }
            clean_cookies.append(clean_c)

    clean_state = {"cookies": clean_cookies, "origins": []}
    try:
        tmp_p = state_file.with_name(f"{state_file.stem}_clean.tmp")
        tmp_p.write_text(json.dumps(clean_state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_p.replace(state_file)
    except Exception:
        pass

    return clean_state


# ==================== 风控自愈协调器 ====================

class WafCoordinator:
    """全局统一静默自愈协调器"""
    COOLDOWN_LOCK = Path(r"D:\抖音视频下载\.waf_cooldown.lock")
    MANUAL_WAKE_SIGNAL = Path(r"D:\抖音视频下载\.manual_wake.signal")

    @classmethod
    def is_in_cooldown(cls) -> bool:
        return cls.COOLDOWN_LOCK.exists()

    @classmethod
    def trigger_cooldown(cls, initiator_tag: str, duration: float = 60.0) -> bool:
        # 触发风控时立即启动凭证自动瘦身净化，清除任何刚生成的风控残留标记
        sanitize_storage_state(STATE_FILE)

        if cls.COOLDOWN_LOCK.exists():
            cls.wait_for_cooldown(initiator_tag)
            return False

        try:
            cls.COOLDOWN_LOCK.write_text(f"initiated by {initiator_tag} at {time.time()}", encoding="utf-8")
            cls.MANUAL_WAKE_SIGNAL.unlink(missing_ok=True)
            print(f"\n🚨 [{initiator_tag}] 检测到滑块/验证码风控！启动【全进程统一静默自愈】（休眠 {int(duration)} 秒）...", flush=True)
            print(f"👉 所有后台浏览器将切断连接（完全零流量以让风控自然消退）。", flush=True)
            print(f"👉 【快捷加速解除】：在 Clash 客户端随意切换另一个节点，然后双击桌面【完成验证.bat】立即换IP全员复活！", flush=True)
            print(f"👉 【无人值守模式】：静默倒计时结束后将自动全员恢复推进。\n", flush=True)
        except Exception:
            pass

        end_time = time.time() + duration
        broken_early = False

        while time.time() < end_time:
            time.sleep(5.0)
            for wid in range(1, 10):
                update_worker_heartbeat(wid)
            if cls.MANUAL_WAKE_SIGNAL.exists():
                print(f"\n⚡ [风控自愈系统] 检测到收到人工唤醒信号（完成验证/切换账号）！立即全员提前唤醒，恢复满速下载！\n", flush=True)
                cls.MANUAL_WAKE_SIGNAL.unlink(missing_ok=True)
                broken_early = True
                break

        cls.COOLDOWN_LOCK.unlink(missing_ok=True)
        for wid in range(1, 10):
            update_worker_heartbeat(wid)
        if not broken_early:
            print(f"\n⚡ [风控自愈系统] 静默期已满 {int(duration)} 秒，服务端频控已充分消退，全员唤醒并重启浏览器！\n", flush=True)

        return broken_early

    @classmethod
    def wait_for_cooldown(cls, worker_tag: str):
        wid = 1
        for char in str(worker_tag):
            if char.isdigit():
                wid = int(char)
                break
        while cls.COOLDOWN_LOCK.exists():
            update_worker_heartbeat(wid)
            time.sleep(5.0)
        update_worker_heartbeat(wid)

    @classmethod
    def check_and_wait(cls, worker_tag: str, worker: 'TableWorker'):
        if cls.COOLDOWN_LOCK.exists():
            worker.close_browser()
            while cls.COOLDOWN_LOCK.exists():
                update_worker_heartbeat(worker.worker_id)
                time.sleep(5.0)
            update_worker_heartbeat(worker.worker_id)
            if worker.worker_id > 1:
                stagger_sec = (worker.worker_id - 1) * 3.0 + random.uniform(1.0, 3.0)
                print(f"[{worker_tag}] ⚡ 触发唤醒错峰保护，交错等待 {stagger_sec:.1f} 秒后再请求...", flush=True)
                time.sleep(stagger_sec)
                update_worker_heartbeat(worker.worker_id)


# ==================== 工作线程实例 ====================

class TableWorker:
    """独立的工作者实例，拥有自己的浏览器和直接 HTTP 客户端"""
    def __init__(self, worker_id: int, dest_dir: Path):
        self.worker_id = worker_id
        self.dest_dir = dest_dir
        # 视频文件下载器：trust_env=False 强制本地网络直连下载，不消耗代理流量、跑满宽带
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
        if not self.playwright:
            self.playwright = sync_playwright().start()

        launch_kwargs = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ]
        }
        if is_clash_running():
            launch_kwargs["proxy"] = {"server": CLASH_PROXY_URL}

        context_kwargs = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 720},
            "locale": "zh-CN",
        }
        clean_state = sanitize_storage_state(STATE_FILE)
        if clean_state and clean_state.get("cookies"):
            context_kwargs["storage_state"] = clean_state

        self.browser = self.playwright.chromium.launch(**launch_kwargs)
        self.context = self.browser.new_context(**context_kwargs)
        self.page = self.context.new_page()

        # 路由拦截：阻断图片媒体字体，屏蔽纯日志/监控上报以节约带宽，并彻底阻断 MSE 播放器后台 fetch/XHR 媒体流
        def should_abort_req(req):
            if should_block_browser_request(req.url, req.resource_type):
                return True
            u = req.url.lower()
            if any(bad in u for bad in ["mon.zijieapi.com", "mcs.zijieapi.com", "snssdk.com/log"]):
                return True
            return False

        self.page.route("**/*", lambda route: (
            route.abort() if should_abort_req(route.request)
            else route.continue_()
        ))
        self.captcha_hit = False
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
        time.sleep(1.0)
        self.start_browser()

    def close(self):
        self.close_browser()
        try:
            self.http_client.close()
        except Exception:
            pass

    def resolve_video_info(self, video_id: str, max_retries: int = 2) -> Tuple[Optional[dict], Optional[str]]:
        WafCoordinator.check_and_wait(f"工作线程 {self.worker_id}", self)
        update_worker_heartbeat(self.worker_id)
        self.resolve_count += 1
        if self.resolve_count >= 25:
            self.restart_browser()

        urls_to_try = [
            f"https://www.douyin.com/video/{video_id}",
            f"https://www.douyin.com/note/{video_id}"
        ]

        for attempt in range(max_retries):
            for target_url in urls_to_try:
                WafCoordinator.check_and_wait(f"工作线程 {self.worker_id}", self)
                update_worker_heartbeat(self.worker_id)
                detail_box = []
                self.captcha_hit = False

                def on_response(res):
                    url_lower = res.url.lower()
                    if "aweme/v1/web/aweme/detail" in url_lower:
                        try:
                            detail_box.append(res.json())
                        except Exception:
                            pass
                    # 严禁将静态脚本/样式误判为风控（抖音正常页面均会加载 rc-verifycenter/sec_sdk_build/.../captcha/index.js 静态库）
                    elif not any(url_lower.endswith(ext) or (ext + "?") in url_lower for ext in [".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".woff", ".woff2", ".webp"]):
                        if any(k in url_lower for k in [
                            "verify.zijieapi.com/captcha/get",
                            "verify.zijieapi.com/captcha/verify",
                            "/captcha/v2/verify",
                            "/captcha/v2/get"
                        ]):
                            self.captcha_hit = True

                try:
                    self.start_browser()
                    self.page.on("response", on_response)
                    try:
                        self.page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
                    except Exception:
                        pass

                    for _ in range(60):
                        if detail_box:
                            break
                        self.page.wait_for_timeout(100)

                    self.page.remove_listener("response", on_response)

                    if detail_box:
                        data = detail_box[0]
                        det = data.get("aweme_detail")
                        if det:
                            return det, None
                        filter_det = data.get("filter_detail") or data.get("aweme_filter_detail") or {}
                        notice = filter_det.get("detail_msg") or filter_det.get("notice")
                        if notice:
                            return None, detail_notice_error(notice)
                        if filter_det.get("aweme_id") or filter_det.get("filter_reason") is not None:
                            return None, "已删除/不可见(作品已被作者删除或设为私密)"
                        status_msg = data.get("status_msg")
                        if status_msg and "验证" in status_msg:
                            return None, "触发验证码风控"
                        return None, "未捕获到有效视频数据(空详情)"

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

                    cur_url_lower = cur_url.lower()
                    if "web_video_404_link" in cur_url_lower:
                        return None, "已删除/不可见(页面404不存在)"

                    if ("验证码" in cur_title or 
                        "安全验证" in cur_title or
                        ("verify" in cur_url_lower and "video" not in cur_url_lower and "note" not in cur_url_lower) or
                        getattr(self, "captcha_hit", False)):
                        return None, "触发验证码风控"

                    try:
                        captcha_el = self.page.locator("#captcha_container, .captcha_verify_container, .secsdk-captcha-drag-wrapper")
                        if captcha_el.count() > 0 and captcha_el.first.is_visible():
                            return None, "触发验证码风控"
                    except Exception:
                        pass
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
        """下载普通视频流（强制本地宽带直连不消耗代理流量，使用 UUID 独立临时文件发布杜绝 WinError 32）"""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        part_id = uuid.uuid4().hex
        tmp_path = target_path.with_name(f".{target_path.name}.{part_id}.part")

        for cdn_url in cdn_urls:
            try:
                with self.http_client.stream("GET", cdn_url, timeout=httpx.Timeout(30.0, connect=15.0)) as resp:
                    if resp.status_code == 200:
                        with open(tmp_path, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=65536):
                                f.write(chunk)
                        if tmp_path.exists() and tmp_path.stat().st_size > 10240:
                            publish_local_file(tmp_path, target_path)
                            return True
            except Exception:
                pass
            finally:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass

        return False

    def synthesize_note_mp4(self, img_urls: List[str], bgm_urls: List[str], target_path: Path) -> bool:
        """图文作品合成动态 MP4（纯本地直连并发拉图并由 FFmpeg 合成高质量视频）"""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = target_path.parent / f"_tmp_{target_path.stem}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        final_tmp_mp4 = target_path.with_name(f"{target_path.stem}.tmp.mp4")

        try:
            downloaded_imgs = []
            for i, u in enumerate(img_urls):
                img_p = tmp_dir / f"img_{i:03d}.jpg"
                try:
                    r = self.http_client.get(u, timeout=10.0)
                    if r.status_code == 200 and len(r.content) > 1024:
                        img_p.write_bytes(r.content)
                        downloaded_imgs.append(img_p)
                except Exception:
                    pass

            if not downloaded_imgs:
                return False

            bgm_p = None
            for bu in bgm_urls:
                try:
                    r = self.http_client.get(bu, timeout=10.0)
                    if r.status_code == 200 and len(r.content) > 1024:
                        bgm_p = tmp_dir / "bgm.mp3"
                        bgm_p.write_bytes(r.content)
                        break
                except Exception:
                    pass

            dur_per_img = 2.5
            total_dur = len(downloaded_imgs) * dur_per_img
            concat_txt = tmp_dir / "concat.txt"
            with open(concat_txt, "w", encoding="utf-8") as f:
                for img in downloaded_imgs:
                    f.write(f"file '{img.name}'\n")
                    f.write(f"duration {dur_per_img}\n")
                f.write(f"file '{downloaded_imgs[-1].name}'\n")

            vf = (
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
                "setsar=1,format=yuv420p"
            )

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_txt.name),
            ]
            if bgm_p and bgm_p.exists():
                cmd.extend([
                    "-stream_loop", "-1", "-i", str(bgm_p.name),
                    "-vf", vf,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-t", str(total_dur),
                    "-shortest",
                    "-f", "mp4",
                    str(final_tmp_mp4.name)
                ])
            else:
                cmd.extend([
                    "-vf", vf,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-t", str(total_dur),
                    "-f", "mp4",
                    str(final_tmp_mp4.name)
                ])

            res = subprocess.run(cmd, cwd=str(tmp_dir), capture_output=True, timeout=60)
            if res.returncode == 0:
                gen_file = tmp_dir / final_tmp_mp4.name
                if gen_file.exists() and gen_file.stat().st_size > 10240:
                    publish_local_file(gen_file, target_path)
                    return True

            return False
        except Exception:
            return False
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if final_tmp_mp4.exists():
                final_tmp_mp4.unlink(missing_ok=True)


# ==================== 单任务项处理逻辑 ====================

def process_task_item(worker: TableWorker, item: tuple) -> dict:
    table_name, folder_dest_str, row_idx, rank, video_id, raw_url = item
    folder_dest = Path(folder_dest_str)
    w_id = worker.worker_id
    tag = f"[工作线程 {w_id} | {table_name}]"

    # 1. 物理文件检查
    vid_target = folder_dest / f"视频-{video_id}.mp4"
    note_target = folder_dest / f"图文-{video_id}.mp4"
    if vid_target.exists() and vid_target.stat().st_size > 10240:
        return {
            "video_id": video_id, "status": "success", "file": vid_target.name,
            "type": "video", "size_mb": round(vid_target.stat().st_size / (1024 * 1024), 2),
            "worker_id": w_id, "rank": rank, "row_idx": row_idx, "is_skip": True
        }
    if note_target.exists() and note_target.stat().st_size > 10240:
        return {
            "video_id": video_id, "status": "success", "file": note_target.name,
            "type": "note", "size_mb": round(note_target.stat().st_size / (1024 * 1024), 2),
            "worker_id": w_id, "rank": rank, "row_idx": row_idx, "is_skip": True
        }

    # 2. 静默等待与心跳更新
    WafCoordinator.check_and_wait(tag, worker)
    update_worker_heartbeat(w_id)

    # 3. 解析视频元数据
    print(f"{tag} 解析第{rank}名 (ID: {video_id})...", flush=True)
    det, err = worker.resolve_video_info(video_id)

    # 4. 风控与自愈处理
    if err and "触发验证码风控" in err:
        try:
            Path(r"D:\抖音视频下载\.last_blocked_url.txt").write_text(f"https://www.douyin.com/video/{video_id}", encoding="utf-8")
        except Exception:
            pass
        manual_ok = WafCoordinator.trigger_cooldown(tag, duration=60.0)
        if w_id > 1:
            time.sleep(random.uniform(2.0, 4.0))
        worker.restart_browser()
        det, err = worker.resolve_video_info(video_id)

    if err:
        if is_permanent_unavailability(err):
            print(f"{tag}   -> ⚠️ {err}", flush=True)
            return {
                "video_id": video_id, "status": "deleted", "reason": err,
                "worker_id": w_id, "rank": rank, "row_idx": row_idx
            }
        else:
            print(f"{tag}   -> ❌ 解析受限/失败: {err}", flush=True)
            return {
                "video_id": video_id, "status": "failed", "reason": err,
                "worker_id": w_id, "rank": rank, "row_idx": row_idx
            }

    # 5. 下载普通视频或合成图文
    aweme_type = det.get("aweme_type")
    images = det.get("images")
    is_note = (aweme_type == 68) or (bool(images) and isinstance(images, list) and len(images) > 0)

    if is_note:
        img_urls = []
        if isinstance(images, list):
            for img_item in images:
                if isinstance(img_item, dict) and img_item.get("url_list"):
                    img_urls.append(img_item["url_list"][-1])
        bgm_urls = det.get("music", {}).get("play_url", {}).get("url_list", [])
        if not img_urls:
            print(f"{tag}   -> ❌ 图文未找到有效图片链接", flush=True)
            return {
                "video_id": video_id, "status": "failed", "reason": "未找到图文图片链接",
                "worker_id": w_id, "rank": rank, "row_idx": row_idx
            }
        ok = worker.synthesize_note_mp4(img_urls, bgm_urls, note_target)
        if ok:
            size_mb = round(note_target.stat().st_size / (1024 * 1024), 2)
            print(f"{tag}   -> 🖼️✅ 图文合成成功: {note_target.name} ({len(img_urls)}图, {size_mb:.1f} MB)", flush=True)
            return {
                "video_id": video_id, "status": "success", "file": note_target.name,
                "type": "note", "size_mb": size_mb, "worker_id": w_id, "rank": rank, "row_idx": row_idx
            }
        else:
            print(f"{tag}   -> ❌ 图文合成失败", flush=True)
            return {
                "video_id": video_id, "status": "failed", "reason": "图文合成失败",
                "worker_id": w_id, "rank": rank, "row_idx": row_idx
            }
    else:
        play_addr = det.get("video", {}).get("play_addr", {})
        cdn_urls = play_addr.get("url_list", [])
        if not cdn_urls:
            print(f"{tag}   -> ❌ 未找到可用视频流", flush=True)
            return {
                "video_id": video_id, "status": "failed", "reason": "未找到可用视频流",
                "worker_id": w_id, "rank": rank, "row_idx": row_idx
            }
        ok = worker.download_video_stream(cdn_urls, vid_target)
        if ok:
            size_mb = round(vid_target.stat().st_size / (1024 * 1024), 2)
            print(f"{tag}   -> 🎬✅ 视频下载成功: {vid_target.name} ({size_mb:.1f} MB)", flush=True)
            return {
                "video_id": video_id, "status": "success", "file": vid_target.name,
                "type": "video", "size_mb": size_mb, "worker_id": w_id, "rank": rank, "row_idx": row_idx
            }
        else:
            print(f"{tag}   -> ❌ 视频流写入失败", flush=True)
            return {
                "video_id": video_id, "status": "failed", "reason": "视频流写入失败",
                "worker_id": w_id, "rank": rank, "row_idx": row_idx
            }


# ==================== 子进程消费循环 ====================

def cooperative_worker_loop(w_id: int, task_queue, result_queue, dest_dir_str: str):
    dest_dir = Path(dest_dir_str)
    worker = TableWorker(w_id, dest_dir)
    tag = f"[工作进程 {w_id}]"
    update_worker_heartbeat(w_id)
    print(f"{tag} 独立进程已启动并完成环境初始化（PID: {os.getpid()}）", flush=True)

    try:
        while True:
            update_worker_heartbeat(w_id)
            try:
                item = task_queue.get(timeout=2.0)
            except Exception:
                continue

            if item is None:
                break

            update_worker_heartbeat(w_id)
            try:
                res = process_task_item(worker, item)
            except Exception as e:
                print(f"{tag} 处理异常，立即自愈重启浏览器: {e}", flush=True)
                try:
                    worker.restart_browser()
                except Exception:
                    pass
                res = {
                    "video_id": item[4], "status": "failed", "reason": f"异常: {e}",
                    "worker_id": w_id, "rank": item[3], "row_idx": item[2]
                }
            result_queue.put(res)
            update_worker_heartbeat(w_id)
            time.sleep(random.uniform(2.0, 3.0))
    finally:
        worker.close()
        print(f"{tag} 子进程已退出并清理资源。", flush=True)


# ==================== 单表格多进程合力调度器 ====================

def process_single_table_cooperatively(
    excel_file: Path,
    task_queue,
    result_queue,
    workers: dict,
    start_worker_func,
    max_limit: Optional[int] = None
) -> bool:
    table_name = excel_file.stem
    folder_dest = DEST_DIR / table_name
    folder_dest.mkdir(parents=True, exist_ok=True)
    manifest_path = folder_dest / "manifest.json"
    completion_flag = folder_dest / "_COMPLETED.flag"

    if completion_flag.exists():
        return True

    free_gb = check_disk_space_gb("D")
    if free_gb is not None and free_gb <= 5.0:
        print(f"\n🚨 [磁盘保护] D盘剩余空间不足 5GB（当前: {free_gb:.2f} GB），触发紧急熔断保护，暂停新表处理！", flush=True)
        return False

    try:
        df = pd.read_excel(excel_file)
    except Exception as e:
        print(f"⚠️ 无法读取表格 {excel_file.name}: {e}")
        return False

    link_col = None
    for candidate in ["播放地址", "视频链接", "作品链接", "链接", "Url", "URL"]:
        if candidate in df.columns:
            link_col = candidate
            break
    if not link_col:
        for c in df.columns:
            if "地址" in c or "链接" in c:
                link_col = c
                break
    if not link_col:
        print(f"⚠️ 表格 {excel_file.name} 中未找到任何视频链接列，跳过！")
        return False

    if max_limit:
        df = df.head(max_limit)

    total_rows = len(df)

    # 读取已有 manifest（防损坏读取）
    manifest = read_manifest(manifest_path)

    def save_manifest():
        write_manifest(manifest_path, manifest)

    # 1. 过滤已完成的项
    items_to_process = []
    skipped_count = 0
    for idx, row in df.iterrows():
        rank = row.get("排名", idx + 1)
        raw_url = str(row.get(link_col, "")).strip()
        video_id = extract_video_id(raw_url)
        if not video_id:
            continue

        vid_f = folder_dest / f"视频-{video_id}.mp4"
        note_f = folder_dest / f"图文-{video_id}.mp4"
        if vid_f.exists() and vid_f.stat().st_size > 10240:
            manifest[video_id] = {"status": "success", "file": vid_f.name, "type": "video"}
            skipped_count += 1
            continue
        if note_f.exists() and note_f.stat().st_size > 10240:
            manifest[video_id] = {"status": "success", "file": note_f.name, "type": "note"}
            skipped_count += 1
            continue

        prev = manifest.get(video_id, {})
        if prev.get("status") == "deleted" and is_permanent_unavailability(prev.get("reason", "")):
            skipped_count += 1
            continue

        items_to_process.append((table_name, str(folder_dest), idx, rank, video_id, raw_url))

    save_manifest()
    # 每张表格启动合力推进前，自动对凭证执行全量瘦身净化
    sanitize_storage_state(STATE_FILE)

    print(f"\n" + "=" * 65)
    print(f"🚀 【{len(workers)} 进程全员合力推进】表格: {excel_file.name}")
    print(f"📊 总行数: {total_rows} | 存量已就绪/跳过: {skipped_count} | 本轮待合力处理: {len(items_to_process)}")
    print(f"=" * 65 + "\n", flush=True)

    # 2. 如果存在待处理项，装填入 task_queue 并全员协作推进
    if items_to_process:
        for item in items_to_process:
            task_queue.put(item)

        completed_in_pass = 0
        total_in_pass = len(items_to_process)
        last_save_time = time.time()

        while completed_in_pass < total_in_pass:
            free_gb = check_disk_space_gb("D")
            if free_gb is not None and free_gb <= 5.0:
                print(f"\n🚨 [磁盘保护] D盘剩余空间不足 5GB（当前: {free_gb:.2f} GB），触发紧急熔断停机！", flush=True)
                sys.exit(0)

            # 监控并守护工作进程存活与心跳
            now = time.time()
            for wid, p in list(workers.items()):
                if p.is_alive():
                    last_hb = get_worker_heartbeat(wid)
                    if not WafCoordinator.is_in_cooldown() and (now - last_hb > 240.0):
                        print(f"\n⚠️ [看门狗] 工作进程 {wid} 假死超时，强制重启以恢复合力推进...", flush=True)
                        try:
                            p.terminate()
                            p.join(timeout=3.0)
                            if p.is_alive():
                                p.kill()
                        except Exception:
                            pass
                        workers[wid] = start_worker_func(wid)
                else:
                    print(f"\n⚠️ [看门狗] 工作进程 {wid} 意外终止，立即拉起新子进程接替队列...", flush=True)
                    workers[wid] = start_worker_func(wid)

            try:
                res = result_queue.get(timeout=1.0)
            except Exception:
                if task_queue.empty():
                    unrecorded = [item for item in items_to_process if item[4] not in manifest]
                    if not unrecorded:
                        break
                    print(f"\n🔄 [队列自愈] 检测到 {len(unrecorded)} 个未记录任务项，已自动重新推入队列！", flush=True)
                    for it in unrecorded:
                        task_queue.put(it)
                continue

            completed_in_pass += 1
            vid = res["video_id"]
            st = res["status"]
            manifest[vid] = {k: v for k, v in res.items() if k not in ["table_name", "rank", "row_idx", "is_skip"]}

            if time.time() - last_save_time > 10.0 or st == "success" or completed_in_pass % 5 == 0:
                save_manifest()
                last_save_time = time.time()

            if completed_in_pass % 20 == 0:
                sanitize_storage_state(STATE_FILE)

            cur_succ = sum(1 for v in manifest.values() if v.get("status") == "success")
            cur_del = sum(1 for v in manifest.values() if v.get("status") == "deleted")
            cur_fail = sum(1 for v in manifest.values() if v.get("status") == "failed")
            print(f"[合力推进 | {table_name}] 进度: {completed_in_pass}/{total_in_pass} ({completed_in_pass/total_in_pass*100:.1f}%) | 成功: {cur_succ} | 删除: {cur_del} | 失败: {cur_fail}", flush=True)

        save_manifest()
        print(f"\n✅ 【首轮全员合力扫描完成】当前表格: {table_name}", flush=True)

    # 3. 查漏补缺：最多 2 轮多进程合力重试所有失败项
    for retry_round in range(1, 3):
        failed_items = []
        for idx, row in df.iterrows():
            rank = row.get("排名", idx + 1)
            raw_url = str(row.get(link_col, "")).strip()
            video_id = extract_video_id(raw_url)
            if video_id and manifest.get(video_id, {}).get("status") == "failed":
                failed_items.append((table_name, str(folder_dest), idx, rank, video_id, raw_url))

        if not failed_items:
            print(f"✨ 【合力查漏补缺】当前无失败项，全员零缺陷完结！", flush=True)
            break

        print(f"\n" + "-" * 55)
        print(f"🔍 [合力查漏补缺 | {table_name}] 启动第 {retry_round}/2 轮全员集中重试（待恢复失败项: {len(failed_items)} 条）")
        print(f"⏳ 全员执行轮间冷却 15 秒...")
        print(f"-" * 55 + "\n", flush=True)
        time.sleep(15.0)

        for item in failed_items:
            task_queue.put(item)

        retry_done = 0
        retry_total = len(failed_items)
        new_recovered = 0
        retried_in_round = set()

        while retry_done < retry_total:
            free_gb = check_disk_space_gb("D")
            if free_gb is not None and free_gb <= 5.0:
                print(f"\n🚨 [磁盘保护] D盘剩余空间不足 5GB（当前: {free_gb:.2f} GB），触发紧急熔断停机！", flush=True)
                sys.exit(0)

            now = time.time()
            for wid, p in list(workers.items()):
                if p.is_alive():
                    last_hb = get_worker_heartbeat(wid)
                    if not WafCoordinator.is_in_cooldown() and (now - last_hb > 240.0):
                        print(f"\n⚠️ [看门狗] 工作进程 {wid} 假死超时，强制重启...", flush=True)
                        try:
                            p.terminate()
                            p.join(timeout=3.0)
                            if p.is_alive():
                                p.kill()
                        except Exception:
                            pass
                        workers[wid] = start_worker_func(wid)
                else:
                    workers[wid] = start_worker_func(wid)

            try:
                res = result_queue.get(timeout=1.0)
            except Exception:
                if task_queue.empty():
                    unretried = [item for item in failed_items if item[4] not in retried_in_round]
                    if not unretried:
                        break
                    print(f"\n🔄 [补捞自愈] 检测到 {len(unretried)} 个未重试任务项，已自动重新推入队列！", flush=True)
                    for it in unretried:
                        task_queue.put(it)
                continue

            retry_done += 1
            vid = res["video_id"]
            retried_in_round.add(vid)
            if res["status"] == "success":
                new_recovered += 1
            manifest[vid] = {k: v for k, v in res.items() if k not in ["table_name", "rank", "row_idx", "is_skip"]}
            save_manifest()

        print(f"📊 [查漏补缺] 第 {retry_round} 轮重试完成：成功挽回 {new_recovered} 部视频！", flush=True)
        if new_recovered == 0:
            break

    save_manifest()
    
    # 终审判决：对经过全员多轮查漏补缺集中重试后，仍明确为作者删除或设为私密不可见的作品做终审归档判定
    for vid, item in manifest.items():
        if item.get("status") == "failed":
            r = str(item.get("reason") or "")
            if any(k in r for k in ["已被作者删除", "因作品权限或已被删除", "设为私密", "对方隐私设置", "已下架", "已删除", "页面404"]):
                item["status"] = "deleted"
    save_manifest()

    final_succ = sum(1 for v in manifest.values() if v.get("status") == "success")
    final_del = sum(1 for v in manifest.values() if v.get("status") == "deleted")
    final_fail = sum(1 for v in manifest.values() if v.get("status") == "failed")
    total_items = final_succ + final_del + final_fail
    effective_rate = (final_succ + final_del) / max(1, total_items)

    # 质量门禁：只有综合有效率达到 97.5% 以上且最终失败数 <= 25 时，才视为真正完结并打标归档
    # 若因代理短时故障导致未完全获取，不打完成标记，保留断点数据，允许后续轮次继续攻坚补漏！
    if effective_rate >= 0.975 and final_fail <= 25:
        completion_flag.touch(exist_ok=True)
        print(f"\n" + "=" * 65)
        print(f"🎉🎉 【合力推进大功告成】表格 {table_name} 达标完结归档！（综合有效率: {effective_rate*100:.1f}%）")
        print(f"📊 最终战报: 成功 {final_succ} 部 | 确认删除 {final_del} 条 | 失败 {final_fail} 条")
        print(f"🏁 已生成 _COMPLETED.flag，全员立即无缝切入下一个待办表格！")
        print(f"=" * 65 + "\n", flush=True)
    else:
        completion_flag.unlink(missing_ok=True)
        print(f"\n" + "=" * 65)
        print(f"⚠️ 【暂缓归档】表格 {table_name} 当前失败项较多（失败 {final_fail} 条，综合有效率: {effective_rate*100:.1f}%）")
        print(f"💡 疑似因代理或风控导致未完全获取，暂不打 _COMPLETED.flag，已保留断点数据，后续轮次将自动再次攻坚！")
        print(f"=" * 65 + "\n", flush=True)

    if update_summary_excel:
        try:
            print("📊 正在同步刷新桌面汇总报表...", flush=True)
            update_summary_excel()
        except Exception as e:
            print(f"⚠️ 更新桌面 Excel 异常: {e}", flush=True)

    return True


# ==================== 主调度入口 ====================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="2进程双线程全员合力推进多表格批量下载器")
    parser.add_argument("--test", type=int, default=None, help="每个表格仅测试前 N 个视频")
    parser.add_argument("--month", type=str, default=None, help="指定月份文件名匹配，例如 2025-10")
    parser.add_argument("--concurrency", type=int, default=2, help="协作工作进程数（默认 2）")
    args = parser.parse_args()

    concurrency = args.concurrency

    # 1. 扫描所有源目录中的表格
    files = []
    for s_dir in SOURCE_DIRS:
        if s_dir.exists():
            files.extend(list(s_dir.rglob("*.xlsx")))

    unique_files = {}
    for f in files:
        if f.stem not in unique_files and not f.name.startswith("~$"):
            unique_files[f.stem] = f

    if not unique_files:
        print("未在源目录中找到有效 Excel 表格！")
        return

    all_tables = list(unique_files.values())
    if args.month:
        month_targets = [m.strip() for m in args.month.split(",") if m.strip()]
        all_tables = [f for f in all_tables if any(t in f.name for t in month_targets)]

    print(f"=======================================================")
    print(f"共发现 {len(all_tables)} 个表格任务。模式：【{concurrency} 进程全员合力推进单表】")
    print(f"保存目录: {DEST_DIR}")
    print(f"命名规范: 视频-视频ID.mp4 / 图文-视频ID.mp4")
    print(f"=======================================================\n", flush=True)

    # 启动前清理任何可能遗留的旧自愈锁
    WafCoordinator.COOLDOWN_LOCK.unlink(missing_ok=True)
    WafCoordinator.MANUAL_WAKE_SIGNAL.unlink(missing_ok=True)

    # 2. 启动持久化工作进程池与 IPC 队列
    task_queue = mp.Queue()
    result_queue = mp.Queue()
    workers: Dict[int, mp.Process] = {}

    def start_worker(w_id: int) -> mp.Process:
        update_worker_heartbeat(w_id)
        p = mp.Process(
            target=cooperative_worker_loop,
            args=(w_id, task_queue, result_queue, str(DEST_DIR)),
            name=f"CooperativeWorker-{w_id}",
            daemon=False
        )
        p.start()
        return p

    for i in range(1, concurrency + 1):
        workers[i] = start_worker(i)
        time.sleep(2.0)

    try:
        round_num = 1
        while True:
            # 过滤尚未完成的表格并按已有进度倒序优先调度
            pending_items = []
            for f in all_tables:
                flag_p = DEST_DIR / f.stem / "_COMPLETED.flag"
                if not flag_p.exists():
                    manifest_p = DEST_DIR / f.stem / "manifest.json"
                    done_cnt = 0
                    if manifest_p.exists():
                        try:
                            m = json.loads(manifest_p.read_text(encoding="utf-8"))
                            done_cnt = sum(1 for v in m.values() if v.get("status") in ["success", "deleted"])
                        except Exception:
                            pass
                    folder_p = DEST_DIR / f.stem
                    if folder_p.exists():
                        try:
                            mp4_cnt = len(list(folder_p.glob("*.mp4")))
                            done_cnt = max(done_cnt, mp4_cnt)
                        except Exception:
                            pass
                    pending_items.append((done_cnt, f))

            if not pending_items:
                print("\n🎉🎉🎉 全部表格均已生成 _COMPLETED.flag，全量 100% 完结归档！")
                break

            # 关键优化：优先推进已有完成度最高的表格，迅速归档！
            pending_items.sort(key=lambda x: x[0], reverse=True)
            pending_tables = [item[1] for item in pending_items]

            print(f"\n" + "=" * 65)
            print(f"🔄 【轮次 {round_num}】待处理/未归档表格数: {len(pending_tables)} / {len(all_tables)}")
            print(f"📋 当前优先攻坚表格队列:")
            for idx, pt in enumerate(pending_tables[:5], 1):
                print(f"   {idx}. {pt.name} (已有进度: {pending_items[idx-1][0]}/1000)")
            print(f"=" * 65 + "\n", flush=True)

            for table_file in pending_tables:
                process_single_table_cooperatively(
                    table_file,
                    task_queue,
                    result_queue,
                    workers,
                    start_worker,
                    max_limit=args.test
                )

            round_num += 1

    finally:
        print("\n正在停止各协作子进程...", flush=True)
        for _ in range(concurrency * 2):
            task_queue.put(None)
        for w_id, p in workers.items():
            try:
                p.join(timeout=5.0)
                if p.is_alive():
                    p.kill()
            except Exception:
                pass
        print("所有子进程已安全退出。\n", flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
