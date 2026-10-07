from __future__ import annotations

import json
import re
import shutil
import threading
import time
from contextlib import ExitStack
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx

from .config import (
    ANTI_CRAWL_COOLDOWN_SECONDS,
    ANTI_CRAWL_STREAK_THRESHOLD,
    ATTEMPTS_COLUMN,
    COOKIE_SWITCH_BATCH_THRESHOLD,
    COOKIE_STRONG_SIGNAL_SWITCH_THRESHOLD,
    COOKIE_RETRY_STRONG_SIGNAL_SWITCH_THRESHOLD,
    DEFAULT_BATCH_DELAY_SECONDS,
    DEFAULT_MODEL,
    DELETED_TRANSCRIPT_TEXT,
    ERROR_COLUMN,
    JOB_DIR,
    OUTPUT_DIR,
    PROCESS_BATCH_DELAY_SECONDS,
    PROCESS_BATCH_SIZE,
    PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
    PROACTIVE_COOLDOWN_SECONDS,
    ROW_MAX_RETRIES,
    ROW_RETRY_DELAY_SECONDS,
    STATUS_COLUMN,
    VIDEO_OUTPUT_DIR,
)
from .cookie_accounts import CookieAccount, CookieAccountManager
from .csv_service import (
    build_processed_output_name,
    ensure_text_column,
    inspect_csv,
    stringify_cell,
    write_csv,
)
from .browser_service import PlaywrightFallback
from .douyin_service import (
    DouyinService,
    DownloadForbiddenError,
    DownloadResult,
    NonVideoDownloadError,
    ParsedVideo,
)
from .job_manager import JobManager
from .media_service import FfmpegService
from .request_coordinator import RequestCoordinator
from .transcription_service import TranscriptionService


SUCCESS_STATUSES = {"成功", "部分成功"}
NON_VIDEO_STATUS = "非视频"
DOWNLOAD_UNAVAILABLE_STATUS = "无法下载"
SKIP_STATUSES = {"跳过", "已删除", NON_VIDEO_STATUS, DOWNLOAD_UNAVAILABLE_STATUS}
RETRY_PENDING_STATUS = "待重试"
FAILED_STATUSES = {"失败", RETRY_PENDING_STATUS}
TERMINAL_STATUSES = SUCCESS_STATUSES | SKIP_STATUSES
PREVIEW_ONLY_DELETE_ERROR = "浏览器兜底命中通用页，仅捕获到预览片段"
INVALID_LINK_DELETE_ATTEMPTS = 2
INVALID_LINK_ERROR = "未在单元格中找到有效的抖音链接"
VIDEO_DETAIL_MISSING_ERROR = "分享页中未找到视频详情"
NON_VIDEO_ERROR = DouyinService.NON_VIDEO_NOTE_ERROR
DOWNLOAD_FORBIDDEN_ERROR = "HTTP 403 Forbidden x2, 浏览器兜底失败: 浏览器兜底下载失败: HTTP 403 x2"
DOWNLOAD_UNAVAILABLE_ATTEMPTS = 2
NON_VIDEO_DOWNLOAD_404_ERROR = "返回内容不是有效视频, HTTP 404 Not Found, 浏览器兜底失败: 浏览器兜底下载失败: 返回内容不是有效媒体, HTTP 404"
NON_VIDEO_DOWNLOAD_404_ATTEMPTS = 2
PREVIEW_ONLY_RETRY_ATTEMPTS = 3
NO_AUDIO_RETRY_ATTEMPTS = 3
NO_AUDIO_ERROR = "视频没有音轨"
ROUTER_DATA_MISSING_ERROR = "分享页中未找到 _ROUTER_DATA"
ITEMINFO_JSON_ERROR = "iteminfo API: Expecting value: line 1 column 1 (char 0)"
DETAIL_JSON_ERROR = "detail API: Expecting value: line 1 column 1 (char 0)"
BROWSER_REAL_VIDEO_ADDRESS_MISSING_ERROR = "浏览器兜底没有捕获到真实视频地址"
DNS_LOOKUP_ERROR = "nodename nor servname provided, or not known"
BROWSER_DISCONNECTED_ERROR = "ERR_INTERNET_DISCONNECTED"
BROWSER_NAV_TIMEOUT_ERROR = "Page.goto: Timeout 15000ms exceeded"
SSL_CERT_VERIFY_ERROR = "CERTIFICATE_VERIFY_FAILED"
ACTIVE_REFUSED_ERROR = "No connection could be made because the target machine actively refused it"
WEAK_SIGNAL_PARSE_FALLBACK = "分享页/接口异常、浏览器兜底异常或网络异常"
STRONG_SIGNAL_CHALLENGE_FALLBACK = "challenge/验证码信号"
DOWNLOAD_ONLY_MODEL = "_download_only"
DOWNLOAD_ONLY_VIDEO_SUFFIX = ".mp4"
DOWNLOAD_ONLY_AUDIO_SUFFIX = ".m4a"


@dataclass(frozen=True)
class RowTask:
    row_index: object
    position: int
    csv_row_number: int
    url_value: str


@dataclass
class RowOutcome:
    status: str
    attempts: int
    error_message: str = ""
    retryable: bool = False
    resets_anti_crawl_state: bool = False
    anti_crawl_suspected: bool = False
    anti_crawl_strong_suspected: bool = False
    anti_crawl_signal_descriptions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RowPlan:
    task: RowTask
    attempts_before: int
    pending_models: Tuple[str, ...]

    @property
    def attempt_number(self) -> int:
        return self.attempts_before + 1


@dataclass
class PreparedRow:
    plan: RowPlan
    row_dir: Path
    video_path: Path
    browser_audio_path: Path
    audio_path: Path
    log_messages: Tuple[str, ...] = ()
    anti_crawl_signal_descriptions: Tuple[str, ...] = ()


@dataclass
class RowExecutionResult:
    plan: RowPlan
    outcome: RowOutcome
    transcripts: Dict[str, str] = field(default_factory=dict)
    log_messages: Tuple[str, ...] = ()
    cleanup_dir: Optional[Path] = None


@dataclass
class CooldownRequest:
    seconds: int
    reason: str
    threshold: int = 0
    affected_tasks: List[RowTask] = field(default_factory=list)
    signal_descriptions: List[str] = field(default_factory=list)


@dataclass
class RunControlState:
    anti_crawl_streak_threshold: int
    anti_crawl_cooldown_seconds: int
    proactive_cooldown_batch_threshold: int
    proactive_cooldown_seconds: int
    suspicious_tasks: List[RowTask] = field(default_factory=list)
    suspicious_signal_descriptions: List[str] = field(default_factory=list)
    pending_cooldown: Optional[CooldownRequest] = None
    healthy_batch_streak: int = 0
    current_batch_had_issue: bool = False
    current_batch_strong_issue: bool = False
    current_batch_strong_signal_descriptions: List[str] = field(default_factory=list)
    strong_signal_batch_streak: int = 0
    strong_signal_phase: str = ""


class RowProcessingError(RuntimeError):
    def __init__(self, stage: str, detail: str) -> None:
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}失败: {detail}")


