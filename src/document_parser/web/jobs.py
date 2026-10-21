from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Event, Lock
from typing import Any
from uuid import uuid4

from ..batch import parse_files
from ..pipeline import DocumentParsingPipeline


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobManager:
    """Run one local batch at a time and persist UI state beside parse outputs."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root.resolve()
        self.jobs_dir = self.output_root / "web_jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parser-web")
        self._lock = Lock()
        self._futures: dict[str, Future[Any]] = {}
        self._cancel_events: dict[str, Event] = {}
        for path in self.jobs_dir.glob("*.json"):
            try:
                data = self._read(path)
            except (OSError, ValueError):
                continue
            if data.get("status") in {"queued", "running", "stopping"}:
                data["status"] = "interrupted"
                data["updated_at"] = _now()
                self._write(path, data)

    def submit(self, files: list[Path], workers: int = 2, input_dir: Path | None = None) -> dict[str, Any]:
        if not 1 <= workers <= 16:
            raise ValueError("workers must be between 1 and 16")
        paths = sorted(path.resolve() for path in files)
        if not paths:
            raise ValueError("No supported documents were selected")
        job_id = f"job_{uuid4().hex[:16]}"
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": _now(),
            "updated_at": _now(),
            "input_dir": str(input_dir.resolve()) if input_dir else "",
            "workers": workers,
            "file_count": len(paths),
            "completed_count": 0,
            "finalized_count": 0,
            "review_required_count": 0,
            "failed_count": 0,
            "cancelled_count": 0,
            "report_path": "",
            "error": "",
            "files": [
                {"source_path": str(path), "status": "queued", "document_id": "",
                 "block_count": 0, "run_dir": "", "error": ""}
                for path in paths
            ],
        }
        cancel_event = Event()
        with self._lock:
            self._write(self.jobs_dir / f"{job_id}.json", job)
            self._cancel_events[job_id] = cancel_event
            self._futures[job_id] = self._executor.submit(
                self._run, job_id, paths, workers, input_dir, cancel_event
            )
        return job

    def _run(
        self, job_id: str, paths: list[Path], workers: int,
        input_dir: Path | None, cancel_event: Event,
    ) -> None:
        self._change(job_id, status="running")

        def progress(event: dict[str, Any]) -> None:
            kind = event["type"]
            if kind == "file_started":
                self._file_change(job_id, event["source_path"], {"status": "running"})
            elif kind == "file_completed":
                self._file_change(job_id, event["result"]["source_path"], event["result"])

        try:
            report = parse_files(
                DocumentParsingPipeline(self.output_root), paths, input_dir=input_dir,
                workers=workers, progress=progress, cancel_event=cancel_event,
            )
            self._change(
                job_id,
                status="cancelled" if report["cancelled_count"] else "completed",
                report_path=report["report_path"],
                completed_at=_now(),
            )
        except Exception as error:
            self._change(
                job_id, status="failed", error=f"{type(error).__name__}: {error}",
                completed_at=_now(),
            )
        finally:
            with self._lock:
                self._cancel_events.pop(job_id, None)

    def _file_change(self, job_id: str, source_path: str, changes: dict[str, Any]) -> None:
        with self._lock:
            path = self._path(job_id)
            job = self._read(path)
            for item in job["files"]:
                if item["source_path"] == source_path:
                    item.update(changes)
                    break
            job["completed_count"] = sum(
                item["status"] in {"finalized", "review_required", "failed", "cancelled"}
                for item in job["files"]
            )
            for status in ("finalized", "review_required", "failed", "cancelled"):
                job[f"{status}_count"] = sum(item["status"] == status for item in job["files"])
            job["updated_at"] = _now()
            self._write(path, job)

    def _change(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            path = self._path(job_id)
            job = self._read(path)
            job.update(changes)
            job["updated_at"] = _now()
            self._write(path, job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            path = self._path(job_id)
            job = self._read(path)
            if job["status"] not in {"queued", "running", "stopping"}:
                raise ValueError("Job is not running")
            event = self._cancel_events.get(job_id)
            if event:
                event.set()
            future = self._futures.get(job_id)
            if future and future.cancel():
                job["status"] = "cancelled"
                job["completed_at"] = _now()
                for item in job["files"]:
                    item["status"] = "cancelled"
                    item["error"] = "Cancelled before parsing"
                job["completed_count"] = job["file_count"]
                job["cancelled_count"] = job["file_count"]
                self._cancel_events.pop(job_id, None)
                self._futures.pop(job_id, None)
            else:
                job["status"] = "stopping"
            job["updated_at"] = _now()
            self._write(path, job)
            return job

    def retry_failed(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        failed = [Path(item["source_path"]) for item in job["files"] if item["status"] == "failed"]
        return self.submit(failed, workers=job["workers"])

    def get(self, job_id: str) -> dict[str, Any]:
        return self._read(self._path(job_id))

    def list(self) -> list[dict[str, Any]]:
        jobs = []
        for path in self.jobs_dir.glob("*.json"):
            try:
                jobs.append(self._read(path))
            except (OSError, ValueError):
                continue
        return sorted(jobs, key=lambda item: item["created_at"], reverse=True)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _path(self, job_id: str) -> Path:
        if not job_id.startswith("job_") or not job_id[4:].isalnum():
            raise ValueError("Invalid job ID")
        path = self.jobs_dir / f"{job_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Job not found: {job_id}")
        return path

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.stem}_{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
