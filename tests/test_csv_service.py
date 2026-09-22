import os
from pathlib import Path

import pandas as pd
import pytest

from app import csv_service
from app.csv_service import (
    build_processed_output_name,
    find_latest_processed_path,
    inspect_csv,
    processed_output_path_for_filename,
    write_csv,
)


def test_write_csv_writes_utf8_sig_content(tmp_path):
    dataframe = pd.DataFrame([{"链接": "https://v.douyin.com/AAA111/", "视频文本": "hello"}])
    output_path = tmp_path / "result.csv"

    write_csv(dataframe, output_path)

    loaded = pd.read_csv(output_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert loaded.loc[0, "视频文本"] == "hello"
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_csv_keeps_existing_output_when_write_fails(tmp_path, monkeypatch):
    output_path = tmp_path / "result.csv"
    output_path.write_text("已积累的旧结果", encoding="utf-8")
    dataframe = pd.DataFrame([{"链接": "https://v.douyin.com/AAA111/"}])

    def _explode(self, *args, **kwargs):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(pd.DataFrame, "to_csv", _explode)

    with pytest.raises(OSError):
        write_csv(dataframe, output_path)

    assert output_path.read_text(encoding="utf-8") == "已积累的旧结果"
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_csv_writes_xlsx_atomically(tmp_path):
    dataframe = pd.DataFrame([{"链接": "https://v.douyin.com/AAA111/", "视频文本": "hello"}])
    output_path = tmp_path / "result.xlsx"

    write_csv(dataframe, output_path)

    loaded = pd.read_excel(output_path, dtype=str, keep_default_na=False, engine="openpyxl")
    assert loaded.loc[0, "视频文本"] == "hello"
    assert list(tmp_path.glob("*.tmp")) == []


def test_inspect_xlsx_reads_single_sheet(tmp_path):
    xlsx_path = tmp_path / "single.xlsx"
    pd.DataFrame([{"链接": "https://v.douyin.com/AAA111/"}]).to_excel(
        xlsx_path, index=False, engine="openpyxl"
    )

    inspection = inspect_csv(xlsx_path)

    assert inspection.file_type == "xlsx"
    assert inspection.columns == ["链接"]
    assert inspection.row_count == 1


def test_inspect_xlsx_rejects_multi_sheet_workbook(tmp_path):
    xlsx_path = tmp_path / "multi.xlsx"
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        pd.DataFrame([{"链接": "a"}]).to_excel(writer, sheet_name="表一", index=False)
        pd.DataFrame([{"链接": "b"}]).to_excel(writer, sheet_name="Sheet2", index=False)

    with pytest.raises(RuntimeError) as excinfo:
        inspect_csv(xlsx_path)

    message = str(excinfo.value)
    assert "多个工作表" in message
    assert "表一" in message
    assert "Sheet2" in message


def test_build_processed_output_name_defaults_to_legacy_name():
    assert build_processed_output_name("1.csv") == "1.processed.csv"
    assert build_processed_output_name("2.xlsx") == "2.processed.csv"
    assert build_processed_output_name("归档.最终.csv") == "归档.最终.processed.csv"


def test_build_processed_output_name_appends_job_id_prefix():
    assert build_processed_output_name("1.csv", "abcd1234efgh5678") == "1.abcd1234.processed.csv"
    assert build_processed_output_name("2.xlsx", "a1b2c3d4") == "2.a1b2c3d4.processed.csv"


def test_processed_output_path_for_filename_uses_output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_service, "OUTPUT_DIR", tmp_path)

    assert processed_output_path_for_filename("1.csv") == tmp_path / "1.processed.csv"
    assert processed_output_path_for_filename("1.csv", "abcd1234efgh5678") == (
        tmp_path / "1.abcd1234.processed.csv"
    )


def test_find_latest_processed_path_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_service, "OUTPUT_DIR", tmp_path)

    assert find_latest_processed_path("1.csv") is None


def test_find_latest_processed_path_returns_newest_by_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_service, "OUTPUT_DIR", tmp_path)
    legacy = tmp_path / "1.processed.csv"
    newer = tmp_path / "1.abcd1234.processed.csv"
    legacy.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    os.utime(legacy, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    assert find_latest_processed_path("1.csv") == newer


def test_find_latest_processed_path_matches_legacy_name(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_service, "OUTPUT_DIR", tmp_path)
    legacy = tmp_path / "1.processed.csv"
    legacy.write_text("old", encoding="utf-8")

    assert find_latest_processed_path("1.csv") == legacy


def test_find_latest_processed_path_ignores_other_stem_prefixes(tmp_path, monkeypatch):
    # "1" 的前缀 glob 不应误吞 "10.processed.csv" 这类同名前缀的其他输入
    monkeypatch.setattr(csv_service, "OUTPUT_DIR", tmp_path)
    other = tmp_path / "10.processed.csv"
    other.write_text("other", encoding="utf-8")
    os.utime(other, (9_000_000, 9_000_000))

    assert find_latest_processed_path("1.csv") is None


def test_find_latest_processed_path_ignores_non_processed_files(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_service, "OUTPUT_DIR", tmp_path)
    unrelated = tmp_path / "1.processed.csv.bak"
    unrelated.write_text("bak", encoding="utf-8")

    assert find_latest_processed_path("1.csv") is None


def test_find_latest_processed_path_supports_glob_metacharacters_in_stem(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_service, "OUTPUT_DIR", tmp_path)
    result = tmp_path / "clip[1]*.abcd1234.processed.csv"
    result.write_text("result", encoding="utf-8")

    assert find_latest_processed_path("clip[1]*.csv") == result
