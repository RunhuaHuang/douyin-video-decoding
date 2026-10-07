from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx

from .account_storage import write_storage_state
from .media_service import FfmpegService
from .config import (
    DOUYIN_CHALLENGE_BASE_COOLDOWN_SECONDS,
    DOUYIN_CHALLENGE_MAX_COOLDOWN_SECONDS,
    DOUYIN_REQUEST_MAX_INTERVAL_SECONDS,
    DOUYIN_REQUEST_MIN_INTERVAL_SECONDS,
    PLAYWRIGHT_AFTER_LOAD_WAIT_MS,
    PLAYWRIGHT_AFTER_PLAY_WAIT_MS,
    PLAYWRIGHT_DASH_AUDIO_GRACE_MS,
    PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS,
    PLAYWRIGHT_HEADLESS,
    PLAYWRIGHT_INSTALL_HINT,
    PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
    PLAYWRIGHT_NETWORK_IDLE_TIMEOUT_MS,
    PLAYWRIGHT_STORAGE_STATE_PATH,
    PLAYWRIGHT_VIDEO_WAIT_TIMEOUT_MS,
)
from .request_coordinator import RequestCoordinator


CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# The fallback Playwright response API exposes ``body()`` only as a fully
# materialized bytes object. Never use that path for a known large media body;
# the streaming HTTP path above must handle it instead.
MAX_BUFFERED_MEDIA_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class BrowserCapture:
    page_url: str
    title: str
    video_urls: Tuple[str, ...]
    audio_urls: Tuple[str, ...] = ()
    preview_urls: Tuple[str, ...] = ()
    author: str = "未知作者"

    @property
    def media_urls(self) -> Tuple[str, ...]:
        return self.video_urls


@dataclass(frozen=True)
class BrowserDownloadResult:
    source_url: str
    page_url: str
    attempts: int
    audio_source_url: str = ""
    audio_attempts: int = 0


