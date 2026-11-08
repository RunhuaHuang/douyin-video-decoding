"""Keep a stuck Playwright call from holding a library work slot forever."""
import multiprocessing
import queue
import re
import threading
import time
from pathlib import Path

import psutil

from .library_downloader import MetadataDownloader


def _serve(connection, state_path, factory):
    engine = None
    try:
        engine = factory(storage_state_path=Path(state_path))
        connection.send({'ready': True})
        while True:
            url = connection.recv()
            if url is None:
                break
            try:
                connection.send({'plan': engine.resolve(url)})
            except Exception as exc:
                connection.send({'error': str(exc)[:3000]})
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        if engine is not None:
            engine.close()
        connection.close()


def _read_replies(connection, replies):
    # recv() can itself wait on a partial frame. Keep it off the deadline owner.
    try:
        while True:
            replies.put(connection.recv())
    except (EOFError, OSError):
        replies.put({'closed': True})


class IsolatedMetadataResolver:
    """Persistent browser child with one wall-clock deadline per resolution.

    Only metadata crosses the pipe. Media transfers and lease heartbeats stay
    in the parent and are never killed when the browser child is recycled.
    """
    def __init__(self, storage_state_path, *, timeout_seconds=90, factory=MetadataDownloader):
        self.state_path = Path(storage_state_path)
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError('解析超时必须大于0')
        self.factory = factory
        self._process = self._connection = self._reader = self._replies = self._root = None

    def _receive(self, deadline):
        try:
            reply = self._replies.get(timeout=max(0, deadline-time.monotonic()))
        except queue.Empty:
            raise TimeoutError from None
        if not isinstance(reply, dict) or reply.get('closed'):
            raise RuntimeError('浏览器异常：解析子进程已退出')
        return reply

    def _start(self, deadline):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        self._connection = parent
        self._replies = queue.Queue()
        self._process = context.Process(target=_serve, args=(child, str(self.state_path), self.factory),
                                        name='library-browser', daemon=True)
        try:
            self._process.start()
            self._root = psutil.Process(self._process.pid)
        finally:
            child.close()
        self._reader = threading.Thread(target=_read_replies, args=(parent, self._replies), daemon=True)
        self._reader.start()
        if self._receive(deadline) != {'ready': True}:
            raise RuntimeError('浏览器异常：解析子进程启动失败')

    def resolve(self, url):
        match = re.search(r'(?:video|note)/(\d+)', url)
        if not match:
            raise RuntimeError('无法识别作品ID')
        deadline = time.monotonic()+self.timeout_seconds
        try:
            if self._process is None:
                self._start(deadline)
            self._connection.send(url)
            reply = self._receive(deadline)
            if 'error' in reply:
                raise RuntimeError(reply['error'])
            plan = reply.get('plan')
            detail = plan.get('detail') if isinstance(plan, dict) else None
            if not isinstance(detail, dict) or str(detail.get('aweme_id')) != match[1]:
                raise RuntimeError('响应作品ID不符，拒绝保存')
            return plan
        except TimeoutError:
            self.close(force=True)
            raise RuntimeError(f'浏览器异常：解析超过{self.timeout_seconds:g}秒，已回收解析进程') from None
        except (EOFError, BrokenPipeError, OSError):
            self.close(force=True)
            raise RuntimeError('浏览器异常：解析子进程通信中断') from None
        except RuntimeError as exc:
            if '浏览器异常' in str(exc):
                self.close(force=True)
            raise

    def close(self, *, force=False):
        process, connection, reader = self._process, self._connection, self._reader
        if process is None:
            return
        descendants = []
        root = self._root
        try:
            if root is not None:
                try:
                    descendants = root.children(recursive=True)
                except psutil.NoSuchProcess:
                    pass
            if not force and process.is_alive():
                try:
                    connection.send(None)
                except (OSError, EOFError):
                    pass
                process.join(timeout=2)
            # Include Chromium's detached process groups, not just the Python child.
            if root is not None:
                try:
                    descendants += root.children(recursive=True)
                except psutil.NoSuchProcess:
                    pass
                for target in reversed(descendants+[root]):
                    try:
                        target.kill()
                    except psutil.NoSuchProcess:
                        pass
            if process.pid is not None:
                process.join(timeout=2)
            if descendants:
                psutil.wait_procs(descendants, timeout=2)
        finally:
            connection.close()
            if reader is not None:
                reader.join(timeout=1)
            if process.pid is not None and not process.is_alive():
                process.close()
            self._process = self._connection = self._reader = self._replies = self._root = None
