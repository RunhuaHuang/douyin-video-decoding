import io
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zipfile import ZipFile

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_MODEL
from app.cookie_accounts import CookieAccountManager
from app.job_manager import JobManager
from app.main import app


def _make_job(manager: JobManager, row_count: int = 10):
    return manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=row_count,
    )


def _configure_job(manager: JobManager, job, *, download_only: bool = True):
    manager.configure_job(
        job_id=job.job_id,
        url_column="链接",
        transcript_column="视频文本",
        model_names_text=DEFAULT_MODEL,
        model_names=[DEFAULT_MODEL],
        overwrite=False,
        download_only=download_only,
    )


def test_quick_download_creates_batch_with_download_only_and_redirects():
    from app import main as main_module

    manager = JobManager()
    previous_manager = main_module.job_manager
    previous_worker = main_module.start_batch_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_batch_worker = lambda batch_id, mode="full": captured.update(batch_id=batch_id, mode=mode)
    try:
        client = TestClient(app)
        response = client.post(
            "/quick-download",
            data={"links": "https://v.douyin.com/AAA111/\n\nhttps://v.douyin.com/BBB222/\n"},
        )
    finally:
        main_module.job_manager = previous_manager
        main_module.start_batch_worker = previous_worker

    assert response.status_code in (200, 303)
    assert captured["mode"] == "full"
    batch = manager.require_batch(captured["batch_id"])
    job = manager.require(batch.job_ids[0])
    assert job.original_filename.startswith("quick-")
    assert job.original_filename.endswith(".csv")
    assert job.url_column == "链接"
    assert job.download_only is True
    assert job.row_count == 2
    assert batch.common_columns == ["链接"]


def test_quick_download_transcribe_disables_download_only():
    from app import main as main_module

    manager = JobManager()
    previous_manager = main_module.job_manager
    previous_worker = main_module.start_batch_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_batch_worker = lambda batch_id, mode="full": captured.update(batch_id=batch_id, mode=mode)
    try:
        client = TestClient(app)
        response = client.post(
            "/quick-download",
            data={"links": "3.82 复制打开抖音，看看【作品】 https://v.douyin.com/CCC333/", "transcribe": "on"},
        )
    finally:
        main_module.job_manager = previous_manager
        main_module.start_batch_worker = previous_worker

    assert response.status_code in (200, 303)
    job = manager.require(manager.require_batch(captured["batch_id"]).job_ids[0])
    assert job.download_only is False


def test_quick_download_rejects_empty_links():
    from app import main as main_module

    manager = JobManager()
    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post("/quick-download", data={"links": "  \n\n "})
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 400
    assert "请至少粘贴一个链接" in response.text
    assert not manager._batches


def test_batch_retry_failed_starts_worker_in_retry_failed_mode():
    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])
    job.status = "failed"
    job.failed_rows = 3

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_batch_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_batch_worker = (
        lambda batch_id, mode="full": captured.update(batch_id=batch_id, mode=mode) or True
    )
    try:
        client = TestClient(app)
        response = client.post(f"/batches/{batch.batch_id}/retry-failed")
    finally:
        main_module.job_manager = previous_manager
        main_module.start_batch_worker = previous_worker

    assert response.status_code == 200
    assert response.json() == {"ok": True, "jobs": 1}
    assert captured == {"batch_id": batch.batch_id, "mode": "retry_failed"}
    assert manager.require_batch(batch.batch_id).status == "queued"


def test_batch_retry_failed_returns_409_when_nothing_to_retry():
    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])
    job.status = "completed"
    job.failed_rows = 0

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(f"/batches/{batch.batch_id}/retry-failed")
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 409
    assert "没有可重试的失败项" in response.json()["detail"]


def test_process_batch_retry_failed_only_requeues_failed_jobs(monkeypatch):
    manager = JobManager()
    job_ok = _make_job(manager)
    job_bad = _make_job(manager)
    batch = manager.create_batch([job_ok, job_bad], common_columns=["链接"])
    job_ok.status = "completed"
    job_bad.status = "failed"
    job_bad.failed_rows = 2

    from app import main as main_module

    previous_manager = main_module.job_manager
    captured = []
    main_module.job_manager = manager
    monkeypatch.setattr(
        main_module.job_processor,
        "process",
        lambda job_id, mode="full": captured.append((job_id, mode)),
    )
    try:
        main_module.process_batch(batch.batch_id, mode="retry_failed")
    finally:
        main_module.job_manager = previous_manager

    assert captured == [(job_bad.job_id, "retry_failed")]
    assert manager.require_batch(batch.batch_id).status == "completed"


