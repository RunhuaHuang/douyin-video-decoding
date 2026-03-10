import threading
from io import BytesIO
from pathlib import Path
from typing import Dict, List
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
    ensure_directories,
)
from .csv_service import inspect_csv, media_type_for_path, processed_output_path_for_filename
from .job_manager import JobManager
from .media_service import FfmpegService
from .pipeline import JobProcessor
from .transcription_service import TranscriptionService


ensure_directories()

app = FastAPI(title="抖音文案提取工具")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

job_manager = JobManager()
job_processor = JobProcessor(job_manager)
ffmpeg_service = FfmpegService()
transcription_service = TranscriptionService(default_model=DEFAULT_MODEL)

MAX_ANTI_CRAWL_STREAK_THRESHOLD = 50
MAX_COOLDOWN_MINUTES = 12 * 60
MAX_PROACTIVE_COOLDOWN_BATCH_THRESHOLD = 500


def start_job_worker(job_id: str, mode: str = "full") -> None:
    worker = threading.Thread(
        target=job_processor.process,
        kwargs={"job_id": job_id, "mode": mode},
        daemon=True,
        name=f"douyin-job-{job_id[:8]}-{mode}",
    )
    worker.start()


def start_batch_worker(batch_id: str, mode: str = "full") -> None:
    worker = threading.Thread(
        target=process_batch,
        kwargs={"batch_id": batch_id, "mode": mode},
        daemon=True,
        name=f"douyin-batch-{batch_id[:8]}-{mode}",
    )
    worker.start()


def process_batch(batch_id: str, mode: str = "full") -> None:
    batch = job_manager.require_batch(batch_id)
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
            job_mode = "resume" if resume_current and index == start_index and job.status == "paused" else "full"
            if mode == "continue_existing" and job_mode == "full":
                job_mode = "continue_existing"

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
        "ffmpeg": "已检测" if ffmpeg_service.is_available() else "未检测到",
        "mlx": runtime["mlx"],
        "sensevoice": runtime["sensevoice"],
        "default_model": DEFAULT_MODEL,
    }


def sanitize_worker_count(value: int, *, maximum: int) -> int:
    return max(1, min(int(value), maximum))


def sanitize_batch_delay(value: int, *, maximum: int) -> int:
    return max(0, min(int(value), maximum))


def sanitize_threshold(value: int, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))


def sanitize_cooldown_minutes(value: int, *, maximum: int) -> int:
    return max(0, min(int(value), maximum))


def build_index_context(*, request: Request, batch=None, page_error: str = "") -> Dict[str, object]:
    return {
        "request": request,
        "batch": batch,
        "health": build_health_snapshot(),
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
    try:
        for file in valid_files:
            safe_name = Path(file.filename or "").name
            save_path = save_upload_file(file)
            with save_path.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)

            try:
                inspection = inspect_csv(save_path)
                inspections.append(inspection)
                jobs.append(
                    job_manager.create_session(
                        original_filename=safe_name,
                        upload_path=save_path,
                        columns=inspection.columns,
                        preview_rows=inspection.preview_rows,
                        row_count=inspection.row_count,
                        file_type=inspection.file_type,
                        resume_output_path=(
                            processed_output_path_for_filename(safe_name)
                            if processed_output_path_for_filename(safe_name).exists()
                            else None
                        ),
                    )
                )
            except Exception as exc:
                errors.append(f"{safe_name}: {exc}")

        if errors:
            raise RuntimeError(" | ".join(errors))

        batch = job_manager.create_batch(
            jobs,
            common_columns=compute_common_columns([inspection.columns for inspection in inspections]),
        )
        return templates.TemplateResponse(
            "index.html",
            build_index_context(request=request, batch=job_manager.batch_snapshot(batch.batch_id)),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "index.html",
            build_index_context(request=request, page_error=str(exc)),
            status_code=400,
        )


