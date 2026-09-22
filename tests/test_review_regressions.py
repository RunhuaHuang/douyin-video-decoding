"""覆盖续跑的数据保留、批次执行权以及账号生命周期的回归场景。"""

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import main, pipeline
from app.account_storage import account_storage_lock, deleted_marker_path
from app.browser_service import PlaywrightFallback
from app.config import DEFAULT_MODEL
from app.cookie_accounts import AccountMetadataError, CookieAccountManager
from app.job_manager import JobManager
from app.pipeline import JobProcessor, RunControlState


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    manager = JobManager()
    accounts = CookieAccountManager(
        accounts_dir=tmp_path / "accounts", legacy_storage_state_path=tmp_path / "legacy.json"
    )
    monkeypatch.setattr(main, "job_manager", manager)
    monkeypatch.setattr(main, "cookie_account_manager", accounts)
    monkeypatch.setattr(main, "_job_workers", {})
    monkeypatch.setattr(main, "_batch_workers", {})
    monkeypatch.setattr(main, "account_relogin_threads", {})
    monkeypatch.setattr(pipeline, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path / "outputs")
    return manager, accounts, TestClient(main.app)


def make_job(manager, tmp_path, name="input.csv"):
    source = tmp_path / name
    pd.DataFrame([{"链接": "https://v.douyin.com/review/"}]).to_csv(source, index=False)
    job = manager.create_session(name, source, ["链接"], [], 1)
    manager.configure_job(job.job_id, "链接", "视频文本", DEFAULT_MODEL, [DEFAULT_MODEL], False)
    return job


def state_for(account):
    state = {"cookies": [{"name": "sessionid", "value": "test", "domain": ".douyin.com", "path": "/"}], "origins": []}
    account.storage_state_path.write_text(json.dumps(state), encoding="utf-8")
    return state


@pytest.mark.parametrize("entry", ["stored", "job_form", "job_api", "batch_form", "batch_api"])
def test_continue_preserves_current_result_instead_of_upload_or_old_snapshot(
    isolated, monkeypatch, tmp_path, entry
):
    manager, _, client = isolated
    job = make_job(manager, tmp_path)
    batch = manager.create_batch([job], ["链接"])
    processor = JobProcessor(
        manager,
        ffmpeg_service_factory=lambda: SimpleNamespace(is_available=lambda: True),
        transcription_service_factory=lambda **kwargs: object(),
        douyin_service_factory=lambda: pytest.fail("已成功的行不能重新下载"),
    )
    current = processor._build_output_path(job)
    current.parent.mkdir(parents=True)
    pd.DataFrame([{"链接": "https://v.douyin.com/review/", "视频文本": "最新转写", "处理状态": "成功"}]).to_csv(current, index=False)
    stale = tmp_path / "old.processed.csv"
    pd.DataFrame([{"链接": "https://v.douyin.com/review/", "视频文本": "旧转写", "处理状态": "成功"}]).to_csv(stale, index=False)
    job.resume_output_path = stale
    manager.mark_completed(job.job_id, current, [])
    monkeypatch.setattr(main, "job_processor", processor)
    monkeypatch.setattr(main, "start_job_worker", lambda job_id, mode: processor.process(job_id, mode) or True)
    monkeypatch.setattr(main, "start_batch_worker", lambda batch_id, mode: main.process_batch(batch_id, mode) or True)
    options = {"url_column": "链接", "transcript_column": "视频文本", "model_names": DEFAULT_MODEL}
    if entry == "stored":
        response = client.post(f"/jobs/{job.job_id}/start", data={"mode": "continue_existing"}, follow_redirects=False)
    elif entry == "job_form":
        response = client.post(f"/jobs/{job.job_id}/continue", data=options, follow_redirects=False)
    elif entry == "job_api":
        response = client.post(f"/api/jobs/{job.job_id}/continue", json=options)
    elif entry == "batch_form":
        response = client.post(f"/batches/{batch.batch_id}/continue", data=options, follow_redirects=False)
    else:
        response = client.post(f"/api/batches/{batch.batch_id}/continue", json=options)
    assert response.status_code in {200, 303}
    assert job.status == "completed", job.error_message
    assert job.output_path == current
    assert pd.read_csv(current).loc[0, "视频文本"] == "最新转写"
    assert pd.read_csv(stale).loc[0, "视频文本"] == "旧转写"