def test_job_rows_endpoint_paginates_output_csv(tmp_path):
    manager = JobManager()
    job = _make_job(manager, row_count=5)
    output_path = tmp_path / "1.processed.csv"
    pd.DataFrame([{"链接": f"https://example.com/{index}", "视频文本": f"文本{index}"} for index in range(5)]).to_csv(
        output_path, index=False, encoding="utf-8-sig"
    )
    manager.update_output(job.job_id, output_path, [])

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.get(f"/api/jobs/{job.job_id}/rows?offset=1&limit=2")
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 5
    assert payload["offset"] == 1
    assert payload["limit"] == 2
    assert payload["columns"] == ["链接", "视频文本"]
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["链接"] == "https://example.com/1"
    assert payload["rows"][1]["视频文本"] == "文本2"


def test_job_rows_endpoint_clamps_limit_and_falls_back_to_upload(tmp_path):
    manager = JobManager()
    upload_path = tmp_path / "links.csv"
    pd.DataFrame([{"链接": f"https://example.com/{index}"} for index in range(3)]).to_csv(
        upload_path, index=False, encoding="utf-8-sig"
    )
    job = manager.create_session(
        original_filename="links.csv",
        upload_path=upload_path,
        columns=["链接"],
        preview_rows=[],
        row_count=3,
    )

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        clamped = client.get(f"/api/jobs/{job.job_id}/rows?limit=999")
        paged = client.get(f"/api/jobs/{job.job_id}/rows?offset=2&limit=50")
        missing = client.get("/api/jobs/no-such-job/rows")
    finally:
        main_module.job_manager = previous_manager

    assert clamped.status_code == 200
    assert clamped.json()["limit"] == 200
    assert paged.status_code == 200
    assert paged.json()["offset"] == 2
    assert len(paged.json()["rows"]) == 1
    assert missing.status_code == 404


def test_job_start_returns_409_without_stored_options():
    manager = JobManager()
    job = _make_job(manager)

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(f"/jobs/{job.job_id}/start", data={"mode": "full"})
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 409
    assert "还没有运行参数" in response.json()["detail"]


def test_job_start_uses_stored_options_and_redirects():
    manager = JobManager()
    job = _make_job(manager)
    _configure_job(manager, job, download_only=True)

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_job_worker = lambda job_id, mode="full": captured.update(job_id=job_id, mode=mode)
    try:
        client = TestClient(app)
        response = client.post(f"/jobs/{job.job_id}/start", data={"mode": "full"})
    finally:
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code in (200, 303)
    assert captured == {"job_id": job.job_id, "mode": "full"}
    configured = manager.require(job.job_id)
    assert configured.url_column == "链接"
    assert configured.download_only is True


def test_job_start_falls_back_to_batch_options():
    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])
    manager.configure_batch(
        batch_id=batch.batch_id,
        url_column="链接",
        transcript_column="视频文本",
        model_names_text=DEFAULT_MODEL,
        model_names=[DEFAULT_MODEL],
        overwrite=False,
        download_only=True,
    )
    job.url_column = None  # 模拟从未单独配置过的任务

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_job_worker = lambda job_id, mode="full": captured.update(job_id=job_id, mode=mode)
    try:
        client = TestClient(app)
        response = client.post(f"/jobs/{job.job_id}/start", data={"mode": "full"})
    finally:
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code in (200, 303)
    assert captured == {"job_id": job.job_id, "mode": "full"}
    assert manager.require(job.job_id).url_column == "链接"


def test_job_start_continue_existing_requires_result_file():
    manager = JobManager()
    job = _make_job(manager)
    _configure_job(manager, job)

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(f"/jobs/{job.job_id}/start", data={"mode": "continue_existing"})
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 409
    assert "没有可续跑的结果文件" in response.json()["detail"]


