import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx

from .browser_service import CHROME_USER_AGENT, PlaywrightFallback
from .config import (
    DOUYIN_BROWSER_FALLBACK_ENABLED,
    DOUYIN_BROWSER_FIRST,
    DOUYIN_CHALLENGE_BASE_COOLDOWN_SECONDS,
    DOUYIN_CHALLENGE_MAX_COOLDOWN_SECONDS,
    DOUYIN_DOWNLOAD_TIMEOUT_SECONDS,
    DOUYIN_REQUEST_MAX_INTERVAL_SECONDS,
    DOUYIN_REQUEST_MIN_INTERVAL_SECONDS,
    DOUYIN_REQUEST_TIMEOUT_SECONDS,
)
from .request_coordinator import RequestCoordinator


COMMON_BROWSER_HEADERS = {
    "User-Agent": CHROME_USER_AGENT,
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

MOBILE_PARSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
    "Upgrade-Insecure-Requests": "1",
}

PAGE_HEADERS = {
    **COMMON_BROWSER_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": "https://www.douyin.com/",
    "Upgrade-Insecure-Requests": "1",
}

API_HEADERS = {
    **COMMON_BROWSER_HEADERS,
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.douyin.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

VIDEO_HEADERS = {
    **COMMON_BROWSER_HEADERS,
    "Accept": "*/*",
    "Referer": "https://www.douyin.com/",
    "Range": "bytes=0-",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}


@dataclass
class ParsedVideo:
    video_id: str
    title: str
    author: str
    download_urls: Tuple[str, ...]
    share_url: str = ""
    page_url: str = ""
    resolved_by: str = "解析接口"
    is_deleted: bool = False
    likes: int = 0
    comments: int = 0
    shares: int = 0
    cover_url: str = ""

    @property
    def download_url(self) -> str:
        return self.download_urls[0] if self.download_urls else ""


@dataclass(frozen=True)
class DownloadResult:
    source_url: str
    strategy: str
    attempts: int
    audio_source_url: str = ""
    audio_attempts: int = 0


class DownloadForbiddenError(RuntimeError):
    """下载被连续拒绝（403 限流）：直连候选与浏览器兜底均命中 403。"""


class NonVideoDownloadError(RuntimeError):
    """下载目标不是视频：图文 note 的地址返回 404/无效内容。"""


class DouyinService:
    NON_VIDEO_NOTE_ERROR = "链接类型为图文 note，非视频内容"

    def __init__(
        self,
        timeout: float = DOUYIN_REQUEST_TIMEOUT_SECONDS,
        download_timeout: float = DOUYIN_DOWNLOAD_TIMEOUT_SECONDS,
        *,
        client: Optional[httpx.Client] = None,
        browser_resolver: Optional[PlaywrightFallback] = None,
        request_interval_range: Tuple[float, float] = (
            DOUYIN_REQUEST_MIN_INTERVAL_SECONDS,
            DOUYIN_REQUEST_MAX_INTERVAL_SECONDS,
        ),
        sleep_fn: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[float, float], float] = random.uniform,
        monotonic_fn: Callable[[], float] = time.monotonic,
        browser_fallback_enabled: bool = DOUYIN_BROWSER_FALLBACK_ENABLED,
        browser_first: bool = DOUYIN_BROWSER_FIRST,
        request_coordinator: Optional[RequestCoordinator] = None,
    ) -> None:
        self.client = client or self._build_http_client(timeout)
        self._owns_client = client is None
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.browser_resolver = browser_resolver
        self.browser_fallback_enabled = browser_fallback_enabled
        self.browser_first = bool(browser_first)
        self.request_interval_range = request_interval_range
        self.sleep_fn = sleep_fn
        self.jitter_fn = jitter_fn
        self.monotonic_fn = monotonic_fn
        self.request_coordinator = request_coordinator or RequestCoordinator()

    @staticmethod
    def _build_http_client(timeout: float) -> httpx.Client:
        client_kwargs = {
            "follow_redirects": True,
            "timeout": httpx.Timeout(connect=10.0, read=timeout, write=timeout, pool=timeout),
        }
        try:
            return httpx.Client(http2=True, **client_kwargs)
        except ImportError:
            return httpx.Client(**client_kwargs)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
        if self.browser_resolver is not None:
            self.browser_resolver.close()

    def reset_attempt_signals(self) -> None:
        self.request_coordinator.reset_attempt_signal()

    def consume_attempt_challenge_signal(self) -> bool:
        return self.request_coordinator.consume_attempt_signal()

    def consume_attempt_challenge_signal_reasons(self) -> Tuple[str, ...]:
        return self.request_coordinator.consume_attempt_signal_reasons()

    def parse_share_text(self, share_text: str) -> ParsedVideo:
        share_url = self.extract_first_url(share_text)
        if not share_url:
            raise RuntimeError("未在单元格中找到有效的抖音链接")
        if self.is_note_url(share_url):
            raise RuntimeError(self.NON_VIDEO_NOTE_ERROR)

        self._sync_client_cookies_from_browser()
        # 浏览器优先：直接用账号池会话解析，跳过直连尝试；
        # 浏览器不可用（如未安装）时仍退回直连解析链路
        if self.browser_first and self.browser_fallback_enabled:
            browser_video, browser_error = self._try_parse_with_browser(share_url)
            if browser_error == self.NON_VIDEO_NOTE_ERROR:
                raise RuntimeError(self.NON_VIDEO_NOTE_ERROR)
            if browser_video is not None:
                return browser_video

        initial_page_was_challenge = False
        try:
            response = self._get(share_url, headers=MOBILE_PARSE_HEADERS)
            response.raise_for_status()
            final_url = str(response.url)
            if self.is_note_url(final_url):
                raise RuntimeError(self.NON_VIDEO_NOTE_ERROR)
            initial_page_was_challenge = self._looks_like_challenge_page(response.text)
        except Exception as exc:
            if self.NON_VIDEO_NOTE_ERROR in str(exc):
                raise RuntimeError(self.NON_VIDEO_NOTE_ERROR) from exc
            browser_video, browser_error = self._try_parse_with_browser(share_url)
            if browser_error == self.NON_VIDEO_NOTE_ERROR:
                raise RuntimeError(self.NON_VIDEO_NOTE_ERROR) from exc
            if browser_video is not None:
                return browser_video
            details = [f"短链解析失败: {exc}"]
            if browser_error:
                details.append(f"浏览器兜底: {browser_error}")
            raise RuntimeError(" | ".join(details)) from exc

        video_id = self.extract_video_id(final_url)

        if not video_id:
            browser_video, browser_error = self._try_parse_with_browser(share_url)
            if browser_error == self.NON_VIDEO_NOTE_ERROR:
                raise RuntimeError(self.NON_VIDEO_NOTE_ERROR)
            browser_video = self._resolve_browser_preview_result(
                browser_video,
                initial_page_was_challenge=initial_page_was_challenge,
            )
            if browser_video is not None:
                return browser_video
            details = [f"无法从链接中提取视频 ID: {final_url}"]
            if browser_error:
                details.append(f"浏览器兜底: {browser_error}")
            raise RuntimeError(" | ".join(details))

        parser_chain = (
            ("分享页", self._parse_from_share_page),
            ("iteminfo API", self._parse_from_iteminfo_api),
            ("detail API", self._parse_from_detail_api),
        )

        errors = []
        last_error: Optional[Exception] = None
        for parser_name, parser in parser_chain:
            try:
                video = parser(video_id, share_url=share_url, page_url=final_url)
                if video.is_deleted or video.download_urls:
                    return video
            except Exception as exc:
                last_error = exc
                errors.append(f"{parser_name}: {exc}")

        browser_video, browser_error = self._try_parse_with_browser(share_url)
        if browser_error == self.NON_VIDEO_NOTE_ERROR:
            raise RuntimeError(self.NON_VIDEO_NOTE_ERROR)
        browser_video = self._resolve_browser_preview_result(
            browser_video,
            initial_page_was_challenge=initial_page_was_challenge,
        )
        if browser_video is not None:
            return browser_video

        if last_error:
            if browser_error:
                errors.append(f"浏览器兜底: {browser_error}")
            raise RuntimeError("抖音解析失败: " + " | ".join(errors)) from last_error

        raise RuntimeError("抖音解析失败：所有策略都没有返回可下载视频地址")

    def download_video(
        self,
        video: ParsedVideo,
        output_path: Path,
        audio_output_path: Optional[Path] = None,
    ) -> DownloadResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        referer = video.page_url or video.share_url or "https://www.douyin.com/"
        self._sync_client_cookies_from_browser()

        errors: List[str] = []
        attempts = 0
        # 浏览器优先（或本来就是浏览器解析的）：先走浏览器会话下载，
        # 失败再退回直连地址
        if self.browser_fallback_enabled and (self.browser_first or video.resolved_by == "浏览器兜底"):
            try:
                browser_result = self._download_via_browser(video, output_path, audio_output_path)
            except Exception as exc:
                errors.append(self._describe_download_error(exc))
            else:
                if browser_result is not None:
                    return DownloadResult(
                        source_url=browser_result.source_url,
                        strategy="浏览器兜底",
                        attempts=browser_result.attempts,
                        audio_source_url=browser_result.audio_source_url,
                        audio_attempts=browser_result.audio_attempts,
                    )

        # 浏览器优先时，只有浏览器路径失败后才预热直连会话，避免无意义的直连请求。
        self._prime_session(video.page_url or video.share_url)
        download_headers = self._build_video_headers(referer)
        for url in self._candidate_download_urls(video.download_urls):
            attempts += 1
            try:
                self._stream_download(url, output_path, download_headers)
                return DownloadResult(
                    source_url=url,
                    strategy="稳定直连",
                    attempts=attempts,
                )
            except Exception as exc:
                errors.append(self._describe_download_error(exc))

        if self.browser_fallback_enabled:
            try:
                browser_result = self._download_via_browser(video, output_path, audio_output_path)
            except Exception as exc:
                errors.append(self._describe_download_error(exc))
            else:
                if browser_result is not None:
                    return DownloadResult(
                        source_url=browser_result.source_url,
                        strategy="浏览器兜底",
                        attempts=attempts + browser_result.attempts,
                        audio_source_url=browser_result.audio_source_url,
                        audio_attempts=browser_result.audio_attempts,
                    )
                errors.append("浏览器兜底不可用")

        summary = ", ".join(self._summarize_errors(errors))
        message = summary or "所有下载策略都失败了"
        # 结构化指纹：message 与旧字符串逐字一致，仅异常类型区分，供 pipeline 做 isinstance 判断
        direct_errors = [error for error in errors if "浏览器兜底" not in error]
        browser_errors = [error for error in errors if "浏览器兜底" in error]
        if direct_errors and all(error.startswith("HTTP 403") for error in direct_errors) and any(
            "403" in error for error in browser_errors
        ):
            raise DownloadForbiddenError(message)
        if (
            any(error == "返回内容不是有效视频" for error in errors)
            and any(error.startswith("HTTP 404") for error in errors)
            and any("返回内容不是有效媒体" in error and "404" in error for error in browser_errors)
        ):
            raise NonVideoDownloadError(message)
        raise RuntimeError(message)

    @staticmethod
    def extract_first_url(text: str) -> Optional[str]:
        matches = re.findall(r"http[s]?://[^\s]+", text)
        if not matches:
            return None
        douyin_matches = [
            match
            for match in matches
            if any(domain in match for domain in ("v.douyin.com", "www.douyin.com", "iesdouyin.com"))
        ]
        target = douyin_matches[0] if douyin_matches else matches[0]
        return target.rstrip("，。！？；,!?;）)]\"'“”‘’<>")

    @staticmethod
    def extract_video_id(url: str) -> Optional[str]:
        patterns = (
            r"/video/(\d+)",
            r"/note/(\d+)",
            r"/share/video/(\d+)",
            r"modal_id=(\d+)",
        )
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def is_note_url(url: str) -> bool:
        return bool(re.search(r"/note/\d+", url or ""))

    def _parse_from_share_page(self, video_id: str, *, share_url: str, page_url: str) -> ParsedVideo:
        share_page_url = f"https://www.iesdouyin.com/share/video/{video_id}"
        response = self._get(share_page_url, headers=MOBILE_PARSE_HEADERS)
        response.raise_for_status()

        match = re.search(
            r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            response.text,
            flags=re.DOTALL,
        )
        if not match:
            raise RuntimeError("分享页中未找到 _ROUTER_DATA")

        router_data = json.loads(match.group(1).strip())
        loader_data = router_data.get("loaderData", {})

        page_keys = ("video_(id)/page", "note_(id)/page")
        item = None
        for page_key in page_keys:
            page_data = loader_data.get(page_key)
            if page_data:
                item_list = page_data.get("videoInfoRes", {}).get("item_list", [])
                if item_list:
                    item = item_list[0]
                    break

        if not item:
            raise RuntimeError("分享页中未找到视频详情")

        return self._parse_item_info(item, video_id, share_url=share_url, page_url=page_url, resolved_by="分享页")

    def _parse_from_iteminfo_api(self, video_id: str, *, share_url: str, page_url: str) -> ParsedVideo:
        api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_id}"
        response = self._get(api_url, headers=API_HEADERS)
        response.raise_for_status()
        payload = response.json()
        item_list = payload.get("item_list", [])
        if not item_list:
            raise RuntimeError("iteminfo API 没有返回视频详情")
        return self._parse_item_info(item_list[0], video_id, share_url=share_url, page_url=page_url, resolved_by="iteminfo API")

    def _parse_from_detail_api(self, video_id: str, *, share_url: str, page_url: str) -> ParsedVideo:
        api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={video_id}&aid=6383"
        headers = dict(API_HEADERS)
        if not self._client_has_cookie("ttwid"):
            headers["Cookie"] = "ttwid=1%7Ctemporary"
        response = self._get(
            api_url,
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        detail = payload.get("aweme_detail")
        if not detail:
            raise RuntimeError("detail API 没有返回视频详情")
        return self._parse_item_info(detail, video_id, share_url=share_url, page_url=page_url, resolved_by="detail API")

    @staticmethod
    def _parse_item_info(
        item: dict,
        video_id: str,
        *,
        share_url: str,
        page_url: str,
        resolved_by: str,
    ) -> ParsedVideo:
        title = (item.get("desc") or item.get("title") or f"douyin_{video_id}").strip()
        author = item.get("author", {}).get("nickname", "未知作者")
        stats = item.get("statistics") or item.get("stats") or {}
        video = item.get("video") or {}
        url_list = DouyinService._collect_download_urls(video)
        if not url_list:
            raise RuntimeError("视频详情中没有播放地址")

        cover = video.get("cover") or {}
        cover_urls = cover.get("url_list") if isinstance(cover, dict) else None
        cover_url = cover_urls[0] if cover_urls else ""

        return ParsedVideo(
            video_id=video_id,
            title=title,
            author=author,
            download_urls=tuple(url_list),
            share_url=share_url,
            page_url=page_url,
            resolved_by=resolved_by,
            likes=int(stats.get("digg_count", 0) or stats.get("diggCount", 0) or 0),
            comments=int(stats.get("comment_count", 0) or stats.get("commentCount", 0) or 0),
            shares=int(stats.get("share_count", 0) or stats.get("shareCount", 0) or 0),
            cover_url=cover_url,
        )

    def browser_fallback_status(self) -> str:
        if not self.browser_fallback_enabled:
            return "已关闭"
        resolver = self._get_browser_resolver()
        if resolver is None:
            return "不可用"
        return resolver.status()

    def _parse_with_browser(self, share_url: str) -> Optional[ParsedVideo]:
        resolver = self._get_browser_resolver()
        if resolver is None:
            return None

        try:
            capture = resolver.capture_video(share_url)
            if self.is_note_url(capture.page_url):
                raise RuntimeError(self.NON_VIDEO_NOTE_ERROR)
            self._sync_client_cookies_from_browser()
            browser_urls = tuple(self._candidate_download_urls(capture.media_urls))
            if not browser_urls and capture.preview_urls:
                raise RuntimeError("浏览器兜底命中通用页，仅捕获到预览片段")
            if not browser_urls:
                if self._looks_like_deleted_title(capture.title):
                    video_id = self.extract_video_id(capture.page_url) or self.extract_video_id(share_url) or "browser"
                    return ParsedVideo(
                        video_id=video_id,
                        title=capture.title,
                        author=capture.author,
                        download_urls=(),
                        share_url=share_url,
                        page_url=capture.page_url,
                        resolved_by="浏览器兜底",
                        is_deleted=True,
                    )
                raise RuntimeError("浏览器兜底没有捕获到真实视频地址")

            video_id = self.extract_video_id(capture.page_url) or self.extract_video_id(share_url) or "browser"
            return ParsedVideo(
                video_id=video_id,
                title=capture.title,
                author=capture.author,
                download_urls=browser_urls,
                share_url=share_url,
                page_url=capture.page_url,
                resolved_by="浏览器兜底",
            )
        finally:
            self._recycle_browser_resolver(resolver)

    def _try_parse_with_browser(self, share_url: str) -> Tuple[Optional[ParsedVideo], str]:
        try:
            return self._parse_with_browser(share_url), ""
        except Exception as exc:
            return None, str(exc).strip() or exc.__class__.__name__

    def _resolve_browser_preview_result(
        self,
        browser_video: Optional[ParsedVideo],
        *,
        initial_page_was_challenge: bool,
    ) -> Optional[ParsedVideo]:
        if browser_video is None:
            return None
        if not browser_video.download_urls:
            return None if self._looks_like_deleted_title(browser_video.title) else browser_video
        return browser_video

    def _download_via_browser(
        self,
        video: ParsedVideo,
        output_path: Path,
        audio_output_path: Optional[Path] = None,
    ):
        resolver = self._get_browser_resolver()
        if resolver is None:
            return None

        source_url = video.share_url or video.page_url
        if not source_url:
            return None

        try:
            return resolver.download_video(source_url, output_path, video.download_urls, audio_output_path)
        except Exception as exc:
            raise RuntimeError(f"浏览器兜底失败: {exc}") from exc
        finally:
            self._recycle_browser_resolver(resolver)

    def _get_browser_resolver(self) -> Optional[PlaywrightFallback]:
        if not self.browser_fallback_enabled:
            return None
        if self.browser_resolver is None:
            self.browser_resolver = PlaywrightFallback(request_coordinator=self.request_coordinator)
        return self.browser_resolver

    @staticmethod
    def _recycle_browser_resolver(resolver: PlaywrightFallback) -> None:
        try:
            resolver.close()
        except Exception:
            return

    def _get(self, url: str, *, headers: Dict[str, str]) -> httpx.Response:
        self._throttle()
        response = self.client.get(url, headers=headers)
        self._note_response_result(response)
        return response

    def _stream_download(self, url: str, output_path: Path, headers: Dict[str, str]) -> None:
        self._throttle()
        buffer = bytearray()
        content_type = ""
        with self.client.stream(
            "GET",
            url,
            headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=self.download_timeout, write=self.download_timeout, pool=self.download_timeout),
        ) as response:
            self._note_response_result(response)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            with output_path.open("wb") as file_handle:
                try:
                    for chunk in response.iter_bytes():
                        if chunk:
                            if len(buffer) < 1024:
                                remaining = 1024 - len(buffer)
                                buffer.extend(chunk[:remaining])
                            file_handle.write(chunk)
                except Exception:
                    # 中途异常时清理半成品文件，避免误用截断内容
                    output_path.unlink(missing_ok=True)
                    raise
        if not self._looks_like_media_payload(bytes(buffer), content_type, url):
            # 流式响应未 read 时 .text 会抛 ResponseNotRead，前 1024 字节 buffer 判定 HTML 挑战页
            if self._looks_like_challenge_page(bytes(buffer).decode("utf-8", errors="ignore")):
                self._note_challenge("验证码挑战页")
            output_path.unlink(missing_ok=True)
            raise RuntimeError("返回内容不是有效视频")

    def _throttle(self) -> None:
        min_interval, max_interval = self.request_interval_range
        self.request_coordinator.wait(
            min_interval=min_interval,
            max_interval=max_interval,
            sleep_fn=self.sleep_fn,
            jitter_fn=self.jitter_fn,
            monotonic_fn=self.monotonic_fn,
        )

    def _prime_session(self, page_url: Optional[str]) -> None:
        if not page_url:
            return
        try:
            self._get(page_url, headers={**PAGE_HEADERS, "Referer": "https://www.douyin.com/"})
        except Exception:
            return

    def _sync_client_cookies_from_browser(self, url: str = "https://www.douyin.com/") -> None:
        resolver = self._get_browser_resolver()
        if resolver is None:
            return
        try:
            cookies = resolver.load_cookies(url)
        except Exception:
            return

        for cookie in cookies:
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            if not name:
                continue
            domain = str(cookie.get("domain") or "").strip()
            path = str(cookie.get("path") or "/").strip() or "/"
            kwargs = {"path": path}
            if domain:
                kwargs["domain"] = domain
            self.client.cookies.set(name, value, **kwargs)

    def _client_has_cookie(self, name: str) -> bool:
        target = (name or "").strip()
        if not target:
            return False
        return any(cookie_name == target for cookie_name in self.client.cookies.keys())

    def _note_response_result(self, response: httpx.Response) -> None:
        content_type = (response.headers.get("content-type", "") or "").lower()
        should_inspect_text = (
            response.status_code in {403, 429}
            or "text/html" in content_type
            or "application/xhtml+xml" in content_type
        )

        if should_inspect_text:
            try:
                body_text = response.text
            except Exception:
                body_text = ""
            if response.status_code == 403:
                self._note_challenge("HTTP 403")
                return
            if response.status_code == 429:
                self._note_challenge("HTTP 429")
                return
            if self._looks_like_challenge_page(body_text):
                self._note_challenge("验证码挑战页")
                return

        if 200 <= response.status_code < 300:
            self.request_coordinator.note_success()

    def _note_challenge(self, reason: str = "") -> None:
        self.request_coordinator.note_challenge(
            base_cooldown_seconds=DOUYIN_CHALLENGE_BASE_COOLDOWN_SECONDS,
            max_cooldown_seconds=DOUYIN_CHALLENGE_MAX_COOLDOWN_SECONDS,
            monotonic_fn=self.monotonic_fn,
            reason=reason,
        )

    @staticmethod
    def _build_video_headers(referer: str) -> Dict[str, str]:
        parsed = urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://www.douyin.com"
        return {
            **VIDEO_HEADERS,
            "Origin": origin,
            "Referer": referer,
        }

    @staticmethod
    def _candidate_download_urls(urls: Sequence[str]) -> List[str]:
        results: List[str] = []
        seen = set()
        for raw_url in urls:
            for candidate in DouyinService._expand_candidate_url(raw_url):
                if candidate not in seen:
                    seen.add(candidate)
                    results.append(candidate)
        return results

    @staticmethod
    def _expand_candidate_url(url: str) -> List[str]:
        source = (url or "").strip()
        if not source:
            return []
        candidates = [source]
        if "playwm" in source:
            candidates.insert(0, source.replace("playwm", "play"))
        return candidates

    @staticmethod
    def _collect_download_urls(video: dict) -> List[str]:
        candidates: List[str] = []
        direct_fields = (
            video.get("play_addr"),
            video.get("playAddr"),
            video.get("download_addr"),
            video.get("downloadAddr"),
        )
        for address in direct_fields:
            candidates.extend(DouyinService._extract_url_list(address))

        bit_rates = video.get("bit_rate") or video.get("bitRate") or []
        for bit_rate in bit_rates:
            candidates.extend(
                DouyinService._extract_url_list(bit_rate.get("play_addr") or bit_rate.get("playAddr"))
            )
            candidates.extend(
                DouyinService._extract_url_list(bit_rate.get("download_addr") or bit_rate.get("downloadAddr"))
            )

        return DouyinService._candidate_download_urls(candidates)

    @staticmethod
    def _extract_url_list(address: Optional[dict]) -> List[str]:
        if not isinstance(address, dict):
            return []
        url_list = address.get("url_list") or address.get("urlList") or []
        return [str(url) for url in url_list if str(url).strip()]

    @staticmethod
    def _describe_download_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            return f"HTTP {exc.response.status_code} {exc.response.reason_phrase}"
        if isinstance(exc, httpx.ReadTimeout):
            return "读取超时"
        if isinstance(exc, httpx.ConnectTimeout):
            return "连接超时"
        if isinstance(exc, httpx.TimeoutException):
            return "请求超时"
        if isinstance(exc, httpx.ConnectError):
            return "连接失败"
        return str(exc).strip() or exc.__class__.__name__

    @staticmethod
    def _summarize_errors(errors: Sequence[str]) -> List[str]:
        counts: Dict[str, int] = {}
        for message in errors:
            counts[message] = counts.get(message, 0) + 1
        results = []
        for message, count in counts.items():
            suffix = f" x{count}" if count > 1 else ""
            results.append(f"{message}{suffix}")
        return results[:4]

    @staticmethod
    def _looks_like_deleted_title(title: str) -> bool:
        normalized = (title or "").strip()
        return normalized.startswith("在抖音记录美好生活")

    @staticmethod
    def _looks_like_challenge_page(text: str) -> bool:
        normalized = (text or "").strip()
        lowered = normalized.lower()
        return (
            "验证码中间页" in normalized
            or "captcha/index.js" in lowered
            or "sec_sdk_build" in lowered
        )

    @staticmethod
    def _has_non_preview_browser_media(urls: Sequence[str]) -> bool:
        return any(not DouyinService._is_browser_preview_url(url) for url in urls)

    @staticmethod
    def _is_browser_preview_url(url: str) -> bool:
        lowered = (url or "").lower()
        return "lf-douyin-pc-web.douyinstatic.com" in lowered and "/uuu_" in lowered

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