class JobProcessor:
    def __init__(
        self,
        manager: JobManager,
        *,
        douyin_service_factory=DouyinService,
        ffmpeg_service_factory=FfmpegService,
        transcription_service_factory=TranscriptionService,
        sleep_fn=time.sleep,
        max_retries: int = ROW_MAX_RETRIES,
        retry_delay_seconds: int = ROW_RETRY_DELAY_SECONDS,
        batch_size: int = PROCESS_BATCH_SIZE,
        batch_delay_seconds: int = PROCESS_BATCH_DELAY_SECONDS,
        anti_crawl_streak_threshold: int = ANTI_CRAWL_STREAK_THRESHOLD,
        anti_crawl_cooldown_seconds: int = ANTI_CRAWL_COOLDOWN_SECONDS,
        proactive_cooldown_batch_threshold: int = PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
        proactive_cooldown_seconds: int = PROACTIVE_COOLDOWN_SECONDS,
        cookie_account_manager: Optional[CookieAccountManager] = None,
        cookie_switch_batch_threshold: int = COOKIE_SWITCH_BATCH_THRESHOLD,
        cookie_strong_signal_switch_threshold: int = COOKIE_STRONG_SIGNAL_SWITCH_THRESHOLD,
        cookie_retry_strong_signal_switch_threshold: int = COOKIE_RETRY_STRONG_SIGNAL_SWITCH_THRESHOLD,
    ) -> None:
        self.manager = manager
        self.douyin_service_factory = douyin_service_factory
        self.ffmpeg_service_factory = ffmpeg_service_factory
        self.transcription_service_factory = transcription_service_factory
        self._request_coordinator = RequestCoordinator()
        self.sleep_fn = sleep_fn
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.batch_size = batch_size
        self.batch_delay_seconds = batch_delay_seconds
        self.anti_crawl_streak_threshold = max(1, int(anti_crawl_streak_threshold))
        self.anti_crawl_cooldown_seconds = max(0, int(anti_crawl_cooldown_seconds))
        self.proactive_cooldown_batch_threshold = max(1, int(proactive_cooldown_batch_threshold))
        self.proactive_cooldown_seconds = max(0, int(proactive_cooldown_seconds))
        self.cookie_account_manager = cookie_account_manager
        self.cookie_switch_batch_threshold = max(1, int(cookie_switch_batch_threshold))
        self.cookie_strong_signal_switch_threshold = max(1, int(cookie_strong_signal_switch_threshold))
        self.cookie_retry_strong_signal_switch_threshold = max(1, int(cookie_retry_strong_signal_switch_threshold))
        # ASR model instances are large (especially SenseVoice). Keep one
        # service per default model across ASR worker threads instead of
        # loading a separate model copy in every thread.
        self._transcriber_services: Dict[Tuple[str, str], TranscriptionService] = {}
        self._transcriber_services_lock = threading.RLock()
        self._transcriber_model_locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._douyin_local = threading.local()
        self._douyin_services_by_job: Dict[str, List[DouyinService]] = {}
        self._douyin_services_lock = threading.Lock()

    def process(self, job_id: str, mode: str = "full") -> None:
        job = self.manager.require(job_id)
        workspace = JOB_DIR / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        run_state = RunControlState(
            anti_crawl_streak_threshold=max(1, int(getattr(job, "anti_crawl_streak_threshold", self.anti_crawl_streak_threshold))),
            anti_crawl_cooldown_seconds=max(0, int(getattr(job, "anti_crawl_cooldown_seconds", self.anti_crawl_cooldown_seconds))),
            proactive_cooldown_batch_threshold=max(
                1,
                int(getattr(job, "proactive_cooldown_batch_threshold", self.proactive_cooldown_batch_threshold)),
            ),
            proactive_cooldown_seconds=max(
                0,
                int(getattr(job, "proactive_cooldown_seconds", self.proactive_cooldown_seconds)),
            ),
        )

        transcriber = None

        try:
            ffmpeg = self.ffmpeg_service_factory()
            download_only = bool(getattr(job, "download_only", False))
            default_model = job.model_names[0] if job.model_names else DEFAULT_MODEL
            transcriber = None if download_only else self.transcription_service_factory(default_model=default_model)
            download_workers = self._sanitize_worker_count(job.download_workers)
            asr_workers = self._sanitize_worker_count(job.asr_workers)
            batch_delay_seconds = self._sanitize_batch_delay(getattr(job, "batch_delay_seconds", self.batch_delay_seconds))

            if not download_only and not ffmpeg.is_available():
                raise RuntimeError("当前系统未检测到 ffmpeg，请先安装后再运行任务")

            source_path = self._resolve_source_path(job, mode)
            inspection = inspect_csv(source_path)
            dataframe = inspection.dataframe

            if mode == "continue_existing" and source_path != job.upload_path and len(dataframe) != job.row_count:
                raise RuntimeError("已有结果文件行数与当前上传文件不一致，无法继续处理")

            if not job.url_column or job.url_column not in dataframe.columns:
                raise RuntimeError("URL 列不存在，请重新选择")
            if not download_only and not job.transcript_column:
                raise RuntimeError("视频文本列不能为空")

            model_names = job.model_names or [DEFAULT_MODEL]
            if download_only:
                output_columns: Dict[str, str] = {}
            else:
                output_columns = self._build_output_columns(job.transcript_column, model_names, transcriber)
            for column_name in output_columns.values():
                ensure_text_column(dataframe, column_name)
            for column_name in (STATUS_COLUMN, ERROR_COLUMN, ATTEMPTS_COLUMN):
                ensure_text_column(dataframe, column_name)

            output_path = self._build_output_path(job)
            self.manager.mark_running(job_id, len(dataframe))
            if download_only:
                video_root = self._video_output_root(job)
                try:
                    self._ensure_video_output_root_writable(video_root)
                except Exception as exc:
                    raise RuntimeError(f"视频保存目录不可用：{video_root}（{self._format_error(exc)}）") from exc
                self.manager.append_log(
                    job_id,
                    f"仅下载模式：视频将保存到 {video_root / Path(job.original_filename).stem}",
                )
            self.manager.append_log(
                job_id,
                self._build_start_message(
                    len(dataframe),
                    len(model_names),
                    mode,
                    download_workers=download_workers,
                    asr_workers=asr_workers,
                    batch_delay_seconds=batch_delay_seconds,
                ),
            )
            if mode == "continue_existing" and source_path != job.upload_path:
                self.manager.append_log(job_id, f"已载入已有结果文件：{source_path.name}")
            self._persist_snapshot(job_id, dataframe, output_path)
            self._sync_progress(job_id, dataframe, current_message="任务执行中")

            tasks = self._build_tasks(dataframe, job)
            primary_tasks, retry_queue = self._split_tasks(
                dataframe, tasks, output_columns, job.overwrite, mode, download_only=download_only
            )
            parallel_enabled = download_workers > 1 or asr_workers > 1

            with ExitStack() as stack:
                download_pool = None
                asr_pool = None
                if parallel_enabled:
                    download_pool = stack.enter_context(
                        ThreadPoolExecutor(
                            max_workers=download_workers,
                            thread_name_prefix=f"dy-download-{job_id[:8]}",
                        )
                    )
                    asr_pool = stack.enter_context(
                        ThreadPoolExecutor(
                            max_workers=asr_workers,
                            thread_name_prefix=f"dy-asr-{job_id[:8]}",
                        )
                    )

                self._process_batches(
                    job_id=job_id,
                    run_state=run_state,
                    dataframe=dataframe,
                    tasks=primary_tasks,
                    output_columns=output_columns,
                    overwrite=job.overwrite,
                    workspace=workspace,
                    ffmpeg=ffmpeg,
                    transcriber=transcriber,
                    output_path=output_path,
                    phase="主任务",
                    retry_queue=retry_queue,
                    download_workers=download_workers,
                    asr_workers=asr_workers,
                    default_model=default_model,
                    download_pool=download_pool,
                    asr_pool=asr_pool,
                    batch_delay_seconds=batch_delay_seconds,
                )

                if not self.manager.is_pause_requested(job_id):
                    self._retry_failed_tasks(
                        job_id=job_id,
                        run_state=run_state,
                        dataframe=dataframe,
                        retry_queue=retry_queue,
                        output_columns=output_columns,
                        overwrite=job.overwrite,
                        workspace=workspace,
                        ffmpeg=ffmpeg,
                        transcriber=transcriber,
                        output_path=output_path,
                        download_workers=download_workers,
                        asr_workers=asr_workers,
                        default_model=default_model,
                        download_pool=download_pool,
                        asr_pool=asr_pool,
                        batch_delay_seconds=batch_delay_seconds,
                    )

            self._persist_snapshot(job_id, dataframe, output_path)
            self._sync_progress(job_id, dataframe)

            output_preview_rows = self._preview_rows(dataframe)
            processed, succeeded, failed, skipped = self._compute_counters(dataframe)

            if self.manager.is_pause_requested(job_id):
                self.manager.append_log(job_id, "任务已暂停")
                self.manager.mark_paused(job_id, output_path, output_preview_rows)
                return

            self.manager.append_log(
                job_id,
                f"任务结束：成功 {succeeded}，失败 {failed}，跳过 {skipped}",
            )
            self.manager.mark_completed(job_id, output_path, output_preview_rows)
        except Exception as exc:
            self.manager.append_log(job_id, f"任务终止：{exc}")
            self.manager.mark_failed(job_id, str(exc))
        finally:
            self._close_job_douyin_services(job_id)
            self._clear_job_transcribers(job_id)
            shutil.rmtree(workspace, ignore_errors=True)

    def _process_batches(
        self,
        *,
        job_id: str,
        run_state: RunControlState,
        dataframe,
        tasks: Sequence[RowTask],
        output_columns: Dict[str, str],
        overwrite: bool,
        workspace: Path,
        ffmpeg: FfmpegService,
        transcriber: TranscriptionService,
        output_path: Path,
        phase: str,
        retry_queue: List[RowTask],
        download_workers: int,
        asr_workers: int,
        default_model: str,
        download_pool: Optional[ThreadPoolExecutor],
        asr_pool: Optional[ThreadPoolExecutor],
        batch_delay_seconds: int,
    ) -> None:
        total_batches = self._batch_count(len(tasks))
        for batch_index, batch in enumerate(self._iter_batches(tasks), start=1):
            if self.manager.is_pause_requested(job_id):
                break

            self._prepare_cookie_account_for_batch(job_id)

            if total_batches > 1:
                self.manager.append_log(
                    job_id,
                    f"{phase} 第 {batch_index}/{total_batches} 批开始，共 {len(batch)} 行",
                )

            if self._should_parallelize_batch(batch, download_workers, asr_workers):
                self._process_batch_parallel(
                    job_id=job_id,
                    run_state=run_state,
                    dataframe=dataframe,
                    tasks=batch,
                    output_columns=output_columns,
                    overwrite=overwrite,
                    workspace=workspace,
                    output_path=output_path,
                    retry_queue=retry_queue,
                    download_workers=download_workers,
                    asr_workers=asr_workers,
                    default_model=default_model,
                    download_pool=download_pool,
                    asr_pool=asr_pool,
                    batch_delay_seconds=batch_delay_seconds,
                )
            else:
                for task in batch:
                    if self.manager.is_pause_requested(job_id):
                        break
                    try:
                        self._process_task(
                            job_id=job_id,
                            run_state=run_state,
                            dataframe=dataframe,
                            task=task,
                            output_columns=output_columns,
                            overwrite=overwrite,
                            workspace=workspace,
                            ffmpeg=ffmpeg,
                            transcriber=transcriber,
                            output_path=output_path,
                            retry_queue=retry_queue,
                            download_only=self._is_download_only(job_id),
                        )
                    except Exception:
                        # Normal rows are checkpointed once per batch. If an
                        # unexpected exception escapes the row handler, keep
                        # rows already completed in this batch before failing.
                        self._persist_snapshot(job_id, dataframe, output_path)
                        raise

            cooled_down = self._finalize_batch(
                job_id=job_id,
                run_state=run_state,
                dataframe=dataframe,
                output_columns=output_columns,
                output_path=output_path,
                    retry_queue=retry_queue,
                allow_proactive_cooldown=batch_index < total_batches or bool(retry_queue),
            )
            # Persist once per batch. Writing the whole DataFrame after every
            # row turns a large spreadsheet into quadratic disk I/O.
            self._persist_snapshot(job_id, dataframe, output_path)
            self._sync_progress(job_id, dataframe)

            self._finalize_cookie_rotation(
                job_id=job_id,
                run_state=run_state,
                phase=phase,
                batch_index=batch_index,
                total_batches=total_batches,
            )

            if batch_index < total_batches and not self.manager.is_pause_requested(job_id) and not cooled_down:
                self.manager.append_log(
                    job_id,
                    f"{phase} 第 {batch_index} 批完成，{batch_delay_seconds} 秒后继续",
                )
                self.sleep_fn(batch_delay_seconds)

    def _retry_failed_tasks(
        self,
        *,
        job_id: str,
        run_state: RunControlState,
        dataframe,
        retry_queue: List[RowTask],
        output_columns: Dict[str, str],
        overwrite: bool,
        workspace: Path,
        ffmpeg: FfmpegService,
        transcriber: TranscriptionService,
        output_path: Path,
        download_workers: int,
        asr_workers: int,
        default_model: str,
        download_pool: Optional[ThreadPoolExecutor],
        asr_pool: Optional[ThreadPoolExecutor],
        batch_delay_seconds: int,
    ) -> None:
        pending = self._dedupe_tasks(retry_queue)
        round_index = 1

        while pending and not self.manager.is_pause_requested(job_id) and round_index <= self.max_retries:
            self._switch_cookie_for_retry_round(job_id, round_index)
            self.manager.append_log(job_id, f"开始第 {round_index} 轮重试，共 {len(pending)} 行")
            next_round: List[RowTask] = []
            total_batches = self._batch_count(len(pending))

            for batch_index, batch in enumerate(self._iter_batches(pending), start=1):
                if self.manager.is_pause_requested(job_id):
                    break

                self._prepare_cookie_account_for_batch(job_id)

                if total_batches > 1:
                    self.manager.append_log(
                        job_id,
                        f"重试 第 {batch_index}/{total_batches} 批开始，共 {len(batch)} 行",
                    )

                if self._should_parallelize_batch(batch, download_workers, asr_workers):
                    self._process_batch_parallel(
                        job_id=job_id,
                        run_state=run_state,
                        dataframe=dataframe,
                        tasks=batch,
                        output_columns=output_columns,
                        overwrite=overwrite,
                        workspace=workspace,
                        output_path=output_path,
                        retry_queue=next_round,
                        download_workers=download_workers,
                        asr_workers=asr_workers,
                        default_model=default_model,
                        download_pool=download_pool,
                        asr_pool=asr_pool,
                        batch_delay_seconds=batch_delay_seconds,
                    )
                else:
                    for task in batch:
                        if self.manager.is_pause_requested(job_id):
                            break
                        try:
                            self._process_task(
                                job_id=job_id,
                                run_state=run_state,
                                dataframe=dataframe,
                                task=task,
                                output_columns=output_columns,
                                overwrite=overwrite,
                                workspace=workspace,
                                ffmpeg=ffmpeg,
                                transcriber=transcriber,
                                output_path=output_path,
                                retry_queue=next_round,
                                download_only=self._is_download_only(job_id),
                            )
                        except Exception:
                            self._persist_snapshot(job_id, dataframe, output_path)
                            raise

                cooled_down = self._finalize_batch(
                    job_id=job_id,
                    run_state=run_state,
                    dataframe=dataframe,
                    output_columns=output_columns,
                    output_path=output_path,
                    retry_queue=next_round,
                    allow_proactive_cooldown=batch_index < total_batches or bool(next_round),
                )
                self._persist_snapshot(job_id, dataframe, output_path)
                self._sync_progress(job_id, dataframe)

                self._finalize_cookie_rotation(
                    job_id=job_id,
                    run_state=run_state,
                    phase="重试",
                    batch_index=batch_index,
                    total_batches=total_batches,
                )

                if batch_index < total_batches and not self.manager.is_pause_requested(job_id) and not cooled_down:
                    self.manager.append_log(
                        job_id,
                        f"重试 第 {batch_index} 批完成，{batch_delay_seconds} 秒后继续",
                    )
                    self.sleep_fn(batch_delay_seconds)

            pending = self._dedupe_tasks(next_round)
            if pending and not self.manager.is_pause_requested(job_id) and round_index < self.max_retries:
                self.manager.append_log(
                    job_id,
                    f"第 {round_index} 轮重试结束，剩余 {len(pending)} 行，{self.retry_delay_seconds} 秒后继续",
                )
                self.sleep_fn(self.retry_delay_seconds)
                round_index += 1
            else:
                round_index += 1

        if pending and not self.manager.is_pause_requested(job_id):
            self.manager.append_log(
                job_id,
                f"重试已达到上限 {self.max_retries} 轮，剩余 {len(pending)} 行将标记为已删除",
            )
            for task in pending:
                attempts = self._get_attempts(dataframe, task.row_index)
                for column_name in output_columns.values():
                    if overwrite or not stringify_cell(dataframe.at[task.row_index, column_name]).strip():
                        dataframe.at[task.row_index, column_name] = DELETED_TRANSCRIPT_TEXT
                self._set_row_meta(
                    dataframe,
                    task.row_index,
                    status="已删除",
                    error_message="",
                    attempts=attempts,
                )
                self.manager.append_log(
                    job_id,
                    f"第 {task.csv_row_number} 行重试 {self.max_retries} 轮后仍未成功，已标记为已删除",
                )
            self._persist_snapshot(job_id, dataframe, output_path)
            self._sync_progress(job_id, dataframe)

    def _process_batch_parallel(
        self,
        *,
        job_id: str,
        run_state: RunControlState,
        dataframe,
        tasks: Sequence[RowTask],
        output_columns: Dict[str, str],
        overwrite: bool,
        workspace: Path,
        output_path: Path,
        retry_queue: List[RowTask],
        download_workers: int,
        asr_workers: int,
        default_model: str,
        download_pool: Optional[ThreadPoolExecutor],
        asr_pool: Optional[ThreadPoolExecutor],
        batch_delay_seconds: int,
    ) -> None:
        plans: List[RowPlan] = []
        download_only = self._is_download_only(job_id)
        for task in tasks:
            if self.manager.is_pause_requested(job_id):
                break
            result = self._plan_task(dataframe, task, output_columns, overwrite, download_only)
            if isinstance(result, RowExecutionResult):
                self._handle_row_result(
                    job_id=job_id,
                    run_state=run_state,
                    dataframe=dataframe,
                    result=result,
                    output_columns=output_columns,
                    output_path=output_path,
                    retry_queue=retry_queue,
                )
                continue
            plans.append(result)

        if not plans:
            return

        if download_pool is None or asr_pool is None:
            raise RuntimeError("并发任务未正确初始化线程池")

        download_futures: Dict[Future, RowPlan] = {
            download_pool.submit(self._run_download_stage, job_id, plan, workspace): plan for plan in plans
        }
        asr_futures: Dict[Future, PreparedRow] = {}

        while download_futures or asr_futures:
            pending_futures = tuple(download_futures.keys()) + tuple(asr_futures.keys())
            done, _ = wait(pending_futures, return_when=FIRST_COMPLETED)
            try:
                for future in done:
                    self._collect_future_result(
                        future=future,
                        job_id=job_id,
                        run_state=run_state,
                        dataframe=dataframe,
                        download_futures=download_futures,
                        asr_futures=asr_futures,
                        output_columns=output_columns,
                        output_path=output_path,
                        retry_queue=retry_queue,
                        default_model=default_model,
                        asr_pool=asr_pool,
                    )
            except Exception:
                # future.result() 抛错（worker 线程未兜底的崩溃）时，先把其余已完成的
                # future 结果照常落盘，避免整批已完成的行随异常一起丢失，再向上抛出。
                leftover = [
                    future
                    for future in tuple(download_futures.keys()) + tuple(asr_futures.keys())
                    if future.done()
                ]
                for future in leftover:
                    try:
                        self._collect_future_result(
                            future=future,
                            job_id=job_id,
                            run_state=run_state,
                            dataframe=dataframe,
                            download_futures=download_futures,
                            asr_futures=asr_futures,
                            output_columns=output_columns,
                            output_path=output_path,
                            retry_queue=retry_queue,
                            default_model=default_model,
                            asr_pool=asr_pool,
                        )
                    except Exception:
                        continue
                # Preserve rows whose futures completed before the crash even
                # though the normal end-of-batch checkpoint is unreachable.
                self._persist_snapshot(job_id, dataframe, output_path)
                raise

        for future in download_futures:
            future.cancel()
        for future in asr_futures:
            future.cancel()

    def _collect_future_result(
        self,
        *,
        future: Future,
        job_id: str,
        run_state: RunControlState,
        dataframe,
        download_futures: Dict[Future, RowPlan],
        asr_futures: Dict[Future, PreparedRow],
        output_columns: Dict[str, str],
        output_path: Path,
        retry_queue: List[RowTask],
        default_model: str,
        asr_pool: ThreadPoolExecutor,
    ) -> None:
        if future in download_futures:
            download_futures.pop(future)
            result = future.result()
            if isinstance(result, PreparedRow):
                for message in result.log_messages:
                    self.manager.append_log(job_id, message)
                self.manager.update_progress(
                    job_id,
                    current_row=result.plan.task.csv_row_number,
                    current_url=result.plan.task.url_value,
                    current_message=f"第 {result.plan.task.csv_row_number} 行进入转写队列",
                )
                asr_futures[
                    asr_pool.submit(
                        self._run_transcription_stage,
                        result,
                        output_columns,
                        default_model,
                        job_id,
                    )
                ] = result
            else:
                self._handle_row_result(
                    job_id=job_id,
                    run_state=run_state,
                    dataframe=dataframe,
                    result=result,
                    output_columns=output_columns,
                    output_path=output_path,
                    retry_queue=retry_queue,
                )
        else:
            asr_futures.pop(future)
            result = future.result()
            self._handle_row_result(
                job_id=job_id,
                run_state=run_state,
                dataframe=dataframe,
                result=result,
                output_columns=output_columns,
                output_path=output_path,
                retry_queue=retry_queue,
            )

    def _plan_task(
        self,
        dataframe,
        task: RowTask,
        output_columns: Dict[str, str],
        overwrite: bool,
        download_only: bool = False,
    ) -> RowPlan | RowExecutionResult:
        current_attempts = self._get_attempts(dataframe, task.row_index)
        pending_models = tuple(
            self._pending_work_for_row(dataframe, task.row_index, output_columns, overwrite, download_only)
        )

        if not task.url_value:
            return RowExecutionResult(
                plan=RowPlan(task=task, attempts_before=current_attempts, pending_models=pending_models),
                outcome=RowOutcome(status="跳过", attempts=current_attempts, error_message="URL 为空"),
                log_messages=(f"第 {task.csv_row_number} 行跳过：URL 为空",),
            )

        if not pending_models:
            skip_reason = "该行已处理过" if download_only else "目标文本列已有内容"
            return RowExecutionResult(
                plan=RowPlan(task=task, attempts_before=current_attempts, pending_models=pending_models),
                outcome=RowOutcome(status="跳过", attempts=current_attempts, error_message=skip_reason),
                log_messages=(f"第 {task.csv_row_number} 行跳过：{skip_reason}",),
            )

        return RowPlan(task=task, attempts_before=current_attempts, pending_models=pending_models)

    def _run_download_stage(self, job_id: str, plan: RowPlan, workspace: Path) -> PreparedRow | RowExecutionResult:
        douyin = self._get_thread_douyin_service(job_id)
        job = self.manager.require(job_id)
        download_only = bool(getattr(job, "download_only", False))
        row_dir = workspace / f"row_{plan.task.position:05d}"
        video_path = row_dir / "source.mp4"
        browser_audio_path = row_dir / "fallback_audio.m4a"
        audio_path = row_dir / "audio.wav"

        try:
            self._reset_attempt_anti_crawl_signals(douyin)
            self.manager.update_progress(
                job_id,
                current_row=plan.task.csv_row_number,
                current_url=plan.task.url_value,
                current_message=f"正在解析第 {plan.task.csv_row_number} 行",
            )
            self.manager.append_log(job_id, f"第 {plan.task.csv_row_number} 行开始解析")
            self._reset_attempt_files(row_dir, video_path, browser_audio_path, audio_path)
            video = self._parse_video(douyin, plan.task.url_value)

            log_messages = [
                f"第 {plan.task.csv_row_number} 行解析成功（{video.resolved_by}）：{video.title}",
            ]

            if video.is_deleted:
                strong_signal_descriptions = self._consume_attempt_challenge_signal_descriptions(douyin)
                log_messages.append(
                    f"第 {plan.task.csv_row_number} 行已删除，已写入“{DELETED_TRANSCRIPT_TEXT}”"
                )
                return RowExecutionResult(
                    plan=plan,
                    outcome=RowOutcome(
                        status="已删除",
                        attempts=plan.attempt_number,
                        resets_anti_crawl_state=True,
                        anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                        anti_crawl_signal_descriptions=strong_signal_descriptions,
                    ),
                    transcripts={
                        model_name: DELETED_TRANSCRIPT_TEXT
                        for model_name in plan.pending_models
                        if model_name != DOWNLOAD_ONLY_MODEL
                    },
                    log_messages=tuple(log_messages),
                    cleanup_dir=row_dir,
                )

            download_result = self._download_video(douyin, video, video_path, browser_audio_path)
            log_messages.append(
                f"第 {plan.task.csv_row_number} 行下载完成：{download_result.strategy}"
            )
            if download_result.audio_source_url:
                log_messages.append(f"第 {plan.task.csv_row_number} 行已捕获补充音频流")
            strong_signal_descriptions = self._consume_attempt_challenge_signal_descriptions(douyin)

            if download_only:
                video_root = self._video_output_root(job)
                saved_path = self._save_download_only_video(job, plan.task, video, video_path, browser_audio_path, video_root)
                log_messages.append(
                    f"第 {plan.task.csv_row_number} 行视频已保存：{saved_path.relative_to(video_root)}"
                )
                return RowExecutionResult(
                    plan=plan,
                    outcome=RowOutcome(
                        status="成功",
                        attempts=plan.attempt_number,
                        anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                        anti_crawl_signal_descriptions=strong_signal_descriptions,
                    ),
                    transcripts={},
                    log_messages=tuple(log_messages),
                    cleanup_dir=row_dir,
                )

            return PreparedRow(
                plan=plan,
                row_dir=row_dir,
                video_path=video_path,
                browser_audio_path=browser_audio_path,
                audio_path=audio_path,
                log_messages=tuple(log_messages),
                anti_crawl_signal_descriptions=strong_signal_descriptions,
            )
        except RowProcessingError as exc:
            if self._is_non_video_parse_error(exc):
                return RowExecutionResult(
                    plan=plan,
                    outcome=RowOutcome(
                        status=NON_VIDEO_STATUS,
                        attempts=plan.attempt_number,
                        error_message=NON_VIDEO_ERROR,
                    ),
                    log_messages=(
                        f"第 {plan.task.csv_row_number} 行识别为图文 note，按“{NON_VIDEO_STATUS}”跳过",
                    ),
                    cleanup_dir=row_dir,
                )
            if self._should_mark_non_video_from_download_error(exc, plan.attempt_number):
                return RowExecutionResult(
                    plan=plan,
                    outcome=RowOutcome(
                        status=NON_VIDEO_STATUS,
                        attempts=plan.attempt_number,
                        error_message=NON_VIDEO_DOWNLOAD_404_ERROR,
                    ),
                    log_messages=(
                        f"第 {plan.task.csv_row_number} 行连续命中图文型 404 下载错误，按“{NON_VIDEO_STATUS}”处理",
                    ),
                    cleanup_dir=row_dir,
                )
            if self._should_mark_download_unavailable(exc, plan.attempt_number):
                return RowExecutionResult(
                    plan=plan,
                    outcome=RowOutcome(
                        status=DOWNLOAD_UNAVAILABLE_STATUS,
                        attempts=plan.attempt_number,
                        error_message=str(exc),
                    ),
                    log_messages=(
                        f"第 {plan.task.csv_row_number} 行连续命中 403 下载限制，按“{DOWNLOAD_UNAVAILABLE_STATUS}”处理",
                    ),
                    cleanup_dir=row_dir,
                )
            delete_reason = self._deleted_reason_for_error(exc, plan.attempt_number)
            if delete_reason:
                self._consume_attempt_challenge_signal_descriptions(douyin)
                return RowExecutionResult(
                    plan=plan,
                    outcome=RowOutcome(
                        status="已删除",
                        attempts=plan.attempt_number,
                        resets_anti_crawl_state=(delete_reason == "分享页未找到视频详情"),
                    ),
                    transcripts={
                        model_name: DELETED_TRANSCRIPT_TEXT
                        for model_name in plan.pending_models
                        if model_name != DOWNLOAD_ONLY_MODEL
                    },
                    log_messages=(
                        f"第 {plan.task.csv_row_number} 行{delete_reason}，按已删除处理，已写入“{DELETED_TRANSCRIPT_TEXT}”",
                    ),
                    cleanup_dir=row_dir,
                )
            retryable = self._is_retryable(exc, plan.attempt_number)
            weak_signal_descriptions = self._weak_signal_descriptions_for_error(exc)
            strong_signal_descriptions = self._consume_attempt_challenge_signal_descriptions(douyin)
            if self._should_mark_deleted_after_retry_ceiling(exc, plan.attempt_number):
                return RowExecutionResult(
                    plan=plan,
                    outcome=RowOutcome(
                        status="已删除",
                        attempts=plan.attempt_number,
                        anti_crawl_suspected=bool(weak_signal_descriptions),
                        anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                        anti_crawl_signal_descriptions=(*weak_signal_descriptions, *strong_signal_descriptions),
                    ),
                    transcripts={
                        model_name: DELETED_TRANSCRIPT_TEXT
                        for model_name in plan.pending_models
                        if model_name != DOWNLOAD_ONLY_MODEL
                    },
                    log_messages=(
                        f"第 {plan.task.csv_row_number} 行重试超过 {self.max_retries} 次仍未成功，按已删除处理，已写入“{DELETED_TRANSCRIPT_TEXT}”",
                    ),
                    cleanup_dir=row_dir,
                )
            status = RETRY_PENDING_STATUS if retryable else "失败"
            return RowExecutionResult(
                plan=plan,
                outcome=RowOutcome(
                    status=status,
                    attempts=plan.attempt_number,
                    error_message=str(exc),
                    retryable=retryable,
                    anti_crawl_suspected=bool(weak_signal_descriptions),
                    anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                    anti_crawl_signal_descriptions=(*weak_signal_descriptions, *strong_signal_descriptions),
                ),
                cleanup_dir=row_dir,
            )
        except Exception as exc:
            status, retryable = self._unexpected_error_status(plan.attempt_number)
            return RowExecutionResult(
                plan=plan,
                outcome=RowOutcome(
                    status=status,
                    attempts=plan.attempt_number,
                    error_message=self._format_error(exc),
                    retryable=retryable,
                ),
                log_messages=(
                    f"第 {plan.task.csv_row_number} 行处理时出现未预期错误（{exc.__class__.__name__}），按{status}处理",
                ),
                cleanup_dir=row_dir,
            )

    def _run_transcription_stage(
        self,
        prepared: PreparedRow,
        output_columns: Dict[str, str],
        default_model: str,
        job_id: str = "",
    ) -> RowExecutionResult:
        ffmpeg = self.ffmpeg_service_factory()
        transcriber = self._get_thread_transcriber(default_model, job_id=job_id)
        log_messages: List[str] = []

        try:
            self._extract_audio(
                ffmpeg,
                prepared.video_path,
                prepared.audio_path,
                prepared.browser_audio_path,
            )
            transcripts, model_failures, model_logs = self._transcribe_models_worker(
                prepared.plan,
                prepared.audio_path,
                transcriber,
                job_id=job_id,
            )
            log_messages.extend(model_logs)

            if not model_failures:
                return RowExecutionResult(
                    plan=prepared.plan,
                    outcome=RowOutcome(
                        status="成功",
                        attempts=prepared.plan.attempt_number,
                        anti_crawl_strong_suspected=bool(prepared.anti_crawl_signal_descriptions),
                        anti_crawl_signal_descriptions=prepared.anti_crawl_signal_descriptions,
                    ),
                    transcripts=transcripts,
                    log_messages=tuple(log_messages),
                    cleanup_dir=prepared.row_dir,
                )

            # 与串行路径同语义：本轮成功转写模型数 >0 且存在失败 → 部分成功；全部失败 → 失败
            status = "部分成功" if transcripts and model_failures else "失败"
            return RowExecutionResult(
                plan=prepared.plan,
                outcome=RowOutcome(
                    status=status,
                    attempts=prepared.plan.attempt_number,
                    error_message=" | ".join(model_failures),
                    anti_crawl_strong_suspected=bool(prepared.anti_crawl_signal_descriptions),
                    anti_crawl_signal_descriptions=prepared.anti_crawl_signal_descriptions,
                ),
                transcripts=transcripts,
                log_messages=tuple(log_messages),
                cleanup_dir=prepared.row_dir,
            )
        except RowProcessingError as exc:
            return RowExecutionResult(
                plan=prepared.plan,
                outcome=RowOutcome(
                    status="失败",
                    attempts=prepared.plan.attempt_number,
                    error_message=str(exc),
                    anti_crawl_strong_suspected=bool(prepared.anti_crawl_signal_descriptions),
                    anti_crawl_signal_descriptions=prepared.anti_crawl_signal_descriptions,
                ),
                log_messages=(),
                cleanup_dir=prepared.row_dir,
            )
        except Exception as exc:
            status, retryable = self._unexpected_error_status(prepared.plan.attempt_number)
            return RowExecutionResult(
                plan=prepared.plan,
                outcome=RowOutcome(
                    status=status,
                    attempts=prepared.plan.attempt_number,
                    error_message=self._format_error(exc),
                    retryable=retryable,
                    anti_crawl_strong_suspected=bool(prepared.anti_crawl_signal_descriptions),
                    anti_crawl_signal_descriptions=prepared.anti_crawl_signal_descriptions,
                ),
                log_messages=(
                    f"第 {prepared.plan.task.csv_row_number} 行转写时出现未预期错误（{exc.__class__.__name__}），按{status}处理",
                ),
                cleanup_dir=prepared.row_dir,
            )

    def _transcribe_models_worker(
        self,
        plan: RowPlan,
        audio_path: Path,
        transcriber: TranscriptionService,
        *,
        job_id: str = "",
    ) -> tuple[Dict[str, str], List[str], List[str]]:
        transcripts: Dict[str, str] = {}
        failures: List[str] = []
        logs: List[str] = []

        for model_name in plan.pending_models:
            try:
                logs.append(f"第 {plan.task.csv_row_number} 行开始转写：{model_name}")
                with self._transcriber_lock_for(model_name, job_id=job_id):
                    transcript = transcriber.transcribe(audio_path, model_name=model_name)
                transcripts[model_name] = transcript
                logs.append(
                    f"第 {plan.task.csv_row_number} 行转写完成：{model_name}，文本长度 {len(transcript)}"
                )
            except Exception as exc:
                failures.append(f"转写失败: {model_name}: {self._format_error(exc)}")

        return transcripts, failures, logs

    def _handle_row_result(
        self,
        *,
        job_id: str,
        run_state: RunControlState,
        dataframe,
        result: RowExecutionResult,
        output_columns: Dict[str, str],
        output_path: Path,
        retry_queue: List[RowTask],
    ) -> None:
        for model_name, text in result.transcripts.items():
            dataframe.at[result.plan.task.row_index, output_columns[model_name]] = text

        self._set_row_meta(
            dataframe,
            result.plan.task.row_index,
            status=result.outcome.status,
            error_message=result.outcome.error_message,
            attempts=result.outcome.attempts,
        )

        for message in result.log_messages:
            self.manager.append_log(job_id, message)

        signal_suffix = self._format_outcome_signal_suffix(result.outcome)
        if result.outcome.retryable:
            retry_queue.append(result.plan.task)
            self.manager.append_log(
                job_id,
                f"第 {result.plan.task.csv_row_number} 行已加入重试队列：{result.outcome.error_message}{signal_suffix}",
            )
        elif result.outcome.status == "失败":
            self.manager.append_log(
                job_id,
                f"第 {result.plan.task.csv_row_number} 行最终失败：{result.outcome.error_message}{signal_suffix}",
            )
        elif result.outcome.status == "部分成功":
            self.manager.append_log(
                job_id,
                f"第 {result.plan.task.csv_row_number} 行部分完成：{result.outcome.error_message}{signal_suffix}",
            )

        self._record_row_control_signal(
            run_state=run_state,
            task=result.plan.task,
            outcome=result.outcome,
        )

        if result.cleanup_dir is not None:
            shutil.rmtree(result.cleanup_dir, ignore_errors=True)

        self._sync_progress(
            job_id,
            dataframe,
            current_row=result.plan.task.csv_row_number,
            current_url=result.plan.task.url_value,
            refresh_counters=False,
        )

    def _get_thread_transcriber(self, default_model: str, *, job_id: str = "") -> TranscriptionService:
        with self._transcriber_services_lock:
            key = (job_id, default_model)
            service = self._transcriber_services.get(key)
            if service is None:
                service = self.transcription_service_factory(default_model=default_model)
                self._transcriber_services[key] = service
            return service

    def _transcriber_lock_for(self, model_name: str, *, job_id: str = "") -> threading.Lock:
        with self._transcriber_services_lock:
            return self._transcriber_model_locks.setdefault((job_id, model_name), threading.Lock())

    def _clear_job_transcribers(self, job_id: str) -> None:
        with self._transcriber_services_lock:
            for key in [key for key in self._transcriber_services if key[0] == job_id]:
                self._transcriber_services.pop(key, None)
            for key in [key for key in self._transcriber_model_locks if key[0] == job_id]:
                self._transcriber_model_locks.pop(key, None)

    def _prepare_cookie_account_for_batch(self, job_id: str) -> Optional[CookieAccount]:
        if self.cookie_account_manager is None:
            return None

        runtime = self.manager.get_effective_cookie_runtime(job_id)
        current_account_id = str(runtime.get("current_cookie_account_id") or "").strip()
        current_account = self.cookie_account_manager.get_account(current_account_id) if current_account_id else None

        if current_account is not None and (current_account.invalid or not current_account.has_usable_state):
            if not current_account.has_usable_state:
                invalid_reason = "cookies 文件缺失或为空会话"
                try:
                    self.cookie_account_manager.mark_invalid(current_account.account_id, invalid_reason)
                except RuntimeError as exc:
                    # metadata 损坏时保留现场（不静默重置），但剔除标记未写入不应拖垮任务
                    self.manager.append_log(
                        job_id,
                        f"账号 metadata 损坏，剔除标记未能写入：{exc}（可对该账号执行重新登录修复）",
                    )
                self.manager.append_log(
                    job_id,
                    f"cookies `{current_account.label}` 不可用，已从账号池剔除：{invalid_reason}",
                )
            current_account = self._switch_cookie_account(
                job_id,
                reason="当前 cookies 不可用，已自动切换",
                prefer_next=True,
                required=True,
            )

        if runtime.get("cookie_switch_requested"):
            switched = self._switch_cookie_account(
                job_id,
                reason="用户手动切换 cookies",
                prefer_next=True,
                required=False,
            )
            if switched is None:
                self._set_cookie_runtime(job_id, cookie_switch_requested=False)
                self.manager.append_log(job_id, "当前只有 1 份可用 cookies，无法切换")
            current_account = switched

        if current_account is None:
            current_account = self.cookie_account_manager.select_current_or_first_available_account(
                current_account_id=current_account_id
            )
            if current_account is None:
                raise RuntimeError('没有可用的 cookies。请先运行 `./login "备注"` 添加账号，或恢复/重新登录已剔除账号。')
            self._apply_cookie_account(job_id, current_account, reason="已装载 cookies 账号")

        return current_account

    def _switch_cookie_for_retry_round(self, job_id: str, round_index: int) -> None:
        if self.cookie_account_manager is None:
            return

        runtime = self.manager.get_effective_cookie_runtime(job_id)
        current_account_id = str(runtime.get("current_cookie_account_id") or "").strip()
        if not current_account_id:
            self._prepare_cookie_account_for_batch(job_id)
            current_account_id = str(
                self.manager.get_effective_cookie_runtime(job_id).get("current_cookie_account_id") or ""
            ).strip()

        switched = self._switch_cookie_account(
            job_id,
            reason=f"开始第 {round_index} 轮重试，切换 cookies",
            prefer_next=True,
            required=False,
        )
        if switched is None:
            self.manager.append_log(job_id, f"第 {round_index} 轮重试开始前没有其他 cookies 可切换，继续使用当前账号")

    def _finalize_cookie_rotation(
        self,
        *,
        job_id: str,
        run_state: RunControlState,
        phase: str,
        batch_index: int,
        total_batches: int,
    ) -> None:
        if self.cookie_account_manager is None:
            self._reset_current_batch_cookie_flags(run_state)
            return

        runtime = self.manager.get_effective_cookie_runtime(job_id)
        current_account_id = str(runtime.get("current_cookie_account_id") or "").strip()
        current_account = self.cookie_account_manager.get_account(current_account_id) if current_account_id else None
        phase_key = "retry" if phase.startswith("重试") else "normal"
        strong_signal_threshold = (
            self.cookie_retry_strong_signal_switch_threshold
            if phase_key == "retry"
            else self.cookie_strong_signal_switch_threshold
        )
        if current_account is not None:
            try:
                self.cookie_account_manager.mark_used(current_account.account_id)
            except RuntimeError as exc:
                self.manager.append_log(
                    job_id,
                    f"账号 metadata 损坏，使用记录未能写入：{exc}（请重新登录修复）",
                )

        total_batches_processed = int(runtime.get("cookie_total_batches_processed") or 0) + 1
        batches_since_switch = int(runtime.get("cookie_batches_since_switch") or 0) + 1
        self._set_cookie_runtime(
            job_id,
            cookie_total_batches_processed=total_batches_processed,
            cookie_batches_since_switch=batches_since_switch,
        )

        switch_reason = ""
        if run_state.current_batch_strong_issue:
            if run_state.strong_signal_phase != phase_key:
                run_state.strong_signal_phase = phase_key
                run_state.strong_signal_batch_streak = 0
            run_state.strong_signal_batch_streak += 1
        else:
            run_state.strong_signal_phase = phase_key
            run_state.strong_signal_batch_streak = 0

        if current_account is not None and run_state.current_batch_strong_issue:
            signal_text = "；".join(run_state.current_batch_strong_signal_descriptions[:3]) or "403/429/验证码"
            if run_state.strong_signal_batch_streak >= strong_signal_threshold:
                try:
                    updated_account = self.cookie_account_manager.note_issue(current_account.account_id, signal_text)
                except RuntimeError as exc:
                    # metadata 损坏时保留现场，本次风控计数跳过但任务继续
                    updated_account = None
                    self.manager.append_log(
                        job_id,
                        f"账号 metadata 损坏，本次强信号计数未能写入：{exc}（可对该账号执行重新登录修复）",
                    )
                challenge_strikes = updated_account.challenge_strikes if updated_account is not None else 0
                if challenge_strikes >= self.cookie_account_manager.invalidation_strike_threshold:
                    if current_account.warning:
                        # 账号此前已处于预警状态，不重复 mark_warning，也不重复记“已进入预警状态”日志
                        self.manager.append_log(
                            job_id,
                            f"cookies `{current_account.label}` 维持预警状态：已累计 {challenge_strikes} 次强信号 strike",
                        )
                    else:
                        warning_reason = (
                            f"连续 {challenge_strikes} 次达到强信号预警阈值：{signal_text}"
                        )
                        try:
                            warned_account = self.cookie_account_manager.mark_warning(
                                current_account.account_id,
                                warning_reason,
                            )
                        except RuntimeError as exc:
                            warned_account = None
                            self.manager.append_log(
                                job_id,
                                f"账号 metadata 损坏，预警状态未能写入：{exc}",
                            )
                        if warned_account is not None:
                            self.manager.append_log(
                                job_id,
                                f"cookies `{warned_account.label}` 已进入预警状态：{warning_reason}",
                            )
                else:
                    self.manager.append_log(
                        job_id,
                        f"cookies `{current_account.label}` 已累计 {challenge_strikes} 次强信号 strike，继续使用当前账号",
                    )
                run_state.strong_signal_batch_streak = 0
        elif current_account is not None:
            try:
                self.cookie_account_manager.mark_success(current_account.account_id)
            except RuntimeError as exc:
                self.manager.append_log(
                    job_id,
                    f"账号 metadata 损坏，成功计数未能写入：{exc}",
                )

        if not switch_reason and runtime.get("cookie_switch_requested"):
            switch_reason = "用户手动切换 cookies"

        if not switch_reason and batches_since_switch >= self.cookie_switch_batch_threshold:
            switch_reason = f"已连续处理 {batches_since_switch} 批，自动切换 cookies"

        if switch_reason:
            switched = self._switch_cookie_account(
                job_id,
                reason=switch_reason,
                prefer_next=True,
                required=False,
            )
            if switched is None and runtime.get("cookie_switch_requested"):
                self._set_cookie_runtime(job_id, cookie_switch_requested=False, cookie_batches_since_switch=0)
                self.manager.append_log(job_id, "当前只有 1 份可用 cookies，继续使用当前账号")
            elif switched is None and batches_since_switch >= self.cookie_switch_batch_threshold:
                self._set_cookie_runtime(job_id, cookie_batches_since_switch=0)
                self.manager.append_log(job_id, "账号池中没有可切换的其他 cookies，已继续使用当前账号")

        self._reset_current_batch_cookie_flags(run_state)

    def _reset_current_batch_cookie_flags(self, run_state: RunControlState) -> None:
        run_state.current_batch_strong_issue = False
        run_state.current_batch_strong_signal_descriptions.clear()

    def _set_cookie_runtime(self, job_id: str, **fields) -> None:
        job = self.manager.require(job_id)
        if job.batch_id:
            self.manager.update_batch_cookie_runtime(job.batch_id, **fields)
        else:
            self.manager.update_job_cookie_runtime(job_id, **fields)

    def _apply_cookie_account(self, job_id: str, account: CookieAccount, *, reason: str) -> CookieAccount:
        self.manager.advance_cookie_generation(job_id)
        self._set_cookie_runtime(
            job_id,
            current_cookie_account_id=account.account_id,
            current_cookie_label=account.label,
            cookie_batches_since_switch=0,
            cookie_switch_requested=False,
            cookie_last_switch_reason=reason,
        )
        self._close_job_douyin_services(job_id)
        self.manager.append_log(job_id, f"当前 cookies 已切换到 `{account.label}`：{reason}")
        return account

    def _switch_cookie_account(
        self,
        job_id: str,
        *,
        reason: str,
        prefer_next: bool,
        required: bool,
    ) -> Optional[CookieAccount]:
        if self.cookie_account_manager is None:
            return None

        runtime = self.manager.get_effective_cookie_runtime(job_id)
        current_account_id = str(runtime.get("current_cookie_account_id") or "").strip()
        account = (
            self.cookie_account_manager.select_next_available_account(
                current_account_id=current_account_id,
                allow_same=not prefer_next,
            )
            if prefer_next
            else self.cookie_account_manager.select_current_or_first_available_account(
                current_account_id=current_account_id
            )
        )
        if account is None:
            if required:
                raise RuntimeError('当前 cookies 已不可用，且账号池中没有其他可用 cookies。请运行 `./login "备注"` 重新登录。')
            return None
        return self._apply_cookie_account(job_id, account, reason=reason)

    def _get_thread_douyin_service(self, job_id: str) -> DouyinService:
        service = getattr(self._douyin_local, "service", None)
        service_job_id = getattr(self._douyin_local, "job_id", None)
        runtime = self.manager.get_effective_cookie_runtime(job_id)
        desired_generation = int(runtime.get("cookie_service_generation") or 0)
        desired_account_id = str(runtime.get("current_cookie_account_id") or "").strip()
        service_generation = getattr(self._douyin_local, "cookie_generation", None)
        service_account_id = getattr(self._douyin_local, "cookie_account_id", "")
        if (
            service is None
            or service_job_id != job_id
            or service_generation != desired_generation
            or service_account_id != desired_account_id
        ):
            if service is not None:
                try:
                    service.close()
                except Exception:
                    pass
            account = None
            if self.cookie_account_manager is not None and desired_account_id:
                account = self.cookie_account_manager.get_account(desired_account_id)
            service = self._create_douyin_service(account=account)
            self._douyin_local.service = service
            self._douyin_local.job_id = job_id
            self._douyin_local.cookie_generation = desired_generation
            self._douyin_local.cookie_account_id = desired_account_id
            self._register_douyin_service(job_id, service)
        return service

    def _create_douyin_service(self, *, account: Optional[CookieAccount] = None):
        service = self.douyin_service_factory()
        if hasattr(service, "request_coordinator"):
            service.request_coordinator = self._request_coordinator
            existing_resolver = getattr(service, "browser_resolver", None)
            if existing_resolver is not None:
                existing_resolver.request_coordinator = self._request_coordinator
        if account is not None and hasattr(service, "request_coordinator"):
            existing_resolver = getattr(service, "browser_resolver", None)
            if existing_resolver is not None:
                try:
                    existing_resolver.close()
                except Exception:
                    pass
            try:
                service.browser_resolver = PlaywrightFallback(
                    storage_state_path=account.storage_state_path,
                    request_coordinator=getattr(service, "request_coordinator", None),
                )
            except Exception:
                pass

        setattr(service, "cookie_account_id", account.account_id if account is not None else "")
        setattr(service, "cookie_account_label", account.label if account is not None else "")
        return service

    def _register_douyin_service(self, job_id: str, service: DouyinService) -> None:
        with self._douyin_services_lock:
            services = self._douyin_services_by_job.setdefault(job_id, [])
            services.append(service)

    def _close_job_douyin_services(self, job_id: str) -> None:
        with self._douyin_services_lock:
            services = self._douyin_services_by_job.pop(job_id, [])
        for service in services:
            try:
                service.close()
            except Exception:
                pass
        if getattr(self._douyin_local, "job_id", None) == job_id:
            self._douyin_local.service = None
            self._douyin_local.job_id = None
            self._douyin_local.cookie_generation = None
            self._douyin_local.cookie_account_id = ""

    def _record_row_control_signal(
        self,
        *,
        run_state: RunControlState,
        task: RowTask,
        outcome: RowOutcome,
    ) -> None:
        if outcome.anti_crawl_strong_suspected:
            run_state.current_batch_strong_issue = True
            for description in outcome.anti_crawl_signal_descriptions:
                self._append_unique_text(run_state.current_batch_strong_signal_descriptions, description)

        if outcome.resets_anti_crawl_state:
            self._reset_anti_crawl_tracking(run_state)
            return

        if outcome.status == "已删除":
            return

        if self._should_trigger_anti_crawl_cooldown(outcome):
            run_state.current_batch_had_issue = True
            if run_state.pending_cooldown is not None:
                self._append_unique_task(run_state.pending_cooldown.affected_tasks, task)
                for description in outcome.anti_crawl_signal_descriptions:
                    self._append_unique_text(run_state.pending_cooldown.signal_descriptions, description)
                run_state.pending_cooldown.reason = self._build_anti_crawl_cooldown_reason(
                    count=len(run_state.pending_cooldown.affected_tasks),
                    seconds=run_state.pending_cooldown.seconds,
                    signal_descriptions=run_state.pending_cooldown.signal_descriptions,
                    threshold=run_state.pending_cooldown.threshold,
                )
                return

            self._append_unique_task(run_state.suspicious_tasks, task)
            for description in outcome.anti_crawl_signal_descriptions:
                self._append_unique_text(run_state.suspicious_signal_descriptions, description)
            if len(run_state.suspicious_tasks) >= run_state.anti_crawl_streak_threshold:
                run_state.pending_cooldown = CooldownRequest(
                    seconds=run_state.anti_crawl_cooldown_seconds,
                    reason=self._build_anti_crawl_cooldown_reason(
                        count=len(run_state.suspicious_tasks),
                        seconds=run_state.anti_crawl_cooldown_seconds,
                        signal_descriptions=run_state.suspicious_signal_descriptions,
                        threshold=run_state.anti_crawl_streak_threshold,
                    ),
                    threshold=run_state.anti_crawl_streak_threshold,
                    affected_tasks=list(run_state.suspicious_tasks),
                    signal_descriptions=list(run_state.suspicious_signal_descriptions),
                )
            return

        if outcome.status == "成功" and run_state.pending_cooldown is None:
            self._reset_anti_crawl_tracking(run_state)

        if outcome.status in {RETRY_PENDING_STATUS, "失败", "部分成功"}:
            run_state.current_batch_had_issue = True

    @staticmethod
    def _should_trigger_anti_crawl_cooldown(outcome: RowOutcome) -> bool:
        # Only weak signals participate in cooldown. Strong signals remain useful
        # for logs, but they are too noisy in real runs to drive the stop logic.
        return outcome.anti_crawl_suspected and outcome.attempts <= 2

    def _finalize_batch(
        self,
        *,
        job_id: str,
        run_state: RunControlState,
        dataframe,
        output_columns: Dict[str, str],
        output_path: Path,
        retry_queue: List[RowTask],
        allow_proactive_cooldown: bool,
    ) -> bool:
        if self.manager.is_pause_requested(job_id):
            return False

        if run_state.pending_cooldown is not None:
            self._perform_cooldown(
                job_id=job_id,
                run_state=run_state,
                dataframe=dataframe,
                output_columns=output_columns,
                output_path=output_path,
                retry_queue=retry_queue,
            )
            return True

        if run_state.current_batch_had_issue:
            run_state.healthy_batch_streak = 0
            run_state.current_batch_had_issue = False
            return False

        run_state.healthy_batch_streak += 1
        if allow_proactive_cooldown and run_state.healthy_batch_streak >= run_state.proactive_cooldown_batch_threshold:
            run_state.pending_cooldown = CooldownRequest(
                seconds=run_state.proactive_cooldown_seconds,
                reason=(
                    f"连续顺利完成 {run_state.healthy_batch_streak} 批，"
                    f"主动冷却 {self._format_duration(run_state.proactive_cooldown_seconds)} 后继续"
                ),
            )
            self._perform_cooldown(
                job_id=job_id,
                run_state=run_state,
                dataframe=dataframe,
                output_columns=output_columns,
                output_path=output_path,
                retry_queue=retry_queue,
            )
            return True

        run_state.current_batch_had_issue = False
        return False

    def _perform_cooldown(
        self,
        *,
        job_id: str,
        run_state: RunControlState,
        dataframe,
        output_columns: Dict[str, str],
        output_path: Path,
        retry_queue: List[RowTask],
    ) -> None:
        request = run_state.pending_cooldown
        if request is None:
            return

        self.manager.append_log(job_id, request.reason)
        self._persist_snapshot(job_id, dataframe, output_path)
        self._sleep_with_cooldown_message(job_id, request.seconds, request.reason)
        self._sync_progress(job_id, dataframe, current_message="任务执行中")

        self._reset_anti_crawl_tracking(run_state)
        run_state.current_batch_had_issue = False
        run_state.healthy_batch_streak = 0

    @staticmethod
    def _reset_anti_crawl_tracking(run_state: RunControlState) -> None:
        run_state.pending_cooldown = None
        run_state.suspicious_tasks.clear()
        run_state.suspicious_signal_descriptions.clear()

    def _sleep_with_cooldown_message(self, job_id: str, seconds: int, reason: str) -> None:
        remaining = max(0, int(seconds))
        if remaining <= 0:
            return

        job = self.manager.require(job_id)
        cooldown_ends_at = datetime.now(timezone.utc) + timedelta(seconds=remaining)
        self.manager.set_cooldown_state(job_id, active=True, ends_at=cooldown_ends_at)
        if job.batch_id:
            self.manager.set_batch_cooldown_state(job.batch_id, active=True, ends_at=cooldown_ends_at)

        skipped = False
        while remaining > 0 and not self.manager.is_pause_requested(job_id):
            if self.manager.is_skip_cooldown_requested(job_id):
                skipped = True
                break
            if job.batch_id and self.manager.is_batch_skip_cooldown_requested(job.batch_id):
                skipped = True
                break
            message = f"{reason}，剩余 {self._format_duration(remaining)}"
            self._update_runtime_message(job_id, message)
            sleep_window = min(60, remaining)
            self.sleep_fn(sleep_window)
            remaining -= sleep_window

        self.manager.set_cooldown_state(job_id, active=False)
        if job.batch_id:
            self.manager.set_batch_cooldown_state(job.batch_id, active=False)

        if skipped:
            self.manager.append_log(job_id, "已手动跳过当前冷却，继续执行")
            self._update_runtime_message(job_id, "任务执行中")

    def _update_runtime_message(self, job_id: str, message: str) -> None:
        job = self.manager.require(job_id)
        self.manager.update_progress(job_id, current_message=message)
        if job.batch_id:
            self.manager.update_batch_progress(job.batch_id, current_message=message)

    @staticmethod
    def _append_unique_task(tasks: List[RowTask], task: RowTask) -> None:
        if any(existing.row_index == task.row_index for existing in tasks):
            return
        tasks.append(task)

    @staticmethod
    def _append_unique_text(items: List[str], value: str) -> None:
        normalized = str(value or "").strip()
        if not normalized or normalized in items:
            return
        items.append(normalized)

    @staticmethod
    def _format_signal_description(level: str, signal: str) -> str:
        return f"{level}冷却信号：{signal}"

    @staticmethod
    def _format_outcome_signal_suffix(outcome: RowOutcome) -> str:
        if not outcome.anti_crawl_signal_descriptions:
            return ""
        return " | " + "；".join(outcome.anti_crawl_signal_descriptions)

    def _build_anti_crawl_cooldown_reason(
        self,
        *,
        count: int,
        seconds: int,
        signal_descriptions: Sequence[str],
        threshold: int,
    ) -> str:
        prefix = f"已达到冷却阈值 {threshold} 条"
        if count > threshold:
            prefix += f"，冷却前累计到 {count} 条冷却信号"
        else:
            prefix += f"，当前累计 {count} 条冷却信号"

        if signal_descriptions:
            signal_text = "；".join(signal_descriptions)
            return (
                f"{prefix}（{signal_text}），"
                f"自动冷却 {self._format_duration(seconds)} 后继续"
            )
        return (
            f"{prefix}，"
            f"自动冷却 {self._format_duration(seconds)} 后继续"
        )

    @staticmethod
    def _format_duration(seconds: int) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        parts = []
        if hours:
            parts.append(f"{hours} 小时")
        if minutes:
            parts.append(f"{minutes} 分钟")
        if not parts:
            parts.append(f"{total_seconds} 秒")
        return "".join(parts)

    @staticmethod
    def _is_suspected_anti_crawl_parse_failure(exc: RowProcessingError) -> bool:
        if exc.stage != "解析":
            return False

        detail = exc.detail or ""
        if VIDEO_DETAIL_MISSING_ERROR in detail:
            return False

        has_router_data_failure = (
            ROUTER_DATA_MISSING_ERROR in detail
            and (ITEMINFO_JSON_ERROR in detail or DETAIL_JSON_ERROR in detail)
        )
        has_browser_capture_failure = (
            PREVIEW_ONLY_DELETE_ERROR in detail
            or BROWSER_REAL_VIDEO_ADDRESS_MISSING_ERROR in detail
        )
        has_network_failure = (
            DNS_LOOKUP_ERROR in detail
            and (BROWSER_DISCONNECTED_ERROR in detail or BROWSER_NAV_TIMEOUT_ERROR in detail)
        ) or (
            SSL_CERT_VERIFY_ERROR in detail and BROWSER_DISCONNECTED_ERROR in detail
        ) or (
            (
                "Connection refused" in detail
                or ACTIVE_REFUSED_ERROR in detail
            )
            and (
                BROWSER_REAL_VIDEO_ADDRESS_MISSING_ERROR in detail or "短链解析失败" in detail
            )
        )

        return has_router_data_failure or has_browser_capture_failure or has_network_failure

    def _weak_signal_descriptions_for_error(self, exc: RowProcessingError) -> Tuple[str, ...]:
        if not self._is_suspected_anti_crawl_parse_failure(exc):
            return ()
        return (self._format_signal_description("弱", WEAK_SIGNAL_PARSE_FALLBACK),)

    @staticmethod
    def _reset_attempt_anti_crawl_signals(douyin: DouyinService) -> None:
        reset = getattr(douyin, "reset_attempt_signals", None)
        if callable(reset):
            try:
                reset()
            except Exception:
                return

    def _consume_attempt_challenge_signal(self, douyin: DouyinService) -> bool:
        return bool(self._consume_attempt_challenge_signal_descriptions(douyin))

    def _consume_attempt_challenge_signal_descriptions(self, douyin: DouyinService) -> Tuple[str, ...]:
        consume_reasons = getattr(douyin, "consume_attempt_challenge_signal_reasons", None)
        if callable(consume_reasons):
            try:
                reasons = tuple(str(reason or "").strip() for reason in consume_reasons())
            except Exception:
                reasons = ()
            filtered = [reason for reason in reasons if reason]
            if filtered:
                return tuple(
                    self._format_signal_description("强", reason)
                    for reason in filtered
                )

        consume = getattr(douyin, "consume_attempt_challenge_signal", None)
        if callable(consume):
            try:
                observed = bool(consume())
            except Exception:
                observed = False
            if observed:
                return (self._format_signal_description("强", STRONG_SIGNAL_CHALLENGE_FALLBACK),)
        return ()

    @staticmethod
    def _should_parallelize_batch(tasks: Sequence[RowTask], download_workers: int, asr_workers: int) -> bool:
        return len(tasks) > 1 and (download_workers > 1 or asr_workers > 1)

    def _process_task(
        self,
        *,
        job_id: str,
        run_state: RunControlState,
        dataframe,
        task: RowTask,
        output_columns: Dict[str, str],
        overwrite: bool,
        workspace: Path,
        ffmpeg: FfmpegService,
        transcriber: TranscriptionService,
        output_path: Path,
        retry_queue: List[RowTask],
        download_only: bool = False,
    ) -> None:
        self.manager.update_progress(
            job_id,
            current_row=task.csv_row_number,
            current_url=task.url_value,
            current_message=f"正在处理第 {task.csv_row_number} 行",
        )

        outcome = self._process_task_once(
            job_id=job_id,
            dataframe=dataframe,
            task=task,
            output_columns=output_columns,
            overwrite=overwrite,
            workspace=workspace,
            ffmpeg=ffmpeg,
            transcriber=transcriber,
            download_only=download_only,
        )

        self._set_row_meta(
            dataframe,
            task.row_index,
            status=outcome.status,
            error_message=outcome.error_message,
            attempts=outcome.attempts,
        )

        signal_suffix = self._format_outcome_signal_suffix(outcome)
        if outcome.retryable:
            retry_queue.append(task)
            self.manager.append_log(
                job_id,
                f"第 {task.csv_row_number} 行已加入重试队列：{outcome.error_message}{signal_suffix}",
            )
        elif outcome.status == "失败":
            self.manager.append_log(job_id, f"第 {task.csv_row_number} 行最终失败：{outcome.error_message}{signal_suffix}")
        elif outcome.status == "部分成功":
            self.manager.append_log(job_id, f"第 {task.csv_row_number} 行部分完成：{outcome.error_message}{signal_suffix}")

        self._record_row_control_signal(
            run_state=run_state,
            task=task,
            outcome=outcome,
        )

        self._sync_progress(
            job_id,
            dataframe,
            current_row=task.csv_row_number,
            current_url=task.url_value,
            refresh_counters=False,
        )

    def _process_task_once(
        self,
        *,
        job_id: str,
        dataframe,
        task: RowTask,
        output_columns: Dict[str, str],
        overwrite: bool,
        workspace: Path,
        ffmpeg: FfmpegService,
        transcriber: TranscriptionService,
        download_only: bool = False,
    ) -> RowOutcome:
        douyin = self._get_thread_douyin_service(job_id)
        job = self.manager.require(job_id)
        current_attempts = self._get_attempts(dataframe, task.row_index)
        pending_models = self._pending_work_for_row(dataframe, task.row_index, output_columns, overwrite, download_only)

        if not task.url_value:
            self.manager.append_log(job_id, f"第 {task.csv_row_number} 行跳过：URL 为空")
            return RowOutcome(status="跳过", attempts=current_attempts, error_message="URL 为空")

        if not pending_models:
            skip_reason = "该行已处理过" if download_only else "目标文本列已有内容"
            self.manager.append_log(job_id, f"第 {task.csv_row_number} 行跳过：{skip_reason}")
            return RowOutcome(status="跳过", attempts=current_attempts, error_message=skip_reason)

        attempt_number = current_attempts + 1
        row_dir = workspace / f"row_{task.position:05d}"
        video_path = row_dir / "source.mp4"
        browser_audio_path = row_dir / "fallback_audio.m4a"
        audio_path = row_dir / "audio.wav"

        try:
            self._reset_attempt_anti_crawl_signals(douyin)
            self._reset_attempt_files(row_dir, video_path, browser_audio_path, audio_path)
            video = self._parse_video(douyin, task.url_value)
            self.manager.append_log(
                job_id,
                f"第 {task.csv_row_number} 行解析成功（{video.resolved_by}）：{video.title}",
            )

            if video.is_deleted:
                strong_signal_descriptions = self._consume_attempt_challenge_signal_descriptions(douyin)
                for column_name in output_columns.values():
                    if overwrite or not stringify_cell(dataframe.at[task.row_index, column_name]).strip():
                        dataframe.at[task.row_index, column_name] = DELETED_TRANSCRIPT_TEXT
                self.manager.append_log(
                    job_id,
                    f"第 {task.csv_row_number} 行已删除，已写入“{DELETED_TRANSCRIPT_TEXT}”",
                )
                return RowOutcome(
                    status="已删除",
                    attempts=attempt_number,
                    resets_anti_crawl_state=True,
                    anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                    anti_crawl_signal_descriptions=strong_signal_descriptions,
                )

            download_result = self._download_video(douyin, video, video_path, browser_audio_path)
            self.manager.append_log(
                job_id,
                f"第 {task.csv_row_number} 行下载完成：{download_result.strategy}",
            )
            if download_result.audio_source_url:
                self.manager.append_log(
                    job_id,
                    f"第 {task.csv_row_number} 行已捕获补充音频流",
                )
            strong_signal_descriptions = self._consume_attempt_challenge_signal_descriptions(douyin)
            if download_only:
                video_root = self._video_output_root(job)
                saved_path = self._save_download_only_video(job, task, video, video_path, browser_audio_path, video_root)
                self.manager.append_log(
                    job_id,
                    f"第 {task.csv_row_number} 行视频已保存：{saved_path.relative_to(video_root)}",
                )
                return RowOutcome(
                    status="成功",
                    attempts=attempt_number,
                    anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                    anti_crawl_signal_descriptions=strong_signal_descriptions,
                )
            self._extract_audio(ffmpeg, video_path, audio_path, browser_audio_path)
            model_failures = self._transcribe_models(
                job_id=job_id,
                csv_row_number=task.csv_row_number,
                audio_path=audio_path,
                dataframe=dataframe,
                row_index=task.row_index,
                output_columns=output_columns,
                pending_models=pending_models,
                transcriber=transcriber,
            )
            strong_signal_descriptions = strong_signal_descriptions or self._consume_attempt_challenge_signal_descriptions(douyin)
            if not model_failures:
                return RowOutcome(
                    status="成功",
                    attempts=attempt_number,
                    anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                    anti_crawl_signal_descriptions=strong_signal_descriptions,
                )

            # 与并行路径同语义：本轮成功转写模型数 >0 且存在失败 → 部分成功；全部失败 → 失败。
            # 不再用“剩余空列数”判断，避免 overwrite=True 时把已成功的行误判为失败。
            succeeded_models = len(pending_models) - len(model_failures)
            status = "部分成功" if succeeded_models > 0 and model_failures else "失败"
            return RowOutcome(
                status=status,
                attempts=attempt_number,
                error_message=" | ".join(model_failures),
                anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                anti_crawl_signal_descriptions=strong_signal_descriptions,
            )
        except RowProcessingError as exc:
            if self._is_non_video_parse_error(exc):
                self.manager.append_log(
                    job_id,
                    f"第 {task.csv_row_number} 行识别为图文 note，按“{NON_VIDEO_STATUS}”跳过",
                )
                return RowOutcome(
                    status=NON_VIDEO_STATUS,
                    attempts=attempt_number,
                    error_message=NON_VIDEO_ERROR,
                )
            if self._should_mark_non_video_from_download_error(exc, attempt_number):
                self.manager.append_log(
                    job_id,
                    f"第 {task.csv_row_number} 行连续命中图文型 404 下载错误，按“{NON_VIDEO_STATUS}”处理",
                )
                return RowOutcome(
                    status=NON_VIDEO_STATUS,
                    attempts=attempt_number,
                    error_message=NON_VIDEO_DOWNLOAD_404_ERROR,
                )
            if self._should_mark_download_unavailable(exc, attempt_number):
                self.manager.append_log(
                    job_id,
                    f"第 {task.csv_row_number} 行连续命中 403 下载限制，按“{DOWNLOAD_UNAVAILABLE_STATUS}”处理",
                )
                return RowOutcome(
                    status=DOWNLOAD_UNAVAILABLE_STATUS,
                    attempts=attempt_number,
                    error_message=str(exc),
                )
            delete_reason = self._deleted_reason_for_error(exc, attempt_number)
            if delete_reason:
                self._consume_attempt_challenge_signal_descriptions(douyin)
                for column_name in output_columns.values():
                    if overwrite or not stringify_cell(dataframe.at[task.row_index, column_name]).strip():
                        dataframe.at[task.row_index, column_name] = DELETED_TRANSCRIPT_TEXT
                self.manager.append_log(
                    job_id,
                    f"第 {task.csv_row_number} 行{delete_reason}，按已删除处理，已写入“{DELETED_TRANSCRIPT_TEXT}”",
                )
                return RowOutcome(
                    status="已删除",
                    attempts=attempt_number,
                    resets_anti_crawl_state=(delete_reason == "分享页未找到视频详情"),
                )
            retryable = self._is_retryable(exc, attempt_number)
            weak_signal_descriptions = self._weak_signal_descriptions_for_error(exc)
            strong_signal_descriptions = self._consume_attempt_challenge_signal_descriptions(douyin)
            if self._should_mark_deleted_after_retry_ceiling(exc, attempt_number):
                for column_name in output_columns.values():
                    if overwrite or not stringify_cell(dataframe.at[task.row_index, column_name]).strip():
                        dataframe.at[task.row_index, column_name] = DELETED_TRANSCRIPT_TEXT
                self.manager.append_log(
                    job_id,
                    f"第 {task.csv_row_number} 行重试超过 {self.max_retries} 次仍未成功，按已删除处理，已写入“{DELETED_TRANSCRIPT_TEXT}”",
                )
                return RowOutcome(
                    status="已删除",
                    attempts=attempt_number,
                    anti_crawl_suspected=bool(weak_signal_descriptions),
                    anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                    anti_crawl_signal_descriptions=(*weak_signal_descriptions, *strong_signal_descriptions),
                )
            status = RETRY_PENDING_STATUS if retryable else "失败"
            return RowOutcome(
                status=status,
                attempts=attempt_number,
                error_message=str(exc),
                retryable=retryable,
                anti_crawl_suspected=bool(weak_signal_descriptions),
                anti_crawl_strong_suspected=bool(strong_signal_descriptions),
                anti_crawl_signal_descriptions=(*weak_signal_descriptions, *strong_signal_descriptions),
            )
        except Exception as exc:
            # 行执行期未预期异常（service 构建期异常在 try 之外，仍按任务失败上抛）
            status, retryable = self._unexpected_error_status(attempt_number)
            self.manager.append_log(
                job_id,
                f"第 {task.csv_row_number} 行处理时出现未预期错误（{exc.__class__.__name__}），按{status}处理",
            )
            return RowOutcome(
                status=status,
                attempts=attempt_number,
                error_message=self._format_error(exc),
                retryable=retryable,
            )
        finally:
            shutil.rmtree(row_dir, ignore_errors=True)

    def _transcribe_models(
        self,
        *,
        job_id: str,
        csv_row_number: int,
        audio_path: Path,
        dataframe,
        row_index,
        output_columns: Dict[str, str],
        pending_models: List[str],
        transcriber: TranscriptionService,
    ) -> List[str]:
        model_failures = []
        for model_name in pending_models:
            column_name = output_columns[model_name]
            try:
                self.manager.append_log(job_id, f"第 {csv_row_number} 行开始转写：{model_name}")
                transcript = transcriber.transcribe(audio_path, model_name=model_name)
                dataframe.at[row_index, column_name] = transcript
                self.manager.append_log(
                    job_id,
                    f"第 {csv_row_number} 行转写完成：{column_name}，文本长度 {len(transcript)}",
                )
            except Exception as exc:
                model_failures.append(f"转写失败: {model_name}: {self._format_error(exc)}")
        return model_failures

    @staticmethod
    def _build_output_columns(
        transcript_column: str,
        model_names: List[str],
        transcriber: TranscriptionService,
    ) -> Dict[str, str]:
        if len(model_names) == 1:
            return {model_names[0]: transcript_column}

        # 不同模型的别名可能相同（如不同仓库下的同名模型），列名冲突时自动加 _2/_3 后缀
        output_columns: Dict[str, str] = {}
        used_columns: set = set()
        for model_name in model_names:
            base_column = f"{transcript_column}__{transcriber.model_alias(model_name)}"
            column_name = base_column
            suffix = 2
            while column_name in used_columns:
                column_name = f"{base_column}_{suffix}"
                suffix += 1
            used_columns.add(column_name)
            output_columns[model_name] = column_name
        return output_columns

    @staticmethod
    def _set_row_meta(dataframe, row_index, *, status: str, error_message: str, attempts: int) -> None:
        dataframe.at[row_index, STATUS_COLUMN] = status
        dataframe.at[row_index, ERROR_COLUMN] = error_message
        dataframe.at[row_index, ATTEMPTS_COLUMN] = str(attempts)

    @staticmethod
    def _pending_models_for_row(dataframe, row_index, output_columns: Dict[str, str], overwrite: bool) -> List[str]:
        return [
            model_name
            for model_name, column_name in output_columns.items()
            if overwrite or not stringify_cell(dataframe.at[row_index, column_name]).strip()
        ]

    def _is_download_only(self, job_id: str) -> bool:
        return bool(getattr(self.manager.require(job_id), "download_only", False))

    @staticmethod
    def _video_output_root(job) -> Path:
        custom_dir = str(getattr(job, "video_output_dir", "") or "").strip()
        return Path(custom_dir).expanduser() if custom_dir else VIDEO_OUTPUT_DIR

    @staticmethod
    def _ensure_video_output_root_writable(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink()

    def _pending_work_for_row(
        self,
        dataframe,
        row_index,
        output_columns: Dict[str, str],
        overwrite: bool,
        download_only: bool,
    ) -> List[str]:
        if not download_only:
            return self._pending_models_for_row(dataframe, row_index, output_columns, overwrite)
        status = stringify_cell(dataframe.at[row_index, STATUS_COLUMN]).strip()
        if status in TERMINAL_STATUSES:
            return []
        return [DOWNLOAD_ONLY_MODEL]

    @staticmethod
    def _save_download_only_video(job, task: RowTask, video: ParsedVideo, video_path: Path, browser_audio_path: Path, root: Path) -> Path:
        target_dir = root / Path(job.original_filename).stem
        target_dir.mkdir(parents=True, exist_ok=True)

        source_path = None
        suffix = DOWNLOAD_ONLY_VIDEO_SUFFIX
        if video_path.exists():
            source_path = video_path
        elif browser_audio_path.exists():
            source_path = browser_audio_path
            suffix = DOWNLOAD_ONLY_AUDIO_SUFFIX

        # 分流视频可能没合并上音轨（合并失败 / ffmpeg 不可用 / 直连接口只回了视频流），
        # 一律 probe 音轨而不是只看 sidecar 是否存在：确认拿到无声视频且连 sidecar
        # 都没有时按“无音轨”抛错，交给上层 NO_AUDIO 重试分类，避免产出静音 mp4 却判成功。
        if source_path == video_path:
            sidecar_available = browser_audio_path.exists() and browser_audio_path.stat().st_size > 0
            try:
                streams = FfmpegService().probe_media(video_path).get("streams") or []
                has_video_stream = any(stream.get("codec_type") == "video" for stream in streams)
                has_audio_stream = any(stream.get("codec_type") == "audio" for stream in streams)
                probe_ok = True
            except Exception:
                # probe 不可用（如无 ffmpeg）时不臆断，退回旧行为
                has_video_stream = False
                has_audio_stream = False
                probe_ok = False
            if probe_ok and has_video_stream and not has_audio_stream and not sidecar_available:
                raise RowProcessingError("抽音频", NO_AUDIO_ERROR)
            if (not probe_ok or not (has_video_stream and has_audio_stream)) and sidecar_available:
                source_path = browser_audio_path
                suffix = DOWNLOAD_ONLY_AUDIO_SUFFIX

        if source_path is None:
            raise RowProcessingError("下载", "下载完成但没有找到可保存的媒体文件")

        safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", video.title)
        safe_title = re.sub(r"\s+", " ", safe_title).strip(" .")
        if len(safe_title) > 50:
            safe_title = safe_title[:50].rstrip()
        stem = f"row{task.csv_row_number:04d}_{safe_title or video.video_id}"
        target_path = target_dir / f"{stem}{suffix}"
        if target_path.exists():
            target_path.unlink()
        shutil.move(str(source_path), str(target_path))
        return target_path

    @staticmethod
    def _reset_attempt_files(row_dir: Path, *paths: Path) -> None:
        shutil.rmtree(row_dir, ignore_errors=True)
        row_dir.mkdir(parents=True, exist_ok=True)
        for path in paths:
            if path.exists():
                path.unlink()

    def _parse_video(self, douyin: DouyinService, url_value: str) -> ParsedVideo:
        try:
            return douyin.parse_share_text(url_value)
        except Exception as exc:
            raise RowProcessingError("解析", self._format_error(exc)) from exc

    def _download_video(
        self,
        douyin: DouyinService,
        video: ParsedVideo,
        video_path: Path,
        browser_audio_path: Path,
    ) -> DownloadResult:
        try:
            return douyin.download_video(video, video_path, browser_audio_path)
        except Exception as exc:
            raise RowProcessingError("下载", self._format_error(exc)) from exc

    def _extract_audio(
        self,
        ffmpeg: FfmpegService,
        video_path: Path,
        audio_path: Path,
        browser_audio_path: Path,
    ) -> None:
        try:
            ffmpeg.extract_audio(
                video_path,
                audio_path,
                supplemental_audio_path=browser_audio_path if browser_audio_path.exists() else None,
            )
        except Exception as exc:
            raise RowProcessingError("抽音频", self._format_error(exc)) from exc

    def _persist_snapshot(self, job_id: str, dataframe, output_path: Path) -> None:
        write_csv(dataframe, output_path)
        self.manager.update_output(job_id, output_path, self._preview_rows(dataframe))

    def _sync_progress(
        self,
        job_id: str,
        dataframe,
        *,
        current_message: Optional[str] = None,
        current_row: Optional[int] = None,
        current_url: Optional[str] = None,
        refresh_counters: bool = True,
    ) -> None:
        if not refresh_counters:
            self.manager.update_progress(
                job_id,
                current_row=current_row,
                current_url=current_url,
                current_message=current_message,
            )
            return

        processed, succeeded, failed, skipped = self._compute_counters(dataframe)
        self.manager.update_progress(
            job_id,
            processed=processed,
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            current_row=current_row,
            current_url=current_url,
            current_message=current_message,
        )

    def _resolve_source_path(self, job, mode: str) -> Path:
        if mode == "full":
            return job.upload_path
        if mode == "continue_existing":
            source_path = job.output_path or job.resume_output_path
            if source_path:
                if not Path(source_path).is_file():
                    raise RuntimeError("已有结果文件不存在，无法续跑；请恢复结果文件或选择重新运行")
                return Path(source_path)
            return job.upload_path
        if job.output_path and Path(job.output_path).exists():
            return Path(job.output_path)
        return job.upload_path

    def _build_output_path(self, job) -> Path:
        if job.output_path:
            return Path(job.output_path)
        # 带 job_id 前缀命名，避免同名输入互相覆盖；job_id 为空时退化为 <stem>.processed.csv
        return OUTPUT_DIR / build_processed_output_name(job.original_filename, job.job_id)

    def _build_tasks(self, dataframe, job) -> List[RowTask]:
        return [
            RowTask(
                row_index=row_index,
                position=position,
                csv_row_number=position + 1,
                url_value=stringify_cell(dataframe.at[row_index, job.url_column]).strip(),
            )
            for position, row_index in enumerate(dataframe.index, start=1)
        ]

    def _split_tasks(
        self,
        dataframe,
        tasks: Sequence[RowTask],
        output_columns: Dict[str, str],
        overwrite: bool,
        mode: str,
        download_only: bool = False,
    ) -> tuple[List[RowTask], List[RowTask]]:
        primary_tasks: List[RowTask] = []
        retry_queue: List[RowTask] = []

        for task in tasks:
            status = stringify_cell(dataframe.at[task.row_index, STATUS_COLUMN]).strip()
            pending_models = self._pending_work_for_row(dataframe, task.row_index, output_columns, overwrite, download_only)

            if mode == "retry_failed":
                if status in FAILED_STATUSES or (status == "部分成功" and pending_models):
                    retry_queue.append(task)
                continue

            if status in FAILED_STATUSES or (status == "部分成功" and pending_models):
                retry_queue.append(task)
                continue

            if status in TERMINAL_STATUSES or (status == "部分成功" and not pending_models):
                continue

            primary_tasks.append(task)

        return primary_tasks, retry_queue

    def _build_start_message(
        self,
        row_count: int,
        model_count: int,
        mode: str,
        *,
        download_workers: int,
        asr_workers: int,
        batch_delay_seconds: int,
    ) -> str:
        prefix = {
            "full": "开始处理",
            "resume": "继续处理",
            "continue_existing": "继续已有结果",
            "retry_failed": "开始重试失败项",
        }.get(mode, "开始处理")
        return (
            f"{prefix}，共 {row_count} 行，模型数 {model_count}，每批 {self.batch_size} 行，"
            f"下载并发 {download_workers}，ASR 并发 {asr_workers}，批间等待 {batch_delay_seconds}s"
        )

    @staticmethod
    def _sanitize_worker_count(value: int) -> int:
        try:
            return max(1, int(value))
        except Exception:
            return 1

    @staticmethod
    def _sanitize_batch_delay(value: int) -> int:
        try:
            return max(0, int(value))
        except Exception:
            return DEFAULT_BATCH_DELAY_SECONDS

    def _compute_counters(self, dataframe) -> Tuple[int, int, int, int]:
        statuses = [stringify_cell(dataframe.at[row_index, STATUS_COLUMN]).strip() for row_index in dataframe.index]
        succeeded = sum(status in SUCCESS_STATUSES for status in statuses)
        failed = sum(status in FAILED_STATUSES for status in statuses)
        skipped = sum(status in SKIP_STATUSES for status in statuses)
        processed = succeeded + failed + skipped
        return processed, succeeded, failed, skipped

    @staticmethod
    def _preview_rows(dataframe) -> List[Dict[str, str]]:
        return [
            {column: stringify_cell(value) for column, value in row.items()}
            for row in dataframe.head(5).to_dict(orient="records")
        ]

    def _get_attempts(self, dataframe, row_index) -> int:
        raw_value = stringify_cell(dataframe.at[row_index, ATTEMPTS_COLUMN]).strip()
        try:
            return int(raw_value) if raw_value else 0
        except ValueError:
            return 0

    def _max_attempts_per_row(self) -> int:
        return self.max_retries + 1

    def _unexpected_error_status(self, attempt_number: int) -> Tuple[str, bool]:
        # 未预期异常（非 RowProcessingError）统一按行失败归一：未到尝试上限前进入重试队列
        retryable = attempt_number < self._max_attempts_per_row()
        return (RETRY_PENDING_STATUS if retryable else "失败"), retryable

    @staticmethod
    def _is_preview_only_parse_error(exc: RowProcessingError) -> bool:
        return exc.stage == "解析" and PREVIEW_ONLY_DELETE_ERROR in exc.detail

    @staticmethod
    def _is_invalid_link_parse_error(exc: RowProcessingError) -> bool:
        return exc.stage == "解析" and INVALID_LINK_ERROR in exc.detail

    @staticmethod
    def _is_video_detail_missing_parse_error(exc: RowProcessingError) -> bool:
        return exc.stage == "解析" and VIDEO_DETAIL_MISSING_ERROR in exc.detail

    @staticmethod
    def _is_non_video_parse_error(exc: RowProcessingError) -> bool:
        return exc.stage == "解析" and NON_VIDEO_ERROR in exc.detail

    @staticmethod
    def _error_chain_contains(exc: BaseException, error_type: type) -> bool:
        # douyin_service 的结构化异常会被 _download_video 包进 RowProcessingError 的
        # __cause__，沿异常链做 isinstance 判断；查不到再退回字符串指纹。
        stack = [exc]
        seen = set()
        while stack:
            current = stack.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, error_type):
                return True
            stack.append(current.__cause__)
            stack.append(current.__context__)
        return False

    @staticmethod
    def _is_download_forbidden_error(exc: RowProcessingError) -> bool:
        return exc.stage == "下载" and DOWNLOAD_FORBIDDEN_ERROR in exc.detail

    def _should_mark_download_unavailable(self, exc: RowProcessingError, attempt_number: int) -> bool:
        if attempt_number < DOWNLOAD_UNAVAILABLE_ATTEMPTS:
            return False
        if self._error_chain_contains(exc, DownloadForbiddenError):
            return True
        return self._is_download_forbidden_error(exc)

    @staticmethod
    def _is_non_video_download_404_error(exc: RowProcessingError) -> bool:
        return exc.stage == "下载" and NON_VIDEO_DOWNLOAD_404_ERROR in exc.detail

    def _should_mark_non_video_from_download_error(self, exc: RowProcessingError, attempt_number: int) -> bool:
        if attempt_number < NON_VIDEO_DOWNLOAD_404_ATTEMPTS:
            return False
        if self._error_chain_contains(exc, NonVideoDownloadError):
            return True
        return self._is_non_video_download_404_error(exc)

    def _should_mark_deleted_from_invalid_link(self, exc: RowProcessingError, attempt_number: int) -> bool:
        return self._is_invalid_link_parse_error(exc) and attempt_number >= INVALID_LINK_DELETE_ATTEMPTS

    @staticmethod
    def _is_no_audio_error(exc: RowProcessingError) -> bool:
        return exc.stage == "抽音频" and NO_AUDIO_ERROR in exc.detail

    def _deleted_reason_for_error(self, exc: RowProcessingError, attempt_number: int) -> str:
        if self._should_mark_deleted_from_invalid_link(exc, attempt_number):
            return "连续未识别到有效抖音链接"
        if self._is_video_detail_missing_parse_error(exc):
            return "分享页未找到视频详情"
        return ""

    def _should_mark_deleted_after_retry_ceiling(self, exc: RowProcessingError, attempt_number: int) -> bool:
        # 原硬编码 3 等价于默认 max_retries；配置更大上限时按上限推导，小上限保持原行为
        return exc.stage in {"解析", "下载", "抽音频"} and attempt_number > max(3, self.max_retries)

    @staticmethod
    def _dedupe_tasks(tasks: Sequence[RowTask]) -> List[RowTask]:
        seen = set()
        results: List[RowTask] = []
        for task in tasks:
            if task.row_index in seen:
                continue
            seen.add(task.row_index)
            results.append(task)
        return results

    def _iter_batches(self, tasks: Sequence[RowTask]):
        for start in range(0, len(tasks), self.batch_size):
            yield list(tasks[start : start + self.batch_size])

    def _batch_count(self, count: int) -> int:
        if count <= 0:
            return 0
        return (count + self.batch_size - 1) // self.batch_size

    def _is_retryable(self, exc: RowProcessingError, attempt_number: int) -> bool:
        if "已删除" in exc.detail:
            return False
        if self._is_preview_only_parse_error(exc):
            return attempt_number < PREVIEW_ONLY_RETRY_ATTEMPTS
        if self._is_no_audio_error(exc):
            return attempt_number < NO_AUDIO_RETRY_ATTEMPTS
        return exc.stage in {"解析", "下载"} and attempt_number < self._max_attempts_per_row()

    @staticmethod
    def _format_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            request = exc.request
            parsed = urlparse(str(request.url))
            target = f"{parsed.netloc}{parsed.path}"
            return f"HTTP {exc.response.status_code} {exc.response.reason_phrase} ({target})"

        if isinstance(exc, httpx.ReadTimeout):
            return "读取超时"

        if isinstance(exc, httpx.ConnectTimeout):
            return "连接超时"

        if isinstance(exc, httpx.TimeoutException):
            return "请求超时"

        if isinstance(exc, httpx.ConnectError):
            return "连接失败"

        if isinstance(exc, json.JSONDecodeError):
            return "响应不是有效 JSON"

        message = str(exc).strip() or exc.__class__.__name__
        message = re.sub(r"\s+", " ", message)
        return JobProcessor._shorten_urls(message)

    @staticmethod
    def _shorten_urls(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            parsed = urlparse(match.group(0))
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        return re.sub(r"https?://[^\s'\"）)]+", replace, text)
