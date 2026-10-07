from __future__ import annotations

import csv
import shutil
import tempfile
import threading
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .config import (
    ANTI_CRAWL_COOLDOWN_SECONDS,
    ANTI_CRAWL_STREAK_THRESHOLD,
    DEFAULT_ASR_WORKERS,
    DEFAULT_BATCH_DELAY_SECONDS,
    DEFAULT_DOWNLOAD_WORKERS,
    DEFAULT_MODEL,
    DEFAULT_TRANSCRIPT_COLUMN,
    MAX_ASR_WORKERS,
    MAX_BATCH_DELAY_SECONDS,
    MAX_DOWNLOAD_WORKERS,
    MODEL_PRESETS,
    PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
    PROACTIVE_COOLDOWN_SECONDS,
    STATIC_DIR,
    TEMPLATE_DIR,
    UPLOAD_DIR,
    WEBUI_HOST,
    WEBUI_PORT,
    ensure_directories,
)
from .cookie_accounts import CookieAccount, CookieAccountManager
from .csv_service import (
    find_latest_processed_path,
    inspect_csv,
    media_type_for_path,
    stringify_cell,
)
from .job_manager import JobManager
from .login_session import login_with_label
from .media_service import FfmpegService
from .pipeline import JobProcessor
from .transcription_service import TranscriptionService


ensure_directories()

app = FastAPI(title="抖音文案提取工具")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

job_manager = JobManager()
cookie_account_manager = CookieAccountManager()
job_processor = JobProcessor(job_manager, cookie_account_manager=cookie_account_manager)
ffmpeg_service = FfmpegService()
transcription_service = TranscriptionService(default_model=DEFAULT_MODEL)

MAX_ANTI_CRAWL_STREAK_THRESHOLD = 50
MAX_COOLDOWN_MINUTES = 12 * 60
MAX_PROACTIVE_COOLDOWN_BATCH_THRESHOLD = 500
account_relogin_threads: Dict[str, threading.Thread] = {}
_account_relogin_lock = threading.Lock()
_worker_registry_lock = threading.RLock()
_job_workers: Dict[str, threading.Thread] = {}
_batch_workers: Dict[str, threading.Thread] = {}


def _header_matches_host(value: str, host: str) -> bool:
    try:
        netloc = urlparse(value).netloc.lower()
    except Exception:
        return False
    return bool(netloc) and netloc == host.lower()


@app.middleware("http")
async def reject_cross_site_posts(request: Request, call_next):
    """防浏览器跨站盲发（CSRF）：POST 带 Origin/Referer 且与 Host 不一致即拒绝；不带这两个头的 curl/API 客户端放行。"""
    if request.method == "POST":
        host = request.headers.get("host", "")
        if host:
            for header_name in ("origin", "referer"):
                header_value = request.headers.get(header_name)
                if header_value and not _header_matches_host(header_value, host):
                    return JSONResponse(status_code=403, content={"detail": "跨站请求被拒绝（Origin/Referer 与 Host 不一致）"})
    return await call_next(request)


def _worker_is_active(registry: Dict[str, threading.Thread], key: str) -> bool:
    with _worker_registry_lock:
        worker = registry.get(key)
        if worker is None:
            return False
        if worker.is_alive():
            return True
        registry.pop(key, None)
        return False


def _job_start_conflict(job_id: str) -> bool:
    """Return whether a job or its owning batch already has a worker."""
    if _worker_is_active(_job_workers, job_id):
        return True
    job = job_manager.get(job_id)
    return bool(job and job.batch_id and _worker_is_active(_batch_workers, job.batch_id))


def _batch_start_conflict(batch_id: str) -> bool:
    """批次和其子任务共用一份执行权，所有启动入口都在 registry 锁内检查。"""
    if _worker_is_active(_batch_workers, batch_id):
        return True
    batch = job_manager.require_batch(batch_id)
    return any(_worker_is_active(_job_workers, job_id) for job_id in batch.job_ids)


def _run_job_worker(job_id: str, mode: str) -> None:
    try:
        job_processor.process(job_id=job_id, mode=mode)
    finally:
        with _worker_registry_lock:
            worker = _job_workers.get(job_id)
            if worker is threading.current_thread():
                _job_workers.pop(job_id, None)


def _run_batch_worker(batch_id: str, mode: str) -> None:
    try:
        process_batch(batch_id=batch_id, mode=mode)
    finally:
        with _worker_registry_lock:
            worker = _batch_workers.get(batch_id)
            if worker is threading.current_thread():
                _batch_workers.pop(batch_id, None)


def start_job_worker(job_id: str, mode: str = "full") -> bool:
    with _worker_registry_lock:
        if _job_start_conflict(job_id):
            return False
        worker = threading.Thread(
            target=_run_job_worker,
            kwargs={"job_id": job_id, "mode": mode},
            daemon=True,
            name=f"douyin-job-{job_id[:8]}-{mode}",
        )
        _job_workers[job_id] = worker
        worker.start()
        return True


def start_batch_worker(batch_id: str, mode: str = "full") -> bool:
    with _worker_registry_lock:
        if _batch_start_conflict(batch_id):
            return False
        worker = threading.Thread(
            target=_run_batch_worker,
            kwargs={"batch_id": batch_id, "mode": mode},
            daemon=True,
            name=f"douyin-batch-{batch_id[:8]}-{mode}",
        )
        _batch_workers[batch_id] = worker
        worker.start()
        return True