def test_job_start_continue_existing_uses_stored_options(tmp_path):
    manager = JobManager()
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=10,
        resume_output_path=tmp_path / "1.processed.csv",
    )
    _configure_job(manager, job)
    job.resume_output_path.write_text("链接,处理状态\nhttps://example.com,成功\n", encoding="utf-8-sig")

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_job_worker = lambda job_id, mode="full": captured.update(job_id=job_id, mode=mode)
    try:
        client = TestClient(app)
        response = client.post(f"/jobs/{job.job_id}/start", data={"mode": "continue_existing"})
    finally:
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code in (200, 303)
    assert captured == {"job_id": job.job_id, "mode": "continue_existing"}


def test_job_start_rejects_unknown_mode():
    manager = JobManager()
    job = _make_job(manager)

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(f"/jobs/{job.job_id}/start", data={"mode": "bogus"})
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 400


def test_cooldown_remaining_seconds_flows_into_payload():
    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])

    ends_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    manager.set_cooldown_state(job.job_id, active=True, ends_at=ends_at)
    manager.set_batch_cooldown_state(batch.batch_id, active=True, ends_at=ends_at)

    job_snapshot = manager.snapshot(job.job_id)
    batch_snapshot = manager.batch_snapshot(batch.batch_id)
    assert 0 < job_snapshot["cooldown_remaining_seconds"] <= 300
    assert 0 < batch_snapshot["cooldown_remaining_seconds"] <= 300
    assert job_snapshot["cooldown_ends_at"] is not None

    manager.set_cooldown_state(job.job_id, active=False)
    manager.set_batch_cooldown_state(batch.batch_id, active=False)
    assert manager.snapshot(job.job_id)["cooldown_remaining_seconds"] == 0
    assert manager.snapshot(job.job_id)["cooldown_ends_at"] is None
    assert manager.batch_snapshot(batch.batch_id)["cooldown_remaining_seconds"] == 0


def test_add_account_starts_relogin_and_creates_account(tmp_path):
    from app import main as main_module
    from app.cookie_accounts import CookieAccountManager

    manager = CookieAccountManager(accounts_dir=tmp_path)
    previous_manager = main_module.cookie_account_manager
    previous_relogin = main_module.start_account_relogin
    started: list[str] = []
    main_module.cookie_account_manager = manager
    main_module.start_account_relogin = lambda account_id: started.append(account_id)
    try:
        client = TestClient(app)
        response = client.post("/accounts", data={"label": "新账号A"})
    finally:
        main_module.cookie_account_manager = previous_manager
        main_module.start_account_relogin = previous_relogin

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["message"].endswith("扫码完成登录后关闭该窗口即可保存。")
    assert started == [payload["account_id"]]
    account = manager.get_account(payload["account_id"])
    assert account is not None
    assert account.label == "新账号A"


def test_add_account_requires_label():
    client = TestClient(app)
    response = client.post("/accounts", data={"label": "   "})
    assert response.status_code == 400
    assert response.json()["detail"] == "请输入备注"


def test_add_account_rejects_duplicate_label(tmp_path):
    from app import main as main_module
    from app.cookie_accounts import CookieAccountManager

    manager = CookieAccountManager(accounts_dir=tmp_path)
    manager.prepare_account("吴倩")
    previous_manager = main_module.cookie_account_manager
    previous_relogin = main_module.start_account_relogin
    started: list[str] = []
    main_module.cookie_account_manager = manager
    main_module.start_account_relogin = lambda account_id: started.append(account_id)
    try:
        client = TestClient(app)
        response = client.post("/accounts", data={"label": "吴倩"})
    finally:
        main_module.cookie_account_manager = previous_manager
        main_module.start_account_relogin = previous_relogin

    assert response.status_code == 409
    assert "已存在" in response.json()["detail"]
    assert started == []


class _AliveWorker:
    def is_alive(self):
        return True


def _make_cookie_manager(tmp_path) -> CookieAccountManager:
    return CookieAccountManager(
        accounts_dir=tmp_path / "browser" / "accounts",
        legacy_storage_state_path=tmp_path / "browser" / "legacy.json",
    )


