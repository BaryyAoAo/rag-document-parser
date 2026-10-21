from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
import re
from threading import Lock
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..file_type import FILE_TYPE_BY_SUFFIX
from ..artifacts import resolve_parsing_artifacts
from ..ocr import OcrReviewStore
from ..ocr.baidu_paddleocr import convert_parse_result_to_blocks
from ..ocr.providers import BaiduPaddleOcrProvider
from ..io_utils import write_json
from ..models import to_plain_data
from ..serialization import ParsedDocumentValidationError, load_resolved_parsed_document
from ..review import StructureReviewStore
from .jobs import JobManager
from .exports import document_markdown
from .preview import office_preview_html


class DirectoryRequest(BaseModel):
    path: str
    recursive: bool = True
    workers: int = Field(default=2, ge=1, le=16)


class ReviewDecision(BaseModel):
    action: str
    reviewer: str
    note: str = ""


class StructureDecision(ReviewDecision):
    corrected_text: str = ""


class BulkOcrRequest(BaseModel):
    run_dir: str


class BulkOcrApproval(BulkOcrRequest):
    reviewer: str
    note: str = ""


class ExportSelection(BaseModel):
    job_id: str
    index: int = Field(ge=0)


class BatchExportRequest(BaseModel):
    entries: list[ExportSelection] = Field(min_length=1, max_length=500)
    target_folder: str
    format: str = "json"