def process_batch(batch_id: str, mode: str = "full") -> None:
    batch = job_manager.require_batch(batch_id)
    run_mode = job_manager.begin_batch_run(batch_id, mode)
    job_ids = batch.job_ids
    start_index = 0
    resume_current = False

    if mode == "resume":
        if batch.current_job_id and batch.current_job_id in job_ids:
            current_index = job_ids.index(batch.current_job_id)
            current_job = job_manager.require(batch.current_job_id)
            if current_job.status == "paused":
                start_index = current_index
                resume_current = True
            else:
                start_index = current_index + 1
        else:
            for index, job_id in enumerate(job_ids):
                if job_manager.require(job_id).status in {"uploaded", "queued", "paused", "running"}:
                    start_index = index
                    break

    try:
        job_manager.mark_batch_running(batch_id)
        total_files = len(job_ids)

        for index in range(start_index, total_files):
            if job_manager.is_batch_pause_requested(batch_id):
                job_manager.mark_batch_paused(batch_id)
                return

            job_id = job_ids[index]
            job = job_manager.require(job_id)
            job_mode = "resume" if resume_current and index == start_index and job.status == "paused" else run_mode
            if job_mode == "retry_failed":
                if job.failed_rows <= 0 or job.status == "running":
                    continue

            if job_mode == "full" and job.status in {"completed", "failed"}:
                continue

            job_manager.update_batch_progress(
                batch_id,
                current_job_id=job_id,
                current_file_index=index + 1,
                current_message=f"处理文件 {index + 1}/{total_files}: {job.original_filename}",
            )

            job_processor.process(job_id, mode=job_mode)
            job = job_manager.require(job_id)
            if job.status == "paused":
                job_manager.mark_batch_paused(batch_id, current_job_id=job_id)
                return

        job_manager.mark_batch_completed(batch_id)
    except Exception as exc:
        job_manager.mark_batch_failed(batch_id, str(exc))


def save_upload_file(file: UploadFile) -> Path:
    safe_name = Path(file.filename or "").name
    upload_dir = UPLOAD_DIR / uuid4().hex
    upload_dir.mkdir(parents=True, exist_ok=True)
    save_path = upload_dir / safe_name
    return save_path


def compute_common_columns(column_sets: List[List[str]]) -> List[str]:
    if not column_sets:
        return []
    common = set(column_sets[0])
    for columns in column_sets[1:]:
        common &= set(columns)
    return [column for column in column_sets[0] if column in common]


def build_health_snapshot() -> Dict[str, str]:
    runtime = transcription_service.runtime_status()
    return {
        "ffmpeg": "已检测" if ffmpeg_service.is_available() else "未检测到 ffmpeg/ffprobe",
        "mlx": runtime["mlx"],
        "sensevoice": runtime["sensevoice"],
        "default_model": DEFAULT_MODEL,
    }


def _is_relogin_thread_alive(account_id: str) -> bool:
    thread = account_relogin_threads.get(account_id)
    if thread is None:
        return False
    if thread.is_alive():
        return True
    account_relogin_threads.pop(account_id, None)
    return False


def _reconcile_account_relogin_state(account_id: str) -> CookieAccount | None:
    account = cookie_account_manager.get_account(account_id)
    if account is None:
        account_relogin_threads.pop(account_id, None)
        return None
    if not account.relogin_in_progress:
        if not _is_relogin_thread_alive(account_id):
            account_relogin_threads.pop(account_id, None)
        return account
    if _is_relogin_thread_alive(account_id):
        return account
    if account.has_usable_state and account.updated_at:
        try:
            relogin_started_at = datetime.fromisoformat(account.updated_at).timestamp()
        except Exception:
            relogin_started_at = 0.0
        try:
            storage_mtime = Path(account.storage_state_path).stat().st_mtime
        except Exception:
            storage_mtime = 0.0
        if storage_mtime >= relogin_started_at:
            account_relogin_threads.pop(account_id, None)
            return cookie_account_manager.record_login(account_id, label=account.label)
    account = cookie_account_manager.mark_relogin_in_progress(account_id, False)
    return account


def _reconcile_all_account_relogin_states() -> None:
    account_ids = {account.account_id for account in cookie_account_manager.list_accounts()}
    account_ids.update(account_relogin_threads.keys())
    for account_id in account_ids:
        _reconcile_account_relogin_state(account_id)


def build_cookie_pool_snapshot(current_account_id: str = "") -> Dict[str, object]:
    _reconcile_all_account_relogin_states()
    return cookie_account_manager.build_pool_snapshot(current_account_id=current_account_id)


def build_job_payload(job_id: str) -> Dict[str, object]:
    job = job_manager.require(job_id)
    payload = job.to_dict()
    payload["batch_status"] = (
        job_manager.get_batch(job.batch_id).status if job.batch_id else ""
    )
    runtime = job_manager.get_effective_cookie_runtime(job_id)
    payload.update(runtime)
    payload["cookie_pool"] = build_cookie_pool_snapshot(payload.get("current_cookie_account_id", ""))
    return payload


def build_batch_payload(batch_id: str) -> Dict[str, object]:
    payload = job_manager.batch_snapshot(batch_id)
    runtime = job_manager.get_batch_cookie_runtime(batch_id)
    payload.update(runtime)
    payload["cookie_pool"] = build_cookie_pool_snapshot(payload.get("current_cookie_account_id", ""))
    return payload


def _available_cookie_accounts() -> List[CookieAccount]:
    _reconcile_all_account_relogin_states()
    return cookie_account_manager.list_available_accounts()


def _usable_cookie_pool() -> Dict[str, object]:
    return cookie_account_manager.build_pool_snapshot()


def _apply_cookie_to_job(job_id: str, account: CookieAccount, reason: str) -> None:
    job = job_manager.require(job_id)
    job_manager.advance_cookie_generation(job_id)
    fields = {
        "current_cookie_account_id": account.account_id,
        "current_cookie_label": account.label,
        "cookie_batches_since_switch": 0,
        "cookie_switch_requested": False,
        "cookie_last_switch_reason": reason,
    }
    if job.batch_id:
        job_manager.update_batch_cookie_runtime(job.batch_id, **fields)
    else:
        job_manager.update_job_cookie_runtime(job_id, **fields)
    job_manager.append_log(job_id, f"当前 cookies 已切换到 `{account.label}`：{reason}")


def switch_cookie_now_for_job(job_id: str, reason: str = "用户手动切换 cookies") -> CookieAccount:
    available_accounts = _available_cookie_accounts()
    if not available_accounts:
        raise RuntimeError('没有可用的 cookies。请先运行 `./login "备注"` 添加账号，或重新登录预警/已剔除账号。')

    runtime = job_manager.get_effective_cookie_runtime(job_id)
    current_account_id = str(runtime.get("current_cookie_account_id") or "").strip()
    next_account = cookie_account_manager.select_next_available_account(
        current_account_id=current_account_id,
        allow_same=False,
    )
    if next_account is None:
        raise RuntimeError("当前只有 1 个可用 cookies，无法切换")

    _apply_cookie_to_job(job_id, next_account, reason)
    return next_account


