import subprocess
from pathlib import Path

from app.media_service import FfmpegService


class _Result:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def test_is_available_requires_ffmpeg_and_ffprobe():
    service = FfmpegService()
    service.ffmpeg_path = "ffmpeg"
    service.ffprobe_path = None
    assert service.is_available() is False
    service.ffprobe_path = "ffprobe"
    assert service.is_available() is True


def test_extract_audio_merges_supplemental_audio_when_video_has_no_audio(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    audio_sidecar_path = tmp_path / "fallback_audio.m4a"
    output_path = tmp_path / "audio.wav"
    merged_path = tmp_path / "source_merged.mp4"

    video_path.write_bytes(b"video")
    audio_sidecar_path.write_bytes(b"audio")

    service = FfmpegService()
    service.ffmpeg_path = "ffmpeg"
    service.ffprobe_path = "ffprobe"

    def fake_probe_media(path: Path) -> dict:
        if path == video_path:
            return {"streams": [{"codec_type": "video"}]}
        if path == audio_sidecar_path:
            return {"streams": [{"codec_type": "audio"}]}
        if path == merged_path:
            return {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
        raise AssertionError(f"unexpected probe path: {path}")

    commands = []
    timeouts = []

    def fake_run(command, capture_output=True, text=True, timeout=None):
        commands.append(command)
        timeouts.append(timeout)
        output_file = Path(command[-1])
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(b"ok")
        return _Result()

    monkeypatch.setattr(service, "probe_media", fake_probe_media)
    monkeypatch.setattr("app.media_service.subprocess.run", fake_run)

    service.extract_audio(
        video_path,
        output_path,
        supplemental_audio_path=audio_sidecar_path,
    )

    assert merged_path.exists()
    assert output_path.exists()
    assert any(str(audio_sidecar_path) in command for command in commands)
    # 同源分离流不能加 -shortest：-c:v copy 下会按关键帧截断丢片尾
    assert "-shortest" not in [argument for command in commands for argument in command]
    # probe 被 fake 掉，此处真实子进程调用是合并与抽取两处，都必须带超时
    assert len(timeouts) == 2
    assert all(timeout is not None for timeout in timeouts)


def test_extract_audio_raises_when_video_has_no_audio_and_no_sidecar(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    output_path = tmp_path / "audio.wav"
    video_path.write_bytes(b"video")

    service = FfmpegService()
    service.ffmpeg_path = "ffmpeg"
    service.ffprobe_path = "ffprobe"

    monkeypatch.setattr(
        service,
        "probe_media",
        lambda path: {"streams": [{"codec_type": "video"}]},
    )

    try:
        service.extract_audio(video_path, output_path)
    except RuntimeError as exc:
        assert str(exc) == "视频没有音轨"
    else:
        raise AssertionError("expected RuntimeError")


def test_probe_media_timeout_raises_runtime_error(tmp_path, monkeypatch):
    service = FfmpegService()
    service.ffprobe_path = "ffprobe"

    def hanging_run(command, capture_output=True, text=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    monkeypatch.setattr("app.media_service.subprocess.run", hanging_run)

    try:
        service.probe_media(tmp_path / "source.mp4")
    except RuntimeError as exc:
        assert "超时" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on timeout")


def test_extract_audio_timeout_raises_runtime_error(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    output_path = tmp_path / "audio.wav"
    video_path.write_bytes(b"video")

    service = FfmpegService()
    service.ffmpeg_path = "ffmpeg"
    service.ffprobe_path = "ffprobe"

    monkeypatch.setattr(
        service,
        "probe_media",
        lambda path: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
    )

    def hanging_run(command, capture_output=True, text=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    monkeypatch.setattr("app.media_service.subprocess.run", hanging_run)

    try:
        service.extract_audio(video_path, output_path)
    except RuntimeError as exc:
        assert "超时" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on timeout")


def test_merge_supplemental_audio_timeout_raises_runtime_error(tmp_path, monkeypatch):
    video_path = tmp_path / "source.mp4"
    audio_sidecar_path = tmp_path / "fallback_audio.m4a"
    merged_path = tmp_path / "source_merged.mp4"
    video_path.write_bytes(b"video")
    audio_sidecar_path.write_bytes(b"audio")

    service = FfmpegService()
    service.ffmpeg_path = "ffmpeg"
    service.ffprobe_path = "ffprobe"

    def fake_probe_media(path: Path) -> dict:
        if path == video_path:
            return {"streams": [{"codec_type": "video"}]}
        if path == audio_sidecar_path:
            return {"streams": [{"codec_type": "audio"}]}
        raise AssertionError(f"unexpected probe path: {path}")

    def hanging_merge_run(command, capture_output=True, text=True, timeout=None):
        if str(merged_path) in command:
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)
        return _Result()

    monkeypatch.setattr(service, "probe_media", fake_probe_media)
    monkeypatch.setattr("app.media_service.subprocess.run", hanging_merge_run)

    try:
        service._merge_supplemental_audio(video_path, audio_sidecar_path)
    except RuntimeError as exc:
        assert "超时" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on merge timeout")
