"""Portable names for generated files and uploaded spreadsheets."""

import re
from pathlib import Path


def portable_name(name: str, *, fallback: str = "file", max_length: int = 120) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    # Device names remain reserved even with an extension, e.g. CON.mp4.
    if re.fullmatch(
        r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]",
        name.split(".", 1)[0].rstrip(),
        re.I,
    ):
        name = "_" + name
    # Leave room for IDs/extensions, also on filesystems with a 255-byte limit.
    name = name[:max_length].encode("utf-8")[:180].decode("utf-8", errors="ignore")
    return name.rstrip(" .") or fallback


def upload_filename(raw_name: str) -> str:
    # Browsers may send C:\fakepath\... even when the server runs on Unix.
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    safe_stem = portable_name(stem, fallback="upload")
    if not suffix:
        return safe_stem
    return safe_stem + "." + portable_name(suffix[1:], fallback="", max_length=15)
