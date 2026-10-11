"""会话文件的跨进程写入与删除协调（Windows / macOS / Linux）。"""

import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import portalocker


def deleted_marker_path(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.name}.deleted")


@contextmanager
def account_storage_lock(state_path: Path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # 锁文件不能随账号删除，否则旧进程与新进程可能锁住不同 inode。
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a") as lock_file:
        portalocker.lock(lock_file, portalocker.LOCK_EX)
        try:
            yield
        finally:
            portalocker.unlock(lock_file)


def write_storage_state(state_path: Path, payload: str) -> bool:
    """原子保存会话；删除标记永久阻止旧浏览器重新创建该账号。"""
    with account_storage_lock(state_path):
        if deleted_marker_path(state_path).exists():
            return False
        temp_path = state_path.with_name(f"{state_path.name}.{uuid4().hex}.tmp")
        try:
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, state_path)
        finally:
            temp_path.unlink(missing_ok=True)
    return True