def start_account_relogin(account_id: str) -> None:
    with _account_relogin_lock:
        account = _reconcile_account_relogin_state(account_id)
        if account is None:
            raise RuntimeError("cookies 账号不存在")
        # 串行化同一账号的重新登录：线程仍存活则拒绝双开；仅线程已死（标记过期）才允许清标记重启
        if _is_relogin_thread_alive(account_id):
            raise RuntimeError("该账号正在重新登录中")
        if cookie_account_manager.mark_relogin_in_progress(account_id, True) is None:
            raise RuntimeError("cookies 账号已删除")

        def worker() -> None:
            try:
                login_with_label(account.label, wait_mode="browser_close", logger=print)
            finally:
                with _account_relogin_lock:
                    if account_relogin_threads.get(account_id) is threading.current_thread():
                        account_relogin_threads.pop(account_id, None)

        thread = threading.Thread(
            target=worker,
            daemon=True,
            name=f"douyin-cookie-relogin-{account_id[:8]}",
        )
        account_relogin_threads[account_id] = thread
        thread.start()


def sanitize_worker_count(value: int, *, maximum: int) -> int:
    return max(1, min(int(value), maximum))


def sanitize_batch_delay(value: int, *, maximum: int) -> int:
    return max(0, min(int(value), maximum))


def sanitize_threshold(value: int, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))


def sanitize_cooldown_minutes(value: int, *, maximum: int) -> int:
    return max(0, min(int(value), maximum))


class ApiRunOptions(BaseModel):
    """启动任务的完整参数（表单与 JSON API 共用一份默认值）。"""

    url_column: str
    transcript_column: str = ""
    model_names: str = DEFAULT_MODEL
    overwrite: bool = False
    download_only: bool = False
    video_output_dir: str = ""
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS
    asr_workers: int = DEFAULT_ASR_WORKERS
    batch_delay_seconds: int = DEFAULT_BATCH_DELAY_SECONDS
    anti_crawl_streak_threshold: int = ANTI_CRAWL_STREAK_THRESHOLD
    anti_crawl_cooldown_minutes: int = ANTI_CRAWL_COOLDOWN_SECONDS // 60
    proactive_cooldown_batch_threshold: int = PROACTIVE_COOLDOWN_BATCH_THRESHOLD
    proactive_cooldown_minutes: int = PROACTIVE_COOLDOWN_SECONDS // 60


_FORM_RUN_OPTION_FIELDS = (
    "url_column",
    "transcript_column",
    "model_names",
    "overwrite",
    "download_only",
    "video_output_dir",
    "download_workers",
    "asr_workers",
    "batch_delay_seconds",
    "anti_crawl_streak_threshold",
    "anti_crawl_cooldown_minutes",
    "proactive_cooldown_batch_threshold",
    "proactive_cooldown_minutes",
)


def _form_run_options(scope: Dict[str, object]) -> SimpleNamespace:
    """从表单端点的局部变量里挑出运行参数字段（四个端点参数名一致）。"""
    return SimpleNamespace(**{name: scope[name] for name in _FORM_RUN_OPTION_FIELDS})


def normalized_run_options(options) -> Dict[str, object]:
    """把用户提交的运行参数清洗成 configure_job/configure_batch 的统一入参。"""
    parsed_model_names = transcription_service.normalize_model_names(
        str(options.model_names or "").strip()
    )
    return {
        "url_column": options.url_column,
        "transcript_column": str(options.transcript_column or "").strip() or DEFAULT_TRANSCRIPT_COLUMN,
        "model_names_text": str(options.model_names or "").strip() or DEFAULT_MODEL,
        "model_names": parsed_model_names,
        "overwrite": bool(options.overwrite),
        "download_only": bool(options.download_only),
        "video_output_dir": str(options.video_output_dir or "").strip() or None,
        "download_workers": sanitize_worker_count(int(options.download_workers), maximum=MAX_DOWNLOAD_WORKERS),
        "asr_workers": sanitize_worker_count(int(options.asr_workers), maximum=MAX_ASR_WORKERS),
        "batch_delay_seconds": sanitize_batch_delay(
            int(options.batch_delay_seconds), maximum=MAX_BATCH_DELAY_SECONDS
        ),
        "anti_crawl_streak_threshold": sanitize_threshold(
            int(options.anti_crawl_streak_threshold),
            minimum=1,
            maximum=MAX_ANTI_CRAWL_STREAK_THRESHOLD,
        ),
        "anti_crawl_cooldown_seconds": sanitize_cooldown_minutes(
            int(options.anti_crawl_cooldown_minutes), maximum=MAX_COOLDOWN_MINUTES
        )
        * 60,
        "proactive_cooldown_batch_threshold": sanitize_threshold(
            int(options.proactive_cooldown_batch_threshold),
            minimum=1,
            maximum=MAX_PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
        ),
        "proactive_cooldown_seconds": sanitize_cooldown_minutes(
            int(options.proactive_cooldown_minutes), maximum=MAX_COOLDOWN_MINUTES
        )
        * 60,
    }


def _resume_output_path_for(safe_name: str) -> Optional[Path]:
    """续跑入口统一走 csv_service 的同名产物定位（按 mtime 取最新一份）。"""
    candidate = find_latest_processed_path(safe_name)
    return candidate if candidate is not None and candidate.exists() else None


def _has_existing_result(job) -> bool:
    return any(
        path is not None and Path(path).is_file()
        for path in (job.output_path, job.resume_output_path)
    )


