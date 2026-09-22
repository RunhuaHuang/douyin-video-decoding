from __future__ import annotations

from pathlib import Path

import httpx

from app.browser_service import BrowserCapture, BrowserDownloadResult
from app.douyin_service import (
    DouyinService,
    DownloadForbiddenError,
    NonVideoDownloadError,
    ParsedVideo,
)


class FakeBrowserResolver:
    def __init__(self) -> None:
        self.capture_calls = 0
        self.download_calls = 0
        self.close_calls = 0

    def capture_video(self, share_url: str) -> BrowserCapture:
        self.capture_calls += 1
        return BrowserCapture(
            page_url="https://www.douyin.com/video/7411111111111111111",
            title="browser-title",
            video_urls=("https://browser.example.com/video.mp4",),
            audio_urls=("https://browser.example.com/audio.m4a",),
        )

    def download_video(
        self,
        share_url: str,
        output_path: Path,
        candidate_urls,
        audio_output_path: Path | None = None,
    ) -> BrowserDownloadResult:
        self.download_calls += 1
        output_path.write_bytes(b"browser-video")
        if audio_output_path is not None:
            audio_output_path.write_bytes(b"browser-audio")
        return BrowserDownloadResult(
            source_url="https://browser.example.com/video.mp4",
            page_url="https://www.douyin.com/video/7411111111111111111",
            attempts=1,
            audio_source_url="https://browser.example.com/audio.m4a" if audio_output_path is not None else "",
            audio_attempts=1 if audio_output_path is not None else 0,
        )

    def status(self) -> str:
        return "已就绪"

    def load_cookies(self, url: str = "https://www.douyin.com/"):
        return []

    def close(self) -> None:
        self.close_calls += 1


def test_parse_item_info_collects_multiple_download_urls():
    item = {
        "desc": "demo",
        "author": {"nickname": "tester"},
        "video": {
            "play_addr": {
                "url_list": [
                    "https://cdn.example.com/playwm/video_a.mp4",
                    "https://cdn.example.com/play/video_b.mp4",
                ]
            },
            "download_addr": {
                "url_list": [
                    "https://cdn.example.com/download/video_c.mp4",
                ]
            },
            "bit_rate": [
                {
                    "play_addr": {
                        "url_list": [
                            "https://cdn.example.com/play/video_d.mp4",
                        ]
                    }
                }
            ],
        },
    }

    parsed = DouyinService._parse_item_info(
        item,
        "7411111111111111111",
        share_url="https://v.douyin.com/example/",
        page_url="https://www.douyin.com/video/7411111111111111111",
        resolved_by="测试",
    )

    assert parsed.download_urls == (
        "https://cdn.example.com/play/video_a.mp4",
        "https://cdn.example.com/playwm/video_a.mp4",
        "https://cdn.example.com/play/video_b.mp4",
        "https://cdn.example.com/download/video_c.mp4",
        "https://cdn.example.com/play/video_d.mp4",
    )


