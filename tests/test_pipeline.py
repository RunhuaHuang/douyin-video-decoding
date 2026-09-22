from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pandas as pd
import pytest

from app.csv_service import inspect_csv
from app.cookie_accounts import CookieAccountManager
from app.job_manager import JobManager
from app.pipeline import (
    DOWNLOAD_ONLY_MODEL,
    JobProcessor,
    PreparedRow,
    RowOutcome,
    RowPlan,
    RowProcessingError,
    RowTask,
    RunControlState,
)
from app.transcription_service import TranscriptionService


class FakeVideo:
    def __init__(self, title: str, download_url: str) -> None:
        self.title = title
        self.download_urls = (download_url,)
        self.share_url = "https://v.douyin.com/example/"
        self.page_url = "https://www.douyin.com/video/1234567890"
        self.resolved_by = "测试替身"
        self.is_deleted = False

    @property
    def download_url(self) -> str:
        return self.download_urls[0]


class FakeDownloadResult:
    def __init__(
        self,
        source_url: str,
        strategy: str = "测试直连",
        attempts: int = 1,
        audio_source_url: str = "",
        audio_attempts: int = 0,
    ) -> None:
        self.source_url = source_url
        self.strategy = strategy
        self.attempts = attempts
        self.audio_source_url = audio_source_url
        self.audio_attempts = audio_attempts


class FakeDouyinService:
    def parse_share_text(self, share_text: str) -> FakeVideo:
        return FakeVideo(title=f"title:{share_text[-4:]}", download_url="https://example.com/video.mp4")

    def download_video(
        self,
        video: FakeVideo,
        output_path: Path,
        audio_output_path: Path | None = None,
    ) -> FakeDownloadResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-video")
        return FakeDownloadResult(source_url=video.download_url)

    def close(self) -> None:
        return None


class StrongSignalDouyinService(FakeDouyinService):
    def __init__(self) -> None:
        self._challenge_reasons = []

    def parse_share_text(self, share_text: str) -> FakeVideo:
        self._challenge_reasons = ["HTTP 403"]
        return super().parse_share_text(share_text)

    def consume_attempt_challenge_signal_reasons(self) -> tuple[str, ...]:
        reasons = tuple(self._challenge_reasons)
        self._challenge_reasons = []
        return reasons


class AlwaysRetryableParseFailureDouyinService(FakeDouyinService):
    def parse_share_text(self, share_text: str) -> FakeVideo:
        raise httpx.ReadTimeout("timed out")


class FakeFfmpegService:
    def __init__(self) -> None:
        self.last_supplemental_audio_path: Path | None = None

    def is_available(self) -> bool:
        return True

    def extract_audio(
        self,
        video_path: Path,
        output_path: Path,
        sample_rate: int = 16000,
        supplemental_audio_path: Path | None = None,
    ) -> None:
        self.last_supplemental_audio_path = supplemental_audio_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-audio")


class FakeTranscriptionService:
    def __init__(self, default_model: str) -> None:
        self.default_model = default_model

    @staticmethod
    def model_alias(model_name: str) -> str:
        return model_name.rsplit("/", 1)[-1].replace("-", "_")

    def transcribe(self, audio_path: Path, model_name: str = "", language: str = "") -> str:
        if "bad-model" in (model_name or self.default_model):
            raise RuntimeError("boom")
        return f"transcript:{audio_path.stem}:{model_name or self.default_model}"