def _discard_created_uploads(
    manager: JobManager,
    session_ids: List[str],
    batch_ids: List[str],
    upload_dirs: List[Path],
) -> None:
    """回滚本次请求创建的 session/batch 记录与上传目录，避免失败请求遗留孤儿数据。"""
    job_registry = getattr(manager, "_jobs", None)
    if isinstance(job_registry, dict):
        for session_id in session_ids:
            job_registry.pop(session_id, None)
    batch_registry = getattr(manager, "_batches", None)
    if isinstance(batch_registry, dict):
        for batch_id in batch_ids:
            batch_registry.pop(batch_id, None)
    for directory in upload_dirs:
        shutil.rmtree(directory, ignore_errors=True)


def _configure_and_start_job(job_id: str, options, mode: str) -> bool:
    """Atomically claim a job worker before resetting/configuring the job."""
    with _worker_registry_lock:
        if _job_start_conflict(job_id):
            return False
        job_manager.configure_job(job_id=job_id, **normalized_run_options(options))
        start_job_worker(job_id, mode=mode)
        return True


def _configure_and_start_batch(batch_id: str, options, mode: str) -> bool:
    """Atomically claim a batch worker before resetting/configuring the batch."""
    with _worker_registry_lock:
        if _batch_start_conflict(batch_id):
            return False
        job_manager.configure_batch(batch_id=batch_id, **normalized_run_options(options))
        start_batch_worker(batch_id, mode=mode)
        return True


def build_index_context(*, request: Request, batch=None, page_error: str = "") -> Dict[str, object]:
    return {
        "request": request,
        "batch": batch,
        "health": build_health_snapshot(),
        "cookie_pool": build_cookie_pool_snapshot(),
        "page_error": page_error,
        "default_transcript_column": DEFAULT_TRANSCRIPT_COLUMN,
        "default_model": DEFAULT_MODEL,
        "default_model_names_text": DEFAULT_MODEL,
        "model_presets": MODEL_PRESETS,
        "default_download_workers": DEFAULT_DOWNLOAD_WORKERS,
        "default_asr_workers": DEFAULT_ASR_WORKERS,
        "default_batch_delay_seconds": DEFAULT_BATCH_DELAY_SECONDS,
        "default_anti_crawl_streak_threshold": ANTI_CRAWL_STREAK_THRESHOLD,
        "default_anti_crawl_cooldown_minutes": ANTI_CRAWL_COOLDOWN_SECONDS // 60,
        "default_proactive_cooldown_batch_threshold": PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
        "default_proactive_cooldown_minutes": PROACTIVE_COOLDOWN_SECONDS // 60,
        "max_download_workers": MAX_DOWNLOAD_WORKERS,
        "max_asr_workers": MAX_ASR_WORKERS,
        "max_batch_delay_seconds": MAX_BATCH_DELAY_SECONDS,
        "max_anti_crawl_streak_threshold": MAX_ANTI_CRAWL_STREAK_THRESHOLD,
        "max_cooldown_minutes": MAX_COOLDOWN_MINUTES,
        "max_proactive_cooldown_batch_threshold": MAX_PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
    }


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", build_index_context(request=request))


@app.post("/preview")
async def preview_csv(request: Request, files: List[UploadFile] = File(...)):
    valid_files = [file for file in files if file.filename]
    if not valid_files:
        return templates.TemplateResponse(
            "index.html",
            build_index_context(request=request, page_error="请先选择文件"),
            status_code=400,
        )

    jobs = []
    inspections = []
    errors = []
    created_session_ids: List[str] = []
    created_upload_dirs: List[Path] = []
    try:
        for file in valid_files:
            safe_name = Path(file.filename or "").name
            save_path = save_upload_file(file)
            created_upload_dirs.append(save_path.parent)
            with save_path.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)

            try:
                inspection = await run_in_threadpool(inspect_csv, save_path)
                inspections.append(inspection)
                job = job_manager.create_session(
                    original_filename=safe_name,
                    upload_path=save_path,
                    columns=inspection.columns,
                    preview_rows=inspection.preview_rows,
                    row_count=inspection.row_count,
                    file_type=inspection.file_type,
                    resume_output_path=_resume_output_path_for(safe_name),
                )
                jobs.append(job)
                created_session_ids.append(job.job_id)
            except Exception as exc:
                errors.append(f"{safe_name}: {exc}")
                shutil.rmtree(save_path.parent, ignore_errors=True)
                if save_path.parent in created_upload_dirs:
                    created_upload_dirs.remove(save_path.parent)

        if errors:
            raise RuntimeError(" | ".join(errors))

        batch = job_manager.create_batch(
            jobs,
            common_columns=compute_common_columns([inspection.columns for inspection in inspections]),
        )
        return templates.TemplateResponse(
            "index.html",
            build_index_context(request=request, batch=build_batch_payload(batch.batch_id)),
        )
    except Exception as exc:
        _discard_created_uploads(job_manager, created_session_ids, [], created_upload_dirs)
        return templates.TemplateResponse(
            "index.html",
            build_index_context(request=request, page_error=str(exc)),
            status_code=400,
        )


@app.post("/quick-download")
async def quick_download(
    request: Request,
    links: str = Form(""),
    transcribe: bool = Form(False),
):
    link_lines = [line.strip() for line in links.splitlines()]
    link_lines = [line for line in link_lines if line]
    if not link_lines:
        return templates.TemplateResponse(
            "index.html",
            build_index_context(request=request, page_error="请至少粘贴一个链接"),
            status_code=400,
        )

    upload_dir = UPLOAD_DIR / uuid4().hex
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"quick-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    save_path = upload_dir / safe_name
    with save_path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["链接"])
        for line in link_lines:
            writer.writerow([line])

    inspection = await run_in_threadpool(inspect_csv, save_path)
    job = job_manager.create_session(
        original_filename=safe_name,
        upload_path=save_path,
        columns=inspection.columns,
        preview_rows=inspection.preview_rows,
        row_count=inspection.row_count,
        file_type=inspection.file_type,
    )
    batch = job_manager.create_batch([job], common_columns=["链接"])
    options = SimpleNamespace(
        url_column="链接",
        transcript_column=DEFAULT_TRANSCRIPT_COLUMN,
        model_names=DEFAULT_MODEL,
        overwrite=False,
        download_only=not transcribe,
        video_output_dir="",
        download_workers=DEFAULT_DOWNLOAD_WORKERS,
        asr_workers=DEFAULT_ASR_WORKERS,
        batch_delay_seconds=DEFAULT_BATCH_DELAY_SECONDS,
        anti_crawl_streak_threshold=ANTI_CRAWL_STREAK_THRESHOLD,
        anti_crawl_cooldown_minutes=ANTI_CRAWL_COOLDOWN_SECONDS // 60,
        proactive_cooldown_batch_threshold=PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
        proactive_cooldown_minutes=PROACTIVE_COOLDOWN_SECONDS // 60,
    )
    if not _configure_and_start_batch(batch.batch_id, options, mode="full"):
        _discard_created_uploads(job_manager, [job.job_id], [batch.batch_id], [save_path.parent])
        raise HTTPException(status_code=409, detail="批次已有 worker 在运行")
    return RedirectResponse(url=f"/batches/{batch.batch_id}", status_code=303)


