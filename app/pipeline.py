import json
import re
import shutil
import threading
import time
from contextlib import ExitStack
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx

from .config import (
    ANTI_CRAWL_COOLDOWN_SECONDS,
    ANTI_CRAWL_STREAK_THRESHOLD,
    ATTEMPTS_COLUMN,
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
)
from .csv_service import (
    build_processed_output_name,
    ensure_text_column,
    inspect_csv,
    stringify_cell,
    write_csv,
)
from .douyin_service import DouyinService, DownloadResult, ParsedVideo
from .job_manager import JobManager
from .media_service import FfmpegService
from .transcription_service import TranscriptionService


SUCCESS_STATUSES = {"成功", "部分成功"}
SKIP_STATUSES = {"跳过", "已删除"}
RETRY_PENDING_STATUS = "待重试"
FAILED_STATUSES = {"失败", RETRY_PENDING_STATUS}
TERMINAL_STATUSES = SUCCESS_STATUSES | SKIP_STATUSES
PREVIEW_ONLY_DELETE_ERROR = "浏览器兜底命中通用页，仅捕获到预览片段"
INVALID_LINK_DELETE_ATTEMPTS = 2
INVALID_LINK_ERROR = "未在单元格中找到有效的抖音链接"
PREVIEW_ONLY_RETRY_ATTEMPTS = 3
NO_AUDIO_RETRY_ATTEMPTS = 3
NO_AUDIO_ERROR = "视频没有音轨"
WEAK_SIGNAL_DELETED_PAGE = "浏览器兜底删除页标题（在抖音记录美好生活）"
WEAK_SIGNAL_PARSE_FALLBACK = "分享页/接口异常且浏览器未捕获视频地址"
STRONG_SIGNAL_CHALLENGE_FALLBACK = "challenge/验证码信号"


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
    restore_deleted_tasks: List[RowTask] = field(default_factory=list)
    signal_descriptions: List[str] = field(default_factory=list)


