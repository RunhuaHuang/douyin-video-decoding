import json
import pandas as pd
from pathlib import Path
from zipfile import ZipFile

from app.cookie_accounts import CookieAccountManager
from app.csv_service import build_processed_output_name, inspect_csv, write_csv
from app.job_manager import JobManager
from app.main import app
from fastapi.testclient import TestClient


def test_inspect_csv_supports_xlsx_file(tmp_path):
    xlsx_path = tmp_path / "input.xlsx"
    pd.DataFrame(
        [
            {"链接": "https://v.douyin.com/AAA111/", "标题": "a"},
            {"链接": "https://v.douyin.com/BBB222/", "标题": "b"},
        ]
    ).to_excel(xlsx_path, index=False, engine="openpyxl")

    inspection = inspect_csv(xlsx_path)

    assert inspection.file_type == "xlsx"
    assert inspection.columns == ["链接", "标题"]
    assert inspection.row_count == 2
    assert inspection.preview_rows[0]["链接"] == "https://v.douyin.com/AAA111/"


def test_write_csv_writes_xlsx_when_output_suffix_is_xlsx(tmp_path):
    dataframe = pd.DataFrame([{"链接": "https://v.douyin.com/AAA111/", "视频文本": "hello"}])
    output_path = tmp_path / "result.xlsx"

    write_csv(dataframe, output_path)

    loaded = pd.read_excel(output_path, dtype=str, keep_default_na=False, engine="openpyxl")
    assert loaded.loc[0, "视频文本"] == "hello"


def test_job_manager_can_create_batch_snapshot(tmp_path):
    manager = JobManager()
    job_a = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接", "标题"],
        preview_rows=[],
        row_count=10,
        resume_output_path=tmp_path / "1.processed.csv",
    )
    job_b = manager.create_session(
        original_filename="2.xlsx",
        upload_path=Path("dummy-b"),
        columns=["链接", "标题", "作者"],
        preview_rows=[],
        row_count=20,
    )
    job_a.resume_output_path.write_text("链接,处理状态\nhttps://example.com,成功\n", encoding="utf-8-sig")

    batch = manager.create_batch([job_a, job_b], common_columns=["链接", "标题"])
    snapshot = manager.batch_snapshot(batch.batch_id)

    assert snapshot["total_files"] == 2
    assert snapshot["resumable_files"] == 1
    assert snapshot["common_columns"] == ["链接", "标题"]
    assert snapshot["jobs"][0]["sequence"] == 1
    assert snapshot["jobs"][0]["resume_available"] is True
    assert snapshot["jobs"][1]["sequence"] == 2


def test_build_processed_output_name_always_returns_csv():
    assert build_processed_output_name("1.csv") == "1.processed.csv"
    assert build_processed_output_name("2.xlsx") == "2.processed.csv"


def test_build_processed_output_name_appends_job_id_prefix():
    assert build_processed_output_name("1.csv", "abcd1234efgh5678") == "1.abcd1234.processed.csv"


def test_batch_download_returns_zip_with_all_ready_outputs(tmp_path):
    manager = JobManager()
    job_a = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=10,
    )
    job_b = manager.create_session(
        original_filename="2.xlsx",
        upload_path=Path("dummy-b"),
        columns=["链接"],
        preview_rows=[],
        row_count=20,
    )
    batch = manager.create_batch([job_a, job_b], common_columns=["链接"])

    output_a = tmp_path / "1.processed.csv"
    output_b = tmp_path / "2.processed.csv"
    output_a.write_text("a", encoding="utf-8")
    output_b.write_text("b", encoding="utf-8")
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
    assert response.headers["content-type"].startswith("application/zip")

    zip_path = tmp_path / "results.zip"
    zip_path.write_bytes(response.content)
    with ZipFile(zip_path) as archive:
        names = sorted(archive.namelist())

    assert names == ["1.processed.csv", "2.processed.csv"]