def create_app(output_root: Path | None = None) -> FastAPI:
    root = (output_root or Path("outputs/web")).resolve()
    manager = JobManager(root)
    ocr_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="parser-ocr")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            ocr_executor.shutdown(wait=True, cancel_futures=False)

    app = FastAPI(title="Document Parser Local UI", version="0.6.0", lifespan=lifespan)
    app.state.jobs = manager
    ocr_executing: set[str] = set()
    ocr_execution_lock = Lock()
    bulk_execution_lock = Lock()
    bulk_executions: dict[str, dict[str, Any]] = {}

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Any, error: ValueError) -> Any:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.exception_handler(FileNotFoundError)
    async def missing_file_handler(request: Any, error: FileNotFoundError) -> Any:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "output_root": str(root), "file_types": sorted(FILE_TYPE_BY_SUFFIX)}

    def _current_job(job: dict[str, Any]) -> dict[str, Any]:
        """Overlay finalized review artifacts without rewriting the original batch record."""
        current = deepcopy(job)
        for item in current.get("files", []):
            run_dir_value = item.get("run_dir")
            if not run_dir_value:
                continue
            run_dir = Path(run_dir_value).resolve()
            if not run_dir.is_dir() or not run_dir.is_relative_to(root):
                continue
            try:
                artifacts = resolve_parsing_artifacts(run_dir)
                if artifacts.source == "base_run":
                    continue
                evaluation = json.loads(artifacts.evaluation_path.read_text(encoding="utf-8-sig"))
                document = json.loads(artifacts.parsed_document_path.read_text(encoding="utf-8-sig"))
                if not isinstance(evaluation, dict) or not isinstance(document, dict):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            item["artifact_source"] = artifacts.source
            item["block_count"] = len(document.get("blocks") or [])
            item["quality_warning_count"] = int(evaluation.get("warning_count") or 0)
            reviews = evaluation.get("manual_review_items") or []
            pending = not isinstance(reviews, list) or any(
                isinstance(review, dict)
                and review.get("status") not in {"resolved", "applied", "excluded"}
                for review in reviews
            )
            if (evaluation.get("status") in {"finalized", "review_required"}
                    and not evaluation.get("error_count") and not pending):
                item["status"] = "finalized"
        for status in ("finalized", "review_required", "failed", "cancelled"):
            current[f"{status}_count"] = sum(item.get("status") == status for item in current["files"])
        return current

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [_current_job(job) for job in manager.list()]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        return _current_job(manager.get(job_id))

    @app.get("/api/history")
    def file_history() -> list[dict[str, Any]]:
        return [
            {"job_id": job["job_id"], "index": index, "created_at": job["created_at"],
             "job_status": job["status"], **item}
            for job in list_jobs() for index, item in enumerate(job.get("files", []))
        ]

    @app.post("/api/jobs/directory", status_code=202)
    def start_directory(body: DirectoryRequest) -> dict[str, Any]:
        source = Path(body.path).expanduser().resolve()
        if source.is_file():
            candidates = [source]
            input_dir = source.parent
        elif source.is_dir():
            iterator = source.rglob("*") if body.recursive else source.glob("*")
            candidates = [path for path in iterator if path.is_file()]
            input_dir = source
        else:
            raise FileNotFoundError(f"Input path not found: {source}")
        files = [
            path for path in candidates
            if path.suffix.lower() in FILE_TYPE_BY_SUFFIX
            and not path.resolve().is_relative_to(root)
        ]
        return manager.submit(files, workers=body.workers, input_dir=input_dir)

    @app.post("/api/jobs/upload", status_code=202)
    def start_upload(
        files: list[UploadFile] = File(...),
        workers: int = Form(default=2),
    ) -> dict[str, Any]:
        if not 1 <= workers <= 16:
            raise ValueError("workers must be between 1 and 16")
        upload_dir = root / "uploads" / uuid4().hex
        upload_dir.mkdir(parents=True, exist_ok=False)
        paths: list[Path] = []
        for index, upload in enumerate(files):
            filename = Path(upload.filename or "").name
            if not filename or Path(filename).suffix.lower() not in FILE_TYPE_BY_SUFFIX:
                continue
            target = upload_dir / f"{index:04d}" / filename
            target.parent.mkdir(parents=True)
            size = 0
            try:
                with target.open("wb") as output:
                    while chunk := upload.file.read(1024 * 1024):
                        size += len(chunk)
                        if size > 200 * 1024 * 1024:
                            raise ValueError(f"File exceeds 200 MB: {filename}")
                        output.write(chunk)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            paths.append(target)
        return manager.submit(paths, workers=workers, input_dir=upload_dir)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        return manager.cancel(job_id)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: str) -> dict[str, Any]:
        return manager.retry_failed(job_id)

    def _file(job_id: str, index: int) -> dict[str, Any]:
        job = _current_job(manager.get(job_id))
        if index < 0 or index >= len(job["files"]):
            raise HTTPException(status_code=404, detail="Document index not found")
        return job["files"][index]

    def _run_dir(job_id: str, index: int) -> Path:
        item = _file(job_id, index)
        if not item.get("run_dir"):
            raise ValueError("This file has no parse output yet")
        run_dir = Path(item["run_dir"]).resolve()
        if not run_dir.is_dir() or not run_dir.is_relative_to(root):
            raise HTTPException(status_code=403, detail="Run directory is outside the output root")
        return run_dir

    def _read_document(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
        artifacts = resolve_parsing_artifacts(run_dir)
        document = json.loads(artifacts.parsed_document_path.read_text(encoding="utf-8-sig"))
        evaluation = json.loads(artifacts.evaluation_path.read_text(encoding="utf-8-sig"))
        return document, evaluation, artifacts.source

    def _export_content(job_id: str, index: int, format: str) -> tuple[bytes, str, str]:
        if format not in {"json", "md"}:
            raise ValueError("Export format must be json or md")
        run_dir = _run_dir(job_id, index)
        try:
            document = load_resolved_parsed_document(run_dir)
        except ParsedDocumentValidationError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if format == "json":
            content = (json.dumps(to_plain_data(document), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            media_type = "application/json; charset=utf-8"
        else:
            content = document_markdown(document).encode("utf-8")
            media_type = "text/markdown; charset=utf-8"
        filename = f"{Path(_file(job_id, index)['source_path']).stem}_ParsedDocument.{format}"
        return content, media_type, filename

    @app.get("/api/jobs/{job_id}/documents/{index}/export")
    def export_document(job_id: str, index: int, format: str = "json") -> Response:
        content, media_type, filename = _export_content(job_id, index, format)
        return Response(content=content, media_type=media_type,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    @app.post("/api/history/export")
    def export_selected_files(body: BatchExportRequest) -> dict[str, Any]:
        target = Path(body.target_folder).expanduser()
        if not target.is_absolute() or not target.is_dir():
            raise ValueError("Target folder must be an existing absolute directory")
        target = target.resolve()
        prepared = [
            (entry, _export_content(entry.job_id, entry.index, body.format))
            for entry in body.entries
        ]
        exported: list[dict[str, str]] = []
        for entry, (content, _, filename) in prepared:
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            unique = f"{stem}_{entry.job_id.removeprefix('job_')[:8]}_{entry.index + 1}{suffix}"
            destination = target / unique
            if destination.exists():
                destination = target / f"{stem}_{uuid4().hex[:8]}{suffix}"
            temporary = target / f".{uuid4().hex}.tmp"
            try:
                temporary.write_bytes(content)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            exported.append({"job_id": entry.job_id, "index": str(entry.index), "path": str(destination)})
        return {"target_folder": str(target), "format": body.format, "exported": exported,
                "exported_count": len(exported)}

    def _open_output_directory(path: Path) -> dict[str, str]:
        path = path.resolve()
        if not path.is_dir() or not path.is_relative_to(root):
            raise HTTPException(status_code=403, detail="Output directory is outside the output root")
        if os.name != "nt":
            raise HTTPException(status_code=501, detail="Open directory is currently available on Windows only")
        os.startfile(str(path))
        return {"path": str(path)}

    @app.post("/api/jobs/{job_id}/open-dir")
    def open_batch_directory(job_id: str) -> dict[str, str]:
        job = manager.get(job_id)
        if not job.get("report_path"):
            raise ValueError("Batch output is not available yet")
        return _open_output_directory(Path(job["report_path"]).parent)

    @app.post("/api/jobs/{job_id}/documents/{index}/open-dir")
    def open_file_directory(job_id: str, index: int) -> dict[str, str]:
        return _open_output_directory(_run_dir(job_id, index))

    @app.get("/api/jobs/{job_id}/source/{index}/preview", response_class=HTMLResponse)
    def source_office_preview(job_id: str, index: int) -> HTMLResponse:
        path = Path(_file(job_id, index)["source_path"])
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.suffix.lower() not in {".docx", ".xlsx", ".xlsm", ".pptx"}:
            raise ValueError("Office preview supports DOCX, XLSX and PPTX")
        html = office_preview_html(path)
        return HTMLResponse(content=html, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    def _ocr_progress(run_dir: Path) -> dict[str, Any] | None:
        states = review_store().task_states_for_run(run_dir)
        if not states:
            return None
        applied = [state for state in states if state["status"] == "applied"]
        status_path = run_dir / "ocr" / "finalized" / "00_finalization_status.json"
        finalization = json.loads(status_path.read_text(encoding="utf-8-sig")) if status_path.is_file() else None
        return {
            "task_count": len(states),
            "applied_count": len(applied),
            "pending_count": len(states) - len(applied),
            "applied_pages": sorted({int(state["page_number"]) for state in applied if state.get("page_number") is not None}),
            "status": finalization.get("status") if finalization else "pending_reviews",
        }

    @app.get("/api/jobs/{job_id}/documents/{index}")
    def document_detail(job_id: str, index: int) -> dict[str, Any]:
        item = _file(job_id, index)
        if not item.get("run_dir"):
            return {"file": item, "manifest": None, "evaluation": None, "blocks": [], "block_count": 0,
                    "ocr_progress": None}
        run_dir = _run_dir(job_id, index)
        def read(name: str) -> Any:
            path = run_dir / name
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        base_document = read("06_parsed_document.json") or {}
        base_evaluation = read("07_parsing_evaluation.json")
        if not base_document:
            return {"file": item, "manifest": read("00_run_manifest.json"),
                    "evaluation": base_evaluation, "base_evaluation": base_evaluation,
                    "artifact_source": "", "title": "", "blocks": [], "block_count": 0,
                    "base_block_count": 0, "ocr_progress": _ocr_progress(run_dir)}
        document, evaluation, artifact_source = _read_document(run_dir)
        blocks = document.get("blocks") or []
        return {
            "file": item,
            "manifest": read("00_run_manifest.json"),
            "evaluation": evaluation,
            "base_evaluation": base_evaluation,
            "artifact_source": artifact_source,
            "title": document.get("title", ""),
            "blocks": blocks[:200],
            "block_count": len(blocks),
            "base_block_count": len(base_document.get("blocks") or []),
            "ocr_progress": _ocr_progress(run_dir),
        }

    @app.get("/api/jobs/{job_id}/documents/{index}/blocks")
    def document_blocks(
        job_id: str, index: int,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
        block_type: str = "",
        page: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        run_dir = _run_dir(job_id, index)
        base = json.loads((run_dir / "06_parsed_document.json").read_text(encoding="utf-8-sig"))
        effective, _, source = _read_document(run_dir)
        base_blocks = base.get("blocks") or []
        effective_blocks = effective.get("blocks") or []
        base_by_id = {str(block.get("block_id")): block for block in base_blocks}
        effective_ids = {str(block.get("block_id")) for block in effective_blocks}
        pairs = [
            {"base": base_by_id.get(str(block.get("block_id"))), "effective": block}
            for block in effective_blocks
        ]
        pairs.extend(
            {"base": block, "effective": None}
            for block in base_blocks if str(block.get("block_id")) not in effective_ids
        )
        types = sorted({
            str((pair["effective"] or pair["base"]).get("block_type") or "")
            for pair in pairs
        })
        if block_type:
            pairs = [pair for pair in pairs if (pair["effective"] or pair["base"]).get("block_type") == block_type]
        if page is not None:
            pairs = [
                pair for pair in pairs
                if (pair["effective"] or pair["base"]).get("page_start") is not None
                and int((pair["effective"] or pair["base"])["page_start"]) <= page
                and int((pair["effective"] or pair["base"]).get("page_end") or
                        (pair["effective"] or pair["base"])["page_start"]) >= page
            ]
        return {"source": source, "total": len(pairs), "offset": offset, "limit": limit,
                "types": types, "items": pairs[offset:offset + limit]}

    def _structure_store(job_id: str, index: int) -> StructureReviewStore:
        return StructureReviewStore(_run_dir(job_id, index))

    @app.get("/api/jobs/{job_id}/documents/{index}/structure")
    def structure_reviews(job_id: str, index: int) -> dict[str, Any]:
        store = _structure_store(job_id, index)
        status = json.loads(store.status_path.read_text(encoding="utf-8-sig")) if store.status_path.is_file() else None
        tasks = store.list_states()
        source_cache: dict[str, dict[str, Any]] = {}
        for task in tasks:
            source_path = Path(task["source_document_path"]).resolve()
            if not source_path.is_file() or not source_path.is_relative_to(root):
                raise HTTPException(status_code=403, detail="Review source is outside the output root")
            key = str(source_path)
            if key not in source_cache:
                document = json.loads(source_path.read_text(encoding="utf-8-sig"))
                source_cache[key] = {str(block.get("block_id")): block for block in document.get("blocks") or []}
            task["source_block"] = source_cache[key].get(str(task.get("object_id") or ""))
        return {"tasks": tasks, "finalization": status}

    @app.post("/api/jobs/{job_id}/documents/{index}/structure/prepare")
    def prepare_structure(job_id: str, index: int) -> dict[str, Any]:
        store = _structure_store(job_id, index)
        store.create_from_run()
        return structure_reviews(job_id, index)

    @app.post("/api/jobs/{job_id}/documents/{index}/structure/{review_id}/decision")
    def decide_structure(job_id: str, index: int, review_id: str, body: StructureDecision) -> dict[str, Any]:
        validate_review_id(review_id)
        if not body.reviewer.strip():
            raise ValueError("Reviewer is required")
        store = _structure_store(job_id, index)
        store.record_decision(review_id, body.action, body.reviewer, body.note, body.corrected_text)
        return structure_reviews(job_id, index)

    @app.post("/api/jobs/{job_id}/documents/{index}/structure/finalize")
    def finalize_structure(job_id: str, index: int) -> dict[str, Any]:
        store = _structure_store(job_id, index)
        return store.finalize()

    @app.get("/api/jobs/{job_id}/source/{index}")
    def source_file(job_id: str, index: int) -> FileResponse:
        item = _file(job_id, index)
        path = Path(item["source_path"])
        if not path.is_file():
            raise FileNotFoundError(path)
        disposition = "inline" if path.suffix.lower() == ".pdf" else "attachment"
        return FileResponse(path, filename=path.name, content_disposition_type=disposition)

    def review_store() -> OcrReviewStore:
        return OcrReviewStore(root / "_ocr_review")

    def validate_review_id(review_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", review_id):
            raise ValueError("Invalid review ID")

    @app.get("/api/reviews/ocr")
    def list_ocr_reviews() -> list[dict[str, Any]]:
        states = review_store().list_task_states()
        source_names: dict[str, str] = {}
        run_jobs = {
            str(Path(file["run_dir"]).resolve()).casefold(): job["job_id"]
            for job in manager.list()
            for file in job.get("files", [])
            if file.get("run_dir")
        }
        with ocr_execution_lock:
            executing = set(ocr_executing)
        for state in states:
            run_dir_value = (state.get("metadata") or {}).get("run_dir")
            run_dir = Path(run_dir_value).resolve() if run_dir_value else None
            source_name = ""
            if run_dir and run_dir.is_relative_to(root):
                key = str(run_dir)
                if key not in source_names:
                    source_file = run_dir / "01_source_file.json"
                    if source_file.is_file():
                        source = json.loads(source_file.read_text(encoding="utf-8-sig"))
                        source_names[key] = str(source.get("original_filename") or Path(source.get("file_path") or "").name)
                    else:
                        source_names[key] = ""
                source_name = source_names[key]
            state["source_filename"] = source_name or state["document_id"]
            state["web_job_id"] = run_jobs.get(str(run_dir).casefold(), "") if run_dir else ""
            if state["review_id"] in executing:
                state["status"] = "executing"
        return sorted(states, key=lambda item: item.get("created_at") or "", reverse=True)

    def _bulk_states(run_dir_value: str) -> tuple[Path, list[dict[str, Any]]]:
        run_dir = Path(run_dir_value).resolve()
        if not run_dir.is_dir() or not run_dir.is_relative_to(root):
            raise HTTPException(status_code=403, detail="Run directory is outside the output root")
        states = review_store().task_states_for_run(run_dir)
        if not states:
            raise ValueError("No OCR review tasks found for this file run")
        return run_dir, states

    @app.post("/api/reviews/ocr/bulk/approve")
    def bulk_approve_ocr(body: BulkOcrApproval) -> dict[str, Any]:
        if not body.reviewer.strip():
            raise ValueError("Reviewer is required")
        run_dir, states = _bulk_states(body.run_dir)
        candidates = [state for state in states if state["status"] == "pending"
                      and "run_ocr" in state.get("allowed_actions", [])]
        if not candidates:
            raise ValueError("No pages are waiting for OCR approval in this file run")
        store = review_store()
        for state in candidates:
            store.record_decision(state["review_id"], "run_ocr", body.reviewer, body.note)
        return {"run_dir": str(run_dir), "approved_count": len(candidates),
                "review_ids": [state["review_id"] for state in candidates]}

    def _run_bulk_page(execution_id: str, review_id: str) -> None:
        with bulk_execution_lock:
            bulk_executions[execution_id]["pages"][review_id]["status"] = "running"
        try:
            execute_ocr(review_id)
        except Exception as error:
            result = "failed"
            detail = f"{type(error).__name__}: {error}"
        else:
            result = "completed"
            detail = ""
        with bulk_execution_lock:
            job = bulk_executions[execution_id]
            job["pages"][review_id].update(status=result, error=detail)
            job["completed_count"] += 1
            if result == "failed":
                job["failed_count"] += 1
            if job["completed_count"] == job["total_count"]:
                job["status"] = "completed_with_errors" if job["failed_count"] else "completed"

    @app.post("/api/reviews/ocr/bulk/execute", status_code=202)
    def bulk_execute_ocr(body: BulkOcrRequest) -> dict[str, Any]:
        run_dir, states = _bulk_states(body.run_dir)
        with ocr_execution_lock:
            executing = set(ocr_executing)
        candidates = [state for state in states if state["status"] == "ocr_requested"
                      and state["review_id"] not in executing]
        if not candidates:
            raise ValueError("No OCR-approved pages are waiting for execution in this file run")
        with bulk_execution_lock:
            if any(job["run_dir"] == str(run_dir) and job["status"] == "running"
                   for job in bulk_executions.values()):
                raise ValueError("Batch OCR is already running for this file")
            execution_id = f"bulk_{uuid4().hex[:16]}"
            job = {
                "execution_id": execution_id,
                "run_dir": str(run_dir),
                "status": "running",
                "total_count": len(candidates),
                "completed_count": 0,
                "failed_count": 0,
                "pages": {state["review_id"]: {"page_number": state.get("page_number"),
                                            "status": "queued", "error": ""} for state in candidates},
            }
            bulk_executions[execution_id] = job
            for state in candidates:
                ocr_executor.submit(_run_bulk_page, execution_id, state["review_id"])
            return deepcopy(job)

    @app.get("/api/reviews/ocr/bulk/executions")
    def list_bulk_executions() -> list[dict[str, Any]]:
        with bulk_execution_lock:
            return deepcopy(list(bulk_executions.values()))

    @app.get("/api/reviews/ocr/{review_id}/preview")
    def ocr_preview(review_id: str) -> FileResponse:
        validate_review_id(review_id)
        state = review_store().get_task_state(review_id)
        path = Path(state["source_preview_path"]).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise FileNotFoundError("OCR preview is not available under the output root")
        return FileResponse(path)

    @app.get("/api/reviews/ocr/{review_id}/blocks")
    def ocr_block_preview(review_id: str) -> dict[str, Any]:
        validate_review_id(review_id)
        state = review_store().get_task_state(review_id)

        def read_blocks(value: str) -> list[dict[str, Any]]:
            if not value:
                return []
            path = Path(value).resolve()
            if not path.is_file() or not path.is_relative_to(root):
                raise FileNotFoundError("OCR block artifact is not available under the output root")
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            blocks = data.get("blocks") if isinstance(data, dict) else data
            if not isinstance(blocks, list):
                raise ValueError("OCR block artifact has an invalid format")
            if state["scope"] == "page" and state.get("page_number"):
                page = int(state["page_number"])
                blocks = [block for block in blocks if block.get("page_start") is not None
                          and int(block["page_start"]) <= page <= int(block.get("page_end") or block["page_start"])]
            elif state.get("target_block_ids"):
                target_ids = set(state["target_block_ids"])
                blocks = [block for block in blocks if block.get("block_id") in target_ids]
            return blocks

        native = read_blocks(state["native_blocks_path"])
        ocr = read_blocks(state.get("ocr_blocks_path") or "")
        return {"native_blocks": native[:30], "native_count": len(native),
                "ocr_blocks": ocr[:30], "ocr_count": len(ocr)}

    @app.post("/api/reviews/ocr/{review_id}/decision")
    def decide_ocr(review_id: str, body: ReviewDecision) -> dict[str, Any]:
        validate_review_id(review_id)
        store = review_store()
        store.record_decision(review_id, body.action, body.reviewer, body.note)
        return store.get_task_state(review_id)

    @app.post("/api/reviews/ocr/{review_id}/execute")
    def execute_ocr(review_id: str) -> dict[str, Any]:
        validate_review_id(review_id)
        with ocr_execution_lock:
            if review_id in ocr_executing:
                raise ValueError("OCR is already running for this task")
            ocr_executing.add(review_id)
        try:
            store = review_store()
            state = store.get_task_state(review_id)
            if state["status"] != "ocr_requested":
                raise ValueError("Task is not waiting for OCR execution")
            preview = Path(state["source_preview_path"])
            if not preview.is_file() or not preview.resolve().is_relative_to(root):
                raise FileNotFoundError("OCR preview is not available under the output root")
            provider = BaiduPaddleOcrProvider.from_env_file(Path(".env"))
            decision_id = state["latest_decision"]["decision_id"]
            attempt_dir = store.root / "ocr_attempts" / review_id / decision_id
            provider_path = attempt_dir / "01_provider_result.json"
            blocks_path = attempt_dir / "02_ocr_blocks.json"
            provider_result = provider.parse_image(preview)
            write_json(provider_path, provider_result)
            blocks = convert_parse_result_to_blocks(
                provider_result["parse_result"],
                document_id=f"{state['document_id']}_ocr_{review_id[-12:]}",
            )
            write_json(blocks_path, to_plain_data(blocks))
            store.record_ocr_result(review_id, provider.provider_name, provider_path, blocks_path)
            return store.get_task_state(review_id)
        finally:
            with ocr_execution_lock:
                ocr_executing.discard(review_id)

    @app.post("/api/reviews/ocr/{review_id}/apply")
    def apply_ocr(review_id: str) -> dict[str, Any]:
        validate_review_id(review_id)
        return review_store().apply(review_id)

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    else:
        @app.get("/")
        def missing_ui() -> dict[str, str]:
            return {"message": "Web UI has not been built. Run npm install and npm run build in frontend/."}
    return app
