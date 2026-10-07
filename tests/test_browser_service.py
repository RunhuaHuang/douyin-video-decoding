from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.browser_service import BrowserCapture, PlaywrightFallback
from app.config import PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS


class _DummyResponse:
    def __init__(self, status: int, body: bytes, content_type: str) -> None:
        self.status = status
        self._body = body
        self.headers = {"content-type": content_type}

    def body(self) -> bytes:
        return self._body


class _DummyRequest:
    def __init__(self, responses: dict[str, _DummyResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.timeouts: list[float] = []

    def get(self, url, headers=None, timeout=None, fail_on_status_code=False, max_retries=0):
        self.calls.append(url)
        self.timeouts.append(timeout)
        return self.responses[url]


class _DummyContext:
    def __init__(self, responses: dict[str, _DummyResponse]) -> None:
        self.request = _DummyRequest(responses)


class _StreamingResponse:
    status_code = 200
    headers = {"content-type": "video/mp4"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_bytes(self):
        yield b"\x00\x00\x00\x18ftypisom"
        yield b"payload"


class _StreamingClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, *args, **kwargs):
        return _StreamingResponse()


DASH_AUDIO_URL = (
    "https://v95-zjwz2tc-web.douyinvod.com/video/tos/cn/tos-cn-ve-15/oUM4p1Av/"
    "media-audio-und-mp4a/?a=6383&mime_type=video_mp4&qs=0&rc=PDhn"
)
DASH_VIDEO_URL = (
    "https://v95-zjwz2tc-web.douyinvod.com/video/tos/cn/tos-cn-vd-0026/oMCUDI4g/"
    "media-video-avc1/?a=6383&mime_type=video_mp4&qs=1&rc=Ojw3"
)


def test_dash_stream_urls_are_classified_by_path():
    # 网页端分流视频：音频流 query 里带 mime_type=video_mp4，必须按 media-audio 路径识别
    assert not PlaywrightFallback._looks_like_video(DASH_AUDIO_URL, "video/mp4")
    assert PlaywrightFallback._looks_like_audio(DASH_AUDIO_URL, "")
    assert PlaywrightFallback._looks_like_video(DASH_VIDEO_URL, "video/mp4")
    assert not PlaywrightFallback._looks_like_audio(DASH_VIDEO_URL, "")


def test_large_media_path_streams_to_disk_without_playwright_body(monkeypatch, tmp_path):
    fallback = PlaywrightFallback()
    monkeypatch.setattr("app.browser_service.httpx.Client", lambda **kwargs: _StreamingClient())

    output_path = tmp_path / "streamed.mp4"
    result = fallback._try_stream_candidate(
        url="https://v95-web.douyinvod.com/video.mp4",
        output_path=output_path,
        referer="https://www.douyin.com/video/123",
        accept="video/mp4",
        destination="video",
        validator=fallback._looks_like_media_payload,
    )

    assert result == "ok"
    assert output_path.read_bytes().endswith(b"payload")


def test_download_video_demotes_misclassified_audio_stream(tmp_path, monkeypatch):
    fallback = PlaywrightFallback()
    capture = BrowserCapture(
        page_url="https://www.douyin.com/video/123",
        title="demo",
        video_urls=(DASH_AUDIO_URL, DASH_VIDEO_URL),
        audio_urls=(),
    )
    responses = {
        DASH_AUDIO_URL: _DummyResponse(200, b"audiofile", "audio/mp4"),
        DASH_VIDEO_URL: _DummyResponse(200, b"realvideo", "video/mp4"),
    }

    monkeypatch.setattr(fallback, "capture_video", lambda share_url: capture)
    monkeypatch.setattr(fallback, "_ensure_context", lambda: _DummyContext(responses))
    monkeypatch.setattr(
        fallback,
        "_video_file_has_stream",
        lambda path, codec_type="video": path.read_bytes() == b"realvideo",
    )
    monkeypatch.setattr(fallback, "_video_file_has_audio", lambda path: True)

    output_path = tmp_path / "video.mp4"
    result = fallback.download_video("https://www.douyin.com/video/123", output_path, ())

    assert result.source_url == DASH_VIDEO_URL
    assert output_path.read_bytes() == b"realvideo"
    assert list(tmp_path.glob("*_browser_audio.m4a")) == []


def test_download_video_never_falls_back_to_audio_as_video(tmp_path, monkeypatch):
    fallback = PlaywrightFallback()
    capture = BrowserCapture(
        page_url="https://www.douyin.com/video/123",
        title="demo",
        video_urls=(DASH_VIDEO_URL, DASH_AUDIO_URL),
        audio_urls=(),
    )
    responses = {
        DASH_VIDEO_URL: _DummyResponse(403, b"blocked", "text/html"),
        DASH_AUDIO_URL: _DummyResponse(200, b"audiofile", "audio/mp4"),
    }

    monkeypatch.setattr(fallback, "capture_video", lambda share_url: capture)
    monkeypatch.setattr(fallback, "_ensure_context", lambda: _DummyContext(responses))
    monkeypatch.setattr(fallback, "_video_file_has_stream", lambda path, codec_type="video": False)

    output_path = tmp_path / "video.mp4"
    try:
        fallback.download_video("https://www.douyin.com/video/123", output_path, (), tmp_path / "audio.m4a")
    except RuntimeError as exc:
        assert "HTTP 403" in str(exc)
    else:
        raise AssertionError("audio stream must not be returned as a successful video download")

    assert not output_path.exists()


def test_download_video_merges_separate_audio_stream(tmp_path, monkeypatch):
    class _StubFfmpeg:
        def _merge_supplemental_audio(self, video_path: Path, supplemental_audio_path: Path) -> Path:
            merged = video_path.with_name("merged.mp4")
            merged.write_bytes(b"video+audio")
            return merged

    fallback = PlaywrightFallback()
    capture = BrowserCapture(
        page_url="https://www.douyin.com/video/123",
        title="demo",
        video_urls=(DASH_VIDEO_URL,),
        audio_urls=("https://v95-web.douyinvod.com/audio.m4a",),
    )
    responses = {
        DASH_VIDEO_URL: _DummyResponse(200, b"videoonly", "video/mp4"),
        "https://v95-web.douyinvod.com/audio.m4a": _DummyResponse(200, b"audiofile", "audio/mp4"),
    }

    monkeypatch.setattr("app.browser_service.FfmpegService", _StubFfmpeg)
    monkeypatch.setattr(fallback, "capture_video", lambda share_url: capture)
    monkeypatch.setattr(fallback, "_ensure_context", lambda: _DummyContext(responses))
    monkeypatch.setattr(
        fallback,
        "_video_file_has_stream",
        lambda path, codec_type="video": path.read_bytes() in (b"videoonly", b"video+audio"),
    )
    monkeypatch.setattr(
        fallback,
        "_video_file_has_audio",
        lambda path: path.read_bytes() == b"video+audio",
    )

    output_path = tmp_path / "video.mp4"
    result = fallback.download_video(
        "https://www.douyin.com/video/123",
        output_path,
        (),
        tmp_path / "browser_audio.m4a",
    )

    assert result.source_url == DASH_VIDEO_URL
    assert output_path.read_bytes() == b"video+audio"
    # 合并成功后单独的音频文件保留给上层兜底（完整模式抽音频用）
    assert (tmp_path / "browser_audio.m4a").read_bytes() == b"audiofile"


def test_download_video_skips_candidate_without_audio(tmp_path, monkeypatch):
    fallback = PlaywrightFallback()
    capture = BrowserCapture(
        page_url="https://www.douyin.com/video/123",
        title="demo",
        video_urls=(
            "https://v26-web.douyinvod.com/no-audio.mp4?mime_type=video_mp4",
            "https://v26-web.douyinvod.com/with-audio.mp4?mime_type=video_mp4",
        ),
        audio_urls=(),
    )
    responses = {
        "https://v26-web.douyinvod.com/no-audio.mp4?mime_type=video_mp4": _DummyResponse(200, b"noaudio", "video/mp4"),
        "https://v26-web.douyinvod.com/with-audio.mp4?mime_type=video_mp4": _DummyResponse(200, b"withaudio", "video/mp4"),
    }

    monkeypatch.setattr(fallback, "capture_video", lambda share_url: capture)
    monkeypatch.setattr(fallback, "_ensure_context", lambda: _DummyContext(responses))
    monkeypatch.setattr(
        fallback,
        "_video_file_has_audio",
        lambda path: path.read_bytes() == b"withaudio",
    )

    output_path = tmp_path / "video.mp4"
    result = fallback.download_video(
        "https://www.douyin.com/video/123",
        output_path,
        (),
        tmp_path / "audio.m4a",
    )

    assert result.source_url == "https://v26-web.douyinvod.com/with-audio.mp4?mime_type=video_mp4"
    assert output_path.read_bytes() == b"withaudio"


def test_download_video_rejects_video_only_when_embedded_audio_is_required(tmp_path, monkeypatch):
    fallback = PlaywrightFallback()
    capture = BrowserCapture(
        page_url="https://www.douyin.com/video/123",
        title="demo",
        video_urls=(DASH_VIDEO_URL,),
        audio_urls=(),
    )
    responses = {
        DASH_VIDEO_URL: _DummyResponse(200, b"videoonly", "video/mp4"),
    }

    monkeypatch.setattr(fallback, "capture_video", lambda share_url: capture)
    monkeypatch.setattr(fallback, "_ensure_context", lambda: _DummyContext(responses))
    monkeypatch.setattr(fallback, "_video_file_has_audio", lambda path: False)
    monkeypatch.setattr(fallback, "_video_file_has_stream", lambda path, codec_type="video": True)

    with pytest.raises(RuntimeError, match="视频不含音轨"):
        fallback.download_video(
            "https://www.douyin.com/video/123",
            tmp_path / "video.mp4",
            (),
            tmp_path / "audio.m4a",
        )


def test_download_video_ignores_preview_candidates(tmp_path, monkeypatch):
    fallback = PlaywrightFallback()
    capture = BrowserCapture(
        page_url="https://www.douyin.com/video/123",
        title="demo",
        video_urls=("https://v26-web.douyinvod.com/real.mp4?mime_type=video_mp4",),
        audio_urls=(),
        preview_urls=("https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/uuu_265.mp4",),
    )
    responses = {
        "https://v26-web.douyinvod.com/real.mp4?mime_type=video_mp4": _DummyResponse(200, b"realvideo", "video/mp4"),
    }

    monkeypatch.setattr(fallback, "capture_video", lambda share_url: capture)
    monkeypatch.setattr(fallback, "_ensure_context", lambda: _DummyContext(responses))

    output_path = tmp_path / "video.mp4"
    result = fallback.download_video(
        "https://www.douyin.com/video/123",
        output_path,
        ("https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/uuu_265.mp4",),
    )

    assert result.source_url == "https://v26-web.douyinvod.com/real.mp4?mime_type=video_mp4"
    assert output_path.read_bytes() == b"realvideo"


def test_load_cookies_reads_persisted_storage_state(tmp_path):
    storage_state_path = tmp_path / "state.json"
    storage_state_path.write_text(
        """
        {
          "cookies": [
            {
              "name": "ttwid",
              "value": "persisted",
              "domain": ".douyin.com",
              "path": "/"
            }
          ],
          "origins": []
        }
        """.strip(),
        encoding="utf-8",
    )

    fallback = PlaywrightFallback(storage_state_path=storage_state_path)

    cookies = fallback.load_cookies()

    assert cookies == [
        {
            "name": "ttwid",
            "value": "persisted",
            "domain": ".douyin.com",
            "path": "/",
        }
    ]


def test_download_video_required_audio_does_not_repeat_identical_pass(tmp_path, monkeypatch):
    fallback = PlaywrightFallback()
    capture = BrowserCapture(
        page_url="https://www.douyin.com/video/123",
        title="demo",
        video_urls=(DASH_VIDEO_URL,),
        audio_urls=(),
    )
    responses = {
        DASH_VIDEO_URL: _DummyResponse(200, b"videoonly", "video/mp4"),
    }
    context = _DummyContext(responses)

    monkeypatch.setattr(fallback, "capture_video", lambda share_url: capture)
    monkeypatch.setattr(fallback, "_ensure_context", lambda: context)
    monkeypatch.setattr(fallback, "_video_file_has_audio", lambda path: False)

    with pytest.raises(RuntimeError, match="视频不含音轨"):
        fallback.download_video(
            "https://www.douyin.com/video/123",
            tmp_path / "video.mp4",
            (),
            tmp_path / "audio.m4a",
        )

    # 第二遍的 URL 集合与校验和第一遍完全相同，注定同结果，不应再发请求
    assert context.request.calls == [DASH_VIDEO_URL]


def test_download_video_uses_dedicated_download_timeout(tmp_path, monkeypatch):
    fallback = PlaywrightFallback()
    capture = BrowserCapture(
        page_url="https://www.douyin.com/video/123",
        title="demo",
        video_urls=(DASH_VIDEO_URL,),
        audio_urls=(),
    )
    responses = {
        DASH_VIDEO_URL: _DummyResponse(200, b"realvideo", "video/mp4"),
    }
    context = _DummyContext(responses)

    monkeypatch.setattr(fallback, "capture_video", lambda share_url: capture)
    monkeypatch.setattr(fallback, "_ensure_context", lambda: context)
    monkeypatch.setattr(
        fallback,
        "_video_file_has_stream",
        lambda path, codec_type="video": path.read_bytes() == b"realvideo",
    )

    output_path = tmp_path / "video.mp4"
    fallback.download_video("https://www.douyin.com/video/123", output_path, ())

    # 整文件下载必须用独立大超时（Playwright 请求超时含 body 读取），不能沿用 15s 导航超时
    assert context.request.timeouts == [float(PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS)]
    assert context.request.timeouts[0] > float(fallback.navigation_timeout_ms)


def test_persist_storage_state_writes_atomically_without_temp_leftover(tmp_path):
    state = {"cookies": [{"name": "ttwid", "value": "1"}], "origins": []}

    class _FakeContext:
        def storage_state(self):
            return state

    target = tmp_path / "state.json"
    fallback = PlaywrightFallback(storage_state_path=target)
    fallback._context = _FakeContext()

    fallback._persist_storage_state()

    assert json.loads(target.read_text(encoding="utf-8")) == state
    assert list(tmp_path.glob("*.tmp")) == []


def test_persist_storage_state_failure_keeps_existing_file(tmp_path):
    class _BrokenContext:
        def storage_state(self):
            raise RuntimeError("context closed")

    target = tmp_path / "state.json"
    target.write_text('{"cookies": [{"name": "keep"}]}', encoding="utf-8")
    fallback = PlaywrightFallback(storage_state_path=target)
    fallback._context = _BrokenContext()

    fallback._persist_storage_state()

    assert json.loads(target.read_text(encoding="utf-8"))["cookies"][0]["name"] == "keep"


class _ManualClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start


class _GracePage:
    """模拟 page.wait_for_timeout：推进假时钟，可在指定次数触发副作用。"""

    def __init__(self, clock: _ManualClock, on_wait=None) -> None:
        self.clock = clock
        self.on_wait = on_wait
        self.wait_calls = 0

    def wait_for_timeout(self, ms) -> None:
        self.wait_calls += 1
        if self.on_wait is not None:
            self.on_wait(self.wait_calls)
        self.clock.now += ms / 1000.0


class _StubLocator:
    def evaluate(self, script: str) -> str:
        return ""


def test_wait_for_preferred_returns_immediately_without_dash_video(monkeypatch):
    fallback = PlaywrightFallback()
    clock = _ManualClock()
    monkeypatch.setattr("app.browser_service.time.monotonic", lambda: clock.now)
    page = _GracePage(clock)
    video_urls = ["https://v26-web.douyinvod.com/plain.mp4?mime_type=video_mp4"]

    fallback._wait_for_preferred_video_urls(page, _StubLocator(), video_urls, [], set(), [])

    # 非分流视频保持原行为：见到 preferred 视频 URL 立即返回
    assert page.wait_calls == 0


def test_wait_for_preferred_waits_bounded_for_dash_audio(monkeypatch):
    fallback = PlaywrightFallback(dash_audio_grace_ms=1000)
    clock = _ManualClock()
    monkeypatch.setattr("app.browser_service.time.monotonic", lambda: clock.now)
    video_urls = [DASH_VIDEO_URL]
    audio_urls: list[str] = []

    def audio_arrives_after_first_poll(call_index: int) -> None:
        if call_index == 1:
            audio_urls.append(DASH_AUDIO_URL)

    page = _GracePage(clock, on_wait=audio_arrives_after_first_poll)
    fallback._wait_for_preferred_video_urls(
        page, _StubLocator(), video_urls, [], set(), audio_urls
    )

    # audio 候选一出现就退出，没有等满整个 deadline
    assert page.wait_calls == 1
    assert audio_urls == [DASH_AUDIO_URL]


def test_wait_for_preferred_dash_grace_is_bounded_without_audio(monkeypatch):
    fallback = PlaywrightFallback(dash_audio_grace_ms=1000)
    clock = _ManualClock()
    monkeypatch.setattr("app.browser_service.time.monotonic", lambda: clock.now)
    page = _GracePage(clock)
    video_urls = [DASH_VIDEO_URL]

    fallback._wait_for_preferred_video_urls(page, _StubLocator(), video_urls, [], set(), [])

    elapsed = clock.now - 1_000.0
    # 等满宽限期后退出，且远早于 3s 的整体 deadline
    assert elapsed >= 1.0
    assert elapsed < 3.0


def test_capture_video_resets_runtime_when_new_page_fails(monkeypatch):
    fallback = PlaywrightFallback()

    class _BrokenContext:
        def new_page(self):
            raise RuntimeError("new_page boom")

    reset_calls: list[int] = []
    monkeypatch.setattr(fallback, "_ensure_context", lambda: _BrokenContext())
    monkeypatch.setattr(fallback, "_reset_runtime", lambda: reset_calls.append(1))

    with pytest.raises(RuntimeError, match="new_page boom"):
        fallback.capture_video("https://www.douyin.com/video/1")

    assert reset_calls == [1]
