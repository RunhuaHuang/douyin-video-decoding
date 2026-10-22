import errno
import json
from pathlib import Path

import httpx
import pytest

import batch_download_huitun as batch
from app import huitun_storage as storage


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)


@pytest.mark.parametrize("storage_error", [errno.EBUSY, errno.EINVAL])
def test_busy_copy_and_cleanup_retry_without_redownloading(tmp_path, monkeypatch, storage_error):
    target = tmp_path / "视频.mp4"
    payload = b"video" * 10000
    requests = []
    worker = batch.TableWorker(1, tmp_path, tmp_path / "state.json")
    worker.http_client.close()
    worker.http_client = httpx.Client(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(200, content=payload)))
    copy = storage.shutil.copyfileobj
    unlink = Path.unlink
    attempts = []

    def busy_once(source, output, **kwargs):
        assert Path(source.name).parent != target.parent
        attempts.append(Path(output.name))
        if len(attempts) == 1:
            output.write(b"partial")
            raise OSError(storage_error, "SMB handle unavailable")
        return copy(source, output, **kwargs)

    def cleanup_busy(path, *args, **kwargs):
        if attempts and path == attempts[0]:
            raise OSError(errno.EBUSY, "cleanup busy too")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(storage.shutil, "copyfileobj", busy_once)
    monkeypatch.setattr(Path, "unlink", cleanup_busy)
    try:
        assert worker.download_video_stream(["https://cdn.example/video.mp4"], target)
        assert target.read_bytes() == payload
        assert len(requests) == 1
        assert len(attempts) == 2 and attempts[0] != attempts[1]
        assert attempts[0].read_bytes() == b"partial"
    finally:
        worker.close()


def test_rename_retry_keeps_existing_file_until_publish(tmp_path, monkeypatch):
    source, target = tmp_path / "local.mp4", tmp_path / "final.mp4"
    source.write_bytes(b"new complete video")
    target.write_bytes(b"previous complete video")
    replace = Path.replace
    attempts = []

    def busy_once(path, destination):
        attempts.append(path)
        assert target.read_bytes() == b"previous complete video"
        if len(attempts) == 1:
            raise OSError(errno.EBUSY, "rename busy")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", busy_once)
    storage.publish_local_file(source, target)
    assert target.read_bytes() == b"new complete video"
    assert len(attempts) == 2


def test_cleanup_error_after_success_does_not_fail_publish(tmp_path, monkeypatch):
    target = tmp_path / "manifest.json"
    unlink = Path.unlink

    def busy(path, *args, **kwargs):
        if path.name.endswith(".part"):
            raise OSError(errno.EBUSY, "stale cleanup handle")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", busy)
    storage.write_manifest(target, {"123": {"status": "success"}})
    assert storage.read_manifest(target) == {"123": {"status": "success"}}


@pytest.mark.parametrize("error,expected_attempts", [(errno.EBUSY, 3), (errno.EINVAL, 3), (errno.ENOSPC, 1)])
def test_failed_publish_preserves_previous_manifest(tmp_path, monkeypatch, error, expected_attempts):
    target = tmp_path / "manifest.json"
    original = '{"kept": {"status": "success"}}'
    target.write_text(original)
    attempts = []

    def fail(path, destination):
        attempts.append(path)
        raise OSError(error, "cannot publish")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError) as exc:
        storage.write_manifest(target, {"new": {"status": "failed"}})
    assert exc.value.errno == error
    assert len(attempts) == expected_attempts
    assert target.read_text() == original


def test_manifest_read_retries_timeout_but_never_swallows_it(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text('{"kept": {"status": "success"}}')
    read = Path.read_text
    attempts = []

    def flaky(path, **kwargs):
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError(errno.ETIMEDOUT, "SMB timeout")
        return read(path, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert storage.read_manifest(path) == {"kept": {"status": "success"}}

    def unavailable(path, **kwargs):
        raise OSError(errno.ETIMEDOUT, "SMB unavailable")

    monkeypatch.setattr(Path, "read_text", unavailable)
    with pytest.raises(OSError):
        storage.read_manifest(path)


def test_corrupt_manifest_is_not_treated_as_empty(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"partial":')
    with pytest.raises(json.JSONDecodeError):
        storage.read_manifest(path)
    assert path.read_text() == '{"partial":'


def test_manifest_invalid_temporary_handle_uses_a_new_file(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    original = {"old": {"status": "success"}}
    path.write_text(json.dumps(original))
    open_file = Path.open
    failed_paths = []

    def invalid_first_temporary(path, mode="r", *args, **kwargs):
        if mode == "xb":
            assert json.loads(open_file(tmp_path / "manifest.json").read()) == original
            failed_paths.append(path)
            if len(failed_paths) == 1:
                raise OSError(errno.EINVAL, "invalid SMB handle")
        return open_file(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", invalid_first_temporary)
    updated = {**original, "new": {"status": "success"}}
    storage.write_manifest(path, updated)
    assert storage.read_manifest(path) == updated
    assert len(failed_paths) == 2 and failed_paths[0] != failed_paths[1]
