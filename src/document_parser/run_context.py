from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .io_utils import safe_stem, write_csv, write_json


class RunContext:
    def __init__(self, output_root: Path, source_path: Path, file_type: str) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = f"{timestamp}_{safe_stem(source_path.stem)}_{file_type}_{uuid4().hex[:12]}"
        self.run_dir = output_root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.logs: list[str] = []
        self.manifest: dict[str, Any] = {
            "run_id": self.run_id,
            "source_path": str(source_path),
            "file_type": file_type,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "stages": {},
        }

    def log(self, message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        self.logs.append(line)
        (self.run_dir / "logs.txt").write_text("\n".join(self.logs) + "\n", encoding="utf-8")

    def write_stage(self, filename: str, data: Any, stats: dict[str, Any] | None = None) -> None:
        write_json(self.run_dir / filename, data)
        self.manifest["stages"][filename] = stats or {}
        write_json(self.run_dir / "00_run_manifest.json", self.manifest)

    def write_text_stage(self, filename: str, text: str, stats: dict[str, Any] | None = None) -> None:
        (self.run_dir / filename).write_text(text, encoding="utf-8")
        self.manifest["stages"][filename] = stats or {}
        write_json(self.run_dir / "00_run_manifest.json", self.manifest)

    def write_csv_stage(
        self,
        filename: str,
        rows: list[dict[str, Any]],
        fieldnames: list[str],
        stats: dict[str, Any] | None = None,
    ) -> None:
        write_csv(self.run_dir / filename, rows, fieldnames)
        self.manifest["stages"][filename] = stats or {}
        write_json(self.run_dir / "00_run_manifest.json", self.manifest)

    def finish(self, stats: dict[str, Any]) -> None:
        self.manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.manifest["summary"] = stats
        write_json(self.run_dir / "00_run_manifest.json", self.manifest)

    def fail(self, error: Exception) -> None:
        self.manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.manifest["summary"] = {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
            "last_completed_stage": next(reversed(self.manifest["stages"]), None),
        }
        write_json(self.run_dir / "00_run_manifest.json", self.manifest)
        self.log(f"failed parsing: {type(error).__name__}: {error}")
