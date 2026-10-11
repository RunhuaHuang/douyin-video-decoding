import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .cookie_accounts import CookieAccountManager
from .browser_service import PlaywrightFallback
from .config import DOUYIN_PREFLIGHT_CHALLENGE
from .douyin_service import DouyinService
from .media_service import FFMPEG_TIMEOUT_SECONDS
from .request_coordinator import RequestCoordinator


DEFAULT_VIDEO_OUTPUT_DIR = Path("output/downloads")


def detect_url_column(df: pd.DataFrame, specified_col: Optional[str] = None) -> str:
    """自动检测或核验 DataFrame 中的链接列。"""
    if specified_col:
        if specified_col in df.columns:
            return specified_col
        raise ValueError(f"指定的列名 '{specified_col}' 不在表格中，可选列: {list(df.columns)}")

    candidates = ["视频链接", "链接", "播放地址", "url", "URL", "短视频链接", "分享链接"]
    for cand in candidates:
        for c in df.columns:
            if cand.lower() in str(c).lower():
                return str(c)

    # 兜底查找第一列包含 http 的列
    for c in df.columns:
        sample_vals = df[c].dropna().astype(str).head(10)
        if any("http" in v for v in sample_vals):
            return str(c)

    raise ValueError(f"未能在表格中自动识别视频链接列，请通过 --url-col 指定。可用列: {list(df.columns)}")