@dataclass
class RunControlState:
    anti_crawl_streak_threshold: int
    anti_crawl_cooldown_seconds: int
    proactive_cooldown_batch_threshold: int
    proactive_cooldown_seconds: int
    suspicious_tasks: List[RowTask] = field(default_factory=list)
    suspicious_deleted_tasks: List[RowTask] = field(default_factory=list)
    suspicious_signal_descriptions: List[str] = field(default_factory=list)
    pending_cooldown: Optional[CooldownRequest] = None
    healthy_batch_streak: int = 0
    current_batch_had_issue: bool = False


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
    ) -> None:
        self.manager = manager
        self.douyin_service_factory = douyin_service_factory
        self.ffmpeg_service_factory = ffmpeg_service_factory
        self.transcription_service_factory = transcription_service_factory
        self.sleep_fn = sleep_fn
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.batch_size = batch_size
        self.batch_delay_seconds = batch_delay_seconds
        self.anti_crawl_streak_threshold = max(1, int(anti_crawl_streak_threshold))
        self.anti_crawl_cooldown_seconds = max(0, int(anti_crawl_cooldown_seconds))
        self.proactive_cooldown_batch_threshold = max(1, int(proactive_cooldown_batch_threshold))
        self.proactive_cooldown_seconds = max(0, int(proactive_cooldown_seconds))
        self._asr_local = threading.local()
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

        douyin = None
        transcriber = None

        try:
            ffmpeg = self.ffmpeg_service_factory()
            default_model = job.model_names[0] if job.model_names else DEFAULT_MODEL
            transcriber = self.transcription_service_factory(default_model=default_model)
            douyin = self._create_douyin_service()
            download_workers = self._sanitize_worker_count(job.download_workers)
            asr_workers = self._sanitize_worker_count(job.asr_workers)
            batch_delay_seconds = self._sanitize_batch_delay(getattr(job, "batch_delay_seconds", self.batch_delay_seconds))

            if not ffmpeg.is_available():
                raise RuntimeError("当前系统未检测到 ffmpeg，请先安装后再运行任务")

            source_path = self._resolve_source_path(job, mode)
            inspection = inspect_csv(source_path)
            dataframe = inspection.dataframe

            if mode == "continue_existing" and source_path != job.upload_path and len(dataframe) != job.row_count:
                raise RuntimeError("已有结果文件行数与当前上传文件不一致，无法继续处理")

            if not job.url_column or job.url_column not in dataframe.columns:
                raise RuntimeError("URL 列不存在，请重新选择")
            if not job.transcript_column:
                raise RuntimeError("视频文本列不能为空")

            model_names = job.model_names or [DEFAULT_MODEL]
            output_columns = self._build_output_columns(job.transcript_column, model_names, transcriber)
            for column_name in output_columns.values():
                ensure_text_column(dataframe, column_name)
            for column_name in (STATUS_COLUMN, ERROR_COLUMN, ATTEMPTS_COLUMN):
                ensure_text_column(dataframe, column_name)

            output_path = self._build_output_path(job)
            self.manager.mark_running(job_id, len(dataframe))
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
            primary_tasks, retry_queue = self._split_tasks(dataframe, tasks, output_columns, job.overwrite, mode)
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
                    douyin=douyin,
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
                        douyin=douyin,
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
            if douyin is not None:
                douyin.close()
            self._close_job_douyin_services(job_id)
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
        douyin: DouyinService,
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
                    self._process_task(
                        job_id=job_id,
                        run_state=run_state,
                        dataframe=dataframe,
                        task=task,
                        output_columns=output_columns,
                        overwrite=overwrite,
                        workspace=workspace,
                        douyin=douyin,
                        ffmpeg=ffmpeg,
                        transcriber=transcriber,
                        output_path=output_path,
                        retry_queue=retry_queue,
                    )

            cooled_down = self._finalize_batch(
                job_id=job_id,
                run_state=run_state,
                dataframe=dataframe,
                output_columns=output_columns,
                output_path=output_path,
                retry_queue=retry_queue,
                allow_proactive_cooldown=batch_index < total_batches or bool(retry_queue),
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
        douyin: DouyinService,
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

        while pending and not self.manager.is_pause_requested(job_id):
            self.manager.append_log(job_id, f"开始第 {round_index} 轮重试，共 {len(pending)} 行")
            next_round: List[RowTask] = []
            total_batches = self._batch_count(len(pending))

            for batch_index, batch in enumerate(self._iter_batches(pending), start=1):
                if self.manager.is_pause_requested(job_id):
                    break

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
                        self._process_task(
                            job_id=job_id,
                            run_state=run_state,
                            dataframe=dataframe,
                            task=task,
                            output_columns=output_columns,
                            overwrite=overwrite,
                            workspace=workspace,
                            douyin=douyin,
                            ffmpeg=ffmpeg,
                            transcriber=transcriber,
                            output_path=output_path,
                            retry_queue=next_round,
                        )

                cooled_down = self._finalize_batch(
                    job_id=job_id,
                    run_state=run_state,
                    dataframe=dataframe,
                    output_columns=output_columns,
                    output_path=output_path,
                    retry_queue=next_round,
                    allow_proactive_cooldown=batch_index < total_batches or bool(next_round),
                )

                if batch_index < total_batches and not self.manager.is_pause_requested(job_id) and not cooled_down:
                    self.manager.append_log(
                        job_id,
                        f"重试 第 {batch_index} 批完成，{batch_delay_seconds} 秒后继续",
                    )
                    self.sleep_fn(batch_delay_seconds)

            pending = self._dedupe_tasks(next_round)
            if pending and not self.manager.is_pause_requested(job_id):
                self.manager.append_log(
                    job_id,
                    f"第 {round_index} 轮重试结束，剩余 {len(pending)} 行，{self.retry_delay_seconds} 秒后继续",
                )
                self.sleep_fn(self.retry_delay_seconds)
                round_index += 1

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
        for task in tasks:
            if self.manager.is_pause_requested(job_id):
                break
            result = self._plan_task(dataframe, task, output_columns, overwrite)
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
            for future in done:
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
                    prepared = asr_futures.pop(future)
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

        for future in download_futures:
            future.cancel()
        for future in asr_futures:
            future.cancel()

    def _plan_task(
        self,
        dataframe,
        task: RowTask,
        output_columns: Dict[str, str],
        overwrite: bool,
    ) -> RowPlan | RowExecutionResult:
        current_attempts = self._get_attempts(dataframe, task.row_index)
        pending_models = tuple(self._pending_models_for_row(dataframe, task.row_index, output_columns, overwrite))

        if not task.url_value:
            return RowExecutionResult(
                plan=RowPlan(task=task, attempts_before=current_attempts, pending_models=pending_models),
                outcome=RowOutcome(status="跳过", attempts=current_attempts, error_message="URL 为空"),
                log_messages=(f"第 {task.csv_row_number} 行跳过：URL 为空",),
            )

        if not pending_models:
            return RowExecutionResult(
                plan=RowPlan(task=task, attempts_before=current_attempts, pending_models=pending_models),
                outcome=RowOutcome(status="跳过", attempts=current_attempts, error_message="目标文本列已有内容"),
                log_messages=(f"第 {task.csv_row_number} 行跳过：目标文本列已有内容",),
            )

        return RowPlan(task=task, attempts_before=current_attempts, pending_models=pending_models)

    def _run_download_stage(self, job_id: str, plan: RowPlan, workspace: Path) -> PreparedRow | RowExecutionResult:
        douyin = self._get_thread_douyin_service(job_id)
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
                self._consume_attempt_challenge_signal_descriptions(douyin)
                log_messages.append(
                    f"第 {plan.task.csv_row_number} 行已删除，已写入“{DELETED_TRANSCRIPT_TEXT}”"
                )
                return RowExecutionResult(
                    plan=plan,
                    outcome=RowOutcome(
                        status="已删除",
                        attempts=plan.attempt_number,
                    ),
                    transcripts={model_name: DELETED_TRANSCRIPT_TEXT for model_name in plan.pending_models},
                    log_messages=tuple(log_messages),
                    cleanup_dir=row_dir,
                )

            download_result = self._download_video(douyin, video, video_path, browser_audio_path)
            log_messages.append(
                f"第 {plan.task.csv_row_number} 行下载完成：{download_result.strategy}"
            )
            if download_result.audio_source_url:
                log_messages.append(f"第 {plan.task.csv_row_number} 行已捕获补充音频流")

            self._consume_attempt_challenge_signal_descriptions(douyin)
            return PreparedRow(
                plan=plan,
                row_dir=row_dir,
                video_path=video_path,
                browser_audio_path=browser_audio_path,
                audio_path=audio_path,
                log_messages=tuple(log_messages),
            )
        except RowProcessingError as exc:
            delete_reason = self._deleted_reason_for_error(exc, plan.attempt_number)
            if delete_reason:
                self._consume_attempt_challenge_signal_descriptions(douyin)
                return RowExecutionResult(
                    plan=plan,
                    outcome=RowOutcome(status="已删除", attempts=plan.attempt_number),
                    transcripts={model_name: DELETED_TRANSCRIPT_TEXT for model_name in plan.pending_models},
                    log_messages=(
                        f"第 {plan.task.csv_row_number} 行{delete_reason}，按已删除处理，已写入“{DELETED_TRANSCRIPT_TEXT}”",
                    ),
                    cleanup_dir=row_dir,
                )
            retryable = self._is_retryable(exc, plan.attempt_number)
            weak_signal_descriptions = self._weak_signal_descriptions_for_error(exc)
            strong_signal_descriptions = self._consume_attempt_challenge_signal_descriptions(douyin)
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

    def _run_transcription_stage(
        self,
        prepared: PreparedRow,
        output_columns: Dict[str, str],
        default_model: str,
    ) -> RowExecutionResult:
        ffmpeg = self.ffmpeg_service_factory()
        transcriber = self._get_thread_transcriber(default_model)
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
            )
            log_messages.extend(model_logs)

            if not model_failures:
                return RowExecutionResult(
                    plan=prepared.plan,
                    outcome=RowOutcome(status="成功", attempts=prepared.plan.attempt_number),
                    transcripts=transcripts,
                    log_messages=tuple(log_messages),
                    cleanup_dir=prepared.row_dir,
                )

            status = "部分成功" if transcripts else "失败"
            return RowExecutionResult(
                plan=prepared.plan,
                outcome=RowOutcome(
                    status=status,
                    attempts=prepared.plan.attempt_number,
                    error_message=" | ".join(model_failures),
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
                ),
                log_messages=(),
                cleanup_dir=prepared.row_dir,
            )

    def _transcribe_models_worker(
        self,
        plan: RowPlan,
        audio_path: Path,
        transcriber: TranscriptionService,
    ) -> tuple[Dict[str, str], List[str], List[str]]:
        transcripts: Dict[str, str] = {}
        failures: List[str] = []
        logs: List[str] = []

        for model_name in plan.pending_models:
            try:
                logs.append(f"第 {plan.task.csv_row_number} 行开始转写：{model_name}")
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

        self._persist_snapshot(job_id, dataframe, output_path)
        self._sync_progress(
            job_id,
            dataframe,
            current_row=result.plan.task.csv_row_number,
            current_url=result.plan.task.url_value,
        )

    def _get_thread_transcriber(self, default_model: str) -> TranscriptionService:
        service = getattr(self._asr_local, "service", None)
        if service is None or getattr(service, "default_model", None) != default_model:
            service = self.transcription_service_factory(default_model=default_model)
            self._asr_local.service = service
        return service

    def _get_thread_douyin_service(self, job_id: str) -> DouyinService:
        service = getattr(self._douyin_local, "service", None)
        service_job_id = getattr(self._douyin_local, "job_id", None)
        if service is None or service_job_id != job_id:
            if service is not None:
                try:
                    service.close()
                except Exception:
                    pass
            service = self._create_douyin_service()
            self._douyin_local.service = service
            self._douyin_local.job_id = job_id
            self._register_douyin_service(job_id, service)
        return service

    def _create_douyin_service(self):
        return self.douyin_service_factory()

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

    def _record_row_control_signal(
        self,
        *,
        run_state: RunControlState,
        task: RowTask,
        outcome: RowOutcome,
    ) -> None:
        if outcome.status == "已删除":
            return

        if self._should_trigger_anti_crawl_cooldown(outcome):
            run_state.current_batch_had_issue = True
            should_restore_deleted = outcome.status == "已删除"
            if run_state.pending_cooldown is not None:
                self._append_unique_task(run_state.pending_cooldown.affected_tasks, task)
                if should_restore_deleted:
                    self._append_unique_task(run_state.pending_cooldown.restore_deleted_tasks, task)
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
            if should_restore_deleted:
                self._append_unique_task(run_state.suspicious_deleted_tasks, task)
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
                    restore_deleted_tasks=list(run_state.suspicious_deleted_tasks),
                    signal_descriptions=list(run_state.suspicious_signal_descriptions),
                )
            return

        if outcome.status == "成功" and run_state.pending_cooldown is None:
            run_state.suspicious_tasks.clear()
            run_state.suspicious_deleted_tasks.clear()
            run_state.suspicious_signal_descriptions.clear()

        if outcome.status in {RETRY_PENDING_STATUS, "失败", "部分成功"}:
            run_state.current_batch_had_issue = True

    @staticmethod
    def _should_trigger_anti_crawl_cooldown(outcome: RowOutcome) -> bool:
        # Retry queues are biased toward hard links, so weak signals only count on
        # the first attempt. Explicit challenge signals stay active across retries.
        return outcome.anti_crawl_strong_suspected or (
            outcome.anti_crawl_suspected and outcome.attempts == 1
        )

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

        for task in request.restore_deleted_tasks:
            self._restore_false_deleted_row(
                job_id=job_id,
                dataframe=dataframe,
                row_task=task,
                output_columns=output_columns,
                retry_queue=retry_queue,
            )

        self.manager.append_log(job_id, request.reason)
        self._persist_snapshot(job_id, dataframe, output_path)
        self._sleep_with_cooldown_message(job_id, request.seconds, request.reason)
        self._sync_progress(job_id, dataframe, current_message="任务执行中")

        run_state.pending_cooldown = None
        run_state.suspicious_tasks.clear()
        run_state.suspicious_deleted_tasks.clear()
        run_state.suspicious_signal_descriptions.clear()
        run_state.current_batch_had_issue = False
        run_state.healthy_batch_streak = 0

    def _restore_false_deleted_row(
        self,
        *,
        job_id: str,
        dataframe,
        row_task: RowTask,
        output_columns: Dict[str, str],
        retry_queue: List[RowTask],
    ) -> None:
        for column_name in output_columns.values():
            if stringify_cell(dataframe.at[row_task.row_index, column_name]).strip() == DELETED_TRANSCRIPT_TEXT:
                dataframe.at[row_task.row_index, column_name] = ""

        attempts = self._get_attempts(dataframe, row_task.row_index)
        self._set_row_meta(
            dataframe,
            row_task.row_index,
            status=RETRY_PENDING_STATUS,
            error_message="疑似触发抖音风控通用页，冷却后重试",
            attempts=attempts,
        )
        self._append_unique_task(retry_queue, row_task)
        self.manager.append_log(
            job_id,
            f"第 {row_task.csv_row_number} 行已从“已删除”恢复为“{RETRY_PENDING_STATUS}”",
        )

    def _sleep_with_cooldown_message(self, job_id: str, seconds: int, reason: str) -> None:
        remaining = max(0, int(seconds))
        if remaining <= 0:
            return

        job = self.manager.require(job_id)
        self.manager.set_cooldown_state(job_id, active=True)
        if job.batch_id:
            self.manager.set_batch_cooldown_state(job.batch_id, active=True)

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
    def _is_suspected_anti_crawl_deleted_video(video: ParsedVideo) -> bool:
        return (
            video.is_deleted
            and video.resolved_by == "浏览器兜底"
            and (video.title or "").strip().startswith("在抖音记录美好生活")
        )

    @staticmethod
    def _is_suspected_anti_crawl_parse_failure(exc: RowProcessingError) -> bool:
        if exc.stage != "解析":
            return False

        detail = exc.detail or ""
        return (
            "分享页中未找到 _ROUTER_DATA" in detail
            and (
                "iteminfo API: Expecting value: line 1 column 1 (char 0)" in detail
                or "detail API: Expecting value: line 1 column 1 (char 0)" in detail
            )
            and "浏览器兜底没有捕获到视频地址" in detail
        )

    def _weak_signal_descriptions_for_video(self, video: ParsedVideo) -> Tuple[str, ...]:
        if not self._is_suspected_anti_crawl_deleted_video(video):
            return ()
        return (self._format_signal_description("弱", WEAK_SIGNAL_DELETED_PAGE),)

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
        douyin: DouyinService,
        ffmpeg: FfmpegService,
        transcriber: TranscriptionService,
        output_path: Path,
        retry_queue: List[RowTask],
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
            douyin=douyin,
            ffmpeg=ffmpeg,
            transcriber=transcriber,
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

        self._persist_snapshot(job_id, dataframe, output_path)
        self._sync_progress(job_id, dataframe, current_row=task.csv_row_number, current_url=task.url_value)

    def _process_task_once(
        self,
        *,
        job_id: str,
        dataframe,
        task: RowTask,
        output_columns: Dict[str, str],
        overwrite: bool,
        workspace: Path,
        douyin: DouyinService,
        ffmpeg: FfmpegService,
        transcriber: TranscriptionService,
    ) -> RowOutcome:
        current_attempts = self._get_attempts(dataframe, task.row_index)
        pending_models = self._pending_models_for_row(dataframe, task.row_index, output_columns, overwrite)

        if not task.url_value:
            self.manager.append_log(job_id, f"第 {task.csv_row_number} 行跳过：URL 为空")
            return RowOutcome(status="跳过", attempts=current_attempts, error_message="URL 为空")

        if not pending_models:
            self.manager.append_log(job_id, f"第 {task.csv_row_number} 行跳过：目标文本列已有内容")
            return RowOutcome(status="跳过", attempts=current_attempts, error_message="目标文本列已有内容")

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
                self._consume_attempt_challenge_signal_descriptions(douyin)
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
            if not model_failures:
                self._consume_attempt_challenge_signal_descriptions(douyin)
                return RowOutcome(status="成功", attempts=attempt_number)

            remaining_models = self._pending_models_for_row(dataframe, task.row_index, output_columns, overwrite)
            status = "部分成功" if remaining_models and len(remaining_models) < len(output_columns) else "失败"
            self._consume_attempt_challenge_signal_descriptions(douyin)
            return RowOutcome(
                status=status,
                attempts=attempt_number,
                error_message=" | ".join(model_failures),
            )
        except RowProcessingError as exc:
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
                return RowOutcome(status="已删除", attempts=attempt_number)
            retryable = self._is_retryable(exc, attempt_number)
            weak_signal_descriptions = self._weak_signal_descriptions_for_error(exc)
            strong_signal_descriptions = self._consume_attempt_challenge_signal_descriptions(douyin)
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

        return {
            model_name: f"{transcript_column}__{transcriber.model_alias(model_name)}"
            for model_name in model_names
        }

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
    ) -> None:
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
            if job.resume_output_path and Path(job.resume_output_path).exists():
                return Path(job.resume_output_path)
            return job.upload_path
        if job.output_path and Path(job.output_path).exists():
            return Path(job.output_path)
        return job.upload_path

    def _build_output_path(self, job) -> Path:
        if job.output_path:
            return Path(job.output_path)
        return OUTPUT_DIR / build_processed_output_name(job.original_filename)

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
    ) -> tuple[List[RowTask], List[RowTask]]:
        primary_tasks: List[RowTask] = []
        retry_queue: List[RowTask] = []

        for task in tasks:
            status = stringify_cell(dataframe.at[task.row_index, STATUS_COLUMN]).strip()
            pending_models = self._pending_models_for_row(dataframe, task.row_index, output_columns, overwrite)

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

    @staticmethod
    def _is_preview_only_parse_error(exc: RowProcessingError) -> bool:
        return exc.stage == "解析" and PREVIEW_ONLY_DELETE_ERROR in exc.detail

    @staticmethod
    def _is_invalid_link_parse_error(exc: RowProcessingError) -> bool:
        return exc.stage == "解析" and INVALID_LINK_ERROR in exc.detail

    def _should_mark_deleted_from_invalid_link(self, exc: RowProcessingError, attempt_number: int) -> bool:
        return self._is_invalid_link_parse_error(exc) and attempt_number >= INVALID_LINK_DELETE_ATTEMPTS

    @staticmethod
    def _is_no_audio_error(exc: RowProcessingError) -> bool:
        return exc.stage == "抽音频" and NO_AUDIO_ERROR in exc.detail

    def _deleted_reason_for_error(self, exc: RowProcessingError, attempt_number: int) -> str:
        if self._should_mark_deleted_from_invalid_link(exc, attempt_number):
            return "连续未识别到有效抖音链接"
        return ""

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