def test_preview_detects_existing_processed_output(tmp_path, monkeypatch):
    existing_output = tmp_path / "1.processed.csv"
    existing_output.write_text("视频链接,处理状态\nhttps://example.com,成功\n", encoding="utf-8-sig")

    from app import main as main_module

    monkeypatch.setattr(
        main_module, "find_latest_processed_path", lambda *args, **kwargs: existing_output, raising=False
    )

    client = TestClient(app)
    response = client.post(
        "/preview",
        files={"files": ("1.csv", "视频链接\nhttps://www.douyin.com/video/1\n", "text/csv")},
    )

    assert response.status_code == 200
    assert "继续已有结果" in response.text
    assert "可继续" in response.text


def test_continue_batch_endpoint_uses_continue_existing_mode(monkeypatch):
    manager = JobManager()
    resume_path = Path("1.processed.csv")
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=10,
        resume_output_path=resume_path,
    )
    batch = manager.create_batch([job], common_columns=["链接"])

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_batch_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_batch_worker = lambda batch_id, mode="full": captured.update(batch_id=batch_id, mode=mode)
    try:
        client = TestClient(app)
        response = client.post(
            f"/batches/{batch.batch_id}/continue",
            data={
                "url_column": "链接",
                "transcript_column": "视频文本",
                "model_names": "mlx-community/whisper-large-v3-turbo",
            },
        )
    finally:
        main_module.job_manager = previous_manager
        main_module.start_batch_worker = previous_worker

    assert response.status_code == 200 or response.status_code == 303
    assert captured == {"batch_id": batch.batch_id, "mode": "continue_existing"}


def test_skip_job_cooldown_endpoint_sets_skip_flag():
    manager = JobManager()
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=1,
    )
    job.status = "running"
    job.cooldown_active = True

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(f"/jobs/{job.job_id}/skip-cooldown")
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 200
    assert manager.require(job.job_id).skip_cooldown_requested is True


def test_skip_batch_cooldown_endpoint_sets_skip_flags():
    manager = JobManager()
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=1,
    )
    batch = manager.create_batch([job], common_columns=["链接"])
    job.status = "running"
    job.cooldown_active = True
    batch.status = "running"
    batch.current_job_id = job.job_id
    batch.cooldown_active = True

    from app import main as main_module

    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        response = client.post(f"/batches/{batch.batch_id}/skip-cooldown")
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 200
    assert manager.require_batch(batch.batch_id).skip_cooldown_requested is True
    assert manager.require(job.job_id).skip_cooldown_requested is True


