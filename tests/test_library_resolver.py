import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from app.library_resolver import IsolatedMetadataResolver


GOOD = 'https://www.douyin.com/video/7600000000000000001'
HANG = 'https://www.douyin.com/video/7600000000000000002'
CRASH = 'https://www.douyin.com/video/7600000000000000003'
WRONG = 'https://www.douyin.com/video/7600000000000000004'


class FakeEngine:
    def __init__(self, storage_state_path):
        self.path = storage_state_path
        self.calls = 0

    def resolve(self, url):
        self.calls += 1
        if url == HANG:
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                                     start_new_session=os.name != 'nt')
            self.path.with_suffix('.child-pid').write_text(str(child.pid))
            time.sleep(120)
        if url == CRASH:
            os._exit(7)
        return {'detail': {'aweme_id': 'wrong' if url == WRONG else url.rsplit('/', 1)[1]},
                'pid': os.getpid(), 'calls': self.calls}

    def close(self):
        pass


class StuckCloseEngine(FakeEngine):
    def close(self):
        time.sleep(120)


class StuckInitEngine(FakeEngine):
    def __init__(self, storage_state_path):
        time.sleep(120)


def running(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_browser_child_reuses_state_but_checks_every_work_id(tmp_path):
    engine = IsolatedMetadataResolver(tmp_path/'state.json', timeout_seconds=5, factory=FakeEngine)
    try:
        first, second = engine.resolve(GOOD), engine.resolve(GOOD)
        assert first['pid'] == second['pid'] != os.getpid()
        assert second['calls'] == 2
        with pytest.raises(RuntimeError, match='ID不符'):
            engine.resolve(WRONG)
    finally:
        engine.close()
    assert not running(first['pid'])


def test_timeout_kills_detached_browser_tree_and_next_work_succeeds(tmp_path):
    state = tmp_path/'state.json'
    state.write_text('{"cookies":[],"origins":[]}')
    media = tmp_path/'视频-7600000000000000999.mp4'
    media.write_bytes(b'complete download awaiting upload')
    marker = tmp_path/'ready.json'
    marker.write_text('retained')
    engine = IsolatedMetadataResolver(state, timeout_seconds=2, factory=FakeEngine)
    try:
        first = engine.resolve(GOOD)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='解析超过2秒'):
            engine.resolve(HANG)
        assert time.monotonic()-started < 9
        descendant = int(state.with_suffix('.child-pid').read_text())
        assert not running(first['pid']) and not running(descendant)
        assert engine.resolve(GOOD)['pid'] != first['pid']
        assert state.read_text() == '{"cookies":[],"origins":[]}'
        assert media.read_bytes() == b'complete download awaiting upload'
        assert marker.read_text() == 'retained'
    finally:
        engine.close()


def test_child_crash_is_recoverable(tmp_path):
    engine = IsolatedMetadataResolver(tmp_path/'state.json', timeout_seconds=5, factory=FakeEngine)
    try:
        with pytest.raises(RuntimeError, match='浏览器异常'):
            engine.resolve(CRASH)
        assert engine.resolve(GOOD)['detail']['aweme_id'] == GOOD.rsplit('/', 1)[1]
    finally:
        engine.close()


def test_deadline_also_covers_stuck_browser_initialization(tmp_path):
    engine = IsolatedMetadataResolver(tmp_path/'state.json', timeout_seconds=1, factory=StuckInitEngine)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='解析超过1秒'):
        engine.resolve(GOOD)
    assert time.monotonic()-started < 7
    assert engine._process is None


def test_shutdown_is_bounded_even_if_browser_close_hangs(tmp_path):
    engine = IsolatedMetadataResolver(tmp_path/'state.json', timeout_seconds=5, factory=StuckCloseEngine)
    pid = engine.resolve(GOOD)['pid']
    started = time.monotonic()
    engine.close()
    assert time.monotonic()-started < 8
    assert not running(pid)