def test_missing_snapshot_does_not_silently_restart_from_upload(isolated, tmp_path):
    manager, _, _ = isolated
    job = make_job(manager, tmp_path)
    job.resume_output_path = tmp_path / "missing.csv"
    with pytest.raises(RuntimeError, match="已有结果文件不存在"):
        JobProcessor(manager)._resolve_source_path(job, "continue_existing")


@pytest.mark.parametrize("action", ["retry-failed", "resume", "run"])
def test_batch_rejects_active_child_before_changing_state(isolated, monkeypatch, tmp_path, action):
    manager, _, client = isolated
    active = make_job(manager, tmp_path, "active.csv")
    other = make_job(manager, tmp_path, "other.csv")
    batch = manager.create_batch([active, other], ["链接"])
    batch.status = "paused"
    active.status, active.failed_rows = "queued", 1
    other.status, other.failed_rows = "completed", 1
    main._job_workers[active.job_id] = SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(main, "start_batch_worker", lambda *args, **kwargs: pytest.fail("冲突时不能启动"))
    response = client.post(f"/batches/{batch.batch_id}/{action}", data={"url_column": "链接"})
    assert response.status_code == 409
    assert batch.status == "paused"
    assert active.status == "queued"
    assert other.failed_rows == 1


def test_worker_entry_points_enforce_both_directions_of_exclusion(isolated, tmp_path):
    manager, _, _ = isolated
    job = make_job(manager, tmp_path)
    batch = manager.create_batch([job], ["链接"])
    alive = SimpleNamespace(is_alive=lambda: True)
    main._batch_workers[batch.batch_id] = alive
    assert main.start_job_worker(job.job_id) is False
    main._batch_workers.clear()
    main._job_workers[job.job_id] = alive
    assert main.start_batch_worker(batch.batch_id) is False


@pytest.mark.parametrize("mode", ["retry_failed", "continue_existing"])
@pytest.mark.parametrize("pause_between_files", [False, True])
def test_batch_resume_keeps_original_mode_for_remaining_files(
    isolated, monkeypatch, tmp_path, mode, pause_between_files
):
    manager, _, client = isolated
    first = make_job(manager, tmp_path, "first.csv")
    second = make_job(manager, tmp_path, "second.csv")
    batch = manager.create_batch([first, second], ["链接"])
    for job in (first, second):
        job.status, job.failed_rows = "completed", 1
    processed = []

    def process(job_id, mode):
        processed.append((job_id, mode))
        job = manager.require(job_id)
        if len(processed) == 1:
            if pause_between_files:
                job.status = "completed"
                manager.request_batch_pause(batch.batch_id)
            else:
                job.status = "paused"
        else:
            job.status = "completed"
            job.failed_rows = 0

    monkeypatch.setattr(main, "job_processor", SimpleNamespace(process=process))
    main.process_batch(batch.batch_id, mode)
    assert batch.status == "paused"
    monkeypatch.setattr(main, "start_batch_worker", lambda batch_id, mode: main.process_batch(batch_id, mode) or True)
    assert client.post(f"/batches/{batch.batch_id}/resume").status_code == 200
    assert (second.job_id, mode) in processed
    assert batch.status == "completed"
    assert second.failed_rows == 0
    assert len(processed) == (2 if pause_between_files else 3)


def test_deleted_account_cannot_be_recreated_by_live_browser_or_metadata_writer(isolated):
    _, accounts, client = isolated
    account = accounts.prepare_account("test-account")
    state = state_for(account)
    accounts.mark_warning(account.account_id, "HTTP 403")
    stale_metadata = json.loads(account.metadata_path.read_text())
    browser = PlaywrightFallback(storage_state_path=account.storage_state_path)
    browser._context = SimpleNamespace(storage_state=lambda: state)
    assert client.post(f"/accounts/{account.account_id}/delete").status_code == 200
    browser._persist_storage_state()
    assert accounts._write_metadata(account.account_id, stale_metadata) is False
    assert not account.storage_state_path.exists()
    assert not account.metadata_path.exists()
    assert accounts.list_accounts() == []
    replacement = accounts.prepare_account(account.label)
    assert replacement.account_id != account.account_id
    state_for(replacement)
    browser._persist_storage_state()
    assert [item.account_id for item in accounts.list_available_accounts()] == [replacement.account_id]


