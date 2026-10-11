import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.cli import download_one
from app.cookie_accounts import CookieAccountManager
from app.csv_service import write_csv
from app.douyin_service import ParsedVideo
from app.path_utils import portable_name, upload_filename
from app.transcription_service import TranscriptionService


@pytest.mark.parametrize("name", ["CON", "nul", "PRN.txt", "AUX", "COM1", "LPT9", "COM¹", "CONIN$", "CONOUT$"])
def test_reserved_names_can_be_written_on_windows(tmp_path, name):
    path = tmp_path / (portable_name(name) + ".mp4")
    path.write_bytes(b"video")
    assert path.read_bytes() == b"video"
    assert path.name.startswith("_")


def test_names_remove_control_characters_and_trailing_dots():
    assert portable_name("ab\x00\x01cd... ") == "ab cd"
    assert portable_name("hello.world", max_length=6) == "hello"
    assert upload_filename(r"C:\fakepath\NUL.csv") == "_NUL.csv"
    assert upload_filename("/tmp/中文 报表.xlsx") == "中文 报表.xlsx"


def test_long_chinese_name_leaves_room_for_id_and_extension(tmp_path):
    name = portable_name("中文标题" * 60)
    assert len(name.encode("utf-8")) <= 180
    path = tmp_path / f"{name}_7411111111111111111.mp4"
    path.write_bytes(b"video")
    assert path.read_bytes() == b"video"


def test_reserved_account_label_is_still_displayed(tmp_path):
    account = CookieAccountManager(accounts_dir=tmp_path).prepare_account("CON")
    assert account.label == "CON"
    assert account.account_id == "_CON"
    assert account.metadata_path.exists()


def test_download_can_save_reserved_video_title(tmp_path):
    class Service:
        def parse_share_text(self, text):
            return ParsedVideo(video_id="1", title="CON", author="", download_urls=("https://example.test/video",), share_url=text)

        def download_video(self, video, output_path, audio_output_path):
            output_path.write_bytes(b"video")

    result = download_one(Service(), "https://example.test/1", tmp_path)
    assert result.name == "_CON.mp4"
    assert result.read_bytes() == b"video"


def test_blocked_spreadsheet_write_keeps_existing_file(tmp_path, monkeypatch):
    destination = tmp_path / "结果.csv"
    destination.write_bytes(b"previous result")

    def occupied(*args):
        raise PermissionError("sharing violation")

    monkeypatch.setattr("app.csv_service.os.replace", occupied)
    with pytest.raises(PermissionError, match="Excel"):
        write_csv(pd.DataFrame([{"text": "new"}]), destination)
    assert destination.read_bytes() == b"previous result"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("cuda,mps,expected", [(True, False, "cuda"), (False, True, "mps"), (False, False, "cpu")])
def test_sensevoice_selects_available_device(monkeypatch, cuda, mps, expected):
    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: cuda), backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)))
    monkeypatch.setattr("app.transcription_service.importlib.import_module", lambda name: torch)
    assert TranscriptionService._preferred_sensevoice_device() == expected


@pytest.mark.parametrize("fail_during_load", [False, True])
def test_cuda_failure_falls_back_to_cpu_and_stays_on_cpu(tmp_path, monkeypatch, fail_during_load):
    service = TranscriptionService()
    loads = []
    runs = []

    def load(**kwargs):
        device = kwargs["device"]
        loads.append(device)
        if fail_during_load and device == "cuda":
            raise RuntimeError("GPU unavailable")
        return device

    def generate(model, path, language):
        runs.append(model)
        if model == "cuda":
            raise RuntimeError("GPU out of memory")
        return "CPU transcription"

    monkeypatch.setattr(service, "_preferred_sensevoice_device", lambda: "cuda")
    monkeypatch.setattr("app.transcription_service.importlib.import_module", lambda name: SimpleNamespace(AutoModel=load))
    monkeypatch.setattr(service, "_generate_sensevoice_text", generate)
    monkeypatch.setattr(service, "_cleanup_sensevoice_runtime", lambda device: None)
    for _ in range(2):
        assert service.transcribe(tmp_path / "audio.wav") == "CPU transcription"
    assert loads == ["cuda", "cpu"]
    assert runs == (["cpu", "cpu"] if fail_during_load else ["cuda", "cpu", "cpu"])


