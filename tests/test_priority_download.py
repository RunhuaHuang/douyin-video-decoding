from pathlib import Path

import download_priority_and_resume as priority


def test_priority_retry_is_bounded_and_keeps_output_and_guest_state(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(priority.bdh, "main", lambda args: calls.append(args.copy()) or 1)
    target = tmp_path / "中文 空格" / "原输出目录"
    state = tmp_path / "guest.json"
    assert priority.download_single_table_multithreaded(
        tmp_path / "榜单.xlsx", target, anonymous_state=state, proxy="none"
    ) == 1
    assert len(calls) == 2  # Remaining failures must never start an endless retry loop.
    assert calls[0] == calls[1]
    args = calls[0]
    assert Path(args[args.index("-o") + 1]) / args[args.index("--output-name") + 1] == target
    assert args[args.index("--anonymous-state") + 1] == str(state)
    assert args[args.index("--proxy") + 1] == "none"


def test_priority_success_does_not_start_retry(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(priority.bdh, "main", lambda args: calls.append(args) or 0)
    assert priority.download_single_table_multithreaded(tmp_path / "榜.xlsx", tmp_path / "out") == 0
    assert len(calls) == 1


def test_priority_test_scope_does_not_expand_into_retry(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(priority.bdh, "main", lambda args: calls.append(args) or 1)
    assert priority.download_single_table_multithreaded(tmp_path / "榜.xlsx", tmp_path / "out", max_test=2) == 1
    assert len(calls) == 1
    assert calls[0][calls[0].index("--test") + 1] == "2"
