from dataclasses import dataclass
import csv
import mimetypes
import os
import re
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

import pandas as pd

from .config import CSV_ENCODINGS, OUTPUT_DIR


@dataclass
class CSVInspection:
    dataframe: pd.DataFrame
    encoding: str
    columns: List[str]
    row_count: int
    preview_rows: List[Dict[str, str]]
    file_type: str = "csv"


def inspect_csv(path: Path) -> CSVInspection:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return inspect_xlsx(path)

    errors = []
    raw_sample = path.read_bytes()[:4096]

    for encoding in CSV_ENCODINGS:
        try:
            delimiter = sniff_delimiter(raw_sample, encoding)
            dataframe = pd.read_csv(
                path,
                dtype=str,
                keep_default_na=False,
                encoding=encoding,
                sep=delimiter if delimiter else ",",
                engine="python" if delimiter else "c",
            )
            dataframe = dataframe.fillna("")
            preview_rows = [
                {column: stringify_cell(value) for column, value in row.items()}
                for row in dataframe.head(5).to_dict(orient="records")
            ]
            return CSVInspection(
                dataframe=dataframe,
                encoding=encoding,
                columns=[str(column) for column in dataframe.columns.tolist()],
                row_count=len(dataframe),
                preview_rows=preview_rows,
            )
        except Exception as exc:
            errors.append(f"{encoding}: {exc}")

    raise RuntimeError("CSV 读取失败。已尝试编码: " + " | ".join(errors))


def inspect_xlsx(path: Path) -> CSVInspection:
    try:
        workbook = pd.ExcelFile(path, engine="openpyxl")
    except Exception as exc:
        raise RuntimeError(f"XLSX 读取失败: {exc}") from exc

    sheet_names = list(workbook.sheet_names)
    if len(sheet_names) > 1:
        workbook.close()
        raise RuntimeError(
            "XLSX 文件包含多个工作表: "
            + ", ".join(sheet_names)
            + "。当前只支持单工作表文件，请拆分后重新上传。"
        )

    try:
        dataframe = workbook.parse(sheet_names[0], dtype=str, keep_default_na=False)
    except Exception as exc:
        raise RuntimeError(f"XLSX 读取失败: {exc}") from exc
    finally:
        workbook.close()

    dataframe = dataframe.fillna("")
    preview_rows = [
        {column: stringify_cell(value) for column, value in row.items()}
        for row in dataframe.head(5).to_dict(orient="records")
    ]
    return CSVInspection(
        dataframe=dataframe,
        encoding="binary",
        columns=[str(column) for column in dataframe.columns.tolist()],
        row_count=len(dataframe),
        preview_rows=preview_rows,
        file_type="xlsx",
    )


def sniff_delimiter(raw_sample: bytes, encoding: str) -> Optional[str]:
    try:
        sample = raw_sample.decode(encoding)
    except Exception:
        return None

    candidates = [",", "\t", ";", "|"]
    detected = []

    for delimiter in candidates:
        first_line = sample.splitlines()[0] if sample.splitlines() else sample
        if delimiter in first_line:
            detected.append(delimiter)

    if len(detected) == 1:
        return detected[0]

    if not detected:
        return None

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="".join(candidates))
        if dialect.delimiter in candidates:
            return dialect.delimiter
    except Exception:
        pass

    return detected[0]


def write_csv(dataframe: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    # 先写同目录临时文件再原子替换，避免崩溃截断已积累的整份结果
    temp_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        if suffix == ".xlsx":
            dataframe.to_excel(temp_path, index=False, engine="openpyxl")
        else:
            dataframe.to_csv(temp_path, index=False, encoding="utf-8-sig")
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def ensure_text_column(dataframe: pd.DataFrame, column_name: str) -> None:
    if column_name not in dataframe.columns:
        dataframe[column_name] = ""
    dataframe[column_name] = dataframe[column_name].fillna("").astype(str)


def stringify_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def build_processed_output_name(original_filename: str, job_id: str = "") -> str:
    stem = Path(original_filename).stem
    if job_id:
        return f"{stem}.{job_id[:8]}.processed.csv"
    return f"{stem}.processed.csv"


def processed_output_path_for_filename(original_filename: str, job_id: str = "") -> Path:
    return OUTPUT_DIR / build_processed_output_name(original_filename, job_id)


_PROCESSED_OUTPUT_NAME_RE = r"{stem}(\.[0-9a-fA-F]{{8}})?\.processed\.csv"


def find_latest_processed_path(original_filename: str) -> Optional[Path]:
    # 同名输入允许多份产物共存（<stem>.processed.csv 与 <stem>.<jobid8>.processed.csv），
    # 按 mtime 取最新一份；正则兜底防止 "1" 的前缀 glob 误匹配 "10.processed.csv"
    stem = Path(original_filename).stem
    pattern = re.compile(_PROCESSED_OUTPUT_NAME_RE.format(stem=re.escape(stem)))
    if not OUTPUT_DIR.exists():
        return None
    # 用目录扫描配合正则，避免用户文件名里的 glob 元字符导致漏匹配。
    candidates = [
        path
        for path in OUTPUT_DIR.iterdir()
        if path.is_file() and pattern.fullmatch(path.name)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def media_type_for_path(path: Path) -> str:
    if path.suffix.lower() == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "text/csv"
