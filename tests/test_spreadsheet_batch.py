import pandas as pd
from pathlib import Path
from zipfile import ZipFile

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


def test_job_manager_can_create_batch_snapshot():
    manager = JobManager()
    job_a = manager.create_session(
        original_filename="1.csv",
        upload_path=Path("dummy-a"),
        columns=["链接", "标题"],
        preview_rows=[],
        row_count=10,
        resume_output_path=Path("1.processed.csv"),
    )
    job_b = manager.create_session(
        original_filename="2.xlsx",
        upload_path=Path("dummy-b"),
        columns=["链接", "标题", "作者"],
        preview_rows=[],
        row_count=20,
    )

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

    monkeypatch.setattr(main_module, "processed_output_path_for_filename", lambda _: existing_output)

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
