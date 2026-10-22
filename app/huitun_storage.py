"""Publish completed files and manifests without reusing interrupted SMB handles."""
import errno
import json
import shutil
import time
import uuid
from pathlib import Path


TRANSIENT_ERRORS = {
    errno.EBUSY, errno.ETIMEDOUT, errno.EAGAIN, errno.EIO,
    errno.ENOTCONN, getattr(errno, "ESTALE", errno.EIO),
}


def retry_storage(operation, *, extra_errnos=()):
    for attempt in range(3):
        try:
            return operation()
        except OSError as exc:
            if exc.errno not in TRANSIENT_ERRORS | set(extra_errnos) or attempt == 2:
                raise
            time.sleep(1 + attempt)


def _publish(target: Path, write):
    def attempt():
        # A new sibling on every attempt avoids reopening a stale SMB file handle.
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            with temporary.open("xb") as stream:
                write(stream)
            temporary.replace(target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A failed cleanup must not hide the write/rename error, or turn
                # an already published file into a failed download.
                pass
    # macOS SMB can return EINVAL for a stale temporary-file handle even when
    # its path is valid. Retry publication with a fresh sibling; do not treat
    # invalid manifest reads as empty or retry arbitrary invalid operations.
    retry_storage(attempt, extra_errnos=(errno.EINVAL,))


def publish_local_file(source: Path, target: Path):
    def write(stream):
        with source.open("rb") as completed:
            shutil.copyfileobj(completed, stream, length=1024 * 1024)
    _publish(target, write)


def read_manifest(path: Path):
    try:
        raw = retry_storage(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    # Unreadable or malformed state must never be replaced with an empty map.
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid manifest object: {path}")
    return data


def write_manifest(path: Path, data):
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    _publish(path, lambda stream: stream.write(raw))