class PlaywrightFallback:
    _storage_state_lock = threading.RLock()

    def __init__(
        self,
        *,
        navigation_timeout_ms: int = PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
        network_idle_timeout_ms: int = PLAYWRIGHT_NETWORK_IDLE_TIMEOUT_MS,
        video_wait_timeout_ms: int = PLAYWRIGHT_VIDEO_WAIT_TIMEOUT_MS,
        after_load_wait_ms: int = PLAYWRIGHT_AFTER_LOAD_WAIT_MS,
        after_play_wait_ms: int = PLAYWRIGHT_AFTER_PLAY_WAIT_MS,
        download_timeout_ms: int = PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS,
        dash_audio_grace_ms: int = PLAYWRIGHT_DASH_AUDIO_GRACE_MS,
        headless: bool = PLAYWRIGHT_HEADLESS,
        storage_state_path: Path = PLAYWRIGHT_STORAGE_STATE_PATH,
        request_coordinator: Optional[RequestCoordinator] = None,
    ) -> None:
        self.navigation_timeout_ms = navigation_timeout_ms
        self.network_idle_timeout_ms = network_idle_timeout_ms
        self.video_wait_timeout_ms = video_wait_timeout_ms
        self.after_load_wait_ms = after_load_wait_ms
        self.after_play_wait_ms = after_play_wait_ms
        self.download_timeout_ms = download_timeout_ms
        self.dash_audio_grace_ms = dash_audio_grace_ms
        self.headless = headless
        self.storage_state_path = Path(storage_state_path)
        self.request_coordinator = request_coordinator
        self._playwright = None
        self._browser = None
        self._context = None

    def close(self) -> None:
        self._reset_runtime()

    def capture_video(self, share_url: str) -> BrowserCapture:
        page = None
        video_urls: List[str] = []
        audio_urls: List[str] = []
        preview_urls: List[str] = []
        seen_urls = set()
        title = ""

        def handle_response(response) -> None:
            try:
                content_type = response.headers.get("content-type", "")
            except Exception:
                content_type = ""

            if self._looks_like_audio(response.url, content_type) and response.url not in seen_urls:
                seen_urls.add(response.url)
                audio_urls.append(response.url)
            elif self._is_preview_video_url(response.url) and response.url not in seen_urls:
                seen_urls.add(response.url)
                preview_urls.append(response.url)
            elif self._looks_like_video(response.url, content_type) and response.url not in seen_urls:
                seen_urls.add(response.url)
                video_urls.append(response.url)

        try:
            page = self._ensure_context().new_page()
            page.on("response", handle_response)
            self._throttle()
            page.goto(share_url, wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=self.network_idle_timeout_ms)
            except Exception:
                pass
            page.wait_for_timeout(self.after_load_wait_ms)

            video_locator = page.locator("video").first
            try:
                video_locator.wait_for(state="attached", timeout=self.video_wait_timeout_ms)
                try:
                    video_locator.evaluate(
                        """
                        (node) => {
                          node.muted = true;
                          if (typeof node.play === "function") {
                            try {
                              node.play();
                            } catch (error) {
                              return false;
                            }
                          }
                          return true;
                        }
                        """
                    )
                except Exception:
                    pass
                self._wait_for_preferred_video_urls(
                    page, video_locator, video_urls, preview_urls, seen_urls, audio_urls
                )
            except Exception:
                pass

            title = page.title().strip()
            if not title:
                try:
                    title = (
                        page.locator("meta[name='description']").first.get_attribute("content") or ""
                    ).strip()
                except Exception:
                    title = ""

            page_text = ""
            try:
                page_text = page.content()
            except Exception:
                page_text = ""
            if self._looks_like_challenge_page(title, page_text):
                self._note_challenge("验证码挑战页")
            else:
                self._note_success()

            self._persist_storage_state()

            return BrowserCapture(
                page_url=page.url,
                title=title or "抖音视频",
                video_urls=tuple(self._prioritize_video_urls(video_urls)),
                audio_urls=tuple(self._dedupe_urls(audio_urls)),
                preview_urls=tuple(self._dedupe_urls(preview_urls)),
            )
        except Exception as exc:
            self._reset_runtime()
            raise RuntimeError(self._format_browser_error(exc)) from exc
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def download_video(
        self,
        share_url: str,
        output_path: Path,
        candidate_urls: Sequence[str],
        audio_output_path: Path | None = None,
        capture: BrowserCapture | None = None,
    ) -> BrowserDownloadResult:
        capture = capture or self.capture_video(share_url)
        all_urls = [*capture.video_urls, *candidate_urls]
        # 网页端长视频是音视频分离流，音频流 URL 的 query 常带 mime_type=video_mp4，
        # 必须按 media-audio 路径特征分流，否则纯音频会被当成视频存下来
        video_candidates = self._prioritize_video_urls(
            [url for url in all_urls if not self._is_audio_like_url(url)]
        )
        audio_candidates = self._dedupe_urls(
            [url for url in all_urls if self._is_audio_like_url(url)]
            + list(capture.audio_urls)
        )

        require_embedded_audio = audio_output_path is not None and not audio_candidates
        download_kwargs = dict(
            output_path=output_path,
            referer=capture.page_url,
            accept="video/webm,video/mp4,video/*;q=0.9,*/*;q=0.8",
            destination="video",
            validator=self._looks_like_media_payload,
            empty_error="浏览器兜底未捕获到可下载的视频地址",
            fail_error_prefix="浏览器兜底下载失败",
        )
        # 第一遍要求文件里有视频画面；全部失败再退回旧的宽松校验兜底。
        # 两遍都只能尝试视频候选，音频流必须走下面独立的音频下载路径。
        post_validator = self._video_file_has_audio if require_embedded_audio else self._video_file_has_stream
        post_validation_error = "视频不含音轨" if require_embedded_audio else "下载结果不含视频画面"
        try:
            source_url, attempts = self._download_candidate_urls(
                urls=video_candidates,
                post_validator=post_validator,
                post_validation_error=post_validation_error,
                **download_kwargs,
            )
        except Exception:
            if require_embedded_audio:
                # 第二遍的 URL 集合和校验与第一遍完全相同（都要求内嵌音轨），
                # 注定得到同样结果，直接按无内嵌音轨报错，不再重复下载
                raise
            source_url, attempts = self._download_candidate_urls(
                urls=video_candidates,
                post_validator=None,
                post_validation_error=post_validation_error,
                **download_kwargs,
            )

        audio_source_url = ""
        audio_attempts = 0
        if audio_candidates:
            audio_target = audio_output_path or output_path.with_name(
                f"{output_path.stem}_browser_audio.m4a"
            )
            try:
                audio_source_url, audio_attempts = self._download_candidate_urls(
                    urls=audio_candidates,
                    output_path=audio_target,
                    referer=capture.page_url,
                    accept="audio/aac,audio/mp4,audio/*;q=0.9,*/*;q=0.8",
                    destination="audio",
                    validator=self._looks_like_audio_payload,
                    empty_error="浏览器兜底未捕获到可下载的音频地址",
                    fail_error_prefix="浏览器兜底音频下载失败",
                )
            except Exception:
                audio_target.unlink(missing_ok=True)
                audio_source_url = ""
                audio_attempts = 0
            else:
                self._merge_audio_if_needed(
                    output_path,
                    audio_target,
                    owns_audio_file=audio_output_path is None,
                )

        return BrowserDownloadResult(
            source_url=source_url,
            page_url=capture.page_url,
            attempts=attempts,
            audio_source_url=audio_source_url,
            audio_attempts=audio_attempts,
        )

    def _merge_audio_if_needed(
        self,
        video_path: Path,
        audio_path: Path,
        *,
        owns_audio_file: bool,
    ) -> None:
        """分流视频不含音轨时合并成单个 mp4；合并失败则保留原样由上层兜底。"""
        if not video_path.exists() or not audio_path.exists():
            return
        try:
            if not self._video_file_has_audio(video_path):
                merged = FfmpegService()._merge_supplemental_audio(video_path, audio_path)
                merged.replace(video_path)
        except Exception:
            return
        finally:
            if owns_audio_file:
                audio_path.unlink(missing_ok=True)

    def status(self) -> str:
        try:
            self._ensure_context()
        except Exception as exc:
            return self._format_browser_error(exc)
        return "已就绪"

    def _ensure_context(self):
        if self._context is not None:
            return self._context

        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("未安装 Playwright Python 依赖") from exc

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
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
            if self.storage_state_path.exists():
                context_kwargs["storage_state"] = str(self.storage_state_path)
            self._context = self._browser.new_context(
                **context_kwargs,
            )
            return self._context
        except Exception as exc:
            self._reset_runtime()
            raise RuntimeError(self._format_browser_error(exc)) from exc

    def _reset_runtime(self) -> None:
        if self._context is not None:
            try:
                self._persist_storage_state()
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    @staticmethod
    def _is_audio_like_url(url: str) -> bool:
        lowered = (url or "").lower()
        return (
            "media-audio" in lowered
            or "mime_type=audio" in lowered
            or "audio_type" in lowered
            or lowered.endswith((".m4a", ".mp3", ".aac"))
        )

    @classmethod
    def _looks_like_video(cls, url: str, content_type: str) -> bool:
        lowered_type = (content_type or "").lower()
        lowered_url = url.lower()
        if cls._is_audio_like_url(lowered_url):
            return False
        return (
            "video" in lowered_type
            or "mime_type=video_mp4" in lowered_url
            or "format=mp4" in lowered_url
            or lowered_url.endswith(".mp4")
        )

    @staticmethod
    def _is_preview_video_url(url: str) -> bool:
        lowered = (url or "").lower()
        return "lf-douyin-pc-web.douyinstatic.com" in lowered and "/uuu_" in lowered

    @staticmethod
    def _is_preferred_video_url(url: str) -> bool:
        lowered = (url or "").lower()
        return (
            "douyinvod.com" in lowered
            or "tos-cn-ve" in lowered
        ) and not PlaywrightFallback._is_preview_video_url(lowered)

    @classmethod
    def _looks_like_audio(cls, url: str, content_type: str) -> bool:
        lowered_type = (content_type or "").lower()
        return "audio" in lowered_type or cls._is_audio_like_url(url)

    @staticmethod
    def _looks_like_media_payload(payload: bytes, content_type: str, url: str) -> bool:
        if not payload:
            return False

        lowered_type = (content_type or "").lower()
        lowered_url = (url or "").lower()
        head = payload[:512]
        stripped = head.lstrip().lower()

        if stripped.startswith((b"<!doctype html", b"<html", b"{", b"[")):
            return False
        if stripped.startswith(b"#extm3u"):
            return False
        if b"ftyp" in head[:32]:
            return True
        if "video" in lowered_type:
            return True
        if "mime_type=video_mp4" in lowered_url or "format=mp4" in lowered_url or lowered_url.endswith(".mp4"):
            return True
        return False

    @staticmethod
    def _looks_like_audio_payload(payload: bytes, content_type: str, url: str) -> bool:
        if not payload:
            return False

        lowered_type = (content_type or "").lower()
        lowered_url = (url or "").lower()
        head = payload[:512]
        stripped = head.lstrip().lower()

        if stripped.startswith((b"<!doctype html", b"<html", b"{", b"[", b"#extm3u")):
            return False
        if stripped.startswith(b"id3"):
            return True
        if b"ftyp" in head[:32]:
            return True
        if "audio" in lowered_type:
            return True
        if "mime_type=audio" in lowered_url or "audio_type" in lowered_url:
            return True
        if lowered_url.endswith((".m4a", ".mp3", ".aac")):
            return True
        return False

    @staticmethod
    def _build_media_headers(referer: str, *, accept: str, destination: str) -> dict:
        parsed = urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://www.douyin.com"
        return {
            "User-Agent": CHROME_USER_AGENT,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": origin,
            "Referer": referer or "https://www.douyin.com/",
            "Range": "bytes=0-",
            "Sec-Fetch-Dest": destination,
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        }

    def _download_candidate_urls(
        self,
        *,
        urls: Sequence[str],
        output_path: Path,
        referer: str,
        accept: str,
        destination: str,
        validator,
        empty_error: str,
        fail_error_prefix: str,
        post_validator=None,
        post_validation_error: str | None = None,
    ) -> tuple[str, int]:
        if not urls:
            raise RuntimeError(empty_error)

        errors = []
        request_context = self._ensure_context().request
        for position, url in enumerate(urls, start=1):
            try:
                self._throttle()
                # APIResponse.body() materializes the entire media file in
                # Python memory. Prefer a bounded stream; fall back to the
                # Playwright request path when the signed URL needs browser
                # handling that a plain HTTP request cannot reproduce.
                streamed = self._try_stream_candidate(
                    url=url,
                    output_path=output_path,
                    referer=referer,
                    accept=accept,
                    destination=destination,
                    validator=validator,
                    post_validator=post_validator,
                    post_validation_error=post_validation_error,
                )
                if streamed is not None:
                    if streamed == "ok":
                        return url, position
                    errors.append(streamed)
                    continue

                # Playwright 请求超时覆盖整个响应（含 body 读取），整文件下载
                # 必须用独立大超时，不能沿用 15s 导航超时
                response = request_context.get(
                    url,
                    headers=self._build_media_headers(referer, accept=accept, destination=destination),
                    timeout=float(self.download_timeout_ms),
                    fail_on_status_code=False,
                    max_retries=0,
                )
                status = response.status
                if 200 <= status < 300:
                    try:
                        content_length = int(response.headers.get("content-length", "0") or 0)
                    except (TypeError, ValueError):
                        content_length = 0
                    if content_length > MAX_BUFFERED_MEDIA_BYTES:
                        errors.append("媒体响应过大且流式下载未接管，已避免一次性读入内存")
                        continue
                    body = response.body()
                    if not validator(
                        body,
                        response.headers.get("content-type", ""),
                        url,
                    ):
                        errors.append("返回内容不是有效媒体")
                        continue
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(body)
                    if post_validator is not None and not post_validator(output_path):
                        output_path.unlink(missing_ok=True)
                        errors.append(post_validation_error or "输出文件校验失败")
                        continue
                    self._note_success()
                    self._persist_storage_state()
                    return url, position

                if status == 403:
                    self._note_challenge("浏览器下载 HTTP 403")
                elif status == 429:
                    self._note_challenge("浏览器下载 HTTP 429")
                errors.append(f"HTTP {status}")
            except Exception as exc:
                errors.append(self._format_browser_error(exc))

        summary = ", ".join(self._summarize_errors(errors))
        raise RuntimeError(f"{fail_error_prefix}: {summary}")

    def _try_stream_candidate(
        self,
        *,
        url: str,
        output_path: Path,
        referer: str,
        accept: str,
        destination: str,
        validator,
        post_validator=None,
        post_validation_error: str | None = None,
    ) -> str | None:
        """Stream a media candidate to disk without buffering its body.

        ``None`` asks the caller to fall back to Playwright's request context.
        A non-empty string is a handled candidate error.
        """
        headers = self._build_media_headers(referer, accept=accept, destination=destination)
        cookie_header = self._cookie_header_for_url(url)
        if cookie_header:
            headers["Cookie"] = cookie_header

        timeout = httpx.Timeout(
            connect=10.0,
            read=self.download_timeout_ms / 1000,
            write=self.download_timeout_ms / 1000,
            pool=self.download_timeout_ms / 1000,
        )
        try:
            with httpx.Client(follow_redirects=True, timeout=timeout) as client:
                with client.stream("GET", url, headers=headers) as response:
                    if not 200 <= response.status_code < 300:
                        return None

                    content_type = response.headers.get("content-type", "")
                    prefix = bytearray()
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with output_path.open("wb") as handle:
                            for chunk in response.iter_bytes():
                                if not chunk:
                                    continue
                                if len(prefix) < 1024:
                                    prefix.extend(chunk[: 1024 - len(prefix)])
                                handle.write(chunk)
                    except Exception:
                        output_path.unlink(missing_ok=True)
                        raise

                    if not validator(bytes(prefix), content_type, url):
                        output_path.unlink(missing_ok=True)
                        return None
                    if post_validator is not None and not post_validator(output_path):
                        output_path.unlink(missing_ok=True)
                        return post_validation_error or "输出文件校验失败"
        except Exception:
            output_path.unlink(missing_ok=True)
            return None

        self._note_success()
        self._persist_storage_state()
        return "ok"

    def _cookie_header_for_url(self, url: str) -> str:
        if self._context is None:
            return ""
        try:
            cookies = self._context.cookies([url])
        except Exception:
            try:
                cookies = self._context.cookies()
            except Exception:
                return ""
        return "; ".join(
            f"{cookie.get('name')}={cookie.get('value')}"
            for cookie in cookies
            if cookie.get("name") and cookie.get("value") is not None
        )

    @staticmethod
    def _dedupe_urls(urls: Sequence[str]) -> List[str]:
        results: List[str] = []
        seen = set()
        for raw_url in urls:
            url = (raw_url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(url)
        return results

    @classmethod
    def _prioritize_video_urls(cls, urls: Sequence[str]) -> List[str]:
        deduped = [url for url in cls._dedupe_urls(urls) if cls._is_preferred_video_url(url)]
        return sorted(deduped, key=cls._video_url_priority)

    @staticmethod
    def _video_url_priority(url: str) -> tuple[int, int]:
        lowered = (url or "").lower()
        if PlaywrightFallback._is_audio_like_url(lowered):
            return (9, len(lowered))
        if "douyinvod.com" in lowered and "mime_type=video_mp4" in lowered:
            return (0, len(lowered))
        if "douyinvod.com" in lowered or "tos-cn-ve" in lowered:
            return (1, len(lowered))
        if "mime_type=video_mp4" in lowered or "format=mp4" in lowered:
            return (2, len(lowered))
        if lowered.endswith(".mp4"):
            return (3, len(lowered))
        return (5, len(lowered))

    def _wait_for_preferred_video_urls(
        self,
        page,
        video_locator,
        video_urls: List[str],
        preview_urls: List[str],
        seen_urls: set[str],
        audio_urls: List[str],
    ) -> None:
        deadline = time.monotonic() + max(
            self.after_play_wait_ms / 1000.0,
            3.0,
        )
        dash_seen_at: Optional[float] = None
        while time.monotonic() < deadline:
            src = self._read_video_src(video_locator)
            if src and src not in seen_urls:
                seen_urls.add(src)
                if self._is_preview_video_url(src):
                    preview_urls.append(src)
                elif self._looks_like_video(src, ""):
                    video_urls.insert(0, src)
            if any(self._is_preferred_video_url(url) for url in video_urls):
                if dash_seen_at is None and any(
                    "media-video" in url.lower() for url in video_urls
                ):
                    # 分流长视频的音频流请求可能滞后于视频流，见到 media-video 特征后
                    # 再给有界宽限等 audio 候选出现，避免提前退出丢音轨
                    dash_seen_at = time.monotonic()
                if dash_seen_at is None:
                    return
                if any(self._is_audio_like_url(url) for url in audio_urls):
                    return
                if time.monotonic() - dash_seen_at >= self.dash_audio_grace_ms / 1000.0:
                    return
            page.wait_for_timeout(500)

    @staticmethod
    def _read_video_src(video_locator) -> str:
        try:
            return str(video_locator.evaluate("(node) => node.currentSrc || node.src || ''")).strip()
        except Exception:
            return ""

    @staticmethod
    def _summarize_errors(errors: Sequence[str]) -> List[str]:
        counts = {}
        for item in errors:
            counts[item] = counts.get(item, 0) + 1
        summary = []
        for message, count in counts.items():
            suffix = f" x{count}" if count > 1 else ""
            summary.append(f"{message}{suffix}")
        return summary[:3]

    @staticmethod
    def _format_browser_error(exc: Exception) -> str:
        message = re.sub(r"\s+", " ", str(exc).strip() or exc.__class__.__name__)
        if "Executable doesn't exist" in message:
            return f"浏览器未安装，请先运行 `{PLAYWRIGHT_INSTALL_HINT}`"
        if "Target page, context or browser has been closed" in message:
            return "浏览器上下文已关闭"
        return message

    def load_cookies(self, url: str = "https://www.douyin.com/") -> List[dict]:
        if self._context is not None:
            try:
                return list(self._context.cookies([url]))
            except Exception:
                return list(self._context.cookies())
        return self._load_persisted_cookies()

    def _persist_storage_state(self) -> None:
        if self._context is None:
            return
        try:
            state = self._context.storage_state()
            payload = json.dumps(state, ensure_ascii=False)
        except Exception:
            return
        try:
            with self._storage_state_lock:
                write_storage_state(self.storage_state_path, payload)
        except Exception:
            pass

    def _load_persisted_cookies(self) -> List[dict]:
        if not self.storage_state_path.exists():
            return []
        try:
            with self._storage_state_lock:
                payload = json.loads(self.storage_state_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if not isinstance(cookies, list):
            return []
        return [cookie for cookie in cookies if isinstance(cookie, dict)]

    def _throttle(self) -> None:
        if self.request_coordinator is None:
            return
        self.request_coordinator.wait(
            min_interval=DOUYIN_REQUEST_MIN_INTERVAL_SECONDS,
            max_interval=DOUYIN_REQUEST_MAX_INTERVAL_SECONDS,
            sleep_fn=time.sleep,
            jitter_fn=random.uniform,
            monotonic_fn=time.monotonic,
        )

    def _note_challenge(self, reason: str = "") -> None:
        if self.request_coordinator is None:
            return
        self.request_coordinator.note_challenge(
            base_cooldown_seconds=DOUYIN_CHALLENGE_BASE_COOLDOWN_SECONDS,
            max_cooldown_seconds=DOUYIN_CHALLENGE_MAX_COOLDOWN_SECONDS,
            monotonic_fn=time.monotonic,
            reason=reason,
        )

    def _note_success(self) -> None:
        if self.request_coordinator is None:
            return
        self.request_coordinator.note_success()

    @staticmethod
    def _looks_like_challenge_page(title: str, page_text: str) -> bool:
        normalized_title = (title or "").strip()
        normalized_text = (page_text or "").strip()
        lowered = normalized_text.lower()
        return (
            "验证码" in normalized_title
            or "验证码中间页" in normalized_text
            or "captcha/index.js" in lowered
            or "sec_sdk_build" in lowered
        )

    @staticmethod
    def _video_file_has_stream(path: Path, codec_type: str = "video") -> bool:
        try:
            probe = FfmpegService().probe_media(path)
        except Exception:
            return False
        streams = probe.get("streams") or []
        return any(stream.get("codec_type") == codec_type for stream in streams)

    @classmethod
    def _video_file_has_audio(cls, path: Path) -> bool:
        return cls._video_file_has_stream(path, "audio")