def test_deleted_marker_blocks_orphan_adoption_by_another_manager(isolated):
    _, accounts, _ = isolated
    account = accounts.prepare_account("old-account")
    state_for(account)
    accounts.delete_account(account.account_id)
    state_for(account)  # 模拟旧版本或外部进程重新写入旧 JSON。
    other = CookieAccountManager(accounts_dir=accounts.accounts_dir)
    assert other.list_accounts() == []
    assert other.get_account(account.account_id) is None


def test_session_save_respects_deletion_in_another_process(isolated):
    _, accounts, _ = isolated
    account = accounts.prepare_account("process-race")
    state_for(account)
    code = """
import sys
from pathlib import Path
from app.account_storage import write_storage_state
print('ready', flush=True)
print(write_storage_state(Path(sys.argv[1]), '{"cookies": []}'), flush=True)
"""
    worker = None
    try:
        with account_storage_lock(account.storage_state_path):
            worker = subprocess.Popen(
                [sys.executable, "-c", code, str(account.storage_state_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            assert worker.stdout.readline().strip() == "ready"
            deleted_marker_path(account.storage_state_path).touch()
            account.storage_state_path.unlink()
            account.metadata_path.unlink()
        output, error = worker.communicate(timeout=10)
        assert worker.returncode == 0, error
        assert output.strip() == "False"
        assert not account.storage_state_path.exists()
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=10)


@pytest.mark.parametrize("contents", ['{broken', '[]', '{"challenge_strikes": "oops"}'])
def test_corrupt_account_is_quarantined_and_relogin_preserves_backup(isolated, contents):
    _, accounts, _ = isolated
    account = accounts.prepare_account("repair-me")
    state_for(account)
    account.metadata_path.write_text(contents)
    assert accounts.get_account(account.account_id).status_text == "信息损坏"
    assert accounts.list_available_accounts() == []
    with pytest.raises(AccountMetadataError):
        accounts.mark_used(account.account_id)
    assert account.metadata_path.read_text() == contents
    pending = accounts.mark_relogin_in_progress(account.account_id, True)
    assert pending.relogin_in_progress
    assert pending.invalid
    backups = list(accounts.accounts_dir.glob("*.corrupt"))
    assert len(backups) == 1
    assert backups[0].read_text() == contents
    accounts.mark_relogin_in_progress(account.account_id, False)
    assert accounts.list_available_accounts() == []  # 取消登录不能启用旧会话。
    accounts.record_login(account.account_id)
    assert len(accounts.list_available_accounts()) == 1
    assert backups[0].read_text() == contents


def test_web_relogin_repairs_metadata_before_launching_login(isolated, monkeypatch):
    _, accounts, client = isolated
    account = accounts.prepare_account("web-repair")
    state_for(account)
    account.metadata_path.write_text("{broken")
    entered, release = threading.Event(), threading.Event()

    def login(label, **kwargs):
        entered.set()
        assert release.wait(5)
        accounts.record_login(account.account_id)

    monkeypatch.setattr(main, "login_with_label", login)
    worker = None
    try:
        response = client.post(f"/accounts/{account.account_id}/relogin")
        assert response.status_code == 200, response.text
        assert entered.wait(5)
        worker = main.account_relogin_threads[account.account_id]
        account.storage_state_path.touch()  # 旧下载浏览器仍可能保存会话。
        snapshot = main.build_cookie_pool_snapshot()
        assert snapshot["accounts"][0]["relogin_in_progress"] is True
        assert snapshot["usable_count"] == 0
        assert client.post(f"/accounts/{account.account_id}/relogin").status_code == 409
    finally:
        release.set()
        if worker:
            worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(accounts.list_available_accounts()) == 1


def test_corrupt_metadata_during_processing_does_not_abort_batch_finalization(isolated, tmp_path):
    manager, accounts, _ = isolated
    job = make_job(manager, tmp_path)
    broken = accounts.prepare_account("broken")
    healthy = accounts.prepare_account("healthy")
    state_for(broken)
    state_for(healthy)
    broken.metadata_path.write_text("{broken")
    manager.update_job_cookie_runtime(job.job_id, current_cookie_account_id=broken.account_id)
    processor = JobProcessor(manager, cookie_account_manager=accounts)
    processor._finalize_cookie_rotation(
        job_id=job.job_id, run_state=RunControlState(5, 0, 20, 0),
        phase="主任务", batch_index=1, total_batches=1,
    )
    assert any("使用记录未能写入" in message for message in job.logs)
    assert processor._prepare_cookie_account_for_batch(job.job_id).account_id == healthy.account_id
    assert broken.metadata_path.read_text() == "{broken"