@app.post("/run/{job_id}")
async def run_job(
    job_id: str,
    url_column: str = Form(...),
    transcript_column: str = Form(""),
    model_names: str = Form(DEFAULT_MODEL),
    overwrite: bool = Form(False),
    download_only: bool = Form(False),
    video_output_dir: str = Form(""),
    download_workers: int = Form(DEFAULT_DOWNLOAD_WORKERS),
    asr_workers: int = Form(DEFAULT_ASR_WORKERS),
    batch_delay_seconds: int = Form(DEFAULT_BATCH_DELAY_SECONDS),
    anti_crawl_streak_threshold: int = Form(ANTI_CRAWL_STREAK_THRESHOLD),
    anti_crawl_cooldown_minutes: int = Form(ANTI_CRAWL_COOLDOWN_SECONDS // 60),
    proactive_cooldown_batch_threshold: int = Form(PROACTIVE_COOLDOWN_BATCH_THRESHOLD),
    proactive_cooldown_minutes: int = Form(PROACTIVE_COOLDOWN_SECONDS // 60),
):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    if job.status == "running":
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    if url_column not in job.columns:
        raise HTTPException(status_code=409, detail="所选链接列不存在于该文件")

    if not _configure_and_start_job(job_id, _form_run_options(locals()), mode="full"):
        raise HTTPException(status_code=409, detail="任务或所属批次已有 worker 在运行")

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/batches/{batch_id}/run")
async def run_batch(
    batch_id: str,
    url_column: str = Form(...),
    transcript_column: str = Form(""),
    model_names: str = Form(DEFAULT_MODEL),
    overwrite: bool = Form(False),
    download_only: bool = Form(False),
    video_output_dir: str = Form(""),
    download_workers: int = Form(DEFAULT_DOWNLOAD_WORKERS),
    asr_workers: int = Form(DEFAULT_ASR_WORKERS),
    batch_delay_seconds: int = Form(DEFAULT_BATCH_DELAY_SECONDS),
    anti_crawl_streak_threshold: int = Form(ANTI_CRAWL_STREAK_THRESHOLD),
    anti_crawl_cooldown_minutes: int = Form(ANTI_CRAWL_COOLDOWN_SECONDS // 60),
    proactive_cooldown_batch_threshold: int = Form(PROACTIVE_COOLDOWN_BATCH_THRESHOLD),
    proactive_cooldown_minutes: int = Form(PROACTIVE_COOLDOWN_SECONDS // 60),
):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    if batch.status == "running":
        return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)

    if not batch.common_columns:
        raise HTTPException(status_code=409, detail="这批文件没有共同表头，无法统一配置")

    if url_column not in batch.common_columns:
        raise HTTPException(status_code=409, detail="所选链接列不在所有文件中都存在")

    if not _configure_and_start_batch(batch_id, _form_run_options(locals()), mode="full"):
        raise HTTPException(status_code=409, detail="批次已有 worker 在运行")
    return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/continue")
async def continue_batch(
    batch_id: str,
    url_column: str = Form(...),
    transcript_column: str = Form(""),
    model_names: str = Form(DEFAULT_MODEL),
    overwrite: bool = Form(False),
    download_only: bool = Form(False),
    video_output_dir: str = Form(""),
    download_workers: int = Form(DEFAULT_DOWNLOAD_WORKERS),
    asr_workers: int = Form(DEFAULT_ASR_WORKERS),
    batch_delay_seconds: int = Form(DEFAULT_BATCH_DELAY_SECONDS),
    anti_crawl_streak_threshold: int = Form(ANTI_CRAWL_STREAK_THRESHOLD),
    anti_crawl_cooldown_minutes: int = Form(ANTI_CRAWL_COOLDOWN_SECONDS // 60),
    proactive_cooldown_batch_threshold: int = Form(PROACTIVE_COOLDOWN_BATCH_THRESHOLD),
    proactive_cooldown_minutes: int = Form(PROACTIVE_COOLDOWN_SECONDS // 60),
):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    if batch.status == "running":
        return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)

    if not batch.common_columns:
        raise HTTPException(status_code=409, detail="这批文件没有共同表头，无法统一配置")

    if url_column not in batch.common_columns:
        raise HTTPException(status_code=409, detail="所选链接列不在所有文件中都存在")

    if not _configure_and_start_batch(
        batch_id,
        _form_run_options(locals()),
        mode="continue_existing",
    ):
        raise HTTPException(status_code=409, detail="批次已有 worker 在运行")
    return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/pause")
async def pause_batch(batch_id: str):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status != "running":
        raise HTTPException(status_code=409, detail="当前批次不在运行中")

    job_manager.request_batch_pause(batch_id)
    if batch.current_job_id:
        current_job = job_manager.get(batch.current_job_id)
        if current_job and current_job.status == "running":
            job_manager.request_pause(batch.current_job_id)
    return JSONResponse({"ok": True})


@app.post("/batches/{batch_id}/resume")
async def resume_batch(batch_id: str):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status != "paused":
        raise HTTPException(status_code=409, detail="当前批次不在暂停状态")

    with _worker_registry_lock:
        if _batch_start_conflict(batch_id):
            raise HTTPException(status_code=409, detail="批次或其子任务已有 worker 在运行")
        job_manager.queue_existing_batch(batch_id, "批次已恢复")
        if batch.current_job_id:
            current_job = job_manager.get(batch.current_job_id)
            if current_job and current_job.status == "paused":
                job_manager.clear_pause(batch.current_job_id)
        job_manager.clear_batch_pause(batch_id)
        start_batch_worker(batch_id, mode="resume")
    return JSONResponse({"ok": True})


@app.post("/batches/{batch_id}/skip-cooldown")
async def skip_batch_cooldown(batch_id: str):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    if not batch.cooldown_active:
        raise HTTPException(status_code=409, detail="当前批次不在冷却中")

    job_manager.request_batch_skip_cooldown(batch_id)
    if batch.current_job_id:
        current_job = job_manager.get(batch.current_job_id)
        if current_job and current_job.cooldown_active:
            job_manager.request_skip_cooldown(batch.current_job_id)
    return JSONResponse({"ok": True})


@app.post("/batches/{batch_id}/retry-failed")
async def retry_failed_batch(batch_id: str):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    with _worker_registry_lock:
        if _batch_start_conflict(batch_id):
            raise HTTPException(status_code=409, detail="批次或其子任务已有 worker 在运行")
        retryable_job_ids = []
        for job_id in batch.job_ids:
            job = job_manager.require(job_id)
            if job.failed_rows <= 0 or job.status == "running":
                continue
            retryable_job_ids.append(job_id)
        if not retryable_job_ids:
            raise HTTPException(status_code=409, detail="当前批次没有可重试的失败项")

        job_manager.queue_existing_batch(batch_id, "失败项重试中")
        if not start_batch_worker(batch_id, mode="retry_failed"):
            raise HTTPException(status_code=409, detail="批次已有 worker 在运行")
    return JSONResponse({"ok": True, "jobs": len(retryable_job_ids)})


@app.post("/batches/{batch_id}/switch-cookie")
async def switch_batch_cookie(batch_id: str):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    available_accounts = _available_cookie_accounts()
    if not available_accounts:
        raise HTTPException(status_code=409, detail='没有可用的 cookies，请先运行 `./login "备注"` 添加账号')
    if len(batch.job_ids) == 0:
        raise HTTPException(status_code=409, detail="当前批次没有可切换的任务")

    anchor_job_id = batch.current_job_id or batch.job_ids[0]
    if batch.status == "running":
        if len(available_accounts) <= 1:
            raise HTTPException(status_code=409, detail="当前只有 1 个可用 cookies，无法切换")
        job_manager.request_batch_cookie_switch(batch_id)
        return JSONResponse({"ok": True, "scheduled": True})

    try:
        account = switch_cookie_now_for_job(anchor_job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "scheduled": False, "account_label": account.label})


@app.post("/accounts")
async def add_cookie_account(label: str = Form("")):
    normalized_label = str(label or "").strip()
    if not normalized_label:
        raise HTTPException(status_code=400, detail="请输入备注")
    existing = next(
        (account for account in cookie_account_manager.list_accounts() if account.label == normalized_label),
        None,
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"备注 `{normalized_label}` 已存在，如需更新请使用「重新登录」")

    account = cookie_account_manager.prepare_account(normalized_label)
    try:
        start_account_relogin(account.account_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(
        {
            "ok": True,
            "account_id": account.account_id,
            "message": f"已为 `{account.label}` 打开登录窗口，扫码完成登录后关闭该窗口即可保存。",
        }
    )


@app.post("/accounts/{account_id}/delete")
async def delete_cookie_account(account_id: str):
    account = _reconcile_account_relogin_state(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="cookies 账号不存在")
    if account.relogin_in_progress:
        raise HTTPException(status_code=409, detail="该账号正在重新登录中")

    try:
        cookie_account_manager.delete_account(account_id)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"账号文件删除失败：{exc}") from exc
    return JSONResponse({"ok": True, "status": "deleted"})


@app.post("/accounts/{account_id}/reset")
async def reset_cookie_account(account_id: str):
    account = _reconcile_account_relogin_state(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="cookies 账号不存在")
    if account.relogin_in_progress:
        raise HTTPException(status_code=409, detail="该账号正在重新登录中")

    cookie_account_manager.reset_account_status(account_id)
    return JSONResponse({"ok": True, "status": "reset"})


@app.post("/accounts/{account_id}/relogin")
async def relogin_cookie_account(account_id: str):
    account = _reconcile_account_relogin_state(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="cookies 账号不存在")

    try:
        start_account_relogin(account_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(
        {
            "ok": True,
            "status": "relogin_started",
            "message": f"已为 `{account.label}` 打开重新登录窗口。登录完成后直接关闭该浏览器窗口即可保存。",
        }
    )


@app.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != "running":
        raise HTTPException(status_code=409, detail="当前任务不在运行中")

    job_manager.request_pause(job_id)
    return JSONResponse({"ok": True})


@app.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != "paused":
        raise HTTPException(status_code=409, detail="当前任务不在暂停状态")

    with _worker_registry_lock:
        if _job_start_conflict(job_id):
            raise HTTPException(status_code=409, detail="任务已有 worker 在运行")
        job_manager.queue_existing_job(job_id, "任务已恢复")
        start_job_worker(job_id, mode="resume")
    return JSONResponse({"ok": True})


@app.post("/jobs/{job_id}/skip-cooldown")
async def skip_job_cooldown(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not job.cooldown_active:
        raise HTTPException(status_code=409, detail="当前任务不在冷却中")

    job_manager.request_skip_cooldown(job_id)
    if job.batch_id:
        batch = job_manager.get_batch(job.batch_id)
        if batch and batch.cooldown_active:
            job_manager.request_batch_skip_cooldown(job.batch_id)
    return JSONResponse({"ok": True})


@app.post("/jobs/{job_id}/switch-cookie")
async def switch_job_cookie(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    available_accounts = _available_cookie_accounts()
    if not available_accounts:
        raise HTTPException(status_code=409, detail='没有可用的 cookies，请先运行 `./login "备注"` 添加账号')

    if job.status == "running":
        if len(available_accounts) <= 1:
            raise HTTPException(status_code=409, detail="当前只有 1 个可用 cookies，无法切换")
        if job.batch_id:
            job_manager.request_batch_cookie_switch(job.batch_id)
        else:
            job_manager.request_cookie_switch(job_id)
        return JSONResponse({"ok": True, "scheduled": True})

    try:
        account = switch_cookie_now_for_job(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "scheduled": False, "account_label": account.label})


@app.post("/jobs/{job_id}/continue")
async def continue_job(
    job_id: str,
    url_column: str = Form(...),
    transcript_column: str = Form(""),
    model_names: str = Form(DEFAULT_MODEL),
    overwrite: bool = Form(False),
    download_only: bool = Form(False),
    video_output_dir: str = Form(""),
    download_workers: int = Form(DEFAULT_DOWNLOAD_WORKERS),
    asr_workers: int = Form(DEFAULT_ASR_WORKERS),
    batch_delay_seconds: int = Form(DEFAULT_BATCH_DELAY_SECONDS),
    anti_crawl_streak_threshold: int = Form(ANTI_CRAWL_STREAK_THRESHOLD),
    anti_crawl_cooldown_minutes: int = Form(ANTI_CRAWL_COOLDOWN_SECONDS // 60),
    proactive_cooldown_batch_threshold: int = Form(PROACTIVE_COOLDOWN_BATCH_THRESHOLD),
    proactive_cooldown_minutes: int = Form(PROACTIVE_COOLDOWN_SECONDS // 60),
):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    if job.status == "running":
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    if url_column not in job.columns:
        raise HTTPException(status_code=409, detail="所选链接列不存在于该文件")

    if not _has_existing_result(job):
        raise HTTPException(status_code=409, detail="没有可续跑的结果文件")

    if not _configure_and_start_job(
        job_id,
        _form_run_options(locals()),
        mode="continue_existing",
    ):
        raise HTTPException(status_code=409, detail="任务或所属批次已有 worker 在运行")
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/start")
async def start_job_with_stored_options(job_id: str, mode: str = Form("full")):
    if mode not in {"full", "continue_existing"}:
        raise HTTPException(status_code=400, detail="不支持的运行模式")

    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    if job.status == "running":
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    source = job
    if job.url_column is None and job.batch_id:
        batch = job_manager.get_batch(job.batch_id)
        if batch and batch.url_column is not None:
            source = batch
    if source.url_column is None:
        raise HTTPException(
            status_code=409,
            detail="该任务还没有运行参数，请先在首页配置批次运行参数",
        )

    options = SimpleNamespace(
        url_column=source.url_column,
        transcript_column=source.transcript_column,
        model_names=source.model_names_text,
        overwrite=source.overwrite,
        download_only=source.download_only,
        video_output_dir=source.video_output_dir or "",
        download_workers=source.download_workers,
        asr_workers=source.asr_workers,
        batch_delay_seconds=source.batch_delay_seconds,
        anti_crawl_streak_threshold=source.anti_crawl_streak_threshold,
        anti_crawl_cooldown_minutes=source.anti_crawl_cooldown_seconds // 60,
        proactive_cooldown_batch_threshold=source.proactive_cooldown_batch_threshold,
        proactive_cooldown_minutes=source.proactive_cooldown_seconds // 60,
    )

    if mode == "continue_existing" and not _has_existing_result(job):
        raise HTTPException(status_code=409, detail="没有可续跑的结果文件")

    if not _configure_and_start_job(job_id, options, mode=mode):
        raise HTTPException(status_code=409, detail="任务或所属批次已有 worker 在运行")
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/retry-failed")
async def retry_failed_job(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status == "running":
        raise HTTPException(status_code=409, detail="任务运行中，无法重试失败项")
    if job.failed_rows <= 0:
        raise HTTPException(status_code=409, detail="当前没有可重试的失败项")

    with _worker_registry_lock:
        if _job_start_conflict(job_id):
            raise HTTPException(status_code=409, detail="任务已有 worker 在运行")
        job_manager.queue_existing_job(job_id, "失败项重试中")
        start_job_worker(job_id, mode="retry_failed")
    return JSONResponse({"ok": True})


@app.get("/jobs/{job_id}")
async def job_detail(request: Request, job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    return templates.TemplateResponse(
        "job.html",
        {
            "request": request,
            "job": build_job_payload(job_id),
            "health": build_health_snapshot(),
        },
    )


@app.get("/batches/{batch_id}")
async def batch_detail(request: Request, batch_id: str):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    return templates.TemplateResponse(
        "batch.html",
        {
            "request": request,
            "batch": build_batch_payload(batch_id),
            "health": build_health_snapshot(),
        },
    )


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JSONResponse(build_job_payload(job_id))


@app.get("/api/jobs/{job_id}/rows")
async def job_rows(job_id: str, offset: int = 0, limit: int = 50):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    source_path = job.output_path
    if not source_path or not Path(source_path).exists():
        source_path = job.upload_path
    try:
        inspection = await run_in_threadpool(inspect_csv, Path(source_path))
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=f"结果文件不存在或暂不可读：{exc}")
    dataframe = inspection.dataframe
    total = len(dataframe)
    offset = max(0, offset)
    limit = max(1, min(int(limit), 200))
    page_rows = [
        {column: stringify_cell(value) for column, value in row.items()}
        for row in dataframe.iloc[offset : offset + limit].to_dict(orient="records")
    ]
    return JSONResponse(
        {
            "total": total,
            "offset": offset,
            "limit": limit,
            "columns": inspection.columns,
            "rows": page_rows,
        }
    )


@app.get("/api/batches/{batch_id}")
async def batch_status(batch_id: str):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return JSONResponse(build_batch_payload(batch_id))


@app.get("/api/accounts")
async def api_accounts():
    return JSONResponse(build_cookie_pool_snapshot())


@app.post("/api/preview")
async def api_preview_csv(files: List[UploadFile] = File(...)):
    """无头上传表格：返回 batch_id、每个文件的 job_id、表头和行数（JSON）。"""
    valid_files = [file for file in files if file.filename]
    if not valid_files:
        raise HTTPException(status_code=400, detail="请先选择文件")

    jobs = []
    inspections = []
    jobs_payload = []
    created_session_ids: List[str] = []
    created_upload_dirs: List[Path] = []
    try:
        for file in valid_files:
            safe_name = Path(file.filename or "").name
            save_path = save_upload_file(file)
            created_upload_dirs.append(save_path.parent)
            with save_path.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)

            try:
                inspection = await run_in_threadpool(inspect_csv, save_path)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"{safe_name}: {exc}")

            job = job_manager.create_session(
                original_filename=safe_name,
                upload_path=save_path,
                columns=inspection.columns,
                preview_rows=inspection.preview_rows,
                row_count=inspection.row_count,
                file_type=inspection.file_type,
                resume_output_path=_resume_output_path_for(safe_name),
            )
            created_session_ids.append(job.job_id)
            inspections.append(inspection)
            jobs.append(job)
            jobs_payload.append(
                {
                    "job_id": job.job_id,
                    "original_filename": job.original_filename,
                    "columns": inspection.columns,
                    "row_count": inspection.row_count,
                    "file_type": inspection.file_type,
                    "preview_rows": inspection.preview_rows,
                    "resume_output_path": str(job.resume_output_path) if job.resume_output_path else None,
                }
            )

        batch = job_manager.create_batch(
            jobs,
            common_columns=compute_common_columns([inspection.columns for inspection in inspections]),
        )
    except Exception:
        _discard_created_uploads(job_manager, created_session_ids, [], created_upload_dirs)
        raise
    return JSONResponse(
        {
            "batch_id": batch.batch_id,
            "common_columns": batch.common_columns,
            "jobs": jobs_payload,
        }
    )


def _start_job_from_api(job_id: str, options: ApiRunOptions, mode: str) -> Dict[str, object]:
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status == "running":
        return build_job_payload(job_id)
    if mode == "continue_existing" and not _has_existing_result(job):
        raise HTTPException(status_code=409, detail="没有可续跑的结果文件")
    if options.url_column not in job.columns:
        raise HTTPException(status_code=409, detail="所选链接列不存在于该文件")
    if not _configure_and_start_job(job_id, options, mode=mode):
        raise HTTPException(status_code=409, detail="任务或所属批次已有 worker 在运行")
    return build_job_payload(job_id)


def _start_batch_from_api(batch_id: str, options: ApiRunOptions, mode: str) -> Dict[str, object]:
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status == "running":
        return build_batch_payload(batch_id)
    if mode == "continue_existing" and not any(
        _has_existing_result(job_manager.require(job_id)) for job_id in batch.job_ids
    ):
        raise HTTPException(status_code=409, detail="当前批次没有可续跑的结果文件")
    if not batch.common_columns:
        raise HTTPException(status_code=409, detail="这批文件没有共同表头，无法统一配置")
    if options.url_column not in batch.common_columns:
        raise HTTPException(status_code=409, detail="所选链接列不在所有文件中都存在")
    if not _configure_and_start_batch(batch_id, options, mode=mode):
        raise HTTPException(status_code=409, detail="批次已有 worker 在运行")
    return build_batch_payload(batch_id)


@app.post("/api/run/{job_id}")
async def api_run_job(job_id: str, options: ApiRunOptions):
    return JSONResponse(_start_job_from_api(job_id, options, mode="full"))


@app.post("/api/jobs/{job_id}/continue")
async def api_continue_job(job_id: str, options: ApiRunOptions):
    return JSONResponse(_start_job_from_api(job_id, options, mode="continue_existing"))


@app.post("/api/batches/{batch_id}/run")
async def api_run_batch(batch_id: str, options: ApiRunOptions):
    return JSONResponse(_start_batch_from_api(batch_id, options, mode="full"))


@app.post("/api/batches/{batch_id}/continue")
async def api_continue_batch(batch_id: str, options: ApiRunOptions):
    return JSONResponse(_start_batch_from_api(batch_id, options, mode="continue_existing"))


@app.get("/download/{job_id}")
async def download_result(job_id: str):
    job = job_manager.get(job_id)
    if not job or not job.output_path:
        raise HTTPException(status_code=404, detail="结果文件不存在")
    return FileResponse(
        path=job.output_path,
        media_type=media_type_for_path(Path(job.output_path)),
        filename=job.output_path.name,
    )


@app.get("/download/batches/{batch_id}")
async def download_batch_results(batch_id: str):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    jobs = [job_manager.require(job_id) for job_id in batch.job_ids]
    ready_jobs = [job for job in jobs if job.output_path and Path(job.output_path).exists()]
    if not ready_jobs:
        raise HTTPException(status_code=404, detail="当前批次还没有可下载的结果文件")

    with tempfile.NamedTemporaryFile(prefix="douyin-batch-", suffix=".zip", delete=False) as temporary:
        archive_path = Path(temporary.name)
    try:
        await run_in_threadpool(_write_batch_zip, ready_jobs, archive_path)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    filename = f"batch_{batch_id[:8]}.zip"
    return FileResponse(
        path=archive_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


def _write_batch_zip(jobs, archive_path: Path) -> None:
    """Build a batch archive off the event loop and stream it from disk."""
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        used_names = set()
        for job in jobs:
            arcname = Path(job.output_path).name
            if arcname in used_names:
                # 正常命名已含 job_id 前 8 位不会撞名；此处兜底防同名文件在 zip 内互相覆盖
                stem = Path(arcname).stem
                suffix = Path(arcname).suffix
                if stem.endswith(".processed"):
                    arcname = f"{stem[: -len('.processed')]}-{job.job_id[:8]}.processed.csv"
                else:
                    arcname = f"{stem}-{job.job_id[:8]}{suffix}"
            used_names.add(arcname)
            archive.write(job.output_path, arcname=arcname)


@app.get("/health")
async def health():
    return build_health_snapshot()


def run() -> None:
    import uvicorn

    # 本机默认仍只监听回环地址；容器部署通过环境变量监听容器网卡。
    uvicorn.run("app.main:app", host=WEBUI_HOST, port=WEBUI_PORT, reload=False)


if __name__ == "__main__":
    run()
