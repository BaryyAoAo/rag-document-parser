from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Event, local
from typing import Any, Callable
from uuid import uuid4

from .file_type import FILE_TYPE_BY_SUFFIX
from .io_utils import write_json
from .pipeline import DocumentParsingPipeline, ParseExecutionError


def parse_directory(
    pipeline: DocumentParsingPipeline,
    input_dir: Path,
    recursive: bool = True,
    workers: int = 2,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    input_dir = input_dir.resolve()
    if not input_dir.is_dir():
        raise NotADirectoryError(input_dir)
    iterator = input_dir.rglob("*") if recursive else input_dir.glob("*")
    files = sorted(
        path for path in iterator if path.is_file() and path.suffix.lower() in FILE_TYPE_BY_SUFFIX
    )
    return parse_files(
        pipeline, files, input_dir=input_dir, workers=workers,
        progress=progress, cancel_event=cancel_event,
    )


def parse_files(
    pipeline: DocumentParsingPipeline,
    files: list[Path],
    input_dir: Path | None = None,
    workers: int = 2,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    files = sorted(path.resolve() for path in files)
    if any(not path.is_file() or path.suffix.lower() not in FILE_TYPE_BY_SUFFIX for path in files):
        raise ValueError("files must contain existing supported documents")
    batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:12]}"
    batch_dir = pipeline.output_root / "batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc)
    results: list[dict[str, Any] | None] = [None] * len(files)
    progress_path = batch_dir / "results.jsonl"
    progress_path.touch(exist_ok=False)
    worker_state = local()

    def emit(event: dict[str, Any]) -> None:
        if progress is not None:
            progress(event)

    emit({"type": "batch_started", "batch_id": batch_id, "file_count": len(files), "files": [str(path) for path in files]})

    def parse_one(path: Path) -> dict[str, Any]:
        emit({"type": "file_started", "source_path": str(path)})
        try:
            if workers == 1:
                worker_pipeline = pipeline
            else:
                if not hasattr(worker_state, "pipeline"):
                    worker_state.pipeline = pipeline.new_worker()
                worker_pipeline = worker_state.pipeline
            result = worker_pipeline.parse(path)
            return {
                "source_path": str(path),
                "status": result.status,
                "document_id": result.parsed_document.document_id,
                "block_count": len(result.parsed_document.blocks),
                "run_dir": result.run_dir,
                "error": "",
            }
        except Exception as error:
            return {
                "source_path": str(path),
                "status": "failed",
                "document_id": "",
                "block_count": 0,
                "run_dir": str(error.run_dir) if isinstance(error, ParseExecutionError) else "",
                "error": str(error) if isinstance(error, ParseExecutionError) else f"{type(error).__name__}: {error}",
            }

    def record(index: int, item: dict[str, Any]) -> None:
        results[index] = item
        with progress_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"index": index, **item}, ensure_ascii=False) + "\n")
        emit({"type": "file_completed", "index": index, "result": item})

    if workers == 1:
        for index, path in enumerate(files):
            if cancel_event is not None and cancel_event.is_set():
                break
            record(index, parse_one(path))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            pending: dict[Future[dict[str, Any]], int] = {}
            next_index = 0
            while next_index < len(files) or pending:
                while (next_index < len(files) and len(pending) < workers * 2
                       and (cancel_event is None or not cancel_event.is_set())):
                    pending[executor.submit(parse_one, files[next_index])] = next_index
                    next_index += 1
                if not pending:
                    break
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    index = pending.pop(future)
                    record(index, future.result())

    if cancel_event is not None and cancel_event.is_set():
        for index, path in enumerate(files):
            if results[index] is None:
                record(index, {
                    "source_path": str(path), "status": "cancelled", "document_id": "",
                    "block_count": 0, "run_dir": "", "error": "Cancelled before parsing",
                })

    completed = [item for item in results if item is not None]
    report = {
        "batch_id": batch_id,
        "created_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_dir": str(input_dir.resolve()) if input_dir is not None else "",
        "workers": workers,
        "file_count": len(files),
        "finalized_count": sum(item["status"] == "finalized" for item in completed),
        "review_required_count": sum(item["status"] == "review_required" for item in completed),
        "failed_count": sum(item["status"] == "failed" for item in completed),
        "cancelled_count": sum(item["status"] == "cancelled" for item in completed),
        "results": completed,
    }
    output_path = batch_dir / "batch_report.json"
    write_json(output_path, report)
    report["report_path"] = str(output_path)
    emit({"type": "batch_finished", "report_path": str(output_path), "report": report})
    return report
