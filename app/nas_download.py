"""Run one spreadsheet download job inside the NAS container.

The command deliberately processes one input file and exits. A caller can
invoke it again after a failure; completed ``视频-ID.mp4``/``图文-ID.mp4``
files are skipped and the remaining rows continue from the same output
directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import VIDEO_OUTPUT_DIR
from .video_downloader import VideoDownloader


PROGRESS_FILENAME = ".douyin-download-progress.json"
DEFAULT_STORAGE_ROOT = Path(os.environ.get("DOUYIN_STORAGE_ROOT", "/nas/videos")).expanduser()
DEFAULT_BATCH_OUTPUT_ROOT = Path(
    os.environ.get("DOUYIN_BATCH_OUTPUT_ROOT", str(DEFAULT_STORAGE_ROOT / "视频下载"))
).expanduser()
HOST_STORAGE_ROOT = Path("/volume5/视频下载")


def _map_storage_path(path: Path) -> Path:
    """Map the NAS host path to the bind-mounted path inside the container."""
    try:
        relative = path.relative_to(HOST_STORAGE_ROOT)
    except ValueError:
        return path
    return DEFAULT_STORAGE_ROOT / relative


def _resolve_input_path(raw_path: str) -> Path:
    path = _map_storage_path(Path(raw_path).expanduser())
    if not path.exists():
        raise FileNotFoundError(f"Excel 文件不存在（容器路径）: {path}")
    if not path.is_file():
        raise ValueError(f"输入路径不是文件: {path}")
    if path.suffix.lower() not in {".xlsx", ".xls"}:
        raise ValueError(f"只支持 Excel 文件（.xlsx/.xls）: {path.name}")
    return path


def _resolve_output_root(raw_path: Optional[str]) -> Path:
    return _map_storage_path(Path(raw_path).expanduser()) if raw_path else DEFAULT_BATCH_OUTPUT_ROOT


def _progress_path(input_path: Path, output_root: Path) -> Path:
    return output_root / input_path.stem / PROGRESS_FILENAME


def _file_signature(path: Path) -> Dict[str, int]:
    stat = path.stat()
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _read_progress(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_progress(path: Path, payload: Dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_download(
    input_path: Path,
    *,
    output_root: Path,
    url_column: str = "视频链接",
    skip_existing: bool = True,
    workers: int = 2,
) -> int:
    output_dir = output_root / input_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / PROGRESS_FILENAME
    signature = _file_signature(input_path)
    previous = _read_progress(progress_path)

    if (
        skip_existing
        and previous.get("status") == "completed"
        and previous.get("input_signature") == signature
    ):
        print(f"已完成，跳过: {input_path} -> {output_dir}")
        return 0

    progress: Dict[str, Any] = {
        "status": "running",
        "input_path": str(input_path),
        "input_signature": signature,
        "output_dir": str(output_dir),
        "total_count": 0,
        "processed_count": 0,
        "success_count": 0,
        "fail_count": 0,
        "percent": 0.0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "previous_status": previous.get("status", ""),
    }
    _write_progress(progress_path, progress)

    def report_progress(index: int, total: int, success: int, failed: int, message: str) -> None:
        progress.update(
            {
                "total_count": total,
                "processed_count": index,
                "success_count": success,
                "fail_count": failed,
                "percent": round(index * 100 / total, 2) if total else 100.0,
                "current_message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _write_progress(progress_path, progress)
        print(
            "PROGRESS "
            + json.dumps(
                {
                    "processed": index,
                    "total": total,
                    "success": success,
                    "failed": failed,
                    "percent": progress["percent"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    downloader = VideoDownloader()
    try:
        success_count, fail_count = downloader.download_file(
            table_path=input_path,
            out_dir=output_dir,
            url_column=url_column or None,
            skip_existing=skip_existing,
            progress_callback=report_progress,
            workers=workers,
        )
    except Exception as exc:
        progress.update(
            {
                "status": "failed",
                "success_count": 0,
                "fail_count": 0,
                "error": str(exc),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _write_progress(progress_path, progress)
        raise
    finally:
        downloader.close()

    progress.update(
        {
            "status": "completed" if fail_count == 0 else "partial",
            "success_count": success_count,
            "fail_count": fail_count,
            "processed_count": success_count + fail_count,
            "percent": 100.0 if success_count + fail_count else 0.0,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_progress(progress_path, progress)
    return 0 if fail_count == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="douyin-batch-download",
        description="下载一个 Excel 的‘视频链接’列；重复执行会跳过已完成文件并断点续接。",
    )
    parser.add_argument("input", help="Excel 路径，可传 NAS 宿主机路径 /volume5/视频下载/... 或容器路径")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="输出根目录；默认容器内 /nas/videos/视频下载",
    )
    parser.add_argument("--url-col", default="视频链接", help="链接列名（默认：视频链接）")
    parser.add_argument("--workers", type=int, default=2, help="下载线程数（默认 2，最多 8；每线程使用独立 cookies）")
    parser.add_argument("--no-skip", action="store_true", help="不跳过已存在文件")
    parser.add_argument("--status", action="store_true", help="只读取任务进度并输出 JSON，不启动下载")
    args = parser.parse_args(argv)

    try:
        input_path = _resolve_input_path(args.input)
        output_root = _resolve_output_root(args.output_dir)
        if args.status:
            payload = _read_progress(_progress_path(input_path, output_root))
            if not payload:
                payload = {
                    "status": "not_started",
                    "input_path": str(input_path),
                    "output_dir": str(output_root / input_path.stem),
                }
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        print(f"输入: {input_path}")
        print(f"输出: {output_root / input_path.stem}")
        return run_download(
            input_path,
            output_root=output_root,
            url_column=args.url_col,
            skip_existing=not args.no_skip,
            workers=args.workers,
        )
    except Exception as exc:
        print(f"下载任务失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
