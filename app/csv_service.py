from dataclasses import dataclass
import csv
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional

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
        dataframe = pd.read_excel(
            path,
            dtype=str,
            keep_default_na=False,
            engine="openpyxl",
        )
    except Exception as exc:
        raise RuntimeError(f"XLSX 读取失败: {exc}") from exc

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
    if suffix == ".xlsx":
        dataframe.to_excel(output_path, index=False, engine="openpyxl")
        return
    dataframe.to_csv(output_path, index=False, encoding="utf-8-sig")


def ensure_text_column(dataframe: pd.DataFrame, column_name: str) -> None:
    if column_name not in dataframe.columns:
        dataframe[column_name] = ""
    dataframe[column_name] = dataframe[column_name].fillna("").astype(str)


def stringify_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def build_processed_output_name(original_filename: str) -> str:
    path = Path(original_filename)
    return f"{path.stem}.processed.csv"


def processed_output_path_for_filename(original_filename: str) -> Path:
    return OUTPUT_DIR / build_processed_output_name(original_filename)


def media_type_for_path(path: Path) -> str:
    if path.suffix.lower() == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "text/csv"
