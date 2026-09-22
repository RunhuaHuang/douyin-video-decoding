import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.cookie_accounts import CookieAccountManager
from app.job_manager import JobManager
from app.models import BatchRecord, JobRecord
from app.request_coordinator import RequestCoordinator


def _make_manager(tmp_path) -> CookieAccountManager:
    accounts_dir = tmp_path / "browser" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    return CookieAccountManager(
        accounts_dir=accounts_dir,
        legacy_storage_state_path=tmp_path / "browser" / "legacy.json",
    )


def _write_state(accounts_dir, account_id: str) -> None:
    (accounts_dir / f"{account_id}.json").write_text(
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


def test_cookie_account_manager_ignores_legacy_default_by_default(tmp_path):
    accounts_dir = tmp_path / "browser" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    legacy_storage_path = tmp_path / "browser" / "legacy.json"
    legacy_storage_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_storage_path.write_text(
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
    (accounts_dir / "__legacy_default__.meta.json").write_text(
        json.dumps(
            {
                "account_id": "__legacy_default__",
                "label": "默认会话",
                "source": "legacy",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = CookieAccountManager(
        accounts_dir=accounts_dir,
        legacy_storage_state_path=legacy_storage_path,
    )

    assert manager.list_accounts() == []
    assert manager.list_available_accounts() == []


def test_cookie_account_manager_migrates_legacy_auto_invalid_to_warning(tmp_path):
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
                "invalid": True,
                "invalid_reason": "连续 3 次命中强风控/验证码：强冷却信号：验证码挑战页",
                "invalidated_at": "2026-03-13T15:08:28.187364+00:00",
                "challenge_strikes": 3,
                "source": "managed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = CookieAccountManager(
        accounts_dir=accounts_dir,
        legacy_storage_state_path=tmp_path / "browser" / "legacy.json",
    )
    account = manager.get_account("demo")

    assert account is not None
    assert account.invalid is False
    assert account.warning is True
    assert account.warning_reason == "连续 3 次命中强风控/验证码：强冷却信号：验证码挑战页"


def test_write_metadata_is_atomic_and_leaves_no_tmp_files(tmp_path):
    manager = _make_manager(tmp_path)
    account = manager.prepare_account("demo")

    manager.note_issue(account.account_id, "HTTP 403")
    manager.mark_used(account.account_id)
    manager.mark_success(account.account_id)

    metadata_path = manager._metadata_path(account.account_id)
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["account_id"] == account.account_id
    leftovers = [
        path.name
        for path in manager.accounts_dir.iterdir()
        if ".tmp" in path.name
    ]
    assert leftovers == []


def test_note_issue_counts_strikes_atomically_under_concurrency(tmp_path):
    manager = _make_manager(tmp_path)
    account = manager.prepare_account("demo")
    _write_state(manager.accounts_dir, account.account_id)

    thread_count = 16
    errors = []

    def _worker() -> None:
        try:
            manager.note_issue(account.account_id, "HTTP 403")
        except Exception as exc:  # pragma: no cover - 失败即断言
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    updated = manager.get_account(account.account_id)
    assert updated.challenge_strikes == thread_count


def test_note_issue_on_warned_account_keeps_strikes_but_records_issue(tmp_path):
    manager = _make_manager(tmp_path)
    account = manager.prepare_account("demo")
    _write_state(manager.accounts_dir, account.account_id)

    manager.note_issue(account.account_id, "HTTP 403")
    warned = manager.mark_warning(account.account_id, "连续强信号")
    assert warned.warning is True

    issued = manager.note_issue(account.account_id, "验证码挑战页")

    assert issued.challenge_strikes == 1
    assert issued.last_issue_reason == "验证码挑战页"
    assert issued.last_issue_at


def test_mark_success_clears_strikes_only_for_unwarned_account(tmp_path):
    manager = _make_manager(tmp_path)
    account = manager.prepare_account("demo")
    _write_state(manager.accounts_dir, account.account_id)

    manager.note_issue(account.account_id, "HTTP 403")
    cleared = manager.mark_success(account.account_id)
    assert cleared.challenge_strikes == 0
    assert cleared.last_issue_reason == ""

    manager.note_issue(account.account_id, "HTTP 403")
    manager.mark_warning(account.account_id, "连续强信号")
    kept = manager.mark_success(account.account_id)
    assert kept.challenge_strikes == 1


def test_list_accounts_skips_relogin_tmp_state_files(tmp_path):
    manager = _make_manager(tmp_path)
    _write_state(manager.accounts_dir, "ghost.tmp")
    _write_state(manager.accounts_dir, "real")

    account_ids = {account.account_id for account in manager.list_accounts()}

    assert "ghost.tmp" not in account_ids
    assert "real" in account_ids


def test_corrupt_metadata_is_preserved_and_not_overwritten(tmp_path):
    manager = _make_manager(tmp_path)
    account = manager.prepare_account("demo")
    _write_state(manager.accounts_dir, account.account_id)
    metadata_path = manager._metadata_path(account.account_id)
    metadata_path.write_text("{not valid json", encoding="utf-8")

    # 读取路径保持弹性：仍能看到账号
    assert manager.get_account(account.account_id) is not None
    assert "demo" in {account.account_id for account in manager.list_accounts()}

    # 写路径必须失败并保留现场，不能用默认 metadata 覆盖损坏文件
    with pytest.raises(RuntimeError, match="损坏"):
        manager.note_issue(account.account_id, "HTTP 403")
    assert metadata_path.read_text(encoding="utf-8") == "{not valid json"

    # 重新登录是全量覆盖，允许重建
    relogged = manager.record_login(account.account_id)
    assert relogged.account_id == account.account_id
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["account_id"] == account.account_id


def test_consume_attempt_signal_reasonless_challenge_is_not_stuck(tmp_path):
    coordinator = RequestCoordinator()

    coordinator.note_challenge(
        base_cooldown_seconds=30.0,
        max_cooldown_seconds=300.0,
        monotonic_fn=lambda: 10.0,
        reason="",
    )
    assert coordinator.consume_attempt_signal() is False
    assert coordinator.consume_attempt_signal_reasons() == ()

    coordinator.note_challenge(
        base_cooldown_seconds=30.0,
        max_cooldown_seconds=300.0,
        monotonic_fn=lambda: 20.0,
        reason="HTTP 403",
    )
    assert coordinator.consume_attempt_signal() is True
    assert coordinator.consume_attempt_signal() is False


def _make_job_record() -> JobRecord:
    return JobRecord(
        job_id="job-1",
        original_filename="demo.csv",
        upload_path=Path("/tmp/demo.csv"),
        columns=["链接"],
        preview_rows=[],
        row_count=1,
    )


def test_cooldown_payload_hidden_when_inactive_even_with_stale_ends_at():
    job = _make_job_record()
    job.cooldown_ends_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    job.cooldown_active = False

    payload = job.to_dict()

    assert payload["cooldown_ends_at"] is None
    assert payload["cooldown_remaining_seconds"] == 0

    job.cooldown_active = True
    payload = job.to_dict()
    assert payload["cooldown_ends_at"] is not None
    assert payload["cooldown_remaining_seconds"] > 0


def test_batch_cooldown_payload_hidden_when_inactive():
    job = _make_job_record()
    batch = BatchRecord(
        batch_id="batch-1",
        job_ids=[job.job_id],
        common_columns=["链接"],
        cooldown_ends_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )

    payload = batch.to_dict([job])

    assert payload["cooldown_ends_at"] is None
    assert payload["cooldown_remaining_seconds"] == 0


def test_advance_cookie_generation_bumps_job_and_batch_atomically():
    manager = JobManager()
    job = manager.create_session(
        original_filename="demo.csv",
        upload_path=Path("/tmp/demo.csv"),
        columns=["链接"],
        preview_rows=[],
        row_count=1,
    )

    assert manager.advance_cookie_generation(job.job_id) == 1
    assert manager.advance_cookie_generation(job.job_id) == 2
    assert manager.get_effective_cookie_runtime(job.job_id)["cookie_service_generation"] == 2

    batch = manager.create_batch([job], common_columns=["链接"])
    assert manager.advance_cookie_generation(job.job_id) == 1
    assert manager.get_batch_cookie_runtime(batch.batch_id)["cookie_service_generation"] == 1
    assert manager.get_effective_cookie_runtime(job.job_id)["cookie_service_generation"] == 1

    thread_count = 8
    threads = [
        threading.Thread(target=manager.advance_cookie_generation, args=(job.job_id,))
        for _ in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert manager.get_batch_cookie_runtime(batch.batch_id)["cookie_service_generation"] == 1 + thread_count


def test_delete_account_removes_files_and_unlists_account(tmp_path):
    from app.cookie_accounts import CookieAccountManager

    manager = CookieAccountManager(accounts_dir=tmp_path)
    account = manager.prepare_account("要删除")
    (tmp_path / f"{account.account_id}.json").write_text('{"cookies": [{"name": "a"}]}', encoding="utf-8")
    assert manager.get_account(account.account_id) is not None

    assert manager.delete_account(account.account_id) is True
    assert manager.get_account(account.account_id) is None
    assert not (tmp_path / f"{account.account_id}.meta.json").exists()
    assert not (tmp_path / f"{account.account_id}.json").exists()
    assert all(a.account_id != account.account_id for a in manager.list_accounts())


def test_delete_account_returns_false_for_missing(tmp_path):
    from app.cookie_accounts import CookieAccountManager

    manager = CookieAccountManager(accounts_dir=tmp_path)
    assert manager.delete_account("不存在的账号") is False
