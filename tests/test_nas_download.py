import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import nas_download
from app.nas_download import PROGRESS_FILENAME, run_download


class FakeDownloader:
    calls = []
    result = (2, 0)

    def __init__(self):
        pass

    def download_file(self, *, table_path, out_dir, url_column, skip_existing, start_row=1, progress_callback=None, workers=2):
        self.__class__.calls.append(
            {
                "table_path": table_path,
                "out_dir": out_dir,
                "url_column": url_column,
                "skip_existing": skip_existing,
                "start_row": start_row,
                "workers": workers,
            }
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "视频-1.mp4").write_bytes(b"video")
        if progress_callback is not None:
            progress_callback(2, 2, 2, 0, "成功")
        return self.result

    def close(self):
        pass


def test_batch_download_uses_stem_and_resumes_completed_file(tmp_path, monkeypatch):
    source = tmp_path / "目标视频列表" / "分享总榜.xlsx"
    source.parent.mkdir()
    source.write_bytes(b"xlsx-placeholder")
    output_root = tmp_path / "视频下载"
    FakeDownloader.calls = []
    monkeypatch.setattr("app.nas_download.VideoDownloader", FakeDownloader)

    assert run_download(source, output_root=output_root) == 0
    target = output_root / "分享总榜"
    assert target.joinpath("视频-1.mp4").exists()
    progress = json.loads((target / PROGRESS_FILENAME).read_text(encoding="utf-8"))
    assert progress["status"] == "completed"
    assert len(FakeDownloader.calls) == 1

    assert run_download(source, output_root=output_root) == 0
    assert len(FakeDownloader.calls) == 1


def test_batch_download_retries_partial_job(tmp_path, monkeypatch):
    source = tmp_path / "分享总榜.xlsx"
    source.write_bytes(b"xlsx-placeholder")
    output_root = tmp_path / "视频下载"
    FakeDownloader.calls = []
    FakeDownloader.result = (1, 1)
    monkeypatch.setattr("app.nas_download.VideoDownloader", FakeDownloader)

    assert run_download(source, output_root=output_root) == 1
    assert run_download(source, output_root=output_root) == 1
    assert len(FakeDownloader.calls) == 2
    progress = json.loads(
        (output_root / "分享总榜" / PROGRESS_FILENAME).read_text(encoding="utf-8")
    )
    assert progress["status"] == "partial"
    FakeDownloader.result = (2, 0)


def test_batch_status_reports_not_started_as_json(tmp_path, capsys):
    source = tmp_path / "分享总榜.xlsx"
    source.write_bytes(b"xlsx-placeholder")

    assert nas_download.main(["--status", str(source), "-o", str(tmp_path / "视频下载")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not_started"


def test_preserved_nas_guest_mode_and_start_row(tmp_path, monkeypatch):
    source = tmp_path / "访客.xlsx"
    source.write_bytes(b"xlsx-placeholder")
    state = tmp_path / "anonymous.json"
    state.write_text('{"cookies": [], "origins": []}')
    options = []
    monkeypatch.setattr(nas_download, "CookieAccountManager", lambda: pytest.fail("guest must not select a login account"))
    monkeypatch.setattr(nas_download, "VideoDownloader", lambda **kwargs: options.append(kwargs) or FakeDownloader())
    FakeDownloader.calls = []
    assert run_download(source, output_root=tmp_path / "out", anonymous_state=state, start_row=4) == 0
    assert options == [{"storage_state_path": state, "anonymous": True}]
    assert FakeDownloader.calls[-1]["start_row"] == 4
    progress = json.loads((tmp_path / "out/访客" / PROGRESS_FILENAME).read_text())
    assert progress["access_mode"] == "anonymous"
    assert progress["start_row"] == 4


def test_preserved_nas_guest_mode_rejects_login_cookies(tmp_path):
    state = tmp_path / "account.json"
    state.write_text(json.dumps({"cookies": [{"name": "sessionid", "value": "fixture"}], "origins": []}))
    with pytest.raises(ValueError, match="登录凭据"):
        run_download(tmp_path / "source.xlsx", output_root=tmp_path / "out", anonymous_state=state)
    assert not (tmp_path / "out").exists()


def test_preserved_nas_explicit_account_is_isolated(tmp_path, monkeypatch):
    source = tmp_path / "指定账号.xlsx"
    source.write_bytes(b"xlsx-placeholder")
    account = SimpleNamespace(label="指定", storage_state_path=tmp_path / "account.json")
    monkeypatch.setattr(nas_download, "CookieAccountManager", lambda: SimpleNamespace(list_available_accounts=lambda: [account]))
    constructors, accounts = [], []
    class SelectedDownloader(FakeDownloader):
        def download_file(self, *, accounts: list, **kwargs):
            assert accounts == ["指定"]
            return super().download_file(**kwargs)
    monkeypatch.setattr(nas_download, "VideoDownloader", lambda **kwargs: constructors.append(kwargs) or SelectedDownloader())
    assert run_download(source, output_root=tmp_path / "out", account_label="指定") == 0
    assert constructors == [{"storage_state_path": account.storage_state_path}]