def _write_cookie_state(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "ttwid",
                        "value": value,
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


def _build_cookie_account_manager(tmp_path: Path, labels: list[str], *, invalidation_threshold: int = 3) -> CookieAccountManager:
    manager = CookieAccountManager(
        accounts_dir=tmp_path / "browser" / "accounts",
        legacy_storage_state_path=tmp_path / "browser" / "legacy.json",
        switch_batch_threshold=30,
        invalidation_strike_threshold=invalidation_threshold,
    )
    for index, label in enumerate(labels, start=1):
        account = manager.prepare_account(label)
        _write_cookie_state(account.storage_state_path, f"cookie-{index}")
        manager.record_login(account.account_id, label=account.label)
    return manager


class FlakyDouyinService(FakeDouyinService):
    def __init__(self) -> None:
        self.attempts = 0

    def parse_share_text(self, share_text: str) -> FakeVideo:
        self.attempts += 1
        if self.attempts < 3:
            raise httpx.ReadTimeout("timed out")
        return super().parse_share_text(share_text)


class AlwaysFailDouyinService(FakeDouyinService):
    def download_video(
        self,
        video: FakeVideo,
        output_path: Path,
        audio_output_path: Path | None = None,
    ) -> FakeDownloadResult:
        raise RuntimeError("403 Forbidden")


class DownloadForbiddenTwiceDouyinService(FakeDouyinService):
    def download_video(
        self,
        video: FakeVideo,
        output_path: Path,
        audio_output_path: Path | None = None,
    ) -> FakeDownloadResult:
        raise RuntimeError("HTTP 403 Forbidden x2, 浏览器兜底失败: 浏览器兜底下载失败: HTTP 403 x2")


class Download404NonVideoDouyinService(FakeDouyinService):
    def download_video(
        self,
        video: FakeVideo,
        output_path: Path,
        audio_output_path: Path | None = None,
    ) -> FakeDownloadResult:
        raise RuntimeError("返回内容不是有效视频, HTTP 404 Not Found, 浏览器兜底失败: 浏览器兜底下载失败: 返回内容不是有效媒体, HTTP 404")


class BrokenDouyinService:
    def __init__(self) -> None:
        raise ImportError("missing optional dependency")


class DeletedVideoDouyinService(FakeDouyinService):
    def parse_share_text(self, share_text: str) -> FakeVideo:
        video = super().parse_share_text(share_text)
        video.title = "在抖音记录美好生活20260308 - 抖音"
        video.is_deleted = True
        video.download_urls = ()
        return video


class NoAudioFfmpegService(FakeFfmpegService):
    def extract_audio(
        self,
        video_path: Path,
        output_path: Path,
        sample_rate: int = 16000,
        supplemental_audio_path: Path | None = None,
    ) -> None:
        raise RuntimeError("视频没有音轨")


class BrowserSidecarDouyinService(FakeDouyinService):
    def download_video(
        self,
        video: FakeVideo,
        output_path: Path,
        audio_output_path: Path | None = None,
    ) -> FakeDownloadResult:
        result = super().download_video(video, output_path, audio_output_path)
        if audio_output_path is not None:
            audio_output_path.parent.mkdir(parents=True, exist_ok=True)
            audio_output_path.write_bytes(b"fallback-audio")
            result.audio_source_url = "https://browser.example.com/audio.m4a"
        return result


class PreviewOnlyDeleteHeuristicDouyinService(FakeDouyinService):
    def parse_share_text(self, share_text: str) -> FakeVideo:
        raise RuntimeError(
            "抖音解析失败: 分享页: 分享页中未找到 _ROUTER_DATA | "
            "iteminfo API: Expecting value: line 1 column 1 (char 0) | "
            "detail API: Expecting value: line 1 column 1 (char 0) | "
            "浏览器兜底: 浏览器兜底命中通用页，仅捕获到预览片段"
        )


class InvalidLinkDeleteHeuristicDouyinService(FakeDouyinService):
    def parse_share_text(self, share_text: str) -> FakeVideo:
        raise RuntimeError("未在单元格中找到有效的抖音链接")


class NonVideoNoteDouyinService(FakeDouyinService):
    def parse_share_text(self, share_text: str) -> FakeVideo:
        raise RuntimeError("链接类型为图文 note，非视频内容")


class RecoverAfterAntiCrawlDouyinService(FakeDouyinService):
    def __init__(self) -> None:
        self.attempts_by_url = {}

    def parse_share_text(self, share_text: str) -> FakeVideo:
        attempts = self.attempts_by_url.get(share_text, 0) + 1
        self.attempts_by_url[share_text] = attempts
        if attempts == 1:
            video = FakeVideo(title="在抖音记录美好生活20260310 - 抖音", download_url="")
            video.resolved_by = "浏览器兜底"
            video.is_deleted = True
            video.download_urls = ()
            return video
        return super().parse_share_text(share_text)


class RecoverAfterMixedAntiCrawlParseFailureDouyinService(FakeDouyinService):
    def __init__(self) -> None:
        self.attempts_by_url = {}

    def parse_share_text(self, share_text: str) -> FakeVideo:
        attempts = self.attempts_by_url.get(share_text, 0) + 1
        self.attempts_by_url[share_text] = attempts
        if share_text.endswith("/1") or share_text.endswith("/3"):
            if attempts <= 2:
                raise RuntimeError(
                    "抖音解析失败: 分享页: 分享页中未找到 _ROUTER_DATA | "
                    "iteminfo API: Expecting value: line 1 column 1 (char 0) | "
                    "detail API: Expecting value: line 1 column 1 (char 0) | "
                    "浏览器兜底: 浏览器兜底没有捕获到真实视频地址"
                )
            return super().parse_share_text(share_text)
        if attempts == 1:
            raise httpx.ReadTimeout("timed out")
        return super().parse_share_text(share_text)


class VideoDetailMissingDeletedDouyinService(FakeDouyinService):
    def parse_share_text(self, share_text: str) -> FakeVideo:
        raise RuntimeError(
            "抖音解析失败: 分享页: 分享页中未找到视频详情 | "
            "iteminfo API: Expecting value: line 1 column 1 (char 0) | "
            "detail API: Expecting value: line 1 column 1 (char 0) | "
            "浏览器兜底: 浏览器兜底没有捕获到真实视频地址"
        )


class PersistentDeletedAfterCooldownDouyinService(FakeDouyinService):
    def __init__(self) -> None:
        self.attempts_by_url = {}

    def parse_share_text(self, share_text: str) -> FakeVideo:
        attempts = self.attempts_by_url.get(share_text, 0) + 1
        self.attempts_by_url[share_text] = attempts
        if attempts <= 2:
            video = FakeVideo(title="在抖音记录美好生活20260310 - 抖音", download_url="")
            video.resolved_by = "浏览器兜底"
            video.is_deleted = True
            video.download_urls = ()
            return video
        return super().parse_share_text(share_text)


class ChallengeDeletedAfterCooldownDouyinService(FakeDouyinService):
    def __init__(self) -> None:
        self.attempts_by_url = {}
        self.challenge_seen = False

    def reset_attempt_signals(self) -> None:
        self.challenge_seen = False

    def consume_attempt_challenge_signal(self) -> bool:
        seen = self.challenge_seen
        self.challenge_seen = False
        return seen

    def parse_share_text(self, share_text: str) -> FakeVideo:
        attempts = self.attempts_by_url.get(share_text, 0) + 1
        self.attempts_by_url[share_text] = attempts
        if attempts <= 2:
            self.challenge_seen = True
            video = FakeVideo(title="在抖音记录美好生活20260310 - 抖音", download_url="")
            video.resolved_by = "浏览器兜底"
            video.is_deleted = True
            video.download_urls = ()
            return video
        return super().parse_share_text(share_text)


def test_pipeline_writes_transcripts_to_same_row(tmp_path, monkeypatch):
    csv_path = tmp_path / "input.csv"
    dataframe = pd.DataFrame(
        [
            {"url": "https://v.douyin.com/AAA111/", "name": "a"},
            {"url": "", "name": "b"},
            {"url": "https://v.douyin.com/BBB222/", "name": "c"},
        ]
    )
    dataframe.to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename="input.csv",
        upload_path=csv_path,
        columns=["url", "name"],
        preview_rows=[],
        row_count=3,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    assert job.output_path is not None

    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert result.loc[0, "视频文本"].startswith("transcript:audio:mlx-community/test-model")
    assert result.loc[1, "视频文本"] == ""
    assert result.loc[2, "视频文本"].startswith("transcript:audio:mlx-community/test-model")
    assert result.loc[0, "处理状态"] == "成功"
    assert result.loc[1, "处理状态"] == "跳过"
    assert result.loc[2, "处理状态"] == "成功"


def test_inspect_csv_supports_single_column_utf8_file(tmp_path):
    csv_path = tmp_path / "single_column.csv"
    csv_path.write_text("链接\r\nhttps://www.douyin.com/video/7407774468921183522\r\n", encoding="utf-8")

    inspection = inspect_csv(csv_path)

    assert inspection.columns == ["链接"]
    assert inspection.row_count == 1
    assert inspection.preview_rows[0]["链接"] == "https://www.douyin.com/video/7407774468921183522"


def test_pipeline_writes_multiple_model_columns(tmp_path, monkeypatch):
    csv_path = tmp_path / "compare.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename="compare.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model-a\nmlx-community/test-model-b",
        model_names=["mlx-community/test-model-a", "mlx-community/test-model-b"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=1,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id)

    result = pd.read_csv(
        manager.require(session.job_id).output_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    assert "视频文本__test_model_a" in result.columns
    assert "视频文本__test_model_b" in result.columns


def test_continue_existing_prefers_processed_output_file(tmp_path, monkeypatch):
    upload_path = tmp_path / "input.csv"
    processed_path = tmp_path / "input.processed.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(upload_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(
        [{"url": "https://v.douyin.com/AAA111/", "视频文本": "已有文本", "处理状态": "成功", "失败原因": "", "尝试次数": "1"}]
    ).to_csv(processed_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename="input.csv",
        upload_path=upload_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
        resume_output_path=processed_path,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id, mode="continue_existing")

    result = pd.read_csv(manager.require(session.job_id).output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert result.loc[0, "视频文本"] == "已有文本"
    assert result.loc[0, "处理状态"] == "成功"


def test_pipeline_keeps_partial_results_when_one_model_fails(tmp_path, monkeypatch):
    csv_path = tmp_path / "partial.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename="partial.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/good-model\nmlx-community/bad-model",
        model_names=["mlx-community/good-model", "mlx-community/bad-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=1,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert job.status == "completed"
    assert job.succeeded_rows == 1
    assert job.failed_rows == 0
    assert result.loc[0, "处理状态"] == "部分成功"
    assert "转写失败" in result.loc[0, "失败原因"]
    assert result.loc[0, "尝试次数"] == "1"
    assert result.loc[0, "视频文本__good_model"].startswith("transcript:audio:mlx-community/good-model")
    assert result.loc[0, "视频文本__bad_model"] == ""


def test_transcription_service_aliases_sensevoice_model():
    assert (
        TranscriptionService.model_alias("FunAudioLLM/SenseVoiceSmall")
        == "sensevoice_small"
    )


def test_transcription_service_cleans_up_sensevoice_runtime_after_success(tmp_path, monkeypatch):
    service = TranscriptionService()
    cleanups = []

    monkeypatch.setattr(service, "_get_sensevoice_model", lambda model_name: ("model", "mps"))
    monkeypatch.setattr(service, "_generate_sensevoice_text", lambda model, audio_path, language: "ok")
    monkeypatch.setattr(service, "_cleanup_sensevoice_runtime", lambda device: cleanups.append(device))

    text = service._transcribe_sensevoice(tmp_path / "audio.wav", "FunAudioLLM/SenseVoiceSmall", "zh")

    assert text == "ok"
    assert cleanups == ["mps"]


def test_transcription_service_cleans_up_mps_and_cpu_on_fallback(tmp_path, monkeypatch):
    service = TranscriptionService()
    cleanups = []
    seen_devices = []

    def fake_get_model(model_name: str):
        if not seen_devices:
            seen_devices.append("mps")
            return "mps-model", "mps"
        return "cpu-model", "cpu"

    def fake_generate(model, audio_path, language):
        if model == "mps-model":
            raise RuntimeError("mps failed")
        return "cpu-ok"

    monkeypatch.setattr(service, "_get_sensevoice_model", fake_get_model)
    monkeypatch.setattr(service, "_generate_sensevoice_text", fake_generate)
    monkeypatch.setattr(service, "_cleanup_sensevoice_runtime", lambda device: cleanups.append(device))

    text = service._transcribe_sensevoice(tmp_path / "audio.wav", "FunAudioLLM/SenseVoiceSmall", "zh")

    assert text == "cpu-ok"
    assert cleanups == ["mps", "cpu"]


def test_pipeline_retries_and_then_succeeds(tmp_path, monkeypatch):
    csv_path = tmp_path / "retry_success.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="retry_success.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FlakyDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=3,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )
    processor.process(session.job_id)

    result = pd.read_csv(
        manager.require(session.job_id).output_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    assert sleeps == [5]
    assert result.loc[0, "处理状态"] == "成功"
    assert result.loc[0, "尝试次数"] == "3"
    assert result.loc[0, "失败原因"] == ""


def test_pipeline_records_final_failure_reason(tmp_path, monkeypatch):
    csv_path = tmp_path / "retry_fail.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="retry_fail.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=AlwaysFailDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=2,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert job.status == "completed"
    assert job.failed_rows == 1
    assert sleeps == [5]
    assert result.loc[0, "处理状态"] == "失败"
    assert result.loc[0, "尝试次数"] == "3"
    assert result.loc[0, "失败原因"] == "下载失败: 403 Forbidden"


def test_pipeline_keeps_preview_only_parse_failures_retryable_until_max_attempts(tmp_path, monkeypatch):
    csv_path = tmp_path / "preview_only.csv"
    pd.DataFrame([{"url": "https://www.douyin.com/video/7337254060967447834"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="preview_only.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=PreviewOnlyDeleteHeuristicDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=3,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )
    processor.process(session.job_id)

    result = pd.read_csv(
        manager.require(session.job_id).output_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )

    assert sleeps == [5]
    assert result.loc[0, "处理状态"] == "失败"
    assert result.loc[0, "视频文本"] == ""
    assert result.loc[0, "尝试次数"] == "3"
    assert "浏览器兜底命中通用页，仅捕获到预览片段" in result.loc[0, "失败原因"]


def test_pipeline_keeps_browser_deleted_results_terminal_without_cooldown(tmp_path, monkeypatch):
    csv_path = tmp_path / "anti_crawl.csv"
    pd.DataFrame(
        [
            {"url": "https://www.douyin.com/video/1"},
            {"url": "https://www.douyin.com/video/2"},
            {"url": "https://www.douyin.com/video/3"},
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="anti_crawl.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=3,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=3,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=RecoverAfterAntiCrawlDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=lambda seconds: sleeps.append(seconds),
        retry_delay_seconds=0,
        batch_size=3,
        anti_crawl_streak_threshold=3,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert job.status == "completed"
    assert sleeps == []
    assert all(result["处理状态"] == "已删除")
    assert all(result["视频文本"] == "已删除")
    assert all(result["尝试次数"] == "1")
    assert not any("自动冷却" in line for line in job.logs)
    assert not any("冷却信号：" in line for line in job.logs)
    assert not any("恢复为“待重试”" in line for line in job.logs)


def test_record_row_control_signal_keeps_suspicion_until_explicit_success():
    processor = JobProcessor(JobManager())
    run_state = RunControlState(
        anti_crawl_streak_threshold=3,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor._record_row_control_signal(
        run_state=run_state,
        task=RowTask(row_index=0, position=1, csv_row_number=2, url_value="u1"),
        outcome=RowOutcome(
            status="失败",
            attempts=2,
            anti_crawl_suspected=True,
            anti_crawl_signal_descriptions=("弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",),
        ),
    )
    assert [task.csv_row_number for task in run_state.suspicious_tasks] == [2]

    processor._record_row_control_signal(
        run_state=run_state,
        task=RowTask(row_index=1, position=2, csv_row_number=3, url_value="u2"),
        outcome=RowOutcome(status="失败", attempts=1, error_message="解析失败: 读取超时"),
    )
    assert [task.csv_row_number for task in run_state.suspicious_tasks] == [2]

    processor._record_row_control_signal(
        run_state=run_state,
        task=RowTask(row_index=2, position=3, csv_row_number=4, url_value="u3"),
        outcome=RowOutcome(status="成功", attempts=1),
    )
    assert run_state.suspicious_tasks == []


def test_record_row_control_signal_ignores_post_threshold_weak_suspicion():
    processor = JobProcessor(JobManager())
    run_state = RunControlState(
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor._record_row_control_signal(
        run_state=run_state,
        task=RowTask(row_index=0, position=1, csv_row_number=2, url_value="u1"),
        outcome=RowOutcome(
            status="失败",
            attempts=3,
            anti_crawl_suspected=True,
            anti_crawl_signal_descriptions=("弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",),
        ),
    )

    assert run_state.suspicious_tasks == []
    assert run_state.pending_cooldown is None


def test_record_row_control_signal_ignores_retry_stage_weak_suspicion():
    processor = JobProcessor(JobManager())
    run_state = RunControlState(
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor._record_row_control_signal(
        run_state=run_state,
        task=RowTask(row_index=0, position=1, csv_row_number=2, url_value="u1"),
        outcome=RowOutcome(
            status="已删除",
            attempts=2,
            anti_crawl_suspected=True,
            anti_crawl_signal_descriptions=("弱冷却信号：浏览器兜底删除页标题（在抖音记录美好生活）",),
        ),
    )

    assert run_state.suspicious_tasks == []
    assert run_state.pending_cooldown is None


def test_record_row_control_signal_ignores_deleted_strong_suspicion():
    processor = JobProcessor(JobManager())
    run_state = RunControlState(
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor._record_row_control_signal(
        run_state=run_state,
        task=RowTask(row_index=0, position=1, csv_row_number=2, url_value="u1"),
        outcome=RowOutcome(
            status="已删除",
            attempts=2,
            anti_crawl_suspected=True,
            anti_crawl_strong_suspected=True,
            anti_crawl_signal_descriptions=("强冷却信号：HTTP 403",),
        ),
    )

    assert run_state.suspicious_tasks == []
    assert run_state.pending_cooldown is None


def test_record_row_control_signal_ignores_non_deleted_strong_suspicion():
    processor = JobProcessor(JobManager())
    run_state = RunControlState(
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor._record_row_control_signal(
        run_state=run_state,
        task=RowTask(row_index=0, position=1, csv_row_number=2, url_value="u1"),
        outcome=RowOutcome(
            status="失败",
            attempts=2,
            anti_crawl_strong_suspected=True,
            anti_crawl_signal_descriptions=("强冷却信号：验证码挑战页",),
        ),
    )

    assert run_state.suspicious_tasks == []
    assert run_state.pending_cooldown is None


def test_record_row_control_signal_trusted_delete_resets_anti_crawl_tracking():
    processor = JobProcessor(JobManager())
    run_state = RunControlState(
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    run_state.suspicious_tasks = [RowTask(row_index=0, position=1, csv_row_number=2, url_value="u1")]
    run_state.suspicious_signal_descriptions = ["弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常"]
    run_state.pending_cooldown = object()  # type: ignore[assignment]

    processor._record_row_control_signal(
        run_state=run_state,
        task=RowTask(row_index=1, position=2, csv_row_number=3, url_value="u2"),
        outcome=RowOutcome(
            status="已删除",
            attempts=1,
            resets_anti_crawl_state=True,
        ),
    )

    assert run_state.suspicious_tasks == []
    assert run_state.suspicious_signal_descriptions == []
    assert run_state.pending_cooldown is None


def test_pipeline_cools_down_after_mixed_suspected_parse_failures(tmp_path, monkeypatch):
    csv_path = tmp_path / "mixed_anti_crawl.csv"
    pd.DataFrame(
        [
            {"url": "https://www.douyin.com/video/1"},
            {"url": "https://www.douyin.com/video/2"},
            {"url": "https://www.douyin.com/video/3"},
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="mixed_anti_crawl.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=3,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=2,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=RecoverAfterMixedAntiCrawlParseFailureDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=lambda seconds: sleeps.append(seconds),
        retry_delay_seconds=0,
        batch_size=3,
        anti_crawl_streak_threshold=2,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert job.status == "completed"
    assert sum(sleeps) == 60
    assert all(result["处理状态"] == "成功")
    assert any("自动冷却" in line for line in job.logs)
    assert any("已加入重试队列" in line and "弱冷却信号：" in line for line in job.logs)


def test_pipeline_marks_deleted_after_repeated_invalid_link_failures(tmp_path, monkeypatch):
    csv_path = tmp_path / "invalid_link.csv"
    pd.DataFrame([{"url": "not-a-douyin-link"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="invalid_link.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=InvalidLinkDeleteHeuristicDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=3,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )
    processor.process(session.job_id)

    result = pd.read_csv(
        manager.require(session.job_id).output_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )

    assert sleeps == []
    assert result.loc[0, "处理状态"] == "已删除"
    assert result.loc[0, "视频文本"] == "已删除"


def test_pipeline_marks_deleted_when_share_page_video_detail_missing(tmp_path, monkeypatch):
    csv_path = tmp_path / "video_detail_missing.csv"
    pd.DataFrame([{"url": "https://www.douyin.com/video/1"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="video_detail_missing.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=VideoDetailMissingDeletedDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=lambda seconds: sleeps.append(seconds),
        retry_delay_seconds=0,
        batch_size=1,
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert job.status == "completed"
    assert sleeps == []
    assert result.loc[0, "处理状态"] == "已删除"
    assert result.loc[0, "视频文本"] == "已删除"
    assert result.loc[0, "尝试次数"] == "1"
    assert not any("自动冷却" in line for line in job.logs)
    assert not any("弱冷却信号：" in line for line in job.logs)


def test_pipeline_does_not_cool_down_for_retry_stage_suspected_deletes(tmp_path, monkeypatch):
    csv_path = tmp_path / "persistent_deleted.csv"
    pd.DataFrame([{"url": "https://www.douyin.com/video/1"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="persistent_deleted.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=PersistentDeletedAfterCooldownDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=lambda seconds: sleeps.append(seconds),
        retry_delay_seconds=0,
        batch_size=1,
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert job.status == "completed"
    assert sleeps == []
    assert result.loc[0, "处理状态"] == "已删除"
    assert result.loc[0, "视频文本"] == "已删除"
    assert result.loc[0, "尝试次数"] == "1"
    assert not any("自动冷却" in line for line in job.logs)
    assert not any("冷却信号：" in line for line in job.logs)
    assert not any("恢复为“待重试”" in line for line in job.logs)


def test_weak_signal_descriptions_match_preview_only_router_data_failure():
    processor = JobProcessor(JobManager())
    exc = RowProcessingError(
        "解析",
        "抖音解析失败: 分享页: 分享页中未找到 _ROUTER_DATA | "
        "iteminfo API: Expecting value: line 1 column 1 (char 0) | "
        "detail API: Expecting value: line 1 column 1 (char 0) | "
        "浏览器兜底: 浏览器兜底命中通用页，仅捕获到预览片段",
    )

    assert processor._weak_signal_descriptions_for_error(exc) == (
        "弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",
    )


def test_weak_signal_descriptions_match_real_video_address_failure():
    processor = JobProcessor(JobManager())
    exc = RowProcessingError(
        "解析",
        "抖音解析失败: 分享页: 分享页中未找到 _ROUTER_DATA | "
        "iteminfo API: Expecting value: line 1 column 1 (char 0) | "
        "detail API: Expecting value: line 1 column 1 (char 0) | "
        "浏览器兜底: 浏览器兜底没有捕获到真实视频地址",
    )

    assert processor._weak_signal_descriptions_for_error(exc) == (
        "弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",
    )


def test_weak_signal_descriptions_match_preview_only_without_router_data_failure():
    processor = JobProcessor(JobManager())
    exc = RowProcessingError(
        "解析",
        "抖音解析失败: 浏览器兜底: 浏览器兜底命中通用页，仅捕获到预览片段",
    )

    assert processor._weak_signal_descriptions_for_error(exc) == (
        "弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",
    )


def test_weak_signal_descriptions_match_real_video_address_without_router_data_failure():
    processor = JobProcessor(JobManager())
    exc = RowProcessingError(
        "解析",
        "抖音解析失败: 浏览器兜底: 浏览器兜底没有捕获到真实视频地址",
    )

    assert processor._weak_signal_descriptions_for_error(exc) == (
        "弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",
    )


def test_weak_signal_descriptions_match_router_data_failure_without_browser_suffix():
    processor = JobProcessor(JobManager())
    exc = RowProcessingError(
        "解析",
        "抖音解析失败: 分享页: 分享页中未找到 _ROUTER_DATA | "
        "iteminfo API: Expecting value: line 1 column 1 (char 0) | "
        "detail API: Expecting value: line 1 column 1 (char 0)",
    )

    assert processor._weak_signal_descriptions_for_error(exc) == (
        "弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",
    )


def test_weak_signal_descriptions_match_network_failures():
    processor = JobProcessor(JobManager())

    dns_exc = RowProcessingError(
        "解析",
        "短链解析失败: [Errno 8] nodename nor servname provided, or not known | "
        "浏览器兜底: Page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.douyin.com/video/1",
    )
    ssl_exc = RowProcessingError(
        "解析",
        "短链解析失败: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate (_ssl.c:1028) | "
        "浏览器兜底: Page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.douyin.com/video/2",
    )
    refused_exc = RowProcessingError(
        "解析",
        "短链解析失败: [Errno 61] Connection refused | "
        "浏览器兜底: 浏览器兜底没有捕获到真实视频地址",
    )
    active_refused_exc = RowProcessingError(
        "解析",
        "短链解析失败: No connection could be made because the target machine actively refused it | "
        "浏览器兜底: 浏览器兜底没有捕获到真实视频地址",
    )

    assert processor._weak_signal_descriptions_for_error(dns_exc) == (
        "弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",
    )
    assert processor._weak_signal_descriptions_for_error(ssl_exc) == (
        "弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",
    )
    assert processor._weak_signal_descriptions_for_error(refused_exc) == (
        "弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",
    )
    assert processor._weak_signal_descriptions_for_error(active_refused_exc) == (
        "弱冷却信号：分享页/接口异常、浏览器兜底异常或网络异常",
    )


def test_video_detail_missing_is_deleted_before_anti_crawl_signal():
    processor = JobProcessor(JobManager())
    exc = RowProcessingError(
        "解析",
        "抖音解析失败: 分享页: 分享页中未找到视频详情 | "
        "iteminfo API: Expecting value: line 1 column 1 (char 0) | "
        "detail API: Expecting value: line 1 column 1 (char 0) | "
        "浏览器兜底: 浏览器兜底命中通用页，仅捕获到预览片段",
    )

    assert processor._deleted_reason_for_error(exc, 1) == "分享页未找到视频详情"
    assert processor._weak_signal_descriptions_for_error(exc) == ()


def test_pipeline_logs_but_does_not_cool_down_for_challenge_signals(tmp_path, monkeypatch):
    csv_path = tmp_path / "challenge_deleted.csv"
    pd.DataFrame([{"url": "https://www.douyin.com/video/1"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="challenge_deleted.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=ChallengeDeletedAfterCooldownDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=lambda seconds: sleeps.append(seconds),
        retry_delay_seconds=0,
        batch_size=1,
        anti_crawl_streak_threshold=1,
        anti_crawl_cooldown_seconds=60,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert job.status == "completed"
    assert sleeps == []
    assert result.loc[0, "处理状态"] == "已删除"
    assert result.loc[0, "视频文本"] == "已删除"
    assert result.loc[0, "尝试次数"] == "1"
    assert not any("自动冷却" in line for line in job.logs)
    assert not any("冷却信号：" in line for line in job.logs)


def test_pipeline_proactively_cools_down_after_healthy_batch_streak(tmp_path, monkeypatch):
    csv_path = tmp_path / "healthy.csv"
    pd.DataFrame(
        [
            {"url": "https://www.douyin.com/video/1"},
            {"url": "https://www.douyin.com/video/2"},
            {"url": "https://www.douyin.com/video/3"},
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="healthy.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=3,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=2,
        proactive_cooldown_seconds=30,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=lambda seconds: sleeps.append(seconds),
        retry_delay_seconds=0,
        batch_size=1,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=2,
        proactive_cooldown_seconds=30,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert job.status == "completed"
    assert sum(sleeps) == 30
    assert all(result["处理状态"] == "成功")
    assert any("主动冷却" in line for line in job.logs)


def test_pipeline_allows_manual_skip_during_cooldown(tmp_path, monkeypatch):
    csv_path = tmp_path / "skip_cooldown.csv"
    pd.DataFrame(
        [
            {"url": "https://www.douyin.com/video/1"},
            {"url": "https://www.douyin.com/video/2"},
            {"url": "https://www.douyin.com/video/3"},
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="skip_cooldown.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=3,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=3,
        anti_crawl_cooldown_seconds=180,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    def sleep_and_skip(seconds: int) -> None:
        sleeps.append(seconds)
        manager.request_skip_cooldown(session.job_id)

    processor = JobProcessor(
        manager,
        douyin_service_factory=RecoverAfterAntiCrawlDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=sleep_and_skip,
        retry_delay_seconds=0,
        batch_size=3,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    assert sleeps == []
    assert not any("已手动跳过当前冷却" in line for line in job.logs)


def test_pipeline_marks_job_failed_when_service_init_raises(tmp_path, monkeypatch):
    csv_path = tmp_path / "broken_init.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename="broken_init.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=BrokenDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "failed"
    assert "missing optional dependency" in job.error_message


def test_pipeline_marks_deleted_rows_without_retry(tmp_path, monkeypatch):
    csv_path = tmp_path / "deleted.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename="deleted.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=DeletedVideoDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
    )
    processor.process(session.job_id)

    result = pd.read_csv(
        manager.require(session.job_id).output_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    assert result.loc[0, "处理状态"] == "已删除"
    assert result.loc[0, "视频文本"] == "已删除"
    assert result.loc[0, "尝试次数"] == "1"


def test_pipeline_marks_note_rows_as_non_video_without_retry(tmp_path, monkeypatch):
    csv_path = tmp_path / "note.csv"
    pd.DataFrame([{"url": "https://www.douyin.com/note/7368733740719295779"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename="note.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=NonVideoNoteDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
    )
    processor.process(session.job_id)

    result = pd.read_csv(
        manager.require(session.job_id).output_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )

    assert result.loc[0, "处理状态"] == "非视频"
    assert result.loc[0, "视频文本"] == ""
    assert result.loc[0, "尝试次数"] == "1"
    assert result.loc[0, "失败原因"] == "链接类型为图文 note，非视频内容"


def test_pipeline_marks_download_forbidden_rows_unavailable_after_second_attempt(tmp_path, monkeypatch):
    csv_path = tmp_path / "download_forbidden.csv"
    pd.DataFrame([{"url": "https://www.douyin.com/video/7416287864591256844"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename="download_forbidden.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=DownloadForbiddenTwiceDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        retry_delay_seconds=0,
    )
    processor.process(session.job_id)

    result = pd.read_csv(
        manager.require(session.job_id).output_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )

    assert result.loc[0, "处理状态"] == "无法下载"
    assert result.loc[0, "视频文本"] == ""
    assert result.loc[0, "尝试次数"] == "2"
    assert "HTTP 403 Forbidden x2" in result.loc[0, "失败原因"]


def test_pipeline_marks_download_404_rows_non_video_after_second_attempt(tmp_path, monkeypatch):
    csv_path = tmp_path / "download_404.csv"
    pd.DataFrame([{"url": "https://www.douyin.com/video/7387762150300814630"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename="download_404.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=Download404NonVideoDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        retry_delay_seconds=0,
    )
    processor.process(session.job_id)

    result = pd.read_csv(
        manager.require(session.job_id).output_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )

    assert result.loc[0, "处理状态"] == "非视频"
    assert result.loc[0, "视频文本"] == ""
    assert result.loc[0, "尝试次数"] == "2"
    assert "返回内容不是有效视频" in result.loc[0, "失败原因"]


def test_pipeline_retries_audio_extraction_no_audio_errors_twice(tmp_path, monkeypatch):
    csv_path = tmp_path / "no_audio.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    sleeps = []
    manager = JobManager()
    session = manager.create_session(
        original_filename="no_audio.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=NoAudioFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=5,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )
    processor.process(session.job_id)

    result = pd.read_csv(
        manager.require(session.job_id).output_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    assert sleeps == [5]
    assert result.loc[0, "处理状态"] == "失败"
    assert result.loc[0, "尝试次数"] == "3"
    assert "视频没有音轨" in result.loc[0, "失败原因"]


def test_pipeline_passes_browser_audio_sidecar_to_ffmpeg(tmp_path, monkeypatch):
    csv_path = tmp_path / "sidecar.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    ffmpeg_service = FakeFfmpegService()
    manager = JobManager()
    session = manager.create_session(
        original_filename="sidecar.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=BrowserSidecarDouyinService,
        ffmpeg_service_factory=lambda: ffmpeg_service,
        transcription_service_factory=FakeTranscriptionService,
    )
    processor.process(session.job_id)

    assert ffmpeg_service.last_supplemental_audio_path is not None
    assert ffmpeg_service.last_supplemental_audio_path.name == "fallback_audio.m4a"


def test_pipeline_rotates_cookie_accounts_after_threshold(tmp_path, monkeypatch):
    csv_path = tmp_path / "rotate.csv"
    pd.DataFrame(
        [
            {"url": "https://www.douyin.com/video/1"},
            {"url": "https://www.douyin.com/video/2"},
            {"url": "https://www.douyin.com/video/3"},
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    cookie_manager = _build_cookie_account_manager(tmp_path, ["主号", "小号A"])
    manager = JobManager()
    session = manager.create_session(
        original_filename="rotate.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=3,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
        cookie_account_manager=cookie_manager,
        cookie_switch_batch_threshold=2,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    assert job.current_cookie_label == "小号A"
    assert job.cookie_total_batches_processed == 3
    assert any("当前 cookies 已切换到 `主号`" in line for line in job.logs)
    assert any("当前 cookies 已切换到 `小号A`" in line for line in job.logs)


def test_pipeline_warns_cookie_account_after_strong_signal(tmp_path, monkeypatch):
    csv_path = tmp_path / "warn.csv"
    pd.DataFrame([{"url": "https://www.douyin.com/video/1"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    cookie_manager = _build_cookie_account_manager(
        tmp_path,
        ["主号", "小号A"],
        invalidation_threshold=1,
    )
    manager = JobManager()
    session = manager.create_session(
        original_filename="warn.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=StrongSignalDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
        cookie_account_manager=cookie_manager,
        cookie_strong_signal_switch_threshold=1,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    warned = cookie_manager.get_account("主号")

    assert job.status == "completed"
    assert warned is not None
    assert warned.warning is True
    assert warned.invalid is False
    assert job.current_cookie_label == "主号"
    assert any("cookies `主号` 已进入预警状态" in line for line in job.logs)


def test_pipeline_retry_phase_uses_retry_strong_signal_threshold(tmp_path, monkeypatch):
    cookie_manager = _build_cookie_account_manager(tmp_path, ["主号", "小号A"])
    manager = JobManager()
    session = manager.create_session(
        original_filename="retry-strong.csv",
        upload_path=tmp_path / "retry-strong.csv",
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=StrongSignalDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
        cookie_account_manager=cookie_manager,
        cookie_strong_signal_switch_threshold=1,
        cookie_retry_strong_signal_switch_threshold=4,
    )
    first_account = cookie_manager.select_current_or_first_available_account()
    assert first_account is not None
    processor._apply_cookie_account(session.job_id, first_account, reason="test setup")

    run_state = RunControlState(
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    for _ in range(3):
        run_state.current_batch_strong_issue = True
        run_state.current_batch_strong_signal_descriptions = ["强冷却信号：HTTP 403"]
        processor._finalize_cookie_rotation(
            job_id=session.job_id,
            run_state=run_state,
            phase="重试",
            batch_index=1,
            total_batches=1,
    )

    job = manager.require(session.job_id)
    assert job.current_cookie_label == "主号"

    run_state.current_batch_strong_issue = True
    run_state.current_batch_strong_signal_descriptions = ["强冷却信号：HTTP 403"]
    processor._finalize_cookie_rotation(
        job_id=session.job_id,
        run_state=run_state,
        phase="重试",
        batch_index=1,
        total_batches=1,
    )

    job = manager.require(session.job_id)
    warned = cookie_manager.get_account("主号")
    assert job.current_cookie_label == "主号"
    assert warned is not None
    assert warned.warning is False
    assert warned.challenge_strikes == 1


def test_pipeline_with_single_cookie_account_keeps_running_on_strong_signal(tmp_path, monkeypatch):
    csv_path = tmp_path / "single-account.csv"
    pd.DataFrame(
        [
            {"url": "https://www.douyin.com/video/1"},
            {"url": "https://www.douyin.com/video/2"},
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    cookie_manager = _build_cookie_account_manager(tmp_path, ["主号"])
    manager = JobManager()
    session = manager.create_session(
        original_filename="single-account.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=2,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=StrongSignalDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
        cookie_account_manager=cookie_manager,
        cookie_strong_signal_switch_threshold=1,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)

    assert job.status == "completed"
    assert job.current_cookie_label == "主号"
    warned = cookie_manager.get_account("主号")
    assert warned is not None
    assert warned.warning is False
    assert warned.challenge_strikes == 2


def test_pipeline_uses_warned_accounts_when_all_accounts_are_warned(tmp_path, monkeypatch):
    csv_path = tmp_path / "all-warned.csv"
    pd.DataFrame(
        [
            {"url": "https://www.douyin.com/video/1"},
            {"url": "https://www.douyin.com/video/2"},
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    cookie_manager = _build_cookie_account_manager(
        tmp_path,
        ["主号", "小号A"],
        invalidation_threshold=1,
    )
    cookie_manager.mark_warning("主号", "测试预警")
    cookie_manager.mark_warning("小号A", "测试预警")

    manager = JobManager()
    session = manager.create_session(
        original_filename="all-warned.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=2,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
        cookie_account_manager=cookie_manager,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    assert job.current_cookie_label in {"主号", "小号A"}


def test_pipeline_retry_rounds_switch_cookie_and_stop_after_three_rounds(tmp_path, monkeypatch):
    csv_path = tmp_path / "retry-limit.csv"
    pd.DataFrame([{"url": "https://www.douyin.com/video/1"}]).to_csv(
        csv_path,
        index=False,
        encoding="utf-8-sig",
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    cookie_manager = _build_cookie_account_manager(tmp_path, ["主号", "小号A"])
    manager = JobManager()
    session = manager.create_session(
        original_filename="retry-limit.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=AlwaysRetryableParseFailureDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=1,
        batch_delay_seconds=0,
        retry_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
        cookie_account_manager=cookie_manager,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert job.status == "completed"
    assert result.loc[0, "处理状态"] == "已删除"
    assert result.loc[0, "尝试次数"] == "4"
    assert result.loc[0, "视频文本"] == "已删除"
    assert any("开始第 1 轮重试，切换 cookies" in line for line in job.logs)
    assert any("开始第 2 轮重试，切换 cookies" in line for line in job.logs)
    assert any("开始第 3 轮重试，切换 cookies" in line for line in job.logs)


class UnavailableFfmpegService(FakeFfmpegService):
    def is_available(self) -> bool:
        return False


def _explode_transcription_factory(default_model):
    raise AssertionError("仅下载模式不应初始化转写服务")


def test_pipeline_download_only_saves_videos_without_transcription(tmp_path, monkeypatch):
    csv_path = tmp_path / "videos.csv"
    pd.DataFrame(
        [
            {"url": "https://v.douyin.com/AAA111/", "name": "a"},
            {"url": "", "name": "b"},
            {"url": "https://v.douyin.com/BBB222/", "name": "c"},
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(pipeline_module, "VIDEO_OUTPUT_DIR", tmp_path / "videos_out")

    manager = JobManager()
    session = manager.create_session(
        original_filename="videos.csv",
        upload_path=csv_path,
        columns=["url", "name"],
        preview_rows=[],
        row_count=3,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="",
        model_names_text="",
        model_names=[],
        overwrite=False,
        download_only=True,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=_explode_transcription_factory,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    assert any("仅下载模式" in line for line in job.logs)

    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert "视频文本" not in result.columns
    assert result.loc[0, "处理状态"] == "成功"
    assert result.loc[1, "处理状态"] == "跳过"
    assert result.loc[2, "处理状态"] == "成功"
    assert result.loc[0, "尝试次数"] == "1"

    video_dir = tmp_path / "videos_out" / "videos"
    saved_files = sorted(video_dir.glob("row*.mp4"))
    assert len(saved_files) == 2
    assert saved_files[0].name.startswith("row0002_")
    assert saved_files[1].name.startswith("row0004_")
    assert saved_files[0].read_bytes() == b"fake-video"


def test_pipeline_download_only_runs_without_ffmpeg(tmp_path, monkeypatch):
    csv_path = tmp_path / "no-ffmpeg.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path, index=False, encoding="utf-8-sig"
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(pipeline_module, "VIDEO_OUTPUT_DIR", tmp_path / "videos_out")

    manager = JobManager()
    session = manager.create_session(
        original_filename="no-ffmpeg.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="",
        model_names_text="",
        model_names=[],
        overwrite=False,
        download_only=True,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=UnavailableFfmpegService,
        transcription_service_factory=_explode_transcription_factory,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    assert list((tmp_path / "videos_out" / "no-ffmpeg").glob("row*.mp4"))


def test_pipeline_download_only_saves_to_custom_video_dir(tmp_path, monkeypatch):
    csv_path = tmp_path / "custom-dir.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path, index=False, encoding="utf-8-sig"
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(pipeline_module, "VIDEO_OUTPUT_DIR", tmp_path / "videos_out")

    custom_dir = tmp_path / "nas-like" / "target"
    manager = JobManager()
    session = manager.create_session(
        original_filename="custom-dir.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="",
        model_names_text="",
        model_names=[],
        overwrite=False,
        download_only=True,
        video_output_dir=str(custom_dir),
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=_explode_transcription_factory,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    assert any(str(custom_dir) in line for line in job.logs)

    saved = list((custom_dir / "custom-dir").glob("row*.mp4"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"fake-video"
    assert not (tmp_path / "videos_out").exists()


def test_pipeline_download_only_fails_fast_when_video_dir_unwritable(tmp_path, monkeypatch):
    csv_path = tmp_path / "bad-dir.csv"
    pd.DataFrame([{"url": "https://v.douyin.com/AAA111/"}]).to_csv(
        csv_path, index=False, encoding="utf-8-sig"
    )

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(pipeline_module, "VIDEO_OUTPUT_DIR", tmp_path / "videos_out")

    # 用一个已存在的文件路径冒充目录，mkdir 必然失败
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")

    manager = JobManager()
    session = manager.create_session(
        original_filename="bad-dir.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="",
        model_names_text="",
        model_names=[],
        overwrite=False,
        download_only=True,
        video_output_dir=str(blocker),
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=_explode_transcription_factory,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "failed"
    assert any("视频保存目录不可用" in line for line in job.logs)


def _download_only_job_fixture(tmp_path: Path, *, video_bytes: bytes, audio_bytes: bytes | None):
    from types import SimpleNamespace

    from app.pipeline import JobProcessor

    row_dir = tmp_path / "row_00001"
    row_dir.mkdir(parents=True)
    video_path = row_dir / "source.mp4"
    video_path.write_bytes(video_bytes)
    audio_path = row_dir / "fallback_audio.m4a"
    if audio_bytes is not None:
        audio_path.write_bytes(audio_bytes)

    job = SimpleNamespace(original_filename="links.csv")
    task = SimpleNamespace(csv_row_number=2)
    video = SimpleNamespace(title="demo", video_id="7411111111111111111")
    return job, task, video, video_path, audio_path


def test_save_download_only_prefers_audio_when_probe_fails(tmp_path, monkeypatch):
    from app import pipeline as pipeline_module

    class _BrokenProbeFfmpeg:
        def probe_media(self, path):
            raise RuntimeError("ffprobe 不可用")

    job, task, video, video_path, audio_path = _download_only_job_fixture(
        tmp_path, video_bytes=b"video-only", audio_bytes=b"audio"
    )
    monkeypatch.setattr(pipeline_module, "FfmpegService", _BrokenProbeFfmpeg)

    saved = JobProcessor._save_download_only_video(
        job, task, video, video_path, audio_path, tmp_path / "videos"
    )

    assert saved.suffix == ".m4a"
    assert saved.read_bytes() == b"audio"


def test_save_download_only_prefers_audio_when_video_has_no_frames(tmp_path, monkeypatch):
    from app import pipeline as pipeline_module

    class _AudioOnlyProbeFfmpeg:
        def probe_media(self, path):
            return {"streams": [{"codec_type": "audio"}]}

    job, task, video, video_path, audio_path = _download_only_job_fixture(
        tmp_path, video_bytes=b"audio-in-mp4", audio_bytes=b"audio"
    )
    monkeypatch.setattr(pipeline_module, "FfmpegService", _AudioOnlyProbeFfmpeg)

    saved = JobProcessor._save_download_only_video(
        job, task, video, video_path, audio_path, tmp_path / "videos"
    )

    assert saved.suffix == ".m4a"


def test_save_download_only_prefers_audio_when_video_has_no_audio_stream(tmp_path, monkeypatch):
    from app import pipeline as pipeline_module

    class _VideoOnlyProbeFfmpeg:
        def probe_media(self, path):
            return {"streams": [{"codec_type": "video"}]}

    job, task, video, video_path, audio_path = _download_only_job_fixture(
        tmp_path, video_bytes=b"video-only", audio_bytes=b"audio"
    )
    monkeypatch.setattr(pipeline_module, "FfmpegService", _VideoOnlyProbeFfmpeg)

    saved = JobProcessor._save_download_only_video(
        job, task, video, video_path, audio_path, tmp_path / "videos"
    )

    assert saved.suffix == ".m4a"
    assert saved.read_bytes() == b"audio"


def test_save_download_only_keeps_merged_video(tmp_path, monkeypatch):
    from app import pipeline as pipeline_module

    class _MuxedProbeFfmpeg:
        def probe_media(self, path):
            return {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}

    job, task, video, video_path, audio_path = _download_only_job_fixture(
        tmp_path, video_bytes=b"merged", audio_bytes=b"audio"
    )
    monkeypatch.setattr(pipeline_module, "FfmpegService", _MuxedProbeFfmpeg)

    saved = JobProcessor._save_download_only_video(
        job, task, video, video_path, audio_path, tmp_path / "videos"
    )

    assert saved.suffix == ".mp4"
    assert saved.read_bytes() == b"merged"


class DeletedOnlyForMarkedUrlDouyinService(FakeDouyinService):
    def parse_share_text(self, share_text: str) -> FakeVideo:
        if "deleted" in share_text:
            raise RuntimeError(
                "抖音解析失败: 分享页: 分享页中未找到视频详情 | "
                "iteminfo API: Expecting value: line 1 column 1 (char 0) | "
                "detail API: Expecting value: line 1 column 1 (char 0) | "
                "浏览器兜底: 浏览器兜底没有捕获到真实视频地址"
            )
        return super().parse_share_text(share_text)


def test_pipeline_download_only_marks_deleted_row_without_crashing(tmp_path, monkeypatch):
    csv_path = tmp_path / "download_only_deleted.csv"
    pd.DataFrame(
        [
            {"url": "https://www.douyin.com/video/deleted1"},
            {"url": "https://v.douyin.com/GOOD11/"},
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    from app import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(pipeline_module, "VIDEO_OUTPUT_DIR", tmp_path / "videos_out")

    manager = JobManager()
    session = manager.create_session(
        original_filename="download_only_deleted.csv",
        upload_path=csv_path,
        columns=["url"],
        preview_rows=[],
        row_count=2,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="",
        model_names_text="",
        model_names=[],
        overwrite=False,
        download_only=True,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=DeletedOnlyForMarkedUrlDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=_explode_transcription_factory,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"

    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert "视频文本" not in result.columns
    assert result.loc[0, "处理状态"] == "已删除"
    assert result.loc[1, "处理状态"] == "成功"
    assert not any("KeyError" in line for line in job.logs)

    saved = list((tmp_path / "videos_out" / "download_only_deleted").glob("row*.mp4"))
    assert len(saved) == 1
    assert saved[0].name.startswith("row0003_")


def test_run_download_stage_excludes_download_only_sentinel_from_deleted_transcripts(tmp_path):
    manager = JobManager()
    session = manager.create_session(
        original_filename="links.csv",
        upload_path=tmp_path / "links.csv",
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="",
        model_names_text="",
        model_names=[],
        overwrite=False,
        download_only=True,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=VideoDetailMissingDeletedDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=_explode_transcription_factory,
    )

    task = RowTask(
        row_index=0,
        position=0,
        csv_row_number=2,
        url_value="https://www.douyin.com/video/deleted1",
    )
    plan = RowPlan(task=task, attempts_before=0, pending_models=(DOWNLOAD_ONLY_MODEL,))

    result = processor._run_download_stage(session.job_id, plan, tmp_path / "workspace")

    assert result.outcome.status == "已删除"
    assert result.transcripts == {}
    assert DOWNLOAD_ONLY_MODEL not in result.transcripts


# ---------- D 路修复回归测试：行链路与状态机 ----------


def _make_job(tmp_path, monkeypatch, filename: str, rows: list[dict], **configure_kwargs):
    from app import pipeline as pipeline_module

    csv_path = tmp_path / filename
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    monkeypatch.setattr(pipeline_module, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pipeline_module, "OUTPUT_DIR", tmp_path / "outputs")

    manager = JobManager()
    session = manager.create_session(
        original_filename=filename,
        upload_path=csv_path,
        columns=list(rows[0].keys()),
        preview_rows=[],
        row_count=len(rows),
    )
    manager.configure_job(session.job_id, **configure_kwargs)
    return manager, session


def test_pipeline_unexpected_row_error_fails_after_retries(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "unexpected.csv",
        [{"url": "https://v.douyin.com/AAA111/"}],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=1,
        retry_delay_seconds=0,
        sleep_fn=lambda seconds: None,
    )

    def broken_parse(douyin, url_value):
        raise OSError("磁盘已满")

    monkeypatch.setattr(processor, "_parse_video", broken_parse)
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert result.loc[0, "处理状态"] == "失败"
    assert result.loc[0, "尝试次数"] == "2"
    assert "磁盘已满" in result.loc[0, "失败原因"]
    assert any("未预期错误" in line for line in job.logs)


def test_run_download_stage_catches_unexpected_errors(tmp_path, monkeypatch):
    manager = JobManager()
    session = manager.create_session(
        original_filename="links.csv",
        upload_path=tmp_path / "links.csv",
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
    )

    def broken_parse(douyin, url_value):
        raise ValueError("corrupted state")

    monkeypatch.setattr(processor, "_parse_video", broken_parse)

    task = RowTask(row_index=0, position=0, csv_row_number=2, url_value="https://v.douyin.com/AAA111/")
    plan = RowPlan(task=task, attempts_before=0, pending_models=("mlx-community/test-model",))

    result = processor._run_download_stage(session.job_id, plan, tmp_path / "workspace")

    assert result.outcome.status == "待重试"
    assert result.outcome.retryable is True
    assert "corrupted state" in result.outcome.error_message


def test_run_transcription_stage_catches_unexpected_errors(tmp_path, monkeypatch):
    manager = JobManager()
    session = manager.create_session(
        original_filename="links.csv",
        upload_path=tmp_path / "links.csv",
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
    )

    def broken_extract(ffmpeg, video_path, audio_path, browser_audio_path):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(processor, "_extract_audio", broken_extract)

    row_dir = tmp_path / "row_00001"
    row_dir.mkdir()
    (row_dir / "source.mp4").write_bytes(b"video")
    plan = RowPlan(
        task=RowTask(row_index=0, position=0, csv_row_number=2, url_value="https://v.douyin.com/AAA111/"),
        attempts_before=0,
        pending_models=("mlx-community/test-model",),
    )
    prepared = PreparedRow(
        plan=plan,
        row_dir=row_dir,
        video_path=row_dir / "source.mp4",
        browser_audio_path=row_dir / "fallback_audio.m4a",
        audio_path=row_dir / "audio.wav",
    )

    result = processor._run_transcription_stage(prepared, {"mlx-community/test-model": "视频文本"}, "mlx-community/test-model")

    assert result.outcome.status == "待重试"
    assert result.outcome.retryable is True
    assert "ffmpeg exploded" in result.outcome.error_message


def test_pipeline_parallel_download_worker_crash_keeps_batch_results(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "parallel_crash.csv",
        [
            {"url": "https://v.douyin.com/GOOD01/"},
            {"url": "https://v.douyin.com/CRASH1/"},
            {"url": "https://v.douyin.com/GOOD02/"},
        ],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=2,
        asr_workers=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=10,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    original_run_download_stage = processor._run_download_stage

    def crashing_run_download_stage(job_id, plan, workspace):
        if "CRASH" in plan.task.url_value:
            # 让同批其余行先完成并落盘，模拟 worker 线程未兜底的崩溃
            time.sleep(0.2)
            raise OSError("simulated worker crash")
        return original_run_download_stage(job_id, plan, workspace)

    monkeypatch.setattr(processor, "_run_download_stage", crashing_run_download_stage)
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "failed"
    assert "simulated worker crash" in job.error_message

    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert sorted(result["处理状态"].tolist()) == ["", "成功", "成功"]
    good_rows = result[result["url"].str.contains("GOOD")]
    assert good_rows["视频文本"].str.startswith("transcript:").all()


def test_pipeline_parallel_download_workers_process_all_rows(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "parallel_ok.csv",
        [{"url": f"https://v.douyin.com/ROW{i}/"} for i in range(4)],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=2,
        asr_workers=2,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=2,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert all(result["处理状态"] == "成功")
    assert all(result["视频文本"].str.startswith("transcript:"))


def test_pipeline_parallel_asr_workers_transcribe_multiple_rows(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "parallel_asr.csv",
        [{"url": f"https://v.douyin.com/ROW{i}/"} for i in range(4)],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=2,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=10,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert all(result["处理状态"] == "成功")
    assert all(result["视频文本"].str.startswith("transcript:"))


def test_pipeline_pause_then_resume_completes_remaining_rows(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "pause_resume.csv",
        [
            {"url": "https://v.douyin.com/ROW1/"},
            {"url": "https://v.douyin.com/ROW2/"},
            {"url": "https://v.douyin.com/ROW3/"},
        ],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    sleeps = []

    def sleep_and_pause(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            manager.request_pause(session.job_id)

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=sleep_and_pause,
        retry_delay_seconds=0,
        batch_size=1,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    processor.process(session.job_id)

    paused_job = manager.require(session.job_id)
    assert paused_job.status == "paused"
    assert sleeps == [0]
    paused_result = pd.read_csv(paused_job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert paused_result.loc[0, "处理状态"] == "成功"
    assert paused_result.loc[1, "处理状态"] == ""
    assert paused_result.loc[2, "处理状态"] == ""

    manager.clear_pause(session.job_id)
    processor.process(session.job_id, mode="resume")

    job = manager.require(session.job_id)
    assert job.status == "completed"
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert all(result["处理状态"] == "成功")
    # 断点续跑不重复处理已完成的行
    assert result.loc[0, "尝试次数"] == "1"
    assert result.loc[1, "尝试次数"] == "1"
    assert result.loc[2, "尝试次数"] == "1"


def test_cookie_generation_rotation_closes_and_rebinds_thread_service(tmp_path):
    cookie_manager = _build_cookie_account_manager(tmp_path, ["主号", "小号A"])
    manager = JobManager()
    session = manager.create_session(
        original_filename="gen-rotate.csv",
        upload_path=tmp_path / "gen-rotate.csv",
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    class SpyDouyinService(FakeDouyinService):
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    created_services = []

    def factory():
        service = SpyDouyinService()
        created_services.append(service)
        return service

    processor = JobProcessor(
        manager,
        douyin_service_factory=factory,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        cookie_account_manager=cookie_manager,
    )

    account_a = cookie_manager.select_current_or_first_available_account()
    assert account_a is not None
    processor._apply_cookie_account(session.job_id, account_a, reason="初始账号")
    service_a = processor._get_thread_douyin_service(session.job_id)
    assert service_a.cookie_account_id == account_a.account_id
    assert service_a.closed is False

    account_b = cookie_manager.select_next_available_account(current_account_id=account_a.account_id)
    assert account_b is not None
    processor._apply_cookie_account(session.job_id, account_b, reason="轮换")

    assert service_a.closed is True
    service_b = processor._get_thread_douyin_service(session.job_id)
    assert service_b is not service_a
    assert service_b.closed is False
    assert service_b.cookie_account_id == account_b.account_id
    assert len(created_services) == 2


def test_pipeline_overwrite_partial_success_uses_current_round(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "overwrite_partial.csv",
        [{"url": "https://v.douyin.com/AAA111/"}],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/good-model\nmlx-community/bad-model",
        model_names=["mlx-community/good-model", "mlx-community/bad-model"],
        overwrite=True,
        download_workers=1,
        asr_workers=1,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=1,
        retry_delay_seconds=0,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    # overwrite=True 时旧逻辑会把本轮有成功模型的行误判为“失败”
    assert result.loc[0, "处理状态"] == "部分成功"
    assert result.loc[0, "视频文本__good_model"].startswith("transcript:audio:mlx-community/good-model")
    assert "转写失败" in result.loc[0, "失败原因"]


def test_pipeline_serial_all_models_failed_marks_failed(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "serial_all_fail.csv",
        [{"url": "https://v.douyin.com/AAA111/"}],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/bad-model-x\nmlx-community/bad-model-y",
        model_names=["mlx-community/bad-model-x", "mlx-community/bad-model-y"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=1,
        retry_delay_seconds=0,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id)

    result = pd.read_csv(manager.require(session.job_id).output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert result.loc[0, "处理状态"] == "失败"
    assert "转写失败: mlx-community/bad-model-x" in result.loc[0, "失败原因"]
    assert "转写失败: mlx-community/bad-model-y" in result.loc[0, "失败原因"]


def test_pipeline_parallel_all_models_failed_marks_failed(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "parallel_all_fail.csv",
        [
            {"url": "https://v.douyin.com/ROW1/"},
            {"url": "https://v.douyin.com/ROW2/"},
        ],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/bad-model-x\nmlx-community/bad-model-y",
        model_names=["mlx-community/bad-model-x", "mlx-community/bad-model-y"],
        overwrite=False,
        download_workers=1,
        asr_workers=2,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        batch_size=10,
        batch_delay_seconds=0,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    processor.process(session.job_id)

    result = pd.read_csv(manager.require(session.job_id).output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    # 并行路径与串行同语义：本轮全部模型失败 → 失败（不是部分成功）
    assert all(result["处理状态"] == "失败")
    assert all("转写失败" in reason for reason in result["失败原因"])


def test_save_download_only_raises_without_audio_track_and_sidecar(tmp_path, monkeypatch):
    from app import pipeline as pipeline_module

    class _VideoOnlyProbeFfmpeg:
        def probe_media(self, path):
            return {"streams": [{"codec_type": "video"}]}

    job, task, video, video_path, audio_path = _download_only_job_fixture(
        tmp_path, video_bytes=b"video-only", audio_bytes=None
    )
    monkeypatch.setattr(pipeline_module, "FfmpegService", _VideoOnlyProbeFfmpeg)

    with pytest.raises(RowProcessingError) as excinfo:
        JobProcessor._save_download_only_video(job, task, video, video_path, audio_path, tmp_path / "videos")

    assert "视频没有音轨" in str(excinfo.value)


def test_pipeline_download_only_silent_video_retries_then_fails(tmp_path, monkeypatch):
    from app import pipeline as pipeline_module

    class _VideoOnlyProbeFfmpeg:
        def probe_media(self, path):
            return {"streams": [{"codec_type": "video"}]}

    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "silent_video.csv",
        [{"url": "https://v.douyin.com/AAA111/"}],
        url_column="url",
        transcript_column="",
        model_names_text="",
        model_names=[],
        overwrite=False,
        download_only=True,
    )
    monkeypatch.setattr(pipeline_module, "VIDEO_OUTPUT_DIR", tmp_path / "videos_out")
    monkeypatch.setattr(pipeline_module, "FfmpegService", _VideoOnlyProbeFfmpeg)

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=_explode_transcription_factory,
        retry_delay_seconds=0,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.status == "completed"
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    # 无声 mp4 不能判成功：接入既有 NO_AUDIO 分类，重试到上限后失败
    assert result.loc[0, "处理状态"] == "失败"
    assert result.loc[0, "尝试次数"] == "3"
    assert "视频没有音轨" in result.loc[0, "失败原因"]
    assert not list((tmp_path / "videos_out" / "silent_video").glob("row*"))


def test_pipeline_deleted_ceiling_scales_with_configured_max_retries(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "ceiling_scale.csv",
        [{"url": "https://v.douyin.com/AAA111/"}],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=AlwaysRetryableParseFailureDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=5,
        retry_delay_seconds=0,
        sleep_fn=lambda seconds: None,
        batch_size=1,
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    result = pd.read_csv(job.output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    # 上限不再是硬编码 3：配 5 轮就真跑满 5 轮（1 次主尝试 + 5 轮重试）后才按已删除处理
    assert result.loc[0, "处理状态"] == "已删除"
    assert result.loc[0, "尝试次数"] == "6"
    assert any("重试超过 5 次仍未成功" in line for line in job.logs)


def test_pipeline_marks_structured_download_forbidden_unavailable(tmp_path, monkeypatch):
    from app.douyin_service import DownloadForbiddenError

    class StructuredForbiddenDouyinService(FakeDouyinService):
        def download_video(self, video, output_path, audio_output_path=None):
            raise DownloadForbiddenError(
                "HTTP 403 Forbidden x2, 浏览器兜底失败: 浏览器兜底下载失败: HTTP 403 x2"
            )

    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "structured_forbidden.csv",
        [{"url": "https://www.douyin.com/video/7416287864591256844"}],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=StructuredForbiddenDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        retry_delay_seconds=0,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id)

    result = pd.read_csv(manager.require(session.job_id).output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert result.loc[0, "处理状态"] == "无法下载"
    assert result.loc[0, "尝试次数"] == "2"
    assert "HTTP 403 Forbidden x2" in result.loc[0, "失败原因"]


def test_pipeline_marks_structured_non_video_download_404(tmp_path, monkeypatch):
    from app.douyin_service import NonVideoDownloadError

    class StructuredNonVideoDouyinService(FakeDouyinService):
        def download_video(self, video, output_path, audio_output_path=None):
            raise NonVideoDownloadError(
                "返回内容不是有效视频, HTTP 404 Not Found, 浏览器兜底失败: 浏览器兜底下载失败: 返回内容不是有效媒体, HTTP 404"
            )

    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "structured_404.csv",
        [{"url": "https://www.douyin.com/video/7387762150300814630"}],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=StructuredNonVideoDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        retry_delay_seconds=0,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id)

    result = pd.read_csv(manager.require(session.job_id).output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert result.loc[0, "处理状态"] == "非视频"
    assert result.loc[0, "尝试次数"] == "2"
    assert "返回内容不是有效视频" in result.loc[0, "失败原因"]


def test_finalize_cookie_rotation_logs_warning_only_once(tmp_path):
    cookie_manager = _build_cookie_account_manager(tmp_path, ["主号"], invalidation_threshold=1)
    manager = JobManager()
    session = manager.create_session(
        original_filename="warn-once.csv",
        upload_path=tmp_path / "warn-once.csv",
        columns=["url"],
        preview_rows=[],
        row_count=1,
    )
    manager.configure_job(
        session.job_id,
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        cookie_account_manager=cookie_manager,
        cookie_strong_signal_switch_threshold=1,
    )
    account = cookie_manager.select_current_or_first_available_account()
    assert account is not None
    processor._apply_cookie_account(session.job_id, account, reason="test setup")

    run_state = RunControlState(
        anti_crawl_streak_threshold=100,
        anti_crawl_cooldown_seconds=0,
        proactive_cooldown_batch_threshold=100,
        proactive_cooldown_seconds=0,
    )

    for _ in range(2):
        run_state.current_batch_strong_issue = True
        run_state.current_batch_strong_signal_descriptions = ["强冷却信号：HTTP 403"]
        processor._finalize_cookie_rotation(
            job_id=session.job_id,
            run_state=run_state,
            phase="主任务",
            batch_index=1,
            total_batches=1,
        )

    job = manager.require(session.job_id)
    warning_logs = [line for line in job.logs if "已进入预警状态" in line]
    assert len(warning_logs) == 1
    assert any("维持预警状态" in line for line in job.logs)
    warned = cookie_manager.get_account(account.account_id)
    assert warned is not None
    assert warned.warning is True


def test_pipeline_disambiguates_duplicate_model_aliases(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "aliases.csv",
        [{"url": "https://v.douyin.com/AAA111/"}],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model\nother/test-model",
        model_names=["mlx-community/test-model", "other/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        max_retries=1,
        retry_delay_seconds=0,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id)

    result = pd.read_csv(manager.require(session.job_id).output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert "视频文本__test_model" in result.columns
    assert "视频文本__test_model_2" in result.columns
    assert result.loc[0, "视频文本__test_model"] == "transcript:audio:mlx-community/test-model"
    assert result.loc[0, "视频文本__test_model_2"] == "transcript:audio:other/test-model"
    assert result.loc[0, "处理状态"] == "成功"


def test_pipeline_output_path_uses_job_id_prefix(tmp_path, monkeypatch):
    manager, session = _make_job(
        tmp_path,
        monkeypatch,
        "named.csv",
        [{"url": "https://v.douyin.com/AAA111/"}],
        url_column="url",
        transcript_column="视频文本",
        model_names_text="mlx-community/test-model",
        model_names=["mlx-community/test-model"],
        overwrite=False,
        download_workers=1,
        asr_workers=1,
    )

    processor = JobProcessor(
        manager,
        douyin_service_factory=FakeDouyinService,
        ffmpeg_service_factory=FakeFfmpegService,
        transcription_service_factory=FakeTranscriptionService,
        sleep_fn=lambda seconds: None,
    )
    processor.process(session.job_id)

    job = manager.require(session.job_id)
    assert job.output_path is not None
    assert Path(job.output_path).name == f"named.{session.job_id[:8]}.processed.csv"