class VideoDownloader:
    """专注下载抖音视频与图文 note 的独立下载引擎。

    特性：
    1. 命名规则严格对齐：
       - 视频: 视频-{video_id}.mp4
       - 图文: 图文-{video_id}.mp4
    2. 图文按 1 秒 1 幅图精准合成 720x1280 竖屏 MP4，内嵌原版 BGM；
    3. 支持账号池会话自动挂载；
    4. 支持单链接、链接列表、以及直接输入 Excel/CSV 批量下载。
    """

    def __init__(
        self,
        account_manager: Optional[CookieAccountManager] = None,
        storage_state_path: Optional[Path] = None,
        recycle_interval: int = 10,
        request_coordinator: Optional[RequestCoordinator] = None,
    ) -> None:
        self.account_manager = account_manager or CookieAccountManager()
        if storage_state_path is None:
            account = self.account_manager.select_current_or_first_available_account()
            self.storage_state_path = account.storage_state_path if account else None
            self.account_label = account.label if account else "未登录/匿名"
        else:
            self.storage_state_path = storage_state_path
            self.account_label = storage_state_path.stem

        self.browser_resolver = PlaywrightFallback(
            storage_state_path=self.storage_state_path or Path("tmp/browser/storage_state.json"),
            request_coordinator=request_coordinator,
        )
        self.recycle_interval = recycle_interval
        self.request_coordinator = request_coordinator
        self._active_downloads = 0
        self._douyin_service: Optional[DouyinService] = None

    def recycle_browser(self) -> None:
        """周期性释放 Chromium 进程与 Playwright 驱动缓冲区，防止长时间运行导致内存堆积。"""
        if self.browser_resolver:
            try:
                self.browser_resolver.close()
            except Exception:
                pass
        import gc
        gc.collect()
        self._active_downloads = 0

    def close(self) -> None:
        if self._douyin_service:
            try:
                self._douyin_service.close()
            except Exception:
                pass
            self._douyin_service = None
        if self.browser_resolver:
            self.browser_resolver.close()

    def _get_douyin_service(self) -> DouyinService:
        if self._douyin_service is None:
            coordinator = self.request_coordinator or RequestCoordinator()
            self.request_coordinator = coordinator
            self.browser_resolver.request_coordinator = coordinator
            self._douyin_service = DouyinService(
                browser_resolver=self.browser_resolver,
                request_coordinator=coordinator,
            )
        return self._douyin_service

    @staticmethod
    def _note_url_for_video(url: str) -> Optional[str]:
        """Return the note route for a video URL carrying the same item ID."""
        converted = re.sub(r"/video/(\d+)(?=\b)", r"/note/\1", url, count=1)
        return converted if converted != url else None

    @staticmethod
    def _is_douyin_challenge_html(html: str) -> bool:
        """Recognize Douyin's JavaScript challenge shell before media lookup."""
        lowered = (html or "").lower()
        return "__ac_signature" in lowered and (
            "byted_acrawler" in lowered or "window.location.reload" in lowered
        ) or "验证码中间页" in lowered or "captcha/index.js" in lowered

    def _download_video_with_capture(
        self,
        share_url: str,
        out_dir: Path,
        *,
        skip_existing: bool,
        allow_note_fallback: bool,
    ) -> Tuple[bool, Path, str]:
        """Use the shared parse/download chain used by the Mac WebUI path."""
        note_url = self._note_url_for_video(share_url) if allow_note_fallback else None
        service = self._get_douyin_service()
        try:
            video = service.parse_share_text(share_url)
            if video.is_deleted:
                return False, out_dir, f"[失败] {share_url}: 视频已删除"
        except Exception as exc:
            if note_url:
                note_ok, note_path, note_message = self.download_url(
                    note_url,
                    out_dir,
                    skip_existing=skip_existing,
                    _allow_note_fallback=False,
                    _is_note_fallback=True,
                )
                if note_ok:
                    return True, note_path, f"[视频转图文重试] {note_message}"
                return False, out_dir, f"[失败] {share_url}: {exc}; note 重试: {note_message}"
            return False, out_dir, f"[失败] {share_url}: {exc}"

        video_id = video.video_id or DouyinService.extract_video_id(share_url) or "unknown"
        final_file = out_dir / f"视频-{video_id}.mp4"
        if skip_existing and final_file.exists() and final_file.stat().st_size > 1024:
            return True, final_file, f"[视频已存在，跳过] {final_file.name}"

        temp_vid = out_dir / f"_tmp_{video_id}.mp4"
        temp_aud = out_dir / f"_tmp_{video_id}.m4a"
        try:
            service.download_video(video, temp_vid, temp_aud)
            if not temp_vid.exists() or temp_vid.stat().st_size <= 0:
                raise RuntimeError("视频流下载未产出文件")
            temp_vid.replace(final_file)
            return True, final_file, f"[视频成功] {final_file.name} ({final_file.stat().st_size:,} 字节)"
        except Exception as exc:
            if note_url:
                note_ok, note_path, note_message = self.download_url(
                    note_url,
                    out_dir,
                    skip_existing=skip_existing,
                    _allow_note_fallback=False,
                    _is_note_fallback=True,
                )
                if note_ok:
                    return True, note_path, f"[视频转图文重试] {note_message}"
                return False, out_dir, f"[失败] {share_url}: {exc}; note 重试: {note_message}"
            return False, out_dir, f"[失败] {share_url}: {exc}"
        finally:
            temp_vid.unlink(missing_ok=True)
            temp_aud.unlink(missing_ok=True)

    def _preflight_challenge(self, ctx, share_url: str) -> bool:
        """Probe the HTML without executing JavaScript before opening a page.

        The challenge shell reloads forever in Chromium, so even a bounded
        ``page.goto(..., wait_until='commit')`` can remain inside Playwright's
        navigation call.  BrowserContext.request receives the same cookies
        without running that script and lets the caller route to ``/note``
        before creating a page.
        """
        try:
            self.browser_resolver._throttle()
            response = ctx.request.get(
                share_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": "https://www.douyin.com/",
                },
                timeout=min(float(self.browser_resolver.navigation_timeout_ms), 10000.0),
                fail_on_status_code=False,
                max_retries=0,
            )
            return self._is_douyin_challenge_html(response.text())
        except Exception:
            # A failed probe must not reject a valid browser navigation. The
            # normal page path still has its own bounded navigation timeout.
            return False

    def download_url(
        self,
        raw_url: str,
        out_dir: Path,
        skip_existing: bool = True,
        _allow_note_fallback: bool = True,
        _is_note_fallback: bool = False,
    ) -> Tuple[bool, Path, str]:
        """下载单个视频或图文链接。

        返回: (is_success, final_path, message)
        """
        share_url = DouyinService.extract_first_url(raw_url) or raw_url.strip()
        if not share_url:
            raise ValueError(f"无效的链接: {raw_url}")
        if not DouyinService.is_douyin_url(share_url):
            raise ValueError(f"不是有效的抖音链接: {share_url}")

        out_dir.mkdir(parents=True, exist_ok=True)

        # 优化：如 URL 中已包含 ID 且本地已有完整文件，毫秒级跳过，完全无需拉起浏览器
        if skip_existing:
            early_id = DouyinService.extract_video_id(share_url)
            if early_id:
                for candidate_name in (f"视频-{early_id}.mp4", f"图文-{early_id}.mp4"):
                    candidate_path = out_dir / candidate_name
                    if candidate_path.exists() and candidate_path.stat().st_size > 1024:
                        prefix = "视频" if candidate_name.startswith("视频") else "图文"
                        return True, candidate_path, f"[{prefix}已存在，跳过] {candidate_path.name}"

        # The original NAS path loaded the video page once to inspect detail
        # JSON and then loaded it again inside PlaywrightFallback.  That second
        # navigation is enough to trigger Douyin's captcha shell.  Video pages
        # now use one BrowserCapture and pass it through to the downloader;
        # note pages keep the dedicated image path below.
        if not _is_note_fallback and not re.search(r"/note/\d+", share_url):
            if self._active_downloads >= self.recycle_interval:
                self.recycle_browser()
            self._active_downloads += 1
            ctx = self.browser_resolver._ensure_context()
            if DOUYIN_PREFLIGHT_CHALLENGE and self._preflight_challenge(ctx, share_url):
                note_url = self._note_url_for_video(share_url) if _allow_note_fallback else None
                if note_url:
                    note_ok, note_path, note_message = self.download_url(
                        note_url,
                        out_dir,
                        skip_existing=skip_existing,
                        _allow_note_fallback=False,
                        _is_note_fallback=True,
                    )
                    if note_ok:
                        return True, note_path, f"[视频转图文重试] {note_message}"
                    return False, out_dir, f"[失败] {share_url}: note 重试: {note_message}"
                return False, out_dir, f"[失败] {share_url}: 检测到抖音验证码挑战页"
            return self._download_video_with_capture(
                share_url,
                out_dir,
                skip_existing=skip_existing,
                allow_note_fallback=_allow_note_fallback,
            )

        # 达到回收周期时主动重置 Chromium 进程。A note fallback belongs
        # to the same logical item and must not consume another recycle slot.
        if not _is_note_fallback:
            if self._active_downloads >= self.recycle_interval:
                self.recycle_browser()
            self._active_downloads += 1

        ctx = self.browser_resolver._ensure_context()

        if self._preflight_challenge(ctx, share_url):
            note_url = self._note_url_for_video(share_url) if _allow_note_fallback else None
            if note_url:
                note_ok, note_path, note_message = self.download_url(
                    note_url,
                    out_dir,
                    skip_existing=skip_existing,
                    _allow_note_fallback=False,
                    _is_note_fallback=True,
                )
                if note_ok:
                    return True, note_path, f"[视频转图文重试] {note_message}"
                return False, out_dir, f"[失败] {share_url}: note 重试: {note_message}"
            return False, out_dir, f"[失败] {share_url}: 检测到抖音反爬挑战页"

        page = ctx.new_page()

        audio_urls: List[str] = []
        detail_data: Dict[str, Any] = {}

        def on_response(resp) -> None:
            try:
                ct = resp.headers.get("content-type", "").lower()
                u = resp.url
                if "audio" in ct or ".mp3" in u or "ies-music" in u or "mime_type=audio" in u:
                    audio_urls.append(u)
                if "/aweme/detail/" in u or "/item_list/" in u:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            detail_data.update(data)
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", on_response)

        try:
            self.browser_resolver._throttle()
            # Challenge pages can execute window.location.reload() forever and
            # never reach DOMContentLoaded.  Wait for the response commit so
            # the HTML can be inspected and routed to /note before the reload
            # loop traps this worker.
            page.goto(share_url, wait_until="commit", timeout=self.browser_resolver.navigation_timeout_ms)
            page.wait_for_timeout(2500)

            # A challenge page can keep reloading without raising a Playwright
            # exception. Detect it explicitly so a /video/<id> request can
            # immediately try the corresponding /note/<id> route.
            try:
                challenge_html = page.content()
            except Exception:
                challenge_html = ""
            if self._is_douyin_challenge_html(challenge_html):
                note_url = self._note_url_for_video(share_url) if _allow_note_fallback else None
                if note_url:
                    try:
                        page.close()
                    except Exception:
                        pass
                    note_ok, note_path, note_message = self.download_url(
                        note_url,
                        out_dir,
                        skip_existing=skip_existing,
                        _allow_note_fallback=False,
                        _is_note_fallback=True,
                    )
                    if note_ok:
                        return True, note_path, f"[视频转图文重试] {note_message}"
                    return False, out_dir, f"[失败] {share_url}: note 重试: {note_message}"
                raise RuntimeError("检测到抖音反爬挑战页")

            final_url = page.url
            video_id = (
                DouyinService.extract_video_id(final_url)
                or DouyinService.extract_video_id(share_url)
                or "unknown"
            )

            # Challenge/redirect responses sometimes include an explicit null
            # aweme_detail. Treat that as an empty payload and fall back to
            # the resolved URL instead of crashing the standalone downloader.
            aweme = detail_data.get("aweme_detail") or {}
            if not isinstance(aweme, dict):
                aweme = {}
            aweme_type = aweme.get("aweme_type")
            if aweme_type is not None:
                is_note = (aweme_type == 68)
            else:
                is_note = bool(re.search(r"/note/\d+", final_url))

            prefix = "图文" if is_note else "视频"
            final_file = out_dir / f"{prefix}-{video_id}.mp4"

            if skip_existing and final_file.exists() and final_file.stat().st_size > 1024:
                return True, final_file, f"[{prefix}已存在，跳过] {final_file.name}"

            if not is_note:
                # 真实视频：调用底层多媒体流捕获
                try:
                    page.close()
                except Exception:
                    pass
                temp_vid = out_dir / f"_tmp_{video_id}.mp4"
                temp_aud = out_dir / f"_tmp_{video_id}.m4a"
                try:
                    self.browser_resolver.download_video(
                        share_url=share_url,
                        output_path=temp_vid,
                        candidate_urls=(),
                        audio_output_path=temp_aud,
                    )
                    if temp_vid.exists() and temp_vid.stat().st_size > 0:
                        temp_vid.replace(final_file)
                        return True, final_file, f"[视频成功] {final_file.name} ({final_file.stat().st_size:,} 字节)"
                    raise RuntimeError("视频流下载未产出文件")
                finally:
                    temp_vid.unlink(missing_ok=True)
                    temp_aud.unlink(missing_ok=True)

            else:
                # 图文 note：提取全量图片并按 1 秒 1 图合成
                img_urls: List[str] = []
                images_field = aweme.get("images")
                if isinstance(images_field, list):
                    for img_obj in images_field:
                        if isinstance(img_obj, dict):
                            u_list = img_obj.get("url_list", [])
                            if u_list:
                                img_urls.append(u_list[-1])

                if not img_urls:
                    try:
                        page.wait_for_selector('img[src*="aweme_images"]', timeout=5000)
                    except Exception:
                        page.wait_for_timeout(1500)

                    img_urls = page.evaluate(
                        """() => {
                            let list = Array.from(document.querySelectorAll('img')).map(i => i.src)
                                .filter(s => s && s.includes('douyinpic.com') && s.includes('aweme_images'));
                            return Array.from(new Set(list));
                        }"""
                    )
                try:
                    page.close()
                except Exception:
                    pass

                if not img_urls:
                    raise RuntimeError("图文页面未提取到有效图片链接")

                with tempfile.TemporaryDirectory(prefix="dy_note_") as tmpdir:
                    td = Path(tmpdir)
                    local_pngs: List[Path] = []
                    for i, img_u in enumerate(img_urls):
                        resp = ctx.request.get(img_u, headers={"Referer": "https://www.douyin.com/"})
                        raw_img = td / f"img_{i:03d}.webp"
                        raw_img.write_bytes(resp.body())
                        png_img = td / f"img_{i:03d}.png"
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", str(raw_img), str(png_img)],
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=FFMPEG_TIMEOUT_SECONDS,
                        )
                        local_pngs.append(png_img)

                    num_imgs = len(local_pngs)
                    dur = float(num_imgs)  # 1.0 秒 1 张图

                    # BGM 获取
                    bgm_file: Optional[Path] = None
                    if audio_urls:
                        resp_a = ctx.request.get(audio_urls[0], headers={"Referer": "https://www.douyin.com/"})
                        bgm_file = td / "bgm.m4a"
                        bgm_file.write_bytes(resp_a.body())

                    temp_out = td / "note_out.mp4"
                    cmd = ["ffmpeg", "-y"]
                    if num_imgs == 1:
                        cmd.extend(["-loop", "1", "-i", str(local_pngs[0].resolve())])
                    else:
                        concat_txt = td / "concat.txt"
                        # Images live beside the manifest. Relative generated names
                        # avoid Windows drive letters, backslashes and quote escaping.
                        with open(concat_txt, "w", encoding="utf-8") as f:
                            for p in local_pngs:
                                f.write(f"file '{p.name}'\n")
                                f.write("duration 1.0\n")
                            # concat demuxer 需重复最后一张防尾帧跳过
                            f.write(f"file '{local_pngs[-1].name}'\n")
                        cmd.extend(["-f", "concat", "-safe", "0", "-i", str(concat_txt)])

                    if bgm_file and bgm_file.stat().st_size > 0:
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
                        timeout=FFMPEG_TIMEOUT_SECONDS,
                    )
                    # The temporary directory may be on a different mount
                    # from a NAS bind volume; Path.replace() then fails with
                    # EXDEV. Stage the copy on the destination filesystem so
                    # the final rename remains atomic.
                    staged_out = out_dir / f".tmp_{video_id}_note.mp4"
                    try:
                        shutil.copyfile(temp_out, staged_out)
                        staged_out.replace(final_file)
                    finally:
                        staged_out.unlink(missing_ok=True)
                    return True, final_file, f"[图文成功] {final_file.name} ({num_imgs}张图, {dur:.1f}秒, {final_file.stat().st_size:,} 字节)"

        except Exception as exc:
            # Some Douyin items are stored as notes but are shared with a
            # /video/<id> URL.  When video parsing/download fails, retry the
            # same item through /note/<id> once; ordinary video failures still
            # return the original error after that bounded second attempt.
            note_url = self._note_url_for_video(share_url) if _allow_note_fallback else None
            if note_url:
                try:
                    if not page.is_closed():
                        page.close()
                except Exception:
                    pass
                note_ok, note_path, note_message = self.download_url(
                    note_url,
                    out_dir,
                    skip_existing=skip_existing,
                    _allow_note_fallback=False,
                    _is_note_fallback=True,
                )
                if note_ok:
                    return True, note_path, f"[视频转图文重试] {note_message}"
                return False, out_dir, f"[失败] {share_url}: {exc}; note 重试: {note_message}"
            return False, out_dir, f"[失败] {share_url}: {exc}"
        finally:
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass

    def download_file(
        self,
        table_path: Path,
        out_dir: Optional[Path] = None,
        url_column: Optional[str] = None,
        skip_existing: bool = True,
        start_row: int = 1,
        progress_callback: Optional[Callable[[int, int, int, int, str], None]] = None,
        workers: int = 2,
        accounts: Optional[Sequence[str]] = None,
    ) -> Tuple[int, int]:
        """从表格文件批量下载视频与图文。

        Workers are long-lived threads, each with its own Playwright browser
        and cookie account. Progress callbacks are serialized in the caller
        thread so the NAS progress JSON remains a single-writer file.
        """
        if not table_path.exists():
            raise FileNotFoundError(f"文件不存在: {table_path}")
        if not 1 <= workers <= 8:
            raise ValueError("workers 必须在 1 到 8 之间")

        stem = table_path.stem
        # 默认保存到 out_dir / <表格文件名>
        target_dir = out_dir or (DEFAULT_VIDEO_OUTPUT_DIR / stem)
        target_dir.mkdir(parents=True, exist_ok=True)

        if table_path.suffix.lower() == ".csv":
            df = pd.read_csv(table_path)
        else:
            df = pd.read_excel(table_path)

        url_col = detect_url_column(df, url_column)
        urls = [
            str(value).strip()
            for value in df[url_col].tolist()
            if pd.notna(value) and str(value).strip()
        ]

        print(f"开始批量下载表格: {table_path.name}")
        print(f"  匹配链接列: [{url_col}], 共 {len(urls)} 条链接")
        print(f"  保存目标目录: {target_dir}")
        print(f"  当前登录账号: {self.account_label}")
        available_accounts = self.account_manager.list_available_accounts()
        if accounts:
            matched_accounts = []
            avail_map = {a.label: a for a in available_accounts}
            for acc_name in accounts:
                if acc_name in avail_map:
                    matched_accounts.append(avail_map[acc_name])
                else:
                    print(f"  [警告] 指定账号 '{acc_name}' 不在可用账号池中或已失效", file=sys.stderr)
            if not matched_accounts:
                raise RuntimeError("指定的账号均不可用，已拒绝静默切换到其他账号")
            worker_accounts = matched_accounts[:workers]
        else:
            worker_accounts = available_accounts[:workers]

        if worker_accounts:
            actual_workers = len(worker_accounts)
            if actual_workers < workers:
                print(f"  可用 cookies 只有 {actual_workers} 个，线程数已降为 {actual_workers}")
            labels_str = "、".join(a.label for a in worker_accounts)
            print(f"  下载线程: {actual_workers}（使用 cookies: {labels_str}）")
        else:
            worker_accounts = []
            actual_workers = 1
            if workers > 1:
                print("  账号池为空，无法为线程分配不同 cookies，线程数已降为 1")
            print("  下载线程: 1（匿名/单线程）")
        if start_row > 1:
            print(f"  起始行指定: 从第 {start_row} 行开始下载 (跳过前 {start_row - 1} 行)")
        print("-" * 60)

        success_count = 0
        fail_count = 0

        tasks = [(index, url) for index, url in enumerate(urls, 1) if index >= start_row]
        if not tasks:
            print("没有需要处理的链接。", flush=True)
            return 0, 0

        # One coordinator is shared by every browser worker in this batch so
        # parallel accounts still respect a single request cadence.
        request_coordinator = self.request_coordinator or RequestCoordinator()
        self.request_coordinator = request_coordinator
        self.browser_resolver.request_coordinator = request_coordinator

        # Prevent duplicate rows for the same item ID from writing the same
        # output file concurrently while keeping unrelated links parallel.
        key_locks: Dict[str, threading.Lock] = {}
        key_locks_guard = threading.Lock()

        def lock_for(raw_url: str) -> threading.Lock:
            key = DouyinService.extract_video_id(raw_url) or raw_url.strip()
            with key_locks_guard:
                return key_locks.setdefault(key, threading.Lock())

        def download_one(worker: "VideoDownloader", row_number: int, raw_url: str):
            with lock_for(raw_url):
                return row_number, worker.download_url(
                    raw_url,
                    target_dir,
                    skip_existing=skip_existing,
                )

        completed_count = 0

        def report_result(row_number: int, raw_url: str, result) -> None:
            nonlocal completed_count, success_count, fail_count
            try:
                _, (ok, path, msg) = result
            except Exception as exc:
                ok, path, msg = False, target_dir, f"[失败] {raw_url}: {exc}"
            completed_count += 1
            if ok:
                success_count += 1
                print(f"[{row_number}/{len(urls)}] {msg}", flush=True)
            else:
                fail_count += 1
                print(f"[{row_number}/{len(urls)}] {msg}", file=sys.stderr, flush=True)
            if progress_callback is not None:
                progress_callback(completed_count, len(tasks), success_count, fail_count, msg)

        if actual_workers == 1:
            # Keep the single-worker path on the caller thread: Playwright's
            # sync API is thread-affine even when only one worker is requested.
            for row_number, raw_url in tasks:
                report_result(row_number, raw_url, download_one(self, row_number, raw_url))
        else:
            task_queue: queue.Queue = queue.Queue()
            result_queue: queue.Queue = queue.Queue()

            def worker_loop(account) -> None:
                # Construct and close Playwright entirely inside this thread;
                # its sync greenlet cannot safely cross thread boundaries.
                worker = VideoDownloader(
                    account_manager=self.account_manager,
                    storage_state_path=account.storage_state_path,
                    recycle_interval=self.recycle_interval,
                    request_coordinator=request_coordinator,
                )
                try:
                    while True:
                        item = task_queue.get()
                        if item is None:
                            task_queue.task_done()
                            return
                        row_number, raw_url = item
                        try:
                            result_queue.put((row_number, raw_url, download_one(worker, row_number, raw_url)))
                        except Exception as exc:
                            result_queue.put((row_number, raw_url, (row_number, (False, target_dir, f"[失败] {raw_url}: {exc}"))))
                        finally:
                            task_queue.task_done()
                finally:
                    worker.close()

            threads = [
                threading.Thread(
                    target=worker_loop,
                    args=(account,),
                    name=f"douyin-download-{account.label}",
                    daemon=True,
                )
                for index, account in enumerate(worker_accounts[:actual_workers])
            ]
            for thread in threads:
                thread.start()
            for task in tasks:
                task_queue.put(task)
            for _ in threads:
                task_queue.put(None)
            for _ in tasks:
                row_number, raw_url, result = result_queue.get()
                report_result(row_number, raw_url, result)
            task_queue.join()
            for thread in threads:
                thread.join()

        print("-" * 60, flush=True)
        print(f"批量下载结束: 共 {len(tasks)} 条 | 成功: {success_count} | 失败: {fail_count}", flush=True)
        return success_count, fail_count


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="douyin-video-dl",
        description="下载抖音视频与图文（图文 1 秒 1 图合成 MP4，文件格式: 视频-ID.mp4 / 图文-ID.mp4）",
    )
    parser.add_argument("inputs", nargs="*", help="抖音链接、分享文本、或 .xlsx/.csv 表格路径")
    parser.add_argument("-o", "--out", default=None, help="保存目录（表格输入时默认为 output/downloads/<表名>）")
    parser.add_argument("--url-col", default=None, help="表格中的链接列名（未指定时自动识别）")
    parser.add_argument("--start-row", type=int, default=1, help="从表格指定行（1开始）开始下载")
    parser.add_argument("--no-skip", action="store_true", help="不跳过已下载的文件（默认跳过）")
    parser.add_argument("--workers", type=int, default=2, help="表格批量下载线程数（默认 2，最多 8）")
    parser.add_argument("--accounts", nargs="+", default=None, help="指定使用的账号标签（如: 思思 老脏）")
    parser.add_argument("--list-accounts", action="store_true", help="列出账号池可用会话后退出")

    args = parser.parse_args(argv)

    account_manager = CookieAccountManager()
    if args.list_accounts:
        accounts = account_manager.list_available_accounts()
        if not accounts:
            print("账号池为空。运行 ./login \"备注\" 添加账号。")
            return 1
        for a in accounts:
            print(f"{a.label}\t{a.storage_state_path}")
        return 0

    if not args.inputs:
        parser.print_help()
        return 2

    downloader = VideoDownloader(account_manager=account_manager)
    skip_existing = not args.no_skip

    try:
        out_base = Path(args.out).expanduser() if args.out else None

        for item in args.inputs:
            p = Path(item)
            if p.exists() and p.suffix.lower() in (".xlsx", ".csv", ".xls"):
                # 输入为表格文件
                if out_base:
                    table_target_dir = out_base if len(args.inputs) == 1 or out_base.name == p.stem else out_base / p.stem
                else:
                    table_target_dir = None
                downloader.download_file(
                    table_path=p,
                    out_dir=table_target_dir,
                    url_column=args.url_col,
                    skip_existing=skip_existing,
                    start_row=args.start_row,
                    workers=args.workers,
                    accounts=args.accounts,
                )
            else:
                # 输入为链接
                target_dir = out_base or DEFAULT_VIDEO_OUTPUT_DIR
                ok, path, msg = downloader.download_url(
                    raw_url=item,
                    out_dir=target_dir,
                    skip_existing=skip_existing,
                )
                print(msg)

        return 0
    finally:
        downloader.close()


if __name__ == "__main__":
    sys.exit(main())
