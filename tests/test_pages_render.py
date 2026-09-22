from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.cookie_accounts import CookieAccountManager
from app.job_manager import JobManager
from app.main import app


def _make_job(manager: JobManager, row_count: int = 10, **overrides):
    fields = {
        "original_filename": "1.csv",
        "upload_path": Path("dummy-a"),
        "columns": ["链接"],
        "preview_rows": [],
        "row_count": row_count,
    }
    fields.update(overrides)
    return manager.create_session(**fields)


def _client_for(monkeypatch, manager: JobManager) -> TestClient:
    from app import main as main_module

    monkeypatch.setattr(main_module, "job_manager", manager)
    return TestClient(app)


def test_index_page_renders(monkeypatch):
    client = _client_for(monkeypatch, JobManager())
    response = client.get("/")
    assert response.status_code == 200
    assert "工作台" in response.text
    assert "快速下载" in response.text
    assert "导入表格" in response.text
    assert "Cookies 账号池" in response.text


def test_job_page_renders_uploaded_job(monkeypatch):
    manager = JobManager()
    job = _make_job(manager)
    client = _client_for(monkeypatch, manager)
    response = client.get(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    assert job.job_id in response.text
    assert "待配置" in response.text
    assert "账号池" in response.text
    assert "结果预览" in response.text
    assert "全部行" in response.text
    assert "日志" in response.text


def test_job_page_renders_completed_job_with_escaped_preview(monkeypatch, tmp_path):
    manager = JobManager()
    job = _make_job(manager)
    preview_rows = [{"链接": "https://v.douyin.com/abc/", "视频文本": "<script>alert(1)</script>"}]
    manager.mark_completed(job.job_id, tmp_path / "1.processed.csv", preview_rows)
    client = _client_for(monkeypatch, manager)
    response = client.get(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    assert "已完成" in response.text
    assert f"/download/{job.job_id}" in response.text
    assert "https://v.douyin.com/abc/" in response.text
    # Jinja 首屏 autoescape：预览单元格中的 HTML 必须被转义
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_job_page_renders_failed_job_with_escaped_error(monkeypatch):
    manager = JobManager()
    job = _make_job(manager)
    manager.mark_failed(job.job_id, "风控校验失败 <b>")
    client = _client_for(monkeypatch, manager)
    response = client.get(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    assert "失败" in response.text
    assert "风控校验失败 &lt;b&gt;" in response.text


def test_job_page_renders_paused_job(monkeypatch, tmp_path):
    manager = JobManager()
    job = _make_job(manager)
    manager.mark_paused(job.job_id, tmp_path / "1.processed.csv", [])
    client = _client_for(monkeypatch, manager)
    response = client.get(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    assert "已暂停" in response.text


def test_job_page_renders_cooldown_countdown(monkeypatch):
    manager = JobManager()
    job = _make_job(manager)
    ends_at = datetime.now(timezone.utc) + timedelta(seconds=185)
    manager.set_cooldown_state(job.job_id, active=True, ends_at=ends_at)
    client = _client_for(monkeypatch, manager)
    response = client.get(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    # 185s ≈ 03:0x，断言到十位避免渲染与断言间跨秒
    assert "冷却剩余 03:0" in response.text


def test_job_page_escapes_original_filename(monkeypatch):
    manager = JobManager()
    job = _make_job(manager, original_filename="xss<script>alert(1)</script>.csv")
    client = _client_for(monkeypatch, manager)
    response = client.get(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    assert "xss&lt;script&gt;alert(1)&lt;/script&gt;.csv" in response.text
    assert "xss<script>" not in response.text


def test_batch_page_renders_jobs_in_multiple_statuses(monkeypatch, tmp_path):
    manager = JobManager()
    job_done = _make_job(manager, original_filename="done.csv")
    job_failed = _make_job(manager, original_filename="failed.csv")
    job_running = _make_job(manager, original_filename="running.csv")
    batch = manager.create_batch([job_done, job_failed, job_running], common_columns=["链接"])
    manager.mark_completed(job_done.job_id, tmp_path / "done.processed.csv", [])
    manager.mark_failed(job_failed.job_id, "下载失败")
    manager.mark_running(job_running.job_id, total_rows=10)
    client = _client_for(monkeypatch, manager)
    response = client.get(f"/batches/{batch.batch_id}")
    assert response.status_code == 200
    assert "批次详情" in response.text
    assert "运行参数" in response.text
    assert "Cookie 轮换" in response.text
    assert "文件进度" in response.text
    for filename in ("done.csv", "failed.csv", "running.csv"):
        assert filename in response.text
    for status_text in ("已完成", "任务失败", "运行中"):
        assert status_text in response.text


def test_batch_page_renders_cooldown_countdown(monkeypatch):
    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])
    ends_at = datetime.now(timezone.utc) + timedelta(seconds=185)
    manager.set_batch_cooldown_state(batch.batch_id, active=True, ends_at=ends_at)
    client = _client_for(monkeypatch, manager)
    response = client.get(f"/batches/{batch.batch_id}")
    assert response.status_code == 200
    assert "冷却剩余 03:0" in response.text


def test_batch_page_escapes_original_filename(monkeypatch):
    manager = JobManager()
    job = _make_job(manager, original_filename="xss<script>alert(1)</script>.csv")
    batch = manager.create_batch([job], common_columns=["链接"])
    client = _client_for(monkeypatch, manager)
    response = client.get(f"/batches/{batch.batch_id}")
    assert response.status_code == 200
    assert "xss&lt;script&gt;alert(1)&lt;/script&gt;.csv" in response.text
    assert "xss<script>" not in response.text


def test_pages_disable_relogin_button_while_relogin_in_progress(monkeypatch, tmp_path):
    from app import main as main_module

    manager = JobManager()
    job = _make_job(manager)
    batch = manager.create_batch([job], common_columns=["链接"])

    cookie_manager = CookieAccountManager(accounts_dir=tmp_path / "accounts")
    relogin_account = cookie_manager.prepare_account("重新登录中")
    cookie_manager.mark_relogin_in_progress(relogin_account.account_id, True)
    cookie_manager.prepare_account("正常账号")

    monkeypatch.setattr(main_module, "job_manager", manager)
    monkeypatch.setattr(main_module, "cookie_account_manager", cookie_manager)
    # 渲染时会 reconcile relogin 状态：伪装登录线程存活，避免 in-progress 标记被清掉
    monkeypatch.setattr(main_module, "_is_relogin_thread_alive", lambda account_id: True)

    client = TestClient(app)
    for path in ("/", f"/jobs/{job.job_id}", f"/batches/{batch.batch_id}"):
        response = client.get(path)
        assert response.status_code == 200
        normalized = " ".join(response.text.split())
        # 首屏两个账号的重新登录按钮：只有「重新登录中」那个带 is-disabled
        assert normalized.count('data-action="relogin" data-account-id="重新登录中"') == 1
        assert normalized.count('data-action="relogin" data-account-id="正常账号"') == 1
        assert (
            normalized.count('class="btn btn-secondary btn-sm account-action-button is-disabled" type="button" data-action="relogin"')
            == 1
        )
