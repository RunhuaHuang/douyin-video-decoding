import json
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .media_service import FfmpegService
from .config import (
    DOUYIN_CHALLENGE_BASE_COOLDOWN_SECONDS,
    DOUYIN_CHALLENGE_MAX_COOLDOWN_SECONDS,
    DOUYIN_REQUEST_MAX_INTERVAL_SECONDS,
    DOUYIN_REQUEST_MIN_INTERVAL_SECONDS,
    PLAYWRIGHT_AFTER_LOAD_WAIT_MS,
    PLAYWRIGHT_AFTER_PLAY_WAIT_MS,
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


@dataclass(frozen=True)
class BrowserCapture:
    page_url: str
    title: str
    video_urls: Tuple[str, ...]
    audio_urls: Tuple[str, ...] = ()
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
        headless: bool = PLAYWRIGHT_HEADLESS,
        storage_state_path: Path = PLAYWRIGHT_STORAGE_STATE_PATH,
        request_coordinator: Optional[RequestCoordinator] = None,
    ) -> None:
        self.navigation_timeout_ms = navigation_timeout_ms
        self.network_idle_timeout_ms = network_idle_timeout_ms
        self.video_wait_timeout_ms = video_wait_timeout_ms
        self.after_load_wait_ms = after_load_wait_ms
        self.after_play_wait_ms = after_play_wait_ms
        self.headless = headless
        self.storage_state_path = Path(storage_state_path)
        self.request_coordinator = request_coordinator
        self._playwright = None
        self._browser = None
        self._context = None

    def close(self) -> None:
        if self._context is not None:
            self._persist_storage_state()
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def capture_video(self, share_url: str) -> BrowserCapture:
        page = self._ensure_context().new_page()
        video_urls: List[str] = []
        audio_urls: List[str] = []
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
            elif self._looks_like_video(response.url, content_type) and response.url not in seen_urls:
                seen_urls.add(response.url)
                video_urls.append(response.url)

        page.on("response", handle_response)
        try:
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
                page.wait_for_timeout(self.after_play_wait_ms)
                src = str(video_locator.evaluate("(node) => node.currentSrc || node.src || ''")).strip()
                if src and src not in seen_urls:
                    seen_urls.add(src)
                    video_urls.insert(0, src)
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
                self._note_challenge()
            else:
                self._note_success()

            self._persist_storage_state()

            return BrowserCapture(
                page_url=page.url,
                title=title or "抖音视频",
                video_urls=tuple(self._dedupe_urls(video_urls)),
                audio_urls=tuple(self._dedupe_urls(audio_urls)),
            )
        except Exception as exc:
            self._reset_runtime()
            raise RuntimeError(self._format_browser_error(exc)) from exc
        finally:
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
    ) -> BrowserDownloadResult:
        capture = self.capture_video(share_url)
        urls = self._prioritize_video_urls([*capture.video_urls, *candidate_urls])
        if not urls:
            raise RuntimeError("浏览器兜底未捕获到可下载的视频地址")

        require_embedded_audio = audio_output_path is not None and not capture.audio_urls
        source_url, attempts = self._download_candidate_urls(
            urls=urls,
            output_path=output_path,
            referer=capture.page_url,
            accept="video/webm,video/mp4,video/*;q=0.9,*/*;q=0.8",
            destination="video",
            validator=self._looks_like_media_payload,
            empty_error="浏览器兜底未捕获到可下载的视频地址",
            fail_error_prefix="浏览器兜底下载失败",
            post_validator=self._video_file_has_audio if require_embedded_audio else None,
            post_validation_error="视频不含音轨",
        )

        audio_source_url = ""
        audio_attempts = 0
        if audio_output_path is not None and capture.audio_urls:
            try:
                audio_source_url, audio_attempts = self._download_candidate_urls(
                    urls=capture.audio_urls,
                    output_path=audio_output_path,
                    referer=capture.page_url,
                    accept="audio/aac,audio/mp4,audio/*;q=0.9,*/*;q=0.8",
                    destination="audio",
                    validator=self._looks_like_audio_payload,
                    empty_error="浏览器兜底未捕获到可下载的音频地址",
                    fail_error_prefix="浏览器兜底音频下载失败",
                )
            except Exception as exc:
                audio_output_path.unlink(missing_ok=True)
                audio_source_url = ""
                audio_attempts = 0

        return BrowserDownloadResult(
            source_url=source_url,
            page_url=capture.page_url,
            attempts=attempts,
            audio_source_url=audio_source_url,
            audio_attempts=audio_attempts,
        )

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
    def _looks_like_video(url: str, content_type: str) -> bool:
        lowered_type = (content_type or "").lower()
        lowered_url = url.lower()
        return (
            "video" in lowered_type
            or "mime_type=video_mp4" in lowered_url
            or "format=mp4" in lowered_url
            or lowered_url.endswith(".mp4")
        )

    @staticmethod
    def _looks_like_audio(url: str, content_type: str) -> bool:
        lowered_type = (content_type or "").lower()
        lowered_url = url.lower()
        return (
            "audio" in lowered_type
            or "mime_type=audio" in lowered_url
            or "audio_type" in lowered_url
            or lowered_url.endswith((".m4a", ".mp3", ".aac"))
        )

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
                response = request_context.get(
                    url,
                    headers=self._build_media_headers(referer, accept=accept, destination=destination),
                    timeout=float(self.navigation_timeout_ms),
                    fail_on_status_code=False,
                    max_retries=0,
                )
                status = response.status
                if 200 <= status < 300:
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

                if status in {403, 429}:
                    self._note_challenge()
                errors.append(f"HTTP {status}")
            except Exception as exc:
                errors.append(self._format_browser_error(exc))

        summary = ", ".join(self._summarize_errors(errors))
        raise RuntimeError(f"{fail_error_prefix}: {summary}")

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
        deduped = cls._dedupe_urls(urls)
        return sorted(deduped, key=cls._video_url_priority)

    @staticmethod
    def _video_url_priority(url: str) -> tuple[int, int]:
        lowered = (url or "").lower()
        if "lf-douyin-pc-web.douyinstatic.com" in lowered and "/uuu_" in lowered:
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
            self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            with self._storage_state_lock:
                self._context.storage_state(path=str(self.storage_state_path))
        except Exception:
            return

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

    def _note_challenge(self) -> None:
        if self.request_coordinator is None:
            return
        self.request_coordinator.note_challenge(
            base_cooldown_seconds=DOUYIN_CHALLENGE_BASE_COOLDOWN_SECONDS,
            max_cooldown_seconds=DOUYIN_CHALLENGE_MAX_COOLDOWN_SECONDS,
            monotonic_fn=time.monotonic,
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
    def _video_file_has_audio(path: Path) -> bool:
        try:
            probe = FfmpegService().probe_media(path)
        except Exception:
            return False
        streams = probe.get("streams") or []
        return any(stream.get("codec_type") == "audio" for stream in streams)
