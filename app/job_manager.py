import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

from .config import (
    ANTI_CRAWL_COOLDOWN_SECONDS,
    ANTI_CRAWL_STREAK_THRESHOLD,
    DEFAULT_ASR_WORKERS,
    DEFAULT_BATCH_DELAY_SECONDS,
    DEFAULT_DOWNLOAD_WORKERS,
    PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
    PROACTIVE_COOLDOWN_SECONDS,
)
from .models import BatchRecord, JobRecord


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, JobRecord] = {}
        self._batches: Dict[str, BatchRecord] = {}
        self._lock = threading.RLock()

    def create_session(
        self,
        original_filename: str,
        upload_path: Path,
        columns: List[str],
        preview_rows: List[Dict[str, str]],
        row_count: int,
        file_type: str = "csv",
        resume_output_path: Optional[Path] = None,
    ) -> JobRecord:
        job_id = uuid4().hex
        job = JobRecord(
            job_id=job_id,
            original_filename=original_filename,
            upload_path=upload_path,
            columns=columns,
            preview_rows=preview_rows,
            row_count=row_count,
            file_type=file_type,
            resume_output_path=resume_output_path,
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def create_batch(self, jobs: List[JobRecord], common_columns: List[str]) -> BatchRecord:
        batch_id = uuid4().hex
        batch = BatchRecord(
            batch_id=batch_id,
            job_ids=[job.job_id for job in jobs],
            common_columns=common_columns,
        )
        with self._lock:
            self._batches[batch_id] = batch
            for index, job in enumerate(jobs, start=1):
                job.batch_id = batch_id
                job.sequence = index
        return batch

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._jobs.get(job_id)

    def require(self, job_id: str) -> JobRecord:
        job = self.get(job_id)
        if not job:
            raise KeyError(f"Job {job_id} 不存在")
        return job

    def get_batch(self, batch_id: str) -> Optional[BatchRecord]:
        with self._lock:
            return self._batches.get(batch_id)

    def require_batch(self, batch_id: str) -> BatchRecord:
        batch = self.get_batch(batch_id)
        if not batch:
            raise KeyError(f"Batch {batch_id} 不存在")
        return batch

    def snapshot(self, job_id: str) -> Dict[str, object]:
        with self._lock:
            return self.require(job_id).to_dict()

    def batch_snapshot(self, batch_id: str) -> Dict[str, object]:
        with self._lock:
            batch = self.require_batch(batch_id)
            jobs = [self.require(job_id) for job_id in batch.job_ids]
            return batch.to_dict(jobs)

    def configure_job(
        self,
        job_id: str,
        url_column: str,
        transcript_column: str,
        model_names_text: str,
        model_names: List[str],
        overwrite: bool,
        download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
        asr_workers: int = DEFAULT_ASR_WORKERS,
        batch_delay_seconds: int = DEFAULT_BATCH_DELAY_SECONDS,
        anti_crawl_streak_threshold: int = ANTI_CRAWL_STREAK_THRESHOLD,
        anti_crawl_cooldown_seconds: int = ANTI_CRAWL_COOLDOWN_SECONDS,
        proactive_cooldown_batch_threshold: int = PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
        proactive_cooldown_seconds: int = PROACTIVE_COOLDOWN_SECONDS,
    ) -> None:
        with self._lock:
            job = self.require(job_id)
            current_cookie_account_id = job.current_cookie_account_id
            current_cookie_label = job.current_cookie_label
            cookie_service_generation = job.cookie_service_generation
            job.url_column = url_column
            job.transcript_column = transcript_column
            job.model_names_text = model_names_text
            job.model_names = model_names
            job.overwrite = overwrite
            job.download_workers = download_workers
            job.asr_workers = asr_workers
            job.batch_delay_seconds = batch_delay_seconds
            job.anti_crawl_streak_threshold = anti_crawl_streak_threshold
            job.anti_crawl_cooldown_seconds = anti_crawl_cooldown_seconds
            job.proactive_cooldown_batch_threshold = proactive_cooldown_batch_threshold
            job.proactive_cooldown_seconds = proactive_cooldown_seconds
            job.status = "queued"
            job.error_message = ""
            job.logs = []
            job.current_message = "任务已入队"
            job.started_at = None
            job.finished_at = None
            job.total_rows = 0
            job.processed_rows = 0
            job.succeeded_rows = 0
            job.failed_rows = 0
            job.skipped_rows = 0
            job.current_row = None
            job.current_url = ""
            job.output_path = None
            job.output_preview_rows = []
            job.pause_requested = False
            job.cooldown_active = False
            job.skip_cooldown_requested = False
            job.current_cookie_account_id = current_cookie_account_id
            job.current_cookie_label = current_cookie_label
            job.cookie_batches_since_switch = 0
            job.cookie_total_batches_processed = 0
            job.cookie_switch_requested = False
            job.cookie_last_switch_reason = ""
            job.cookie_service_generation = cookie_service_generation

    def configure_batch(
        self,
        batch_id: str,
        *,
        url_column: str,
        transcript_column: str,
        model_names_text: str,
        model_names: List[str],
        overwrite: bool,
        download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
        asr_workers: int = DEFAULT_ASR_WORKERS,
        batch_delay_seconds: int = DEFAULT_BATCH_DELAY_SECONDS,
        anti_crawl_streak_threshold: int = ANTI_CRAWL_STREAK_THRESHOLD,
        anti_crawl_cooldown_seconds: int = ANTI_CRAWL_COOLDOWN_SECONDS,
        proactive_cooldown_batch_threshold: int = PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
        proactive_cooldown_seconds: int = PROACTIVE_COOLDOWN_SECONDS,
    ) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            current_cookie_account_id = batch.current_cookie_account_id
            current_cookie_label = batch.current_cookie_label
            cookie_service_generation = batch.cookie_service_generation
            batch.url_column = url_column
            batch.transcript_column = transcript_column
            batch.model_names_text = model_names_text
            batch.model_names = model_names
            batch.overwrite = overwrite
            batch.download_workers = download_workers
            batch.asr_workers = asr_workers
            batch.batch_delay_seconds = batch_delay_seconds
            batch.anti_crawl_streak_threshold = anti_crawl_streak_threshold
            batch.anti_crawl_cooldown_seconds = anti_crawl_cooldown_seconds
            batch.proactive_cooldown_batch_threshold = proactive_cooldown_batch_threshold
            batch.proactive_cooldown_seconds = proactive_cooldown_seconds
            batch.status = "queued"
            batch.error_message = ""
            batch.current_message = "批次已入队"
            batch.current_job_id = None
            batch.current_file_index = 0
            batch.started_at = None
            batch.finished_at = None
            batch.pause_requested = False
            batch.cooldown_active = False
            batch.skip_cooldown_requested = False
            batch.current_cookie_account_id = current_cookie_account_id
            batch.current_cookie_label = current_cookie_label
            batch.cookie_batches_since_switch = 0
            batch.cookie_total_batches_processed = 0
            batch.cookie_switch_requested = False
            batch.cookie_last_switch_reason = ""
            batch.cookie_service_generation = cookie_service_generation
            for job_id in batch.job_ids:
                self.configure_job(
                    job_id=job_id,
                    url_column=url_column,
                    transcript_column=transcript_column,
                    model_names_text=model_names_text,
                    model_names=model_names,
                    overwrite=overwrite,
                    download_workers=download_workers,
                    asr_workers=asr_workers,
                    batch_delay_seconds=batch_delay_seconds,
                    anti_crawl_streak_threshold=anti_crawl_streak_threshold,
                    anti_crawl_cooldown_seconds=anti_crawl_cooldown_seconds,
                    proactive_cooldown_batch_threshold=proactive_cooldown_batch_threshold,
                    proactive_cooldown_seconds=proactive_cooldown_seconds,
                )

    def mark_running(self, job_id: str, total_rows: int) -> None:
        with self._lock:
            job = self.require(job_id)
            job.status = "running"
            job.started_at = datetime.now(timezone.utc)
            job.total_rows = total_rows
            job.current_message = "任务执行中"
            job.pause_requested = False
            job.cooldown_active = False
            job.skip_cooldown_requested = False

    def mark_batch_running(self, batch_id: str) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            batch.status = "running"
            if batch.started_at is None:
                batch.started_at = datetime.now(timezone.utc)
            batch.finished_at = None
            batch.error_message = ""
            batch.pause_requested = False
            batch.cooldown_active = False
            batch.skip_cooldown_requested = False
            batch.current_message = "批次执行中"

    def queue_existing_job(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self.require(job_id)
            job.status = "queued"
            job.current_message = message
            job.error_message = ""
            job.finished_at = None
            job.pause_requested = False
            job.cooldown_active = False
            job.skip_cooldown_requested = False

    def queue_existing_batch(self, batch_id: str, message: str) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            batch.status = "queued"
            batch.current_message = message
            batch.error_message = ""
            batch.finished_at = None
            batch.pause_requested = False
            batch.cooldown_active = False
            batch.skip_cooldown_requested = False

    def update_progress(
        self,
        job_id: str,
        *,
        processed: Optional[int] = None,
        succeeded: Optional[int] = None,
        failed: Optional[int] = None,
        skipped: Optional[int] = None,
        current_row: Optional[int] = None,
        current_url: Optional[str] = None,
        current_message: Optional[str] = None,
    ) -> None:
        with self._lock:
            job = self.require(job_id)
            if processed is not None:
                job.processed_rows = processed
            if succeeded is not None:
                job.succeeded_rows = succeeded
            if failed is not None:
                job.failed_rows = failed
            if skipped is not None:
                job.skipped_rows = skipped
            if current_row is not None:
                job.current_row = current_row
            if current_url is not None:
                job.current_url = current_url
            if current_message is not None:
                job.current_message = current_message

    def get_effective_cookie_runtime(self, job_id: str) -> Dict[str, object]:
        with self._lock:
            job = self.require(job_id)
            source = self.require_batch(job.batch_id) if job.batch_id else job
            return self._cookie_runtime_payload(source)

    def get_batch_cookie_runtime(self, batch_id: str) -> Dict[str, object]:
        with self._lock:
            batch = self.require_batch(batch_id)
            return self._cookie_runtime_payload(batch)

    def update_job_cookie_runtime(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self.require(job_id)
            self._apply_cookie_runtime_fields(job, fields)

    def update_batch_cookie_runtime(self, batch_id: str, **fields) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            self._apply_cookie_runtime_fields(batch, fields)

    def request_cookie_switch(self, job_id: str) -> None:
        self.update_job_cookie_runtime(job_id, cookie_switch_requested=True)

    def request_batch_cookie_switch(self, batch_id: str) -> None:
        self.update_batch_cookie_runtime(batch_id, cookie_switch_requested=True)

    def update_batch_progress(
        self,
        batch_id: str,
        *,
        current_job_id: Optional[str] = None,
        current_file_index: Optional[int] = None,
        current_message: Optional[str] = None,
    ) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            if current_job_id is not None:
                batch.current_job_id = current_job_id
            if current_file_index is not None:
                batch.current_file_index = current_file_index
            if current_message is not None:
                batch.current_message = current_message

    def append_log(self, job_id: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            job = self.require(job_id)
            job.logs.append(f"[{timestamp}] {message}")
            job.logs = job.logs[-500:]

    def update_output(
        self,
        job_id: str,
        output_path: Path,
        output_preview_rows: List[Dict[str, str]],
    ) -> None:
        with self._lock:
            job = self.require(job_id)
            job.output_path = output_path
            job.output_preview_rows = output_preview_rows

    def request_pause(self, job_id: str) -> None:
        with self._lock:
            job = self.require(job_id)
            job.pause_requested = True
            if job.status == "running":
                job.current_message = "正在暂停，当前行完成后生效"

    def clear_pause(self, job_id: str) -> None:
        with self._lock:
            job = self.require(job_id)
            job.pause_requested = False

    def set_cooldown_state(self, job_id: str, *, active: bool) -> None:
        with self._lock:
            job = self.require(job_id)
            job.cooldown_active = active
            if not active:
                job.skip_cooldown_requested = False

    def request_skip_cooldown(self, job_id: str) -> None:
        with self._lock:
            job = self.require(job_id)
            job.skip_cooldown_requested = True

    def is_skip_cooldown_requested(self, job_id: str) -> bool:
        with self._lock:
            return self.require(job_id).skip_cooldown_requested

    def request_batch_pause(self, batch_id: str) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            batch.pause_requested = True
            if batch.status == "running":
                batch.current_message = "正在暂停批次，当前文件完成后生效"

    def clear_batch_pause(self, batch_id: str) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            batch.pause_requested = False

    def set_batch_cooldown_state(self, batch_id: str, *, active: bool) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            batch.cooldown_active = active
            if not active:
                batch.skip_cooldown_requested = False

    def request_batch_skip_cooldown(self, batch_id: str) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            batch.skip_cooldown_requested = True

    def is_batch_skip_cooldown_requested(self, batch_id: str) -> bool:
        with self._lock:
            return self.require_batch(batch_id).skip_cooldown_requested

    def is_pause_requested(self, job_id: str) -> bool:
        with self._lock:
            return self.require(job_id).pause_requested

    def is_batch_pause_requested(self, batch_id: str) -> bool:
        with self._lock:
            return self.require_batch(batch_id).pause_requested

    def mark_paused(
        self,
        job_id: str,
        output_path: Path,
        output_preview_rows: List[Dict[str, str]],
    ) -> None:
        with self._lock:
            job = self.require(job_id)
            job.status = "paused"
            job.output_path = output_path
            job.output_preview_rows = output_preview_rows
            job.current_message = "任务已暂停"
            job.pause_requested = False
            job.cooldown_active = False
            job.skip_cooldown_requested = False
            job.finished_at = datetime.now(timezone.utc)

    def mark_batch_paused(self, batch_id: str, current_job_id: Optional[str] = None) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            batch.status = "paused"
            batch.pause_requested = False
            batch.current_message = "批次已暂停"
            batch.cooldown_active = False
            batch.skip_cooldown_requested = False
            if current_job_id is not None:
                batch.current_job_id = current_job_id
            batch.finished_at = datetime.now(timezone.utc)

    def mark_completed(
        self,
        job_id: str,
        output_path: Path,
        output_preview_rows: List[Dict[str, str]],
    ) -> None:
        with self._lock:
            job = self.require(job_id)
            job.status = "completed"
            job.output_path = output_path
            job.output_preview_rows = output_preview_rows
            job.current_message = "任务完成"
            job.pause_requested = False
            job.cooldown_active = False
            job.skip_cooldown_requested = False
            job.finished_at = datetime.now(timezone.utc)

    def mark_batch_completed(self, batch_id: str) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            batch.status = "completed"
            batch.current_message = "批次完成"
            batch.pause_requested = False
            batch.cooldown_active = False
            batch.skip_cooldown_requested = False
            batch.finished_at = datetime.now(timezone.utc)

    def mark_failed(self, job_id: str, error_message: str) -> None:
        with self._lock:
            job = self.require(job_id)
            job.status = "failed"
            job.error_message = error_message
            job.current_message = "任务失败"
            job.pause_requested = False
            job.cooldown_active = False
            job.skip_cooldown_requested = False
            job.finished_at = datetime.now(timezone.utc)

    def mark_batch_failed(self, batch_id: str, error_message: str) -> None:
        with self._lock:
            batch = self.require_batch(batch_id)
            batch.status = "failed"
            batch.error_message = error_message
            batch.current_message = "批次失败"
            batch.pause_requested = False
            batch.cooldown_active = False
            batch.skip_cooldown_requested = False
            batch.finished_at = datetime.now(timezone.utc)

    @staticmethod
    def _apply_cookie_runtime_fields(record, fields: Dict[str, object]) -> None:
        for key, value in fields.items():
            if value is None:
                continue
            setattr(record, key, value)

    @staticmethod
    def _cookie_runtime_payload(record) -> Dict[str, object]:
        return {
            "current_cookie_account_id": record.current_cookie_account_id,
            "current_cookie_label": record.current_cookie_label,
            "cookie_batches_since_switch": record.cookie_batches_since_switch,
            "cookie_total_batches_processed": record.cookie_total_batches_processed,
            "cookie_switch_requested": record.cookie_switch_requested,
            "cookie_last_switch_reason": record.cookie_last_switch_reason,
            "cookie_service_generation": record.cookie_service_generation,
        }
