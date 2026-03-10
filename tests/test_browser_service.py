from pathlib import Path

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


def test_download_video_skips_candidate_without_audio(tmp_path, monkeypatch):
    fallback = PlaywrightFallback()
    capture = BrowserCapture(
        page_url="https://www.douyin.com/video/123",
        title="demo",
        video_urls=("https://cdn.example.com/no-audio.mp4", "https://cdn.example.com/with-audio.mp4"),
        audio_urls=(),
    )
    responses = {
        "https://cdn.example.com/no-audio.mp4": _DummyResponse(200, b"noaudio", "video/mp4"),
        "https://cdn.example.com/with-audio.mp4": _DummyResponse(200, b"withaudio", "video/mp4"),
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

    assert result.source_url == "https://cdn.example.com/with-audio.mp4"
    assert output_path.read_bytes() == b"withaudio"


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
