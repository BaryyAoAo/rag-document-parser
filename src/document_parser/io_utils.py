from __future__ import annotations

import hashlib
import csv
import json
import re
from pathlib import Path
from typing import Any

from .models import to_plain_data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", value)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:80] or "file"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_plain_data(data), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_text_auto(path: Path) -> str:
    # utf-8-sig also reads plain UTF-8 and removes a CSV BOM when present.
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")