def test_batch_retry_failed_raises_409_when_worker_start_fails():
    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])
    job.status = "failed"
    job.failed_rows = 2

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_batch_worker
    captured: dict[str, str] = {}

    def fake_start(batch_id, mode="full"):
        captured.update(batch_id=batch_id, mode=mode)
        return False

    main_module.job_manager = manager
    main_module.start_batch_worker = fake_start
    try:
        client = TestClient(app)
        response = client.post(f"/batches/{batch.batch_id}/retry-failed")
    finally:
        main_module.job_manager = previous_manager
        main_module.start_batch_worker = previous_worker

    assert response.status_code == 409
    assert captured == {"batch_id": batch.batch_id, "mode": "retry_failed"}


def test_batch_retry_failed_rechecks_worker_state_inside_lock():
    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])
    job.status = "failed"
    job.failed_rows = 2

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    main_module._batch_workers[batch.batch_id] = _AliveWorker()
    try:
        client = TestClient(app)
        response = client.post(f"/batches/{batch.batch_id}/retry-failed")
    finally:
        main_module._batch_workers.pop(batch.batch_id, None)
        main_module.job_manager = previous_manager

    assert response.status_code == 409
    assert "已有 worker" in response.json()["detail"]
    # 启动被拒时批次不能被留在 queued 假死
    assert manager.require_batch(batch.batch_id).status == "uploaded"


def test_batch_retry_failed_rejects_active_single_worker():
    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])
    job.status = "failed"
    job.failed_rows = 2

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    main_module._job_workers[job.job_id] = _AliveWorker()
    try:
        client = TestClient(app)
        response = client.post(f"/batches/{batch.batch_id}/retry-failed")
    finally:
        main_module._job_workers.pop(job.job_id, None)
        main_module.job_manager = previous_manager

    assert response.status_code == 409
    assert "已有 worker" in response.json()["detail"]


def test_batch_run_rejects_start_when_member_job_worker_is_active():
    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_batch_worker
    main_module.job_manager = manager
    main_module._job_workers[job.job_id] = _AliveWorker()
    main_module.start_batch_worker = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("成员任务已有单任务 worker，批次 worker 不应启动")
    )
    try:
        client = TestClient(app)
        response = client.post(f"/batches/{batch.batch_id}/run", data={"url_column": "链接"})
    finally:
        main_module._job_workers.pop(job.job_id, None)
        main_module.job_manager = previous_manager
        main_module.start_batch_worker = previous_worker

    assert response.status_code == 409
    assert "已有 worker" in response.json()["detail"]
    # 配置也不能被重置
    assert manager.require(job.job_id).status == "uploaded"


def test_job_rows_returns_409_when_source_file_is_missing():
    manager = JobManager()
    job = _make_job(manager)  # upload_path=output_path 均不存在

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.get(f"/api/jobs/{job.job_id}/rows")
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 409
    assert "结果文件不存在或暂不可读" in response.json()["detail"]


def test_preview_rolls_back_created_sessions_and_upload_dirs(tmp_path, monkeypatch):
    from app import main as main_module

    manager = JobManager()
    upload_root = tmp_path / "uploads"
    real_inspect = main_module.inspect_csv

    def fake_inspect(path):
        if path.name == "bad.csv":
            raise RuntimeError("CSV 读取失败")
        return real_inspect(path)

    monkeypatch.setattr(main_module, "inspect_csv", fake_inspect)
    monkeypatch.setattr(main_module, "UPLOAD_DIR", upload_root)

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(
            "/preview",
            files=[
                ("files", ("good.csv", "链接\nhttps://example.com/1\n", "text/csv")),
                ("files", ("bad.csv", "whatever\n", "text/csv")),
            ],
        )
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 400
    assert "bad.csv" in response.text
    assert manager._jobs == {}
    assert not upload_root.exists() or list(upload_root.iterdir()) == []


def test_api_preview_rolls_back_created_sessions_and_upload_dirs(tmp_path, monkeypatch):
    from app import main as main_module

    manager = JobManager()
    upload_root = tmp_path / "uploads"
    real_inspect = main_module.inspect_csv

    def fake_inspect(path):
        if path.name == "bad.csv":
            raise RuntimeError("CSV 读取失败")
        return real_inspect(path)

    monkeypatch.setattr(main_module, "inspect_csv", fake_inspect)
    monkeypatch.setattr(main_module, "UPLOAD_DIR", upload_root)

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(
            "/api/preview",
            files=[
                ("files", ("good.csv", "链接\nhttps://example.com/1\n", "text/csv")),
                ("files", ("bad.csv", "whatever\n", "text/csv")),
            ],
        )
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 400
    assert "bad.csv" in response.json()["detail"]
    assert manager._jobs == {}
    assert not upload_root.exists() or list(upload_root.iterdir()) == []