def test_download_video_falls_back_to_next_candidate(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if "video_a" in str(request.url):
            return httpx.Response(403, request=request)
        return httpx.Response(200, request=request, content=b"ok-video")

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        browser_fallback_enabled=False,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    video = ParsedVideo(
        video_id="7411111111111111111",
        title="demo",
        author="tester",
        download_urls=(
            "https://cdn.example.com/play/video_a.mp4",
            "https://cdn.example.com/play/video_b.mp4",
        ),
        share_url="https://v.douyin.com/example/",
        page_url="https://www.douyin.com/video/7411111111111111111",
    )

    output_path = tmp_path / "video.mp4"
    result = service.download_video(video, output_path)
    service.close()

    assert output_path.read_bytes() == b"ok-video"
    assert result.source_url.endswith("video_b.mp4")
    assert any("video_a.mp4" in url for url in requests)
    assert any("video_b.mp4" in url for url in requests)


def test_download_video_uses_browser_fallback_after_direct_failures(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    resolver = FakeBrowserResolver()
    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        browser_resolver=resolver,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    video = ParsedVideo(
        video_id="7411111111111111111",
        title="demo",
        author="tester",
        download_urls=("https://cdn.example.com/play/video_a.mp4",),
        share_url="https://v.douyin.com/example/",
        page_url="https://www.douyin.com/video/7411111111111111111",
    )

    output_path = tmp_path / "video.mp4"
    audio_output_path = tmp_path / "fallback_audio.m4a"
    result = service.download_video(video, output_path, audio_output_path)
    service.close()

    assert output_path.read_bytes() == b"browser-video"
    assert audio_output_path.read_bytes() == b"browser-audio"
    assert result.strategy == "浏览器兜底"
    assert result.audio_source_url.endswith("audio.m4a")
    assert resolver.download_calls == 1


def test_parse_share_text_rejects_direct_note_as_non_video():
    service = DouyinService(
        browser_fallback_enabled=False,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    try:
        service.parse_share_text("https://www.douyin.com/note/7368733740719295779")
    except RuntimeError as exc:
        assert str(exc) == "链接类型为图文 note，非视频内容"
    else:
        raise AssertionError("expected direct note url to be treated as non-video")
    finally:
        service.close()


def test_parse_share_text_rejects_short_link_that_resolves_to_note():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "v.douyin.com" in url:
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://www.douyin.com/note/7368733740719295779"},
            )
        return httpx.Response(200, request=request, text="<html>note</html>")

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        browser_fallback_enabled=False,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    try:
        service.parse_share_text("https://v.douyin.com/example/")
    except RuntimeError as exc:
        assert str(exc) == "链接类型为图文 note，非视频内容"
    else:
        raise AssertionError("expected note redirect to be treated as non-video")
    finally:
        service.close()


def test_parse_share_text_rejects_video_url_that_browser_resolves_to_note():
    class NoteRedirectResolver(FakeBrowserResolver):
        def capture_video(self, share_url: str) -> BrowserCapture:
            return BrowserCapture(
                page_url="https://www.douyin.com/note/7341321026665499913",
                title="note-page",
                video_urls=(),
                audio_urls=(),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "iesdouyin.com/share/video/" in url:
            return httpx.Response(200, request=request, text="<html>empty</html>")
        if "iteminfo" in url or "aweme/detail" in url:
            return httpx.Response(200, request=request, text="not json")
        return httpx.Response(200, request=request, text="<html></html>")

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        browser_resolver=NoteRedirectResolver(),
        browser_fallback_enabled=True,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    try:
        service.parse_share_text("https://www.douyin.com/video/7341321026665499913")
    except RuntimeError as exc:
        assert str(exc) == "链接类型为图文 note，非视频内容"
    else:
        raise AssertionError("expected browser note redirect to be treated as non-video")
    finally:
        service.close()


def test_parse_share_text_uses_browser_when_interfaces_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "v.douyin.com" in url:
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://www.douyin.com/video/7411111111111111111"},
            )
        if "iesdouyin.com/share/video/" in url:
            return httpx.Response(200, request=request, text="<html>empty</html>")
        if "iteminfo" in url or "aweme/detail" in url:
            return httpx.Response(200, request=request, text="not json")
        return httpx.Response(404, request=request)

    resolver = FakeBrowserResolver()
    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        browser_resolver=resolver,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    parsed = service.parse_share_text("复制打开抖音 https://v.douyin.com/example/")
    service.close()

    assert parsed.resolved_by == "浏览器兜底"
    assert parsed.title == "browser-title"
    assert parsed.download_url == "https://browser.example.com/video.mp4"
    assert resolver.capture_calls == 1


def test_parse_share_text_keeps_retryable_when_interfaces_fail_and_browser_only_has_preview():
    class PreviewOnlyResolver(FakeBrowserResolver):
        def capture_video(self, share_url: str) -> BrowserCapture:
            return BrowserCapture(
                page_url="https://www.douyin.com/video/7337254060967447834",
                title="在抖音记录美好生活20260309 - 抖音",
                video_urls=(),
                audio_urls=(),
                preview_urls=("https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/uuu_265.mp4",),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "iesdouyin.com/share/video/" in url:
            return httpx.Response(200, request=request, text="<html>empty</html>")
        if "iteminfo" in url or "aweme/detail" in url:
            return httpx.Response(200, request=request, text="not json")
        return httpx.Response(200, request=request, text="<html></html>")

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        browser_resolver=PreviewOnlyResolver(),
        browser_fallback_enabled=True,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    try:
        service.parse_share_text("https://www.douyin.com/video/7337254060967447834")
    except RuntimeError as exc:
        assert "浏览器兜底: 浏览器兜底命中通用页，仅捕获到预览片段" in str(exc)
    else:
        raise AssertionError("expected preview-only browser parse to stay retryable")
    finally:
        service.close()


def test_parse_share_text_syncs_browser_cookies_to_http_client():
    class CookieResolver(FakeBrowserResolver):
        def load_cookies(self, url: str = "https://www.douyin.com/"):
            return [
                {
                    "name": "ttwid",
                    "value": "cookie-from-browser",
                    "domain": ".douyin.com",
                    "path": "/",
                }
            ]

    seen_cookie_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookie_headers.append(request.headers.get("cookie", ""))
        url = str(request.url)
        if "v.douyin.com" in url:
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://www.douyin.com/video/7411111111111111111"},
            )
        if "iesdouyin.com/share/video/" in url:
            return httpx.Response(200, request=request, text="<html>empty</html>")
        if "iteminfo" in url:
            return httpx.Response(
                200,
                request=request,
                json={
                    "item_list": [
                        {
                            "desc": "demo",
                            "author": {"nickname": "tester"},
                            "video": {
                                "play_addr": {
                                    "url_list": ["https://cdn.example.com/play/video_a.mp4"],
                                }
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404, request=request)

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        browser_resolver=CookieResolver(),
        browser_first=False,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    parsed = service.parse_share_text("复制打开抖音 https://v.douyin.com/example/")
    service.close()

    assert parsed.download_url
    assert any("ttwid=cookie-from-browser" in header for header in seen_cookie_headers)


def test_detail_api_keeps_real_browser_cookie_instead_of_temporary():
    class CookieResolver(FakeBrowserResolver):
        def load_cookies(self, url: str = "https://www.douyin.com/"):
            return [
                {
                    "name": "ttwid",
                    "value": "cookie-from-browser",
                    "domain": ".douyin.com",
                    "path": "/",
                }
            ]

    seen_detail_cookie_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "aweme/detail" in url:
            seen_detail_cookie_headers.append(request.headers.get("cookie", ""))
            return httpx.Response(
                200,
                request=request,
                json={
                    "aweme_detail": {
                        "desc": "detail-demo",
                        "author": {"nickname": "tester"},
                        "video": {
                            "play_addr": {
                                "url_list": ["https://cdn.example.com/play/video_a.mp4"],
                            }
                        },
                    }
                },
            )
        return httpx.Response(404, request=request)

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        browser_resolver=CookieResolver(),
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    service._sync_client_cookies_from_browser()
    parsed = service._parse_from_detail_api(
        "7411111111111111111",
        share_url="https://v.douyin.com/example/",
        page_url="https://www.douyin.com/video/7411111111111111111",
    )
    service.close()

    assert parsed.title == "detail-demo"
    assert seen_detail_cookie_headers
    assert all("cookie-from-browser" in header for header in seen_detail_cookie_headers)
    assert all("temporary" not in header for header in seen_detail_cookie_headers)


def test_browser_video_url_priority_filters_preview_urls():
    from app.browser_service import PlaywrightFallback

    urls = [
        "https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/uuu_265.mp4",
        "https://v26-web.douyinvod.com/abc/video/tos/cn/tos-cn-ve-15/test/?mime_type=video_mp4&__vid=1",
        "https://cdn.example.com/fallback.mp4",
    ]

    prioritized = PlaywrightFallback._prioritize_video_urls(urls)

    assert "douyinvod.com" in prioritized[0]
    assert all("uuu_265.mp4" not in url for url in prioritized)


def test_parse_with_browser_raises_for_preview_only_result():
    class PreviewOnlyResolver(FakeBrowserResolver):
        def capture_video(self, share_url: str) -> BrowserCapture:
            return BrowserCapture(
                page_url=share_url,
                title="在抖音记录美好生活20260309 - 抖音",
                video_urls=(),
                audio_urls=(),
                preview_urls=("https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/uuu_265.mp4",),
            )

    service = DouyinService(browser_resolver=PreviewOnlyResolver(), browser_fallback_enabled=True)
    try:
        service._parse_with_browser("https://www.douyin.com/video/7336856430550601010")
    except RuntimeError as exc:
        assert str(exc) == "浏览器兜底命中通用页，仅捕获到预览片段"
    else:
        raise AssertionError("expected preview-only browser result to be retryable")
    finally:
        service.close()


def test_parse_with_browser_marks_deleted_only_without_media():
    class DeletedResolver(FakeBrowserResolver):
        def capture_video(self, share_url: str) -> BrowserCapture:
            return BrowserCapture(
                page_url=share_url,
                title="在抖音记录美好生活20260309 - 抖音",
                video_urls=(),
                audio_urls=(),
            )

    service = DouyinService(browser_resolver=DeletedResolver(), browser_fallback_enabled=True)
    parsed = service._parse_with_browser("https://www.douyin.com/video/7336856430550601010")
    service.close()

    assert parsed is not None
    assert parsed.is_deleted is True
    assert parsed.download_urls == ()


def test_parse_with_browser_recycles_resolver_after_each_use():
    resolver = FakeBrowserResolver()
    service = DouyinService(browser_resolver=resolver, browser_fallback_enabled=True)
    try:
        parsed = service._parse_with_browser("https://www.douyin.com/video/7336856430550601010")
        assert parsed is not None
        assert resolver.close_calls == 1
    finally:
        service.close()


def test_download_via_browser_recycles_resolver_after_each_use(tmp_path):
    resolver = FakeBrowserResolver()
    service = DouyinService(browser_resolver=resolver, browser_fallback_enabled=True)
    video = ParsedVideo(
        video_id="7411111111111111111",
        title="demo",
        author="tester",
        download_urls=("https://browser.example.com/video.mp4",),
        share_url="https://www.douyin.com/video/7411111111111111111",
        page_url="https://www.douyin.com/video/7411111111111111111",
        resolved_by="浏览器兜底",
    )

    try:
        result = service._download_via_browser(video, tmp_path / "video.mp4", tmp_path / "audio.m4a")
        assert result is not None
        assert resolver.close_calls == 1
    finally:
        service.close()


def test_resolve_browser_preview_result_keeps_retryable_when_initial_page_is_challenge():
    browser_video = ParsedVideo(
        video_id="7336856430550601010",
        title="在抖音记录美好生活20260309 - 抖音",
        author="tester",
        download_urls=(),
        share_url="https://www.douyin.com/video/7336856430550601010",
        page_url="https://www.douyin.com/video/7336856430550601010",
        resolved_by="浏览器兜底",
    )

    service = DouyinService(browser_fallback_enabled=True)
    resolved = service._resolve_browser_preview_result(browser_video, initial_page_was_challenge=True)
    service.close()

    assert resolved is None


def test_resolve_browser_preview_result_keeps_retryable_when_not_challenge():
    browser_video = ParsedVideo(
        video_id="7337254060967447834",
        title="在抖音记录美好生活20260309 - 抖音",
        author="tester",
        download_urls=(),
        share_url="https://www.douyin.com/video/7337254060967447834",
        page_url="https://www.douyin.com/video/7337254060967447834",
        resolved_by="浏览器兜底",
    )

    service = DouyinService(browser_fallback_enabled=True)
    resolved = service._resolve_browser_preview_result(browser_video, initial_page_was_challenge=False)
    service.close()

    assert resolved is None


def test_parse_share_text_browser_first_skips_direct_attempts():
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(str(request.url))
        return httpx.Response(403, request=request)

    resolver = FakeBrowserResolver()
    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        browser_resolver=resolver,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    parsed = service.parse_share_text("https://www.douyin.com/video/7411111111111111111")
    service.close()

    assert parsed.resolved_by == "浏览器兜底"
    assert parsed.download_url == "https://browser.example.com/video.mp4"
    assert resolver.capture_calls == 1
    assert seen_requests == []


def test_download_video_browser_first_prefers_browser(tmp_path):
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(str(request.url))
        return httpx.Response(403, request=request)

    resolver = FakeBrowserResolver()
    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        browser_resolver=resolver,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    video = ParsedVideo(
        video_id="7411111111111111111",
        title="demo",
        author="tester",
        download_urls=("https://cdn.example.com/play/video_a.mp4",),
        share_url="https://v.douyin.com/example/",
        page_url="https://www.douyin.com/video/7411111111111111111",
        resolved_by="解析接口",
    )

    output_path = tmp_path / "video.mp4"
    result = service.download_video(video, output_path, tmp_path / "browser_audio.m4a")
    service.close()

    assert result.strategy == "浏览器兜底"
    assert resolver.download_calls == 1
    assert output_path.read_bytes() == b"browser-video"
    assert seen_requests == []


def test_stream_download_notes_challenge_when_html_captcha_page_unread(tmp_path):
    # 迭代器 content 保持 _content 未加载，模拟真实流式响应下 .text 抛 ResponseNotRead 的场景
    html_chunks = [
        b"<html><head><script src=\"https://lf-cdn-tos.bytescm.com/obj/static/captcha/index.js\"></script></head>",
        b"<body>sec_sdk_build</body></html>",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
            content=iter(html_chunks),
        )

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        browser_fallback_enabled=False,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    output_path = tmp_path / "video.mp4"
    try:
        service._stream_download("https://cdn.example.com/play/video_a.mp4", output_path, {})
    except RuntimeError as exc:
        assert str(exc) == "返回内容不是有效视频"
    else:
        raise AssertionError("expected html captcha page to be rejected as invalid video")
    finally:
        reasons = service.consume_attempt_challenge_signal_reasons()
        service.close()

    assert not output_path.exists()
    assert "验证码挑战页" in reasons


def test_stream_download_removes_partial_file_when_stream_aborts(tmp_path):
    def broken_stream():
        yield b"partial-video-bytes"
        raise RuntimeError("connection reset")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "video/mp4"},
            content=broken_stream(),
        )

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        browser_fallback_enabled=False,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    output_path = tmp_path / "video.mp4"
    try:
        service._stream_download("https://cdn.example.com/play/video_a.mp4", output_path, {})
    except RuntimeError as exc:
        assert "connection reset" in str(exc)
    else:
        raise AssertionError("expected mid-stream failure to propagate")
    finally:
        service.close()

    assert not output_path.exists()


def test_extract_first_url_prefers_douyin_domain():
    text = "先看 https://example.com/intro 再复制 https://v.douyin.com/AbC123/ 到浏览器"

    assert DouyinService.extract_first_url(text) == "https://v.douyin.com/AbC123/"


def test_extract_first_url_falls_back_to_first_url_when_no_douyin_link():
    text = "参考 https://example.com/a 和 https://another.example.com/b"

    assert DouyinService.extract_first_url(text) == "https://example.com/a"


def test_extract_first_url_strips_trailing_quote_and_bracket_chars():
    text = '分享"<https://www.douyin.com/video/7411111111111111111>"'

    assert DouyinService.extract_first_url(text) == "https://www.douyin.com/video/7411111111111111111"


class DownloadForbiddenBrowserResolver(FakeBrowserResolver):
    def download_video(self, share_url, output_path, candidate_urls, audio_output_path=None):
        raise RuntimeError("浏览器兜底下载失败: HTTP 403 x2")


class Download404BrowserResolver(FakeBrowserResolver):
    def download_video(self, share_url, output_path, candidate_urls, audio_output_path=None):
        raise RuntimeError("浏览器兜底下载失败: 返回内容不是有效媒体, HTTP 404")


def test_download_video_raises_forbidden_error_when_all_paths_hit_403(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        browser_resolver=DownloadForbiddenBrowserResolver(),
        browser_first=False,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    video = ParsedVideo(
        video_id="7411111111111111111",
        title="demo",
        author="tester",
        download_urls=("https://cdn.example.com/playwm/video_a.mp4",),
        share_url="https://v.douyin.com/example/",
        page_url="https://www.douyin.com/video/7411111111111111111",
        resolved_by="解析接口",
    )

    try:
        service.download_video(video, tmp_path / "video.mp4")
    except RuntimeError as exc:
        assert type(exc) is DownloadForbiddenError
        assert str(exc) == "HTTP 403 Forbidden x2, 浏览器兜底失败: 浏览器兜底下载失败: HTTP 403 x2"
    else:
        raise AssertionError("expected DownloadForbiddenError when all paths hit 403")
    finally:
        service.close()


def test_download_video_raises_non_video_error_for_note_404_pattern(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "cdn.example.com/playwm/" in url:
            return httpx.Response(404, request=request)
        if "cdn.example.com/play/" in url:
            return httpx.Response(200, request=request, text="<html><body>note page</body></html>")
        return httpx.Response(200, request=request, text="<html></html>")

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        browser_resolver=Download404BrowserResolver(),
        browser_first=False,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    video = ParsedVideo(
        video_id="7411111111111111111",
        title="demo",
        author="tester",
        download_urls=("https://cdn.example.com/playwm/video_a.mp4",),
        share_url="https://v.douyin.com/example/",
        page_url="https://www.douyin.com/video/7411111111111111111",
        resolved_by="解析接口",
    )

    try:
        service.download_video(video, tmp_path / "video.mp4")
    except RuntimeError as exc:
        assert type(exc) is NonVideoDownloadError
        assert str(exc) == (
            "返回内容不是有效视频, HTTP 404 Not Found, "
            "浏览器兜底失败: 浏览器兜底下载失败: 返回内容不是有效媒体, HTTP 404"
        )
    else:
        raise AssertionError("expected NonVideoDownloadError for note-style 404 pattern")
    finally:
        service.close()


def test_download_video_keeps_plain_runtime_error_for_other_failures(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    service = DouyinService(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        browser_fallback_enabled=False,
        browser_first=False,
        request_interval_range=(0.0, 0.0),
        sleep_fn=lambda _: None,
        jitter_fn=lambda a, b: 0.0,
    )

    video = ParsedVideo(
        video_id="7411111111111111111",
        title="demo",
        author="tester",
        download_urls=("https://cdn.example.com/play/video_a.mp4",),
        share_url="https://v.douyin.com/example/",
        page_url="https://www.douyin.com/video/7411111111111111111",
        resolved_by="解析接口",
    )

    try:
        service.download_video(video, tmp_path / "video.mp4")
    except RuntimeError as exc:
        assert type(exc) is RuntimeError
        assert "HTTP 500 Internal Server Error" in str(exc)
    else:
        raise AssertionError("expected plain RuntimeError for unclassified failures")
    finally:
        service.close()