def test_build_cookie_pool_snapshot_clears_stale_relogin_flag(tmp_path):
    accounts_dir = tmp_path / "browser" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / "demo.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "ttwid",
                        "value": "cookie",
                        "domain": ".douyin.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (accounts_dir / "demo.meta.json").write_text(
        json.dumps(
            {
                "account_id": "demo",
                "label": "demo",
                "relogin_in_progress": True,
                "source": "managed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from app import main as main_module

    previous_manager = main_module.cookie_account_manager
    previous_threads = dict(main_module.account_relogin_threads)
    main_module.cookie_account_manager = CookieAccountManager(
        accounts_dir=accounts_dir,
        legacy_storage_state_path=tmp_path / "browser" / "legacy.json",
    )
    main_module.account_relogin_threads = {}
    try:
        snapshot = main_module.build_cookie_pool_snapshot()
        account = main_module.cookie_account_manager.get_account("demo")
    finally:
        main_module.cookie_account_manager = previous_manager
        main_module.account_relogin_threads = previous_threads

    assert snapshot["accounts"][0]["relogin_in_progress"] is False
    assert account is not None
    assert account.relogin_in_progress is False


def test_build_cookie_pool_snapshot_recovers_when_cookie_file_was_updated(tmp_path):
    accounts_dir = tmp_path / "browser" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    storage_path = accounts_dir / "demo.json"
    storage_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "ttwid",
                        "value": "cookie",
                        "domain": ".douyin.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    meta_path = accounts_dir / "demo.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "account_id": "demo",
                "label": "demo",
                "relogin_in_progress": True,
                "updated_at": "2026-03-14T00:00:00+00:00",
                "source": "managed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    storage_path.touch()

    from app import main as main_module

    previous_manager = main_module.cookie_account_manager
    previous_threads = dict(main_module.account_relogin_threads)
    main_module.cookie_account_manager = CookieAccountManager(
        accounts_dir=accounts_dir,
        legacy_storage_state_path=tmp_path / "browser" / "legacy.json",
    )
    main_module.account_relogin_threads = {}
    try:
        snapshot = main_module.build_cookie_pool_snapshot()
        account = main_module.cookie_account_manager.get_account("demo")
    finally:
        main_module.cookie_account_manager = previous_manager
        main_module.account_relogin_threads = previous_threads

    assert snapshot["accounts"][0]["relogin_in_progress"] is False
    assert account is not None
    assert account.relogin_in_progress is False


def test_relogin_endpoint_allows_retry_when_stale_flag_exists(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "browser" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / "demo.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "ttwid",
                        "value": "cookie",
                        "domain": ".douyin.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (accounts_dir / "demo.meta.json").write_text(
        json.dumps(
            {
                "account_id": "demo",
                "label": "demo",
                "relogin_in_progress": True,
                "source": "managed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from app import main as main_module

    previous_manager = main_module.cookie_account_manager
    previous_threads = dict(main_module.account_relogin_threads)
    captured = {}
    main_module.cookie_account_manager = CookieAccountManager(
        accounts_dir=accounts_dir,
        legacy_storage_state_path=tmp_path / "browser" / "legacy.json",
    )
    main_module.account_relogin_threads = {}
    monkeypatch.setattr(main_module, "start_account_relogin", lambda account_id: captured.update(account_id=account_id))
    try:
        client = TestClient(app)
        response = client.post("/accounts/demo/relogin")
    finally:
        main_module.cookie_account_manager = previous_manager
        main_module.account_relogin_threads = previous_threads

    assert response.status_code == 200
    assert captured == {"account_id": "demo"}


def test_relogin_endpoint_allows_restart_when_thread_is_still_tracked(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "browser" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / "demo.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "ttwid",
                        "value": "cookie",
                        "domain": ".douyin.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (accounts_dir / "demo.meta.json").write_text(
        json.dumps(
            {
                "account_id": "demo",
                "label": "demo",
                "relogin_in_progress": True,
                "source": "managed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class DummyThread:
        def is_alive(self):
            return True

    from app import main as main_module

    previous_manager = main_module.cookie_account_manager
    previous_threads = dict(main_module.account_relogin_threads)
    captured = {}
    main_module.cookie_account_manager = CookieAccountManager(
        accounts_dir=accounts_dir,
        legacy_storage_state_path=tmp_path / "browser" / "legacy.json",
    )
    main_module.account_relogin_threads = {"demo": DummyThread()}
    monkeypatch.setattr(main_module, "start_account_relogin", lambda account_id: captured.update(account_id=account_id))
    try:
        client = TestClient(app)
        response = client.post("/accounts/demo/relogin")
    finally:
        main_module.cookie_account_manager = previous_manager
        main_module.account_relogin_threads = previous_threads

    assert response.status_code == 200
    assert captured == {"account_id": "demo"}


def test_api_preview_returns_batch_and_jobs_json(tmp_path, monkeypatch):
    from app import main as main_module

    csv_path = tmp_path / "links.csv"
    csv_path.write_text("链接\nhttps://v.douyin.com/AAA111/\n", encoding="utf-8")

    manager = JobManager()
    previous_manager = main_module.job_manager
    main_module.job_manager = manager
    try:
        client = TestClient(app)
        with csv_path.open("rb") as handle:
            response = client.post("/api/preview", files={"files": ("links.csv", handle, "text/csv")})
    finally:
        main_module.job_manager = previous_manager

    assert response.status_code == 200
    payload = response.json()
    assert payload["batch_id"]
    assert payload["common_columns"] == ["链接"]
    assert len(payload["jobs"]) == 1
    job = payload["jobs"][0]
    assert job["job_id"]
    assert job["columns"] == ["链接"]
    assert job["row_count"] == 1


def test_api_run_job_configures_and_starts_full_mode(monkeypatch):
    manager = JobManager()
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=10,
    )

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_job_worker = lambda job_id, mode="full": captured.update(job_id=job_id, mode=mode)
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/run/{job.job_id}",
            json={"url_column": "链接", "download_only": True},
        )
    finally:
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code == 200
    assert captured == {"job_id": job.job_id, "mode": "full"}
    payload = response.json()
    assert payload["job_id"] == job.job_id
    assert manager.require(job.job_id).download_only is True


def test_api_run_job_rejects_unknown_url_column(monkeypatch):
    manager = JobManager()
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=10,
    )

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    main_module.job_manager = manager
    main_module.start_job_worker = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("invalid input must not start a worker")
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/run/{job.job_id}",
            json={"url_column": "不存在的列"},
        )
    finally:
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code == 409
    assert "不存在" in response.json()["detail"]


def test_api_run_job_does_not_reset_job_when_worker_is_active(monkeypatch):
    manager = JobManager()
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=10,
    )

    from app import main as main_module

    class _AliveWorker:
        def is_alive(self):
            return True

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    main_module.job_manager = manager
    main_module._job_workers[job.job_id] = _AliveWorker()
    main_module.start_job_worker = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("an active worker must not be started twice")
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/run/{job.job_id}",
            json={"url_column": "链接", "download_only": True},
        )
    finally:
        main_module._job_workers.pop(job.job_id, None)
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code == 409
    assert manager.require(job.job_id).status == "uploaded"


def test_api_run_job_rejects_job_owned_by_running_batch(monkeypatch):
    manager = JobManager()
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=10,
    )
    batch = manager.create_batch([job], common_columns=["链接"])

    from app import main as main_module

    class _AliveWorker:
        def is_alive(self):
            return True

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    main_module.job_manager = manager
    main_module._batch_workers[batch.batch_id] = _AliveWorker()
    main_module.start_job_worker = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("a job inside a running batch must not start independently")
    )
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/run/{job.job_id}",
            json={"url_column": "链接", "download_only": True},
        )
    finally:
        main_module._batch_workers.pop(batch.batch_id, None)
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code == 409
    assert "所属批次" in response.json()["detail"]
    assert manager.require(job.job_id).status == "uploaded"