def test_quick_download_rolls_back_batch_and_job_when_start_conflicts(tmp_path, monkeypatch):
    from app import main as main_module

    manager = JobManager()
    upload_root = tmp_path / "uploads"

    monkeypatch.setattr(main_module, "UPLOAD_DIR", upload_root)
    monkeypatch.setattr(main_module, "_configure_and_start_batch", lambda *args, **kwargs: False)

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post("/quick-download", data={"links": "https://v.douyin.com/AAA111/"})
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 409
    assert manager._jobs == {}
    assert manager._batches == {}
    assert not upload_root.exists() or list(upload_root.iterdir()) == []


def test_job_continue_requires_existing_result_file():
    manager = JobManager()
    job = _make_job(manager)
    _configure_job(manager, job)

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(f"/jobs/{job.job_id}/continue", data={"url_column": "链接"})
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 409
    assert "没有可续跑的结果文件" in response.json()["detail"]


def test_job_continue_starts_worker_once_result_file_exists(tmp_path):
    manager = JobManager()
    job = _make_job(manager)
    _configure_job(manager, job)
    job.resume_output_path = tmp_path / "1.processed.csv"
    job.resume_output_path.write_text("链接,处理状态\nhttps://example.com,成功\n", encoding="utf-8-sig")

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_job_worker = lambda job_id, mode="full": captured.update(job_id=job_id, mode=mode) or True
    try:
        client = TestClient(app)
        response = client.post(f"/jobs/{job.job_id}/continue", data={"url_column": "链接"})
    finally:
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code in (200, 303)
    assert captured == {"job_id": job.job_id, "mode": "continue_existing"}


def test_api_continue_requires_existing_result_file():
    manager = JobManager()
    job = _make_job(manager)
    _configure_job(manager, job)

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    main_module.job_manager = manager
    main_module.start_job_worker = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("没有结果文件时不能启动续跑")
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/jobs/{job.job_id}/continue",
            json={"url_column": "链接", "download_only": True},
        )
    finally:
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code == 409
    assert "没有可续跑的结果文件" in response.json()["detail"]


def test_csrf_middleware_rejects_post_with_foreign_origin():
    client = TestClient(app)
    response = client.post(
        "/quick-download",
        data={"links": ""},
        headers={"Origin": "http://evil.example.com"},
    )
    assert response.status_code == 403
    assert "跨站请求被拒绝" in response.json()["detail"]


def test_csrf_middleware_rejects_post_with_foreign_referer():
    client = TestClient(app)
    response = client.post(
        "/quick-download",
        data={"links": ""},
        headers={"Referer": "http://evil.example.com/form"},
    )
    assert response.status_code == 403


def test_csrf_middleware_allows_same_origin_and_headerless_posts():
    client = TestClient(app)
    headerless = client.post("/quick-download", data={"links": "  "})
    assert headerless.status_code == 400  # 放行后由业务校验拒绝
    same_origin = client.post("/quick-download", data={"links": "  "}, headers={"Origin": "http://testserver"})
    assert same_origin.status_code == 400
    same_referer = client.post(
        "/quick-download",
        data={"links": "  "},
        headers={"Referer": "http://testserver/preview"},
    )
    assert same_referer.status_code == 400


