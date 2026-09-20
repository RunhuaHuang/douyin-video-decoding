from __future__ import annotations

from pathlib import Path

from app.cli import download_one, main, sanitize_title
from app.cookie_accounts import CookieAccountManager
from app.douyin_service import ParsedVideo


class FakeService:
    def __init__(self, fail_urls=(), audio_only=False):
        self.fail_urls = fail_urls
        self.audio_only = audio_only
        self.parsed = []
        self.closed = False

    def parse_share_text(self, share_text: str) -> ParsedVideo:
        if share_text in self.fail_urls:
            raise RuntimeError("解析失败")
        self.parsed.append(share_text)
        return ParsedVideo(
            video_id="7411111111111111111",
            title='视频/标题:demo',
            author="tester",
            download_urls=("https://cdn.example.com/play/video_a.mp4",),
            share_url=share_text,
        )

    def download_video(self, video: ParsedVideo, output_path: Path, audio_output_path=None):
        if self.audio_only:
            audio_output_path.write_bytes(b"audio")
        else:
            output_path.write_bytes(b"video")

    def close(self) -> None:
        self.closed = True


def test_sanitize_title_strips_filesystem_unsafe_chars():
    assert sanitize_title('a/b:c*d?"e<f>g|\n\t h.. .') == "a b c d e f g h"


def test_download_one_saves_video_with_safe_title(tmp_path):
    service = FakeService()

    path = download_one(service, "https://v.douyin.com/example/", tmp_path)

    assert path.name == "视频 标题 demo.mp4"
    assert path.read_bytes() == b"video"
    assert list(tmp_path.glob("_cli_*")) == []


def test_download_one_falls_back_to_audio_file(tmp_path):
    service = FakeService(audio_only=True)

    path = download_one(service, "https://v.douyin.com/example/", tmp_path)

    assert path.name == "视频 标题 demo.m4a"
    assert path.read_bytes() == b"audio"


def test_download_one_falls_back_to_audio_when_merge_left_video_only(tmp_path, monkeypatch):
    class VideoAndAudioService(FakeService):
        def download_video(self, video, output_path, audio_output_path=None):
            output_path.write_bytes(b"video-only")
            audio_output_path.write_bytes(b"audio")

    class VideoOnlyProbe:
        def probe_media(self, path):
            return {"streams": [{"codec_type": "video"}]}

    from app import cli as cli_module

    monkeypatch.setattr(cli_module, "FfmpegService", VideoOnlyProbe)
    path = download_one(VideoAndAudioService(), "https://v.douyin.com/example/", tmp_path)

    assert path.name == "视频 标题 demo.m4a"
    assert path.read_bytes() == b"audio"


def test_main_downloads_all_urls(tmp_path, capsys):
    manager = CookieAccountManager(accounts_dir=tmp_path / "accounts")
    service = FakeService()

    code = main(
        ["https://v.douyin.com/a/", "https://www.douyin.com/video/1"],
        account_manager=manager,
        service_factory=lambda: service,
    )
    out = capsys.readouterr().out

    assert code == 0
    assert len(service.parsed) == 2
    assert "共 2 成功 / 0 失败" in out
    assert service.closed


def test_main_uses_custom_out_dir_and_reports_failures(tmp_path, capsys):
    manager = CookieAccountManager(accounts_dir=tmp_path / "accounts")
    service = FakeService(fail_urls=("https://v.douyin.com/bad/",))

    code = main(
        ["https://v.douyin.com/bad/", "https://v.douyin.com/good/", "-o", str(tmp_path / "dl")],
        account_manager=manager,
        service_factory=lambda: service,
    )

    assert code == 1
    # 失败的跳过，成功的仍然下载
    assert service.parsed == ["https://v.douyin.com/good/"]
    assert (tmp_path / "dl" / "视频 标题 demo.mp4").read_bytes() == b"video"


def test_main_list_accounts_empty_pool(tmp_path, capsys):
    manager = CookieAccountManager(accounts_dir=tmp_path / "accounts")

    code = main(["--list-accounts"], account_manager=manager)
    out = capsys.readouterr().out

    assert code == 1
    assert "账号池为空" in out


def test_download_one_does_not_overwrite_same_title(tmp_path):
    service = FakeService()

    first = download_one(service, "https://v.douyin.com/a/", tmp_path)
    second = download_one(service, "https://v.douyin.com/b/", tmp_path)

    assert first.name == "视频 标题 demo.mp4"
    assert second.name == "视频 标题 demo_7411111111111111111.mp4"
    assert first.read_bytes() == b"video"
    assert second.read_bytes() == b"video"
