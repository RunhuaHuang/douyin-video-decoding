from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import (
    ANTI_CRAWL_COOLDOWN_SECONDS,
    ANTI_CRAWL_STREAK_THRESHOLD,
    DEFAULT_ASR_WORKERS,
    DEFAULT_BATCH_DELAY_SECONDS,
    DEFAULT_DOWNLOAD_WORKERS,
    PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
    PROACTIVE_COOLDOWN_SECONDS,
)


@dataclass
class JobRecord:
    job_id: str
    original_filename: str
    upload_path: Path
    columns: List[str]
    preview_rows: List[Dict[str, str]]
    row_count: int
    batch_id: Optional[str] = None
    sequence: int = 1
    file_type: str = "csv"
    resume_output_path: Optional[Path] = None
    status: str = "uploaded"
    url_column: Optional[str] = None
    transcript_column: Optional[str] = None
    model_names_text: Optional[str] = None
    model_names: List[str] = field(default_factory=list)
    overwrite: bool = False
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS
    asr_workers: int = DEFAULT_ASR_WORKERS
    batch_delay_seconds: int = DEFAULT_BATCH_DELAY_SECONDS
    anti_crawl_streak_threshold: int = ANTI_CRAWL_STREAK_THRESHOLD
    anti_crawl_cooldown_seconds: int = ANTI_CRAWL_COOLDOWN_SECONDS
    proactive_cooldown_batch_threshold: int = PROACTIVE_COOLDOWN_BATCH_THRESHOLD
    proactive_cooldown_seconds: int = PROACTIVE_COOLDOWN_SECONDS
    total_rows: int = 0
    processed_rows: int = 0
    succeeded_rows: int = 0
    failed_rows: int = 0
    skipped_rows: int = 0
    current_row: Optional[int] = None
    current_message: str = ""
    current_url: str = ""
    logs: List[str] = field(default_factory=list)
    output_path: Optional[Path] = None
    output_preview_rows: List[Dict[str, str]] = field(default_factory=list)
    error_message: str = ""
    pause_requested: bool = False
    cooldown_active: bool = False
    skip_cooldown_requested: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["upload_path"] = str(self.upload_path)
        data["output_path"] = str(self.output_path) if self.output_path else None
        data["resume_output_path"] = str(self.resume_output_path) if self.resume_output_path else None
        data["resume_available"] = bool(self.resume_output_path)
        data["created_at"] = self.created_at.isoformat()
        data["started_at"] = self.started_at.isoformat() if self.started_at else None
        data["finished_at"] = self.finished_at.isoformat() if self.finished_at else None
        data["output_filename"] = self.output_path.name if self.output_path else None
        return data


@dataclass
class BatchRecord:
    batch_id: str
    job_ids: List[str]
    common_columns: List[str]
    status: str = "uploaded"
    current_job_id: Optional[str] = None
    current_file_index: int = 0
    current_message: str = ""
    error_message: str = ""
    url_column: Optional[str] = None
    transcript_column: Optional[str] = None
    model_names_text: Optional[str] = None
    model_names: List[str] = field(default_factory=list)
    overwrite: bool = False
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS
    asr_workers: int = DEFAULT_ASR_WORKERS
    batch_delay_seconds: int = DEFAULT_BATCH_DELAY_SECONDS
    anti_crawl_streak_threshold: int = ANTI_CRAWL_STREAK_THRESHOLD
    anti_crawl_cooldown_seconds: int = ANTI_CRAWL_COOLDOWN_SECONDS
    proactive_cooldown_batch_threshold: int = PROACTIVE_COOLDOWN_BATCH_THRESHOLD
    proactive_cooldown_seconds: int = PROACTIVE_COOLDOWN_SECONDS
    pause_requested: bool = False
    cooldown_active: bool = False
    skip_cooldown_requested: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def to_dict(self, jobs: List[JobRecord]) -> Dict[str, object]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["started_at"] = self.started_at.isoformat() if self.started_at else None
        data["finished_at"] = self.finished_at.isoformat() if self.finished_at else None
        data["jobs"] = [job.to_dict() for job in jobs]
        data["total_files"] = len(jobs)
        data["resumable_files"] = sum(bool(job.resume_output_path) for job in jobs)
        data["completed_files"] = sum(job.status == "completed" for job in jobs)
        data["failed_files"] = sum(job.status == "failed" for job in jobs)
        data["paused_files"] = sum(job.status == "paused" for job in jobs)
        data["running_files"] = sum(job.status == "running" for job in jobs)
        data["queued_files"] = sum(job.status == "queued" for job in jobs)
        data["uploaded_files"] = sum(job.status == "uploaded" for job in jobs)
        data["processed_rows"] = sum(job.processed_rows for job in jobs)
        data["total_rows"] = sum(job.total_rows or job.row_count for job in jobs)
        return data
