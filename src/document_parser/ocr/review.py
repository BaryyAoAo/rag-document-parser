from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid

from ..cleaner import clean_blocks
from ..evaluation import evaluate_parsed_document
from ..io_utils import write_json
from ..models import Block, ImageData, ParsedDocument, SourceTrace, TableData
from ..quality import build_quality_report
from ..repair import repair_blocks


ALLOWED_ACTIONS = (
    "run_ocr",
    "approve_native",
    "approve_ocr",
    "merge_results",
    "retry_ocr",
    "exclude_page",
    "reject_document",
)
FINAL_ACTIONS = {
    "approve_native",
    "approve_ocr",
    "merge_results",
    "exclude_page",
    "reject_document",
}


@dataclass
class OcrReviewTask:
    review_id: str
    review_stage: str
    document_id: str
    page_number: int | None
    reason_codes: list[str]
    reason: str
    suggested_action: str
    source_preview_path: str
    native_blocks_path: str
    ocr_blocks_path: str = ""
    scope: str = "page"
    bbox: list[float] | None = None
    target_block_ids: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=lambda: list(ALLOWED_ACTIONS))
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _now_iso())


class OcrReviewStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.tasks_dir = self.root / "tasks"
        self.applied_dir = self.root / "applied"
        self.decisions_path = self.root / "decisions.jsonl"
        self.ocr_results_path = self.root / "ocr_results.jsonl"
        self.queue_path = self.root / "review_queue.csv"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.applied_dir.mkdir(parents=True, exist_ok=True)

    def create_task(self, task: OcrReviewTask) -> dict[str, Any]:
        self._validate_task(task)
        task_path = self._task_path(task.review_id)
        if task_path.exists():
            raise ValueError(f"Review task already exists: {task.review_id}")
        data = asdict(task)
        write_json(task_path, data)
        self._refresh_queue_csv()
        return self.get_task_state(task.review_id)

    def list_task_states(self, status: str | None = None) -> list[dict[str, Any]]:
        states = [self.get_task_state(path.stem) for path in sorted(self.tasks_dir.glob("*.json"))]
        if status:
            states = [state for state in states if state["status"] == status]
        return states

    def task_states_for_run(self, run_dir: Path) -> list[dict[str, Any]]:
        target = run_dir.resolve()
        return [
            state
            for state in self.list_task_states()
            if _same_path((state.get("metadata") or {}).get("run_dir"), target)
        ]

    def get_task(self, review_id: str) -> dict[str, Any]:
        path = self._task_path(review_id)
        if not path.is_file():
            raise FileNotFoundError(f"Review task not found: {review_id}")
        return _read_json(path)

    def get_task_state(self, review_id: str) -> dict[str, Any]:
        task = self._effective_task(review_id)
        latest = self.latest_decision(review_id)
        latest_ocr_result = self.latest_ocr_result(review_id)
        if latest is None:
            status = "pending"
        elif self._apply_manifest_path(review_id, latest["decision_id"]).is_file():
            status = "applied"
        elif latest["selected_action"] in {"run_ocr", "retry_ocr"}:
            status = (
                "ocr_result_ready"
                if latest_ocr_result
                and latest_ocr_result.get("requested_decision_id") == latest["decision_id"]
                else "ocr_requested"
            )
        else:
            status = "decided"
        return {
            **task,
            "status": status,
            "latest_decision": latest,
            "latest_ocr_result": latest_ocr_result,
        }

    def decisions(self, review_id: str | None = None) -> list[dict[str, Any]]:
        if not self.decisions_path.is_file():
            return []
        records = []
        for line in self.decisions_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if review_id is None or record.get("review_id") == review_id:
                records.append(record)
        return records

    def latest_decision(self, review_id: str) -> dict[str, Any] | None:
        records = self.decisions(review_id)
        return records[-1] if records else None

    def ocr_results(self, review_id: str | None = None) -> list[dict[str, Any]]:
        if not self.ocr_results_path.is_file():
            return []
        records = []
        for line in self.ocr_results_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if review_id is None or record.get("review_id") == review_id:
                records.append(record)
        return records

    def latest_ocr_result(self, review_id: str) -> dict[str, Any] | None:
        records = self.ocr_results(review_id)
        return records[-1] if records else None

    def record_ocr_result(
        self,
        review_id: str,
        provider: str,
        provider_result_path: Path,
        ocr_blocks_path: Path,
    ) -> dict[str, Any]:
        decision = self.latest_decision(review_id)
        if decision is None or decision["selected_action"] not in {"run_ocr", "retry_ocr"}:
            raise ValueError("OCR result requires a latest run_ocr or retry_ocr decision")
        if not provider_result_path.is_file() or not ocr_blocks_path.is_file():
            raise FileNotFoundError("OCR provider result and OCR blocks must both exist")
        result = {
            "ocr_result_id": f"ocr_result_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "review_id": review_id,
            "requested_decision_id": decision["decision_id"],
            "provider": provider,
            "provider_result_path": str(provider_result_path.resolve()),
            "ocr_blocks_path": str(ocr_blocks_path.resolve()),
            "completed_at": _now_iso(),
        }
        with self.ocr_results_path.open("a", encoding="utf-8", newline="") as file:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
        self._refresh_queue_csv()
        return result

    def record_decision(
        self,
        review_id: str,
        action: str,
        reviewer: str,
        note: str = "",
        native_block_ids: list[str] | None = None,
        ocr_block_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        task = self._effective_task(review_id)
        if action not in task["allowed_actions"] or action not in ALLOWED_ACTIONS:
            raise ValueError(f"Action is not allowed for {review_id}: {action}")
        if not reviewer.strip():
            raise ValueError("Reviewer is required")
        native_ids = _clean_ids(native_block_ids)
        ocr_ids = _clean_ids(ocr_block_ids)
        if action == "merge_results" and not native_ids and not ocr_ids:
            raise ValueError("merge_results requires native_block_ids and/or ocr_block_ids")
        if action in {"approve_ocr", "merge_results", "retry_ocr"} and not task.get("ocr_blocks_path"):
            if action != "retry_ocr":
                raise ValueError(f"Action {action} requires ocr_blocks_path")

        decision = {
            "decision_id": f"decision_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "review_id": review_id,
            "selected_action": action,
            "reviewer": reviewer.strip(),
            "review_note": note.strip(),
            "native_block_ids": native_ids,
            "ocr_block_ids": ocr_ids,
            "decided_at": _now_iso(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self.decisions_path.open("a", encoding="utf-8", newline="") as file:
            file.write(json.dumps(decision, ensure_ascii=False) + "\n")
        self._refresh_queue_csv()
        return decision

    def apply(self, review_id: str) -> dict[str, Any]:
        task = self._effective_task(review_id)
        decision = self.latest_decision(review_id)
        if decision is None:
            raise ValueError(f"Review task has no decision: {review_id}")
        action = decision["selected_action"]
        if action not in FINAL_ACTIONS:
            raise ValueError(f"Action {action} is not a final action and cannot be applied")

        manifest_path = self._apply_manifest_path(review_id, decision["decision_id"])
        if manifest_path.is_file():
            return _read_json(manifest_path)

        native_all = _read_block_list(Path(task["native_blocks_path"]))
        native_page = _task_native_blocks(native_all, task)
        ocr_all = (
            _read_block_list(Path(task["ocr_blocks_path"]))
            if task.get("ocr_blocks_path")
            else []
        )
        ocr_page = _remap_ocr_blocks(
            ocr_all,
            document_id=task["document_id"],
            page_number=task.get("page_number"),
            native_blocks=native_page,
            review_id=review_id,
        )

        final_blocks: list[dict[str, Any]] = []
        final_source = {
            "approve_native": "native",
            "approve_ocr": "ocr",
            "merge_results": "merged",
            "exclude_page": "excluded",
            "reject_document": "rejected",
        }[action]
        if action == "approve_native":
            final_blocks = _mark_final_source(native_page, "native")
        elif action == "approve_ocr":
            final_blocks = _mark_final_source(ocr_page, "ocr")
        elif action == "merge_results":
            selected_native = _select_blocks(native_page, decision["native_block_ids"], "native")
            selected_ocr = _select_blocks(ocr_page, decision["ocr_block_ids"], "ocr")
            final_blocks = _sort_blocks(selected_native + selected_ocr)
        elif action == "exclude_page":
            final_blocks = []
        elif action == "reject_document":
            final_blocks = []

        apply_dir = manifest_path.parent
        apply_dir.mkdir(parents=True, exist_ok=False)
        blocks_path = apply_dir / "01_final_page_blocks.json"
        write_json(blocks_path, final_blocks)
        rebuild = self._rebuild_document_if_available(
            task=task,
            final_blocks=final_blocks,
            apply_dir=apply_dir,
            rejected=action == "reject_document",
        )
        manifest = {
            "review_id": review_id,
            "decision_id": decision["decision_id"],
            "document_id": task["document_id"],
            "page_number": task["page_number"],
            "target_block_ids": list(task.get("target_block_ids") or []),
            "selected_action": action,
            "final_source": final_source,
            "document_blocked": action == "reject_document",
            "page_excluded": action == "exclude_page",
            "native_page_block_count": len(native_page),
            "ocr_page_block_count": len(ocr_page),
            "final_page_block_count": len(final_blocks),
            "replaces_block_ids": [block.get("block_id") for block in native_page],
            "final_page_blocks_path": str(blocks_path),
            "requires_downstream_rebuild": rebuild["status"] != "completed" and action != "reject_document",
            "downstream_rebuild": rebuild,
            "applied_at": _now_iso(),
        }
        write_json(manifest_path, manifest)
        run_dir = (task.get("metadata") or {}).get("run_dir")
        if run_dir:
            manifest["document_finalization"] = self.finalize_run(Path(run_dir))
            write_json(manifest_path, manifest)
        self._refresh_queue_csv()
        return manifest

    def finalize_run(self, run_dir: Path) -> dict[str, Any]:
        run_dir = run_dir.resolve()
        states = self.task_states_for_run(run_dir)
        if not states:
            raise ValueError(f"No OCR review tasks found for run: {run_dir}")

        unresolved = [state["review_id"] for state in states if state["status"] != "applied"]
        status_path = run_dir / "ocr" / "finalized" / "00_finalization_status.json"
        if unresolved:
            status = {
                "status": "pending_reviews",
                "run_dir": str(run_dir),
                "task_count": len(states),
                "resolved_count": len(states) - len(unresolved),
                "unresolved_review_ids": unresolved,
                "updated_at": _now_iso(),
            }
            write_json(status_path, status)
            return status

        applied = []
        for state in states:
            decision = state["latest_decision"]
            manifest_path = self._apply_manifest_path(state["review_id"], decision["decision_id"])
            applied.append((state, decision, _read_json(manifest_path)))
        if any(manifest["document_blocked"] for _, _, manifest in applied):
            status = {
                "status": "blocked",
                "reason": "document_rejected_by_review",
                "run_dir": str(run_dir),
                "review_ids": [state["review_id"] for state, _, _ in applied],
                "updated_at": _now_iso(),
            }
            write_json(status_path, status)
            return status

        source_paths = {str(Path(state["native_blocks_path"]).resolve()) for state, _, _ in applied}
        if len(source_paths) != 1:
            raise ValueError("All OCR review tasks for one run must share the same ParsedDocument")
        source_value = _read_json(Path(next(iter(source_paths))))
        if not isinstance(source_value, dict) or not isinstance(source_value.get("blocks"), list):
            raise ValueError("Document finalization requires native_blocks_path to contain ParsedDocument")

        decision_ids = sorted(decision["decision_id"] for _, decision, _ in applied)
        finalization_id = "final_" + hashlib.sha256("|".join(decision_ids).encode("utf-8")).hexdigest()[:12]
        final_dir = run_dir / "ocr" / "finalized" / finalization_id
        final_manifest_path = final_dir / "05_finalization_manifest.json"
        if final_manifest_path.is_file():
            return _read_json(final_manifest_path)

        page_replacements: dict[int, list[dict[str, Any]]] = {}
        block_replacements: dict[str, list[dict[str, Any]]] = {}
        page_sources: dict[str, str] = {}
        target_sources: dict[str, str] = {}
        source_blocks = list(source_value["blocks"])
        for state, decision, manifest in applied:
            action = decision["selected_action"]
            page_number = state.get("page_number")
            target_block_ids = list(state.get("target_block_ids") or [])
            if page_number is not None:
                page_sources[str(page_number)] = manifest["final_source"]
            for block_id in target_block_ids:
                target_sources[block_id] = manifest["final_source"]
            if action == "approve_native":
                continue
            final_blocks = _read_block_list(Path(manifest["final_page_blocks_path"]))
            if target_block_ids:
                available_ids = {str(block.get("block_id")) for block in source_blocks}
                missing = sorted(set(target_block_ids) - available_ids)
                if missing:
                    raise ValueError(f"Reviewed target blocks are missing: {missing}")
                overlapping = sorted(set(target_block_ids) & set(block_replacements))
                if overlapping:
                    raise ValueError(f"Multiple OCR reviews replace the same blocks: {overlapping}")
                for block_id in target_block_ids:
                    block_replacements[block_id] = final_blocks
                continue
            if page_number is None:
                raise ValueError(
                    f"Review task has neither page_number nor target_block_ids: {state['review_id']}"
                )
            page_number = int(page_number)
            spanning = [
                block.get("block_id")
                for block in source_blocks
                if _block_spans_multiple_pages(block) and _block_contains_page(block, page_number)
            ]
            if spanning:
                status = {
                    "status": "blocked",
                    "reason": "reviewed_page_intersects_cross_page_block",
                    "page_number": page_number,
                    "spanning_block_ids": spanning,
                    "updated_at": _now_iso(),
                }
                write_json(status_path, status)
                return status
            page_replacements[page_number] = final_blocks

        retained = [
            block
            for block in source_blocks
            if str(block.get("block_id")) not in block_replacements
            and not any(
                _block_contains_page(block, page_number) for page_number in page_replacements
            )
        ]
        replacement_blocks = [
            block
            for page_number in sorted(page_replacements)
            for block in page_replacements[page_number]
        ]
        seen_groups: set[int] = set()
        for blocks in block_replacements.values():
            identity = id(blocks)
            if identity in seen_groups:
                continue
            seen_groups.add(identity)
            replacement_blocks.extend(blocks)
        outputs = _build_document_outputs(
            source_value=source_value,
            plain_blocks=retained + replacement_blocks,
            output_dir=final_dir,
        )
        finalization = {
            "status": "completed",
            "finalization_id": finalization_id,
            "run_dir": str(run_dir),
            "document_id": source_value.get("document_id"),
            "task_count": len(states),
            "decision_ids": decision_ids,
            "page_sources": page_sources,
            "target_sources": target_sources,
            **outputs,
            "finalized_at": _now_iso(),
        }
        write_json(final_manifest_path, finalization)
        write_json(status_path, finalization)
        return finalization

    def _rebuild_document_if_available(
        self,
        task: dict[str, Any],
        final_blocks: list[dict[str, Any]],
        apply_dir: Path,
        rejected: bool,
    ) -> dict[str, Any]:
        if rejected:
            return {"status": "blocked", "reason": "document_rejected"}
        source_value = _read_json(Path(task["native_blocks_path"]))
        if not isinstance(source_value, dict) or not isinstance(source_value.get("blocks"), list):
            return {
                "status": "not_available",
                "reason": "native_blocks_path_does_not_contain_parsed_document",
            }

        source_blocks = source_value["blocks"]
        target_block_ids = set(task.get("target_block_ids") or [])
        if target_block_ids:
            source_ids = {str(block.get("block_id")) for block in source_blocks}
            missing = sorted(target_block_ids - source_ids)
            if missing:
                return {
                    "status": "blocked",
                    "reason": "target_blocks_not_found",
                    "missing_block_ids": missing,
                }
            retained = [
                block
                for block in source_blocks
                if str(block.get("block_id")) not in target_block_ids
            ]
        else:
            page_number = int(task["page_number"])
            spanning = [
                block.get("block_id")
                for block in source_blocks
                if int(block.get("page_start") or 0) < page_number < int(block.get("page_end") or 0)
            ]
            if spanning:
                return {
                    "status": "blocked",
                    "reason": "target_page_is_inside_cross_page_block",
                    "spanning_block_ids": spanning,
                }

            retained = [
                block
                for block in source_blocks
                if not (
                    int(block.get("page_start") or 0) <= page_number
                    <= int(block.get("page_end") or 0)
                )
            ]
        block_objects = [_block_from_plain(block) for block in retained + final_blocks]
        rebuilt_blocks = repair_blocks(clean_blocks(block_objects))
        quality_report = build_quality_report(task["document_id"], [], rebuilt_blocks)
        document = ParsedDocument(
            document_id=task["document_id"],
            title=str(source_value.get("title", "")),
            file_type=str(source_value.get("file_type", "pdf")),
            source_path=str(source_value.get("source_path", "")),
            source_url=str(source_value.get("source_url", "")),
            metadata=dict(source_value.get("metadata") or {}),
            blocks=rebuilt_blocks,
            quality_report=quality_report,
        )
        evaluation = evaluate_parsed_document(document)

        document_path = apply_dir / "03_rebuilt_parsed_document.json"
        evaluation_path = apply_dir / "04_rebuilt_evaluation.json"
        write_json(document_path, document)
        write_json(evaluation_path, evaluation)
        return {
            "status": "completed",
            "parsed_document_path": str(document_path),
            "evaluation_path": str(evaluation_path),
            "block_count": len(rebuilt_blocks),
            "evaluation_status": evaluation.status,
        }

    def _validate_task(self, task: OcrReviewTask) -> None:
        if not task.review_id.strip():
            raise ValueError("review_id is required")
        if not task.document_id.strip():
            raise ValueError("document_id is required")
        if task.page_number is not None and task.page_number < 1:
            raise ValueError("page_number must be >= 1")
        if task.page_number is None and not task.target_block_ids:
            raise ValueError("page_number or target_block_ids is required")
        if task.suggested_action not in task.allowed_actions:
            raise ValueError("suggested_action must be included in allowed_actions")
        if not Path(task.native_blocks_path).is_file():
            raise FileNotFoundError(f"Native blocks file not found: {task.native_blocks_path}")
        if task.ocr_blocks_path and not Path(task.ocr_blocks_path).is_file():
            raise FileNotFoundError(f"OCR blocks file not found: {task.ocr_blocks_path}")

    def _task_path(self, review_id: str) -> Path:
        return self.tasks_dir / f"{review_id}.json"

    def _effective_task(self, review_id: str) -> dict[str, Any]:
        task = self.get_task(review_id)
        result = self.latest_ocr_result(review_id)
        if result is None:
            return task
        task["review_stage"] = "post_ocr"
        task["ocr_blocks_path"] = result["ocr_blocks_path"]
        task["allowed_actions"] = [
            "approve_native",
            "approve_ocr",
            "merge_results",
            "retry_ocr",
            "exclude_page",
            "reject_document",
        ]
        return task

    def _apply_manifest_path(self, review_id: str, decision_id: str) -> Path:
        return self.applied_dir / review_id / decision_id / "02_apply_manifest.json"

    def _refresh_queue_csv(self) -> None:
        rows = []
        for state in self.list_task_states():
            latest = state.get("latest_decision") or {}
            rows.append(
                {
                    "review_id": state["review_id"],
                    "status": state["status"],
                    "review_stage": state["review_stage"],
                    "document_id": state["document_id"],
                    "page_number": state["page_number"],
                    "reason": state["reason"],
                    "suggested_action": state["suggested_action"],
                    "selected_action": latest.get("selected_action", ""),
                    "reviewer": latest.get("reviewer", ""),
                    "reviewed_at": latest.get("decided_at", ""),
                    "source_preview_path": state["source_preview_path"],
                }
            )
        fieldnames = [
            "review_id",
            "status",
            "review_stage",
            "document_id",
            "page_number",
            "reason",
            "suggested_action",
            "selected_action",
            "reviewer",
            "reviewed_at",
            "source_preview_path",
        ]
        self.root.mkdir(parents=True, exist_ok=True)
        with self.queue_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_block_list(path: Path) -> list[dict[str, Any]]:
    value = _read_json(path)
    if isinstance(value, dict):
        value = value.get("blocks", [])
    if not isinstance(value, list):
        raise ValueError(f"Blocks file must contain a JSON array: {path}")
    return [item for item in value if isinstance(item, dict)]


def _build_document_outputs(
    source_value: dict[str, Any],
    plain_blocks: list[dict[str, Any]],
    output_dir: Path,
) -> dict[str, Any]:
    block_objects = [_block_from_plain(block) for block in plain_blocks]
    rebuilt_blocks = repair_blocks(clean_blocks(block_objects))
    document_id = str(source_value.get("document_id", ""))
    quality_report = build_quality_report(document_id, [], rebuilt_blocks)
    document = ParsedDocument(
        document_id=document_id,
        title=str(source_value.get("title", "")),
        file_type=str(source_value.get("file_type", "pdf")),
        source_path=str(source_value.get("source_path", "")),
        source_url=str(source_value.get("source_url", "")),
        metadata=dict(source_value.get("metadata") or {}),
        blocks=rebuilt_blocks,
        quality_report=quality_report,
    )
    evaluation = evaluate_parsed_document(document)

    output_dir.mkdir(parents=True, exist_ok=True)
    document_path = output_dir / "01_finalized_parsed_document.json"
    evaluation_path = output_dir / "02_finalized_evaluation.json"
    write_json(document_path, document)
    write_json(evaluation_path, evaluation)
    return {
        "parse_status": evaluation.status,
        "parsed_document_path": str(document_path),
        "evaluation_path": str(evaluation_path),
        "block_count": len(rebuilt_blocks),
        "evaluation_status": evaluation.status,
    }


def _block_from_plain(value: dict[str, Any]) -> Block:
    data = deepcopy(value)
    table_data = data.get("table_data")
    image = data.get("image")
    source_trace = data.get("source_trace")
    return Block(
        block_id=str(data.get("block_id", "")),
        document_id=str(data.get("document_id", "")),
        block_type=str(data.get("block_type", "paragraph")),
        text=data.get("text"),
        table_data=TableData(**table_data) if isinstance(table_data, dict) else None,
        image=ImageData(**image) if isinstance(image, dict) else None,
        page_start=data.get("page_start"),
        page_end=data.get("page_end"),
        bbox=data.get("bbox"),
        heading_path=list(data.get("heading_path") or []),
        metadata=dict(data.get("metadata") or {}),
        source_trace=SourceTrace(**source_trace) if isinstance(source_trace, dict) else None,
        quality_flags=list(data.get("quality_flags") or []),
        order=int(data.get("order") or 0),
    )


def _page_blocks(blocks: list[dict[str, Any]], page_number: int) -> list[dict[str, Any]]:
    return [
        deepcopy(block)
        for block in blocks
        if int(block.get("page_start") or 0) <= page_number <= int(block.get("page_end") or 0)
    ]


def _remap_ocr_blocks(
    blocks: list[dict[str, Any]],
    document_id: str,
    page_number: int | None,
    native_blocks: list[dict[str, Any]],
    review_id: str,
) -> list[dict[str, Any]]:
    anchor = native_blocks[0] if native_blocks else {}
    page_start = page_number if page_number is not None else anchor.get("page_start")
    page_end = page_number if page_number is not None else anchor.get("page_end")
    unit = f"p{page_number:04d}" if page_number is not None else review_id[-12:]
    remapped = []
    for index, source in enumerate(blocks, start=1):
        block = deepcopy(source)
        original_id = str(block.get("block_id", ""))
        block["block_id"] = f"{document_id}_{unit}_ocr_review_{index:04d}"
        block["document_id"] = document_id
        block["page_start"] = page_start
        block["page_end"] = page_end
        block["order"] = int(anchor.get("order") or 0) + index
        metadata = dict(block.get("metadata") or {})
        metadata["ocr_original_block_id"] = original_id
        metadata["ocr_source_page_number"] = source.get("page_start")
        metadata["replaces_block_ids"] = [item.get("block_id") for item in native_blocks]
        block["metadata"] = metadata
        remapped.append(block)
    return remapped


def _task_native_blocks(
    blocks: list[dict[str, Any]],
    task: dict[str, Any],
) -> list[dict[str, Any]]:
    target_ids = set(task.get("target_block_ids") or [])
    if target_ids:
        available = {str(block.get("block_id")) for block in blocks}
        missing = sorted(target_ids - available)
        if missing:
            raise ValueError(f"Target blocks were not found: {missing}")
        return [
            deepcopy(block)
            for block in blocks
            if str(block.get("block_id")) in target_ids
        ]
    return _page_blocks(blocks, int(task["page_number"]))


def _mark_final_source(blocks: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    marked = []
    for block in blocks:
        item = deepcopy(block)
        metadata = dict(item.get("metadata") or {})
        metadata["final_source"] = source
        metadata["ocr_status"] = f"approved_{source}"
        metadata["ocr_decision"] = f"approved_{source}"
        item["metadata"] = metadata
        item["quality_flags"] = [
            flag
            for flag in item.get("quality_flags", [])
            if flag not in {"manual_review", "auto_ocr_candidate", "review_before_ocr", "auto_ocr"}
        ]
        marked.append(item)
    return _sort_blocks(marked)


def _select_blocks(
    blocks: list[dict[str, Any]],
    selected_ids: list[str],
    source: str,
) -> list[dict[str, Any]]:
    if selected_ids == ["all"]:
        selected = blocks
    else:
        selected_set = set(selected_ids)
        available = {str(block.get("block_id")) for block in blocks}
        missing = selected_set - available
        if missing:
            raise ValueError(f"Selected {source} block IDs were not found: {sorted(missing)}")
        selected = [block for block in blocks if str(block.get("block_id")) in selected_set]
    return _mark_final_source(selected, source)


def _sort_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(block: dict[str, Any]) -> tuple[Any, ...]:
        bbox = block.get("bbox") or [0, 0, 0, 0]
        return (
            int(block.get("page_start") or 0),
            float(bbox[1]) if len(bbox) > 1 else 0.0,
            float(bbox[0]) if bbox else 0.0,
            int(block.get("order") or 0),
        )

    result = sorted(blocks, key=sort_key)
    for index, block in enumerate(result, start=1):
        block["order"] = index
    return result


def _clean_ids(values: list[str] | None) -> list[str]:
    return [value.strip() for value in (values or []) if value.strip()]


def _same_path(value: Any, target: Path) -> bool:
    if not value:
        return False
    try:
        return Path(str(value)).resolve() == target
    except (OSError, ValueError):
        return False


def _block_contains_page(block: dict[str, Any], page_number: int) -> bool:
    page_start = int(block.get("page_start") or 0)
    page_end = int(block.get("page_end") or page_start)
    return page_start <= page_number <= page_end


def _block_spans_multiple_pages(block: dict[str, Any]) -> bool:
    page_start = int(block.get("page_start") or 0)
    page_end = int(block.get("page_end") or page_start)
    return page_end > page_start


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