def test_api_run_batch_rejects_unknown_url_column(monkeypatch):
    manager = JobManager()
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=10,
    )
    batch = manager.create_batch([job], common_columns=["链接"])

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_batch_worker
    main_module.job_manager = manager
    main_module.start_batch_worker = lambda batch_id, mode="full": None
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/batches/{batch.batch_id}/run",
            json={"url_column": "不存在的列"},
        )
    finally:
        main_module.job_manager = previous_manager
        main_module.start_batch_worker = previous_worker

    assert response.status_code == 409
    assert "不在所有文件中都存在" in response.json()["detail"]


def test_api_accounts_returns_pool_snapshot(monkeypatch):
    from app import main as main_module

    monkeypatch.setattr(
        main_module,
        "build_cookie_pool_snapshot",
        lambda current_account_id="": {"accounts": [], "current_account_id": ""},
    )

    client = TestClient(app)
    response = client.get("/api/accounts")

    assert response.status_code == 200
    assert response.json() == {"accounts": [], "current_account_id": ""}


def test_run_job_endpoint_configures_and_starts_full_mode(monkeypatch):
    manager = JobManager()
    job = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接"],
        preview_rows=[],
        row_count=10,
    )

    from app import main as main_module

    previous_manager = main_module.job_manager
    previous_worker = main_module.start_job_worker
    captured: dict[str, str] = {}
    main_module.job_manager = manager
    main_module.start_job_worker = lambda job_id, mode="full": captured.update(job_id=job_id, mode=mode)
    try:
        client = TestClient(app)
        response = client.post(
            f"/run/{job.job_id}",
            data={
                "url_column": "链接",
                "transcript_column": "视频文本",
                "model_names": "FunAudioLLM/SenseVoiceSmall",
                "download_only": "true",
            },
        )
    finally:
        main_module.job_manager = previous_manager
        main_module.start_job_worker = previous_worker

    assert response.status_code == 200 or response.status_code == 303
    assert captured == {"job_id": job.job_id, "mode": "full"}
    configured = manager.require(job.job_id)
    assert configured.download_only is True
    assert configured.transcript_column == "视频文本"
