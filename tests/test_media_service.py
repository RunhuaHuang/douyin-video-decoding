from pathlib import Path

from app.media_service import FfmpegService


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

    class Result:
        def __init__(self, returncode: int = 0, stderr: str = "") -> None:
            self.returncode = returncode
            self.stderr = stderr

    def fake_run(command, capture_output=True, text=True):
        commands.append(command)
        output_file = Path(command[-1])
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(b"ok")
        return Result()

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
    assert any(str(merged_path) in command for command in commands)


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