def test_batch_download_zip_disambiguates_same_named_outputs(tmp_path):
    manager = JobManager()
    job_a = _make_job(manager, row_count=1)
    job_b = _make_job(manager, row_count=1)
    batch = manager.create_batch([job_a, job_b], common_columns=["链接"])

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    output_a = dir_a / "1.processed.csv"
    output_b = dir_b / "1.processed.csv"
    output_a.write_bytes("链接,视频文本\nhttps://a.example.com,甲\n".encode("utf-8-sig"))
    output_b.write_bytes("链接,视频文本\nhttps://b.example.com,乙\n".encode("utf-8-sig"))
    manager.update_output(job_a.job_id, output_a, [])
    manager.update_output(job_b.job_id, output_b, [])

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.get(f"/download/batches/{batch.batch_id}")
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 200
    with ZipFile(io.BytesIO(response.content)) as archive:
        contents = {name: archive.read(name).decode("utf-8-sig") for name in archive.namelist()}

    disambiguated = f"1-{job_b.job_id[:8]}.processed.csv"
    assert set(contents) == {"1.processed.csv", disambiguated}
    assert "https://a.example.com" in contents["1.processed.csv"]
    assert "https://b.example.com" in contents[disambiguated]


def test_preview_resume_detection_uses_find_latest_processed_path(tmp_path, monkeypatch):
    from app import main as main_module

    existing_output = tmp_path / "1.abc123de.processed.csv"
    existing_output.write_text("链接,视频文本\nhttps://example.com/old,旧文本\n", encoding="utf-8-sig")
    monkeypatch.setattr(
        main_module,
        "find_latest_processed_path",
        lambda name: existing_output if name == "1.csv" else None,
    )

    manager = JobManager()
    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(
            "/preview",
            files={"files": ("1.csv", "链接\nhttps://example.com/new\n", "text/csv")},
        )
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 200
    (job,) = list(manager._jobs.values())
    assert job.resume_output_path == existing_output


def test_relogin_rejects_concurrent_start_and_restarts_after_thread_dies(tmp_path, monkeypatch):
    from app import main as main_module

    manager = _make_cookie_manager(tmp_path)
    account = manager.prepare_account("小号B")

    previous_manager = main_module.cookie_account_manager
    previous_threads = dict(main_module.account_relogin_threads)
    main_module.cookie_account_manager = manager
    main_module.account_relogin_threads = {}

    entered = threading.Event()
    release = threading.Event()

    def fake_login(label, *, wait_mode, logger):
        entered.set()
        release.wait(10)

    monkeypatch.setattr(main_module, "login_with_label", fake_login)
    try:
        main_module.start_account_relogin(account.account_id)
        assert entered.wait(10)

        with pytest.raises(RuntimeError, match="正在重新登录中"):
            main_module.start_account_relogin(account.account_id)

        release.set()
        first = main_module.account_relogin_threads[account.account_id]
        first.join(timeout=10)
        assert not first.is_alive()
        assert account.account_id not in main_module.account_relogin_threads

        # 线程已死：允许清掉过期标记并重启
        main_module.start_account_relogin(account.account_id)
        second = main_module.account_relogin_threads[account.account_id]
        assert second is not first
        second.join(timeout=10)
    finally:
        release.set()
        leftover = main_module.account_relogin_threads.get(account.account_id)
        if leftover is not None:
            leftover.join(timeout=10)
        main_module.cookie_account_manager = previous_manager
        main_module.account_relogin_threads = previous_threads


def test_relogin_worker_only_pops_its_own_registry_entry(tmp_path, monkeypatch):
    from app import main as main_module

    manager = _make_cookie_manager(tmp_path)
    account = manager.prepare_account("小号C")

    previous_manager = main_module.cookie_account_manager
    previous_threads = dict(main_module.account_relogin_threads)
    main_module.cookie_account_manager = manager
    main_module.account_relogin_threads = {}

    hijacker = threading.Thread(target=lambda: None)  # 占位槽位，worker 不得替别人 pop
    real_worker = []

    def fake_login(label, *, wait_mode, logger):
        real_worker.append(main_module.account_relogin_threads[account.account_id])
        main_module.account_relogin_threads[account.account_id] = hijacker

    monkeypatch.setattr(main_module, "login_with_label", fake_login)
    try:
        main_module.start_account_relogin(account.account_id)
        real_worker[0].join(timeout=10)
        assert main_module.account_relogin_threads.get(account.account_id) is hijacker
    finally:
        main_module.account_relogin_threads.pop(account.account_id, None)
        main_module.cookie_account_manager = previous_manager
        main_module.account_relogin_threads = previous_threads


