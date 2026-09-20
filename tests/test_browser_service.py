from __future__ import annotations

from pathlib import Path

import pytest

from app.browser_service import BrowserCapture, PlaywrightFallback


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

    def get(self, url, headers=None, timeout=None, fail_on_status_code=False, max_retries=0):
        return self.responses[url]


class _DummyContext:
    def __init__(self, responses: dict[str, _DummyResponse]) -> None:
        self.request = _DummyRequest(responses)


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