@app.post("/run/{job_id}")
async def run_job(
    job_id: str,
    url_column: str = Form(...),
    transcript_column: str = Form(...),
    model_names: str = Form(DEFAULT_MODEL),
    overwrite: bool = Form(False),
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

    parsed_model_names = transcription_service.normalize_model_names(model_names.strip())
    download_workers = sanitize_worker_count(download_workers, maximum=MAX_DOWNLOAD_WORKERS)
    asr_workers = sanitize_worker_count(asr_workers, maximum=MAX_ASR_WORKERS)
    batch_delay_seconds = sanitize_batch_delay(batch_delay_seconds, maximum=MAX_BATCH_DELAY_SECONDS)
    anti_crawl_streak_threshold = sanitize_threshold(
        anti_crawl_streak_threshold,
        minimum=1,
        maximum=MAX_ANTI_CRAWL_STREAK_THRESHOLD,
    )
    anti_crawl_cooldown_minutes = sanitize_cooldown_minutes(
        anti_crawl_cooldown_minutes,
        maximum=MAX_COOLDOWN_MINUTES,
    )
    proactive_cooldown_batch_threshold = sanitize_threshold(
        proactive_cooldown_batch_threshold,
        minimum=1,
        maximum=MAX_PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
    )
    proactive_cooldown_minutes = sanitize_cooldown_minutes(
        proactive_cooldown_minutes,
        maximum=MAX_COOLDOWN_MINUTES,
    )

    job_manager.configure_job(
        job_id=job_id,
        url_column=url_column,
        transcript_column=transcript_column.strip() or DEFAULT_TRANSCRIPT_COLUMN,
        model_names_text=model_names.strip() or DEFAULT_MODEL,
        model_names=parsed_model_names,
        overwrite=overwrite,
        download_workers=download_workers,
        asr_workers=asr_workers,
        batch_delay_seconds=batch_delay_seconds,
        anti_crawl_streak_threshold=anti_crawl_streak_threshold,
        anti_crawl_cooldown_seconds=anti_crawl_cooldown_minutes * 60,
        proactive_cooldown_batch_threshold=proactive_cooldown_batch_threshold,
        proactive_cooldown_seconds=proactive_cooldown_minutes * 60,
    )

    start_job_worker(job_id, mode="full")

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/batches/{batch_id}/run")
async def run_batch(
    batch_id: str,
    url_column: str = Form(...),
    transcript_column: str = Form(...),
    model_names: str = Form(DEFAULT_MODEL),
    overwrite: bool = Form(False),
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

    parsed_model_names = transcription_service.normalize_model_names(model_names.strip())
    download_workers = sanitize_worker_count(download_workers, maximum=MAX_DOWNLOAD_WORKERS)
    asr_workers = sanitize_worker_count(asr_workers, maximum=MAX_ASR_WORKERS)
    batch_delay_seconds = sanitize_batch_delay(batch_delay_seconds, maximum=MAX_BATCH_DELAY_SECONDS)
    anti_crawl_streak_threshold = sanitize_threshold(
        anti_crawl_streak_threshold,
        minimum=1,
        maximum=MAX_ANTI_CRAWL_STREAK_THRESHOLD,
    )
    anti_crawl_cooldown_minutes = sanitize_cooldown_minutes(
        anti_crawl_cooldown_minutes,
        maximum=MAX_COOLDOWN_MINUTES,
    )
    proactive_cooldown_batch_threshold = sanitize_threshold(
        proactive_cooldown_batch_threshold,
        minimum=1,
        maximum=MAX_PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
    )
    proactive_cooldown_minutes = sanitize_cooldown_minutes(
        proactive_cooldown_minutes,
        maximum=MAX_COOLDOWN_MINUTES,
    )
    job_manager.configure_batch(
        batch_id=batch_id,
        url_column=url_column,
        transcript_column=transcript_column.strip() or DEFAULT_TRANSCRIPT_COLUMN,
        model_names_text=model_names.strip() or DEFAULT_MODEL,
        model_names=parsed_model_names,
        overwrite=overwrite,
        download_workers=download_workers,
        asr_workers=asr_workers,
        batch_delay_seconds=batch_delay_seconds,
        anti_crawl_streak_threshold=anti_crawl_streak_threshold,
        anti_crawl_cooldown_seconds=anti_crawl_cooldown_minutes * 60,
        proactive_cooldown_batch_threshold=proactive_cooldown_batch_threshold,
        proactive_cooldown_seconds=proactive_cooldown_minutes * 60,
    )
    start_batch_worker(batch_id, mode="full")
    return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)


@app.post("/batches/{batch_id}/continue")
async def continue_batch(
    batch_id: str,
    url_column: str = Form(...),
    transcript_column: str = Form(...),
    model_names: str = Form(DEFAULT_MODEL),
    overwrite: bool = Form(False),
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

    parsed_model_names = transcription_service.normalize_model_names(model_names.strip())
    download_workers = sanitize_worker_count(download_workers, maximum=MAX_DOWNLOAD_WORKERS)
    asr_workers = sanitize_worker_count(asr_workers, maximum=MAX_ASR_WORKERS)
    batch_delay_seconds = sanitize_batch_delay(batch_delay_seconds, maximum=MAX_BATCH_DELAY_SECONDS)
    anti_crawl_streak_threshold = sanitize_threshold(
        anti_crawl_streak_threshold,
        minimum=1,
        maximum=MAX_ANTI_CRAWL_STREAK_THRESHOLD,
    )
    anti_crawl_cooldown_minutes = sanitize_cooldown_minutes(
        anti_crawl_cooldown_minutes,
        maximum=MAX_COOLDOWN_MINUTES,
    )
    proactive_cooldown_batch_threshold = sanitize_threshold(
        proactive_cooldown_batch_threshold,
        minimum=1,
        maximum=MAX_PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
    )
    proactive_cooldown_minutes = sanitize_cooldown_minutes(
        proactive_cooldown_minutes,
        maximum=MAX_COOLDOWN_MINUTES,
    )
    job_manager.configure_batch(
        batch_id=batch_id,
        url_column=url_column,
        transcript_column=transcript_column.strip() or DEFAULT_TRANSCRIPT_COLUMN,
        model_names_text=model_names.strip() or DEFAULT_MODEL,
        model_names=parsed_model_names,
        overwrite=overwrite,
        download_workers=download_workers,
        asr_workers=asr_workers,
        batch_delay_seconds=batch_delay_seconds,
        anti_crawl_streak_threshold=anti_crawl_streak_threshold,
        anti_crawl_cooldown_seconds=anti_crawl_cooldown_minutes * 60,
        proactive_cooldown_batch_threshold=proactive_cooldown_batch_threshold,
        proactive_cooldown_seconds=proactive_cooldown_minutes * 60,
    )
    start_batch_worker(batch_id, mode="continue_existing")
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


@app.post("/jobs/{job_id}/continue")
async def continue_job(
    job_id: str,
    url_column: str = Form(...),
    transcript_column: str = Form(...),
    model_names: str = Form(DEFAULT_MODEL),
    overwrite: bool = Form(False),
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

    parsed_model_names = transcription_service.normalize_model_names(model_names.strip())
    download_workers = sanitize_worker_count(download_workers, maximum=MAX_DOWNLOAD_WORKERS)
    asr_workers = sanitize_worker_count(asr_workers, maximum=MAX_ASR_WORKERS)
    batch_delay_seconds = sanitize_batch_delay(batch_delay_seconds, maximum=MAX_BATCH_DELAY_SECONDS)
    anti_crawl_streak_threshold = sanitize_threshold(
        anti_crawl_streak_threshold,
        minimum=1,
        maximum=MAX_ANTI_CRAWL_STREAK_THRESHOLD,
    )
    anti_crawl_cooldown_minutes = sanitize_cooldown_minutes(
        anti_crawl_cooldown_minutes,
        maximum=MAX_COOLDOWN_MINUTES,
    )
    proactive_cooldown_batch_threshold = sanitize_threshold(
        proactive_cooldown_batch_threshold,
        minimum=1,
        maximum=MAX_PROACTIVE_COOLDOWN_BATCH_THRESHOLD,
    )
    proactive_cooldown_minutes = sanitize_cooldown_minutes(
        proactive_cooldown_minutes,
        maximum=MAX_COOLDOWN_MINUTES,
    )

    job_manager.configure_job(
        job_id=job_id,
        url_column=url_column,
        transcript_column=transcript_column.strip() or DEFAULT_TRANSCRIPT_COLUMN,
        model_names_text=model_names.strip() or DEFAULT_MODEL,
        model_names=parsed_model_names,
        overwrite=overwrite,
        download_workers=download_workers,
        asr_workers=asr_workers,
        batch_delay_seconds=batch_delay_seconds,
        anti_crawl_streak_threshold=anti_crawl_streak_threshold,
        anti_crawl_cooldown_seconds=anti_crawl_cooldown_minutes * 60,
        proactive_cooldown_batch_threshold=proactive_cooldown_batch_threshold,
        proactive_cooldown_seconds=proactive_cooldown_minutes * 60,
    )

    start_job_worker(job_id, mode="continue_existing")
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
            "job": job.to_dict(),
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
            "batch": job_manager.batch_snapshot(batch_id),
        },
    )


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JSONResponse(job.to_dict())


@app.get("/api/batches/{batch_id}")
async def batch_status(batch_id: str):
    batch = job_manager.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return JSONResponse(job_manager.batch_snapshot(batch_id))


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

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for job in ready_jobs:
            archive.write(job.output_path, arcname=Path(job.output_path).name)

    buffer.seek(0)
    filename = f"batch_{batch_id[:8]}.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(buffer, media_type="application/zip", headers=headers)


@app.get("/health")
async def health():
    return build_health_snapshot()


def run() -> None:
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