def test_login_session_uses_unique_temp_path_for_concurrent_writers(tmp_path, monkeypatch):
    from app import login_session

    account_path = tmp_path / "browser" / "accounts" / "main.json"
    temp_paths: list[Path] = []

    class DummyAccount:
        account_id = "main"
        label = "主号"
        storage_state_path = account_path

    class DummyAccountManager:
        def prepare_account(self, label):
            return DummyAccount()

        def mark_relogin_in_progress(self, account_id, active):
            return DummyAccount()

        def record_login(self, account_id, *, label=None):
            return DummyAccount()

    class _FakePage:
        def goto(self, url, wait_until=""):
            return None

        def wait_for_load_state(self, state, timeout=0):
            return None

        def on(self, event, handler):
            return None

        def is_closed(self):
            return True

    class _FakeContext:
        def __init__(self):
            self.page = _FakePage()

        def clear_cookies(self):
            return None

        def add_init_script(self, script):
            return None

        def new_page(self):
            return self.page

        def storage_state(self, path):
            temp_paths.append(Path(path))
            Path(path).write_text(
                json.dumps({"cookies": [{"name": "ttwid", "value": "x"}], "origins": []}),
                encoding="utf-8",
            )

        def cookies(self, urls=None):
            return []

        def close(self):
            return None

        @property
        def pages(self):
            return []

    class _FakeBrowser:
        def __init__(self, context):
            self._context = context

        def new_context(self, **kwargs):
            return self._context

        def close(self):
            return None

    class _FakeChromium:
        def __init__(self, context):
            self._context = context

        def launch(self, headless=False):
            return _FakeBrowser(self._context)

    class _FakePlaywright:
        def __init__(self, context):
            self.chromium = _FakeChromium(context)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _FakeSyncPlaywright:
        def __call__(self):
            return _FakePlaywright(_FakeContext())

    monkeypatch.setattr(login_session, "CookieAccountManager", lambda: DummyAccountManager())
    monkeypatch.setattr(login_session, "ensure_directories", lambda: None)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", _FakeSyncPlaywright())
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    threads = [
        threading.Thread(
            target=login_session.login_with_label,
            args=("主号",),
            kwargs={"wait_mode": "enter", "logger": lambda message: None},
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert account_path.exists()
    assert len(temp_paths) == 2
    assert len({path.name for path in temp_paths}) == 2
    assert all(re.fullmatch(r"main\.[0-9a-f]{8}\.tmp\.json", path.name) for path in temp_paths)
    assert list(account_path.parent.glob("*.tmp.json")) == []


def test_delete_account_route_removes_account(tmp_path):
    from app import main as main_module
    from app.cookie_accounts import CookieAccountManager

    manager = CookieAccountManager(accounts_dir=tmp_path)
    account = manager.prepare_account("要删除")
    previous_manager = main_module.cookie_account_manager
    main_module.cookie_account_manager = manager
    try:
        client = TestClient(app)
        response = client.post(f"/accounts/{account.account_id}/delete")
    finally:
        main_module.cookie_account_manager = previous_manager

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert manager.get_account(account.account_id) is None


def test_delete_account_route_404_for_missing(tmp_path):
    from app import main as main_module
    from app.cookie_accounts import CookieAccountManager

    previous_manager = main_module.cookie_account_manager
    main_module.cookie_account_manager = CookieAccountManager(accounts_dir=tmp_path)
    try:
        client = TestClient(app)
        response = client.post("/accounts/不存在/delete")
    finally:
        main_module.cookie_account_manager = previous_manager

    assert response.status_code == 404


def test_delete_account_route_409_while_relogin_in_progress(tmp_path):
    from app import main as main_module
    from app.cookie_accounts import CookieAccountManager

    manager = CookieAccountManager(accounts_dir=tmp_path)
    account = manager.prepare_account("登录中")
    manager.mark_relogin_in_progress(account.account_id, True)
    previous_manager = main_module.cookie_account_manager
    previous_alive = main_module._is_relogin_thread_alive
    main_module.cookie_account_manager = manager
    main_module._is_relogin_thread_alive = lambda account_id: True
    try:
        client = TestClient(app)
        response = client.post(f"/accounts/{account.account_id}/delete")
    finally:
        main_module.cookie_account_manager = previous_manager
        main_module._is_relogin_thread_alive = previous_alive

    assert response.status_code == 409
    assert manager.get_account(account.account_id) is not None
