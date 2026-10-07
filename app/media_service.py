from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# probe/抽取/合并遇到异常输入都可能挂住不返回，统一给子进程超时，避免占死工作线程
FFMPEG_TIMEOUT_SECONDS = 300


class FfmpegService:
    def __init__(self) -> None:
        self.ffmpeg_path = shutil.which("ffmpeg")
        self.ffprobe_path = shutil.which("ffprobe")

    def is_available(self) -> bool:
        return bool(self.ffmpeg_path and self.ffprobe_path)

    def require(self) -> str:
        if not self.ffmpeg_path:
            raise RuntimeError("未检测到 ffmpeg，请先在系统中安装 ffmpeg")
        return self.ffmpeg_path

    def require_probe(self) -> str:
        if not self.ffprobe_path:
            raise RuntimeError("未检测到 ffprobe，请确认 ffmpeg 安装完整")
        return self.ffprobe_path

    def probe_media(self, video_path: Path) -> dict:
        ffprobe_path = self.require_probe()
        command = [
            ffprobe_path,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-print_format",
            "json",
            str(video_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("媒体探测失败: 探测超时，请检查输入文件") from exc
        if result.returncode != 0:
            message = self._extract_error_message(result.stderr)
            raise RuntimeError(f"媒体探测失败: {message}")

        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("媒体探测失败: 响应不是有效 JSON") from exc

    def extract_audio(
        self,
        video_path: Path,
        output_path: Path,
        sample_rate: int = 16000,
        supplemental_audio_path: Path | None = None,
    ) -> None:
        ffmpeg_path = self.require()
        source_path = video_path
        probe = self.probe_media(source_path)
        streams = probe.get("streams") or []
        if not streams:
            raise RuntimeError("输入文件不是有效视频")

        if not self._has_audio_stream(streams):
            if supplemental_audio_path is not None and supplemental_audio_path.exists():
                source_path = self._merge_supplemental_audio(video_path, supplemental_audio_path)
                probe = self.probe_media(source_path)
                streams = probe.get("streams") or []
            if not self._has_audio_stream(streams):
                raise RuntimeError("视频没有音轨")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            ffmpeg_path,
            "-i",
            str(source_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-f",
            "wav",
            "-y",
            str(output_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("音频提取失败: ffmpeg 执行超时") from exc
        if result.returncode != 0:
            message = self._extract_error_message(result.stderr)
            raise RuntimeError(f"音频提取失败: {message}")

    def _merge_supplemental_audio(self, video_path: Path, supplemental_audio_path: Path) -> Path:
        ffmpeg_path = self.require()
        audio_probe = self.probe_media(supplemental_audio_path)
        audio_streams = [
            stream for stream in (audio_probe.get("streams") or []) if stream.get("codec_type") == "audio"
        ]
        if not audio_streams:
            raise RuntimeError("补充音频不是有效音轨")

        merged_path = video_path.with_name(f"{video_path.stem}_merged.mp4")
        # 同源分离流音视频时长一致；不要加 -shortest：-c:v copy 下它会按关键帧
        # 对齐截断，容易把片尾截掉丢内容
        command = [
            ffmpeg_path,
            "-i",
            str(video_path),
            "-i",
            str(supplemental_audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-y",
            str(merged_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("音视频合并失败: ffmpeg 执行超时") from exc
        if result.returncode != 0:
            message = self._extract_error_message(result.stderr)
            raise RuntimeError(f"音视频合并失败: {message}")
        return merged_path

    @staticmethod
    def _has_audio_stream(streams: list[dict]) -> bool:
        return any(stream.get("codec_type") == "audio" for stream in streams)

    @staticmethod
    def _extract_error_message(stderr: str) -> str:
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        if not lines:
            return "未知 ffmpeg 错误"

        joined = "\n".join(lines)
        if "Output file #0 does not contain any stream" in joined:
            return "视频没有音轨"
        if "Stream map" in joined and "matches no streams" in joined:
            return "视频没有音轨"

        tail = lines[-3:]
        return " | ".join(tail)