def test_mlx_rejected_before_import_on_unsupported_platform(tmp_path, monkeypatch):
    monkeypatch.setattr("app.transcription_service.MLX_SUPPORTED", False)
    with pytest.raises(RuntimeError, match="SenseVoice"):
        TranscriptionService().transcribe(tmp_path / "audio.wav", "mlx-community/whisper-small-mlx")


def test_windows_model_presets_do_not_offer_mlx():
    # Patch platform reporting in an isolated interpreter, never pathlib's host OS.
    code = "import sys; sys.platform = 'win32'; from app.config import MODEL_PRESETS; import json; print(json.dumps(MODEL_PRESETS))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    presets = json.loads(result.stdout)
    assert presets and all("sensevoice" in model.lower() for preset in presets for model in preset["models"])


def test_launcher_opens_only_after_its_server_starts(monkeypatch):
    from app.launcher import _open_when_ready

    opened = []
    monkeypatch.setattr("app.launcher.webbrowser.open", opened.append)
    stopped = threading.Event()
    stopped.set()
    _open_when_ready(SimpleNamespace(started=False), stopped, "http://localhost:8000")
    assert opened == []
    stopped.clear()
    _open_when_ready(SimpleNamespace(started=True), stopped, "http://localhost:8000")
    assert opened == ["http://localhost:8000"]


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="requires FFmpeg and FFprobe")
def test_note_concat_in_unicode_space_and_quote_directory(tmp_path, monkeypatch):
    from app.media_service import FfmpegService
    from app.video_downloader import VideoDownloader

    image = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=16x16", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"],
        capture_output=True, check=True, timeout=30,
    ).stdout
    root = tmp_path / "中文 space's directory"
    root.mkdir()
    temporary_directory = tempfile.TemporaryDirectory
    monkeypatch.setattr("app.video_downloader.tempfile.TemporaryDirectory", lambda **kwargs: temporary_directory(dir=root, **kwargs))
    manager = CookieAccountManager(accounts_dir=tmp_path / "accounts")
    downloader = VideoDownloader(account_manager=manager)
    context = MagicMock()
    page = context.new_page.return_value
    page.url = "https://www.douyin.com/note/123"
    page.content.return_value = "<html></html>"
    page.evaluate.return_value = ["https://example.test/1", "https://example.test/2"]
    context.request.get.return_value.body.return_value = image
    monkeypatch.setattr(downloader, "_preflight_challenge", lambda *args: False)
    monkeypatch.setattr(downloader.browser_resolver, "_ensure_context", lambda: context)
    try:
        success, path, message = downloader.download_url(page.url, root / "输出", skip_existing=False)
        assert success, message
        probe = FfmpegService().probe_media(path)
        assert {stream["codec_type"] for stream in probe["streams"]} == {"video", "audio"}
        assert abs(float(probe["format"]["duration"]) - 2.0) < 0.2
        assert not list(root.glob("dy_note_*"))
    finally:
        downloader.close()


@pytest.mark.skipif(os.environ.get("DOUYIN_BROWSER_SMOKE") != "1", reason="opt-in installed Chromium smoke test")
def test_playwright_worker_can_launch_and_persist_session(tmp_path):
    from app.browser_service import PlaywrightFallback

    errors = []
    state = tmp_path / "账号.json"

    def worker():
        browser = PlaywrightFallback(storage_state_path=state)
        try:
            page = browser._ensure_context().new_page()
            page.set_content("<title>Windows smoke</title>")
            assert page.title() == "Windows smoke"
        except Exception as exc:
            errors.append(exc)
        finally:
            browser.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=45)
    assert not thread.is_alive()
    assert not errors
    assert isinstance(json.loads(state.read_text(encoding="utf-8"))["cookies"], list)
