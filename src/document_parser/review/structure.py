from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid

from ..artifacts import resolve_parsing_artifacts
from ..evaluation import evaluate_parsed_document
from ..io_utils import write_json
from ..models import Block, ImageData, ParsedDocument, SourceTrace, TableData
from ..quality import build_quality_report


STRUCTURE_ACTIONS = ("approve", "correct_text", "exclude_block", "reject_document")
REVIEW_FLAG_BY_TYPE = {
    "cross_page_paragraph": "merged_cross_page_paragraph",
    "continued_table": "merged_continued_table",
}


class StructureReviewStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir.resolve()
        self.root = self.run_dir / "structure_review"
        self.tasks_dir = self.root / "tasks"
        self.decisions_path = self.root / "decisions.jsonl"
        self.status_path = self.root / "finalized" / "00_finalization_status.json"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def create_from_run(self) -> list[dict[str, Any]]:
        source = resolve_parsing_artifacts(self.run_dir, include_structure=False)
        evaluation = _read_json(source.evaluation_path)
        states = []
        for item in evaluation.get("manual_review_items") or []:
            if item.get("review_type") not in REVIEW_FLAG_BY_TYPE:
                continue
            task = {
                **item,
                "allowed_actions": list(STRUCTURE_ACTIONS),
                "source_artifact": source.source,
                "source_revision_id": source.revision_id,
                "source_document_path": str(source.parsed_document_path),
                "source_evaluation_path": str(source.evaluation_path),
                "run_dir": str(self.run_dir),
                "created_at": _now_iso(),
            }
            path = self.tasks_dir / f"{task['review_id']}.json"
            if not path.exists():
                write_json(path, task)
            states.append(self.task_state(str(task["review_id"])))
        return states

    def list_states(self) -> list[dict[str, Any]]:
        return [self.task_state(path.stem) for path in sorted(self.tasks_dir.glob("*.json"))]

    def task_state(self, review_id: str) -> dict[str, Any]:
        task = _read_json(self._task_path(review_id))
        decision = self.latest_decision(review_id)
        return {**task, "status": "decided" if decision else "pending", "latest_decision": decision}

    def record_decision(
        self,
        review_id: str,
        action: str,
        reviewer: str,
        note: str = "",
        corrected_text: str = "",
    ) -> dict[str, Any]:
        self.task_state(review_id)
        if action not in STRUCTURE_ACTIONS:
            raise ValueError(f"Unsupported action: {action}")
        if action == "correct_text" and not corrected_text.strip():
            raise ValueError("correct_text requires corrected_text")
        decision = {
            "decision_id": f"decision_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "review_id": review_id,
            "selected_action": action,
            "corrected_text": corrected_text if action == "correct_text" else "",
            "reviewer": reviewer.strip(),
            "note": note.strip(),
            "decided_at": _now_iso(),
        }
        with self.decisions_path.open("a", encoding="utf-8", newline="") as file:
            file.write(json.dumps(decision, ensure_ascii=False) + "\n")
        return decision

    def latest_decision(self, review_id: str) -> dict[str, Any] | None:
        matches = [item for item in self._decisions() if item.get("review_id") == review_id]
        return matches[-1] if matches else None

    def finalize(self) -> dict[str, Any]:
        states = self.list_states()
        if not states:
            raise ValueError("No structure review tasks exist for this run")
        pending = [state["review_id"] for state in states if state["status"] == "pending"]
        if pending:
            raise ValueError(f"Structure review still has pending tasks: {', '.join(pending)}")
        source_paths = {state["source_document_path"] for state in states}
        if len(source_paths) != 1:
            raise ValueError("Structure review tasks refer to different source documents")
        source = _read_json(Path(next(iter(source_paths))))
        blocks = deepcopy(source.get("blocks") or [])
        by_id = {str(block.get("block_id")): block for block in blocks}
        decisions = []
        for state in states:
            decision = state["latest_decision"]
            decisions.append(decision)
            if decision["selected_action"] == "reject_document":
                status = {
                    "status": "blocked",
                    "reason": "document_rejected_by_structure_review",
                    "updated_at": _now_iso(),
                }
                write_json(self.status_path, status)
                return status
            block = by_id.get(str(state.get("object_id") or ""))
            if block is None:
                raise ValueError(f"Reviewed block not found: {state.get('object_id')}")
            _apply_decision(block, state, decision)

        decision_ids = sorted(item["decision_id"] for item in decisions)
        finalization_id = "final_" + hashlib.sha256("|".join(decision_ids).encode("utf-8")).hexdigest()[:12]
        final_dir = self.root / "finalized" / finalization_id
        manifest_path = final_dir / "03_finalization_manifest.json"
        if manifest_path.is_file():
            return _read_json(manifest_path)
        block_objects = [_block_from_plain(block) for block in blocks]
        document_id = str(source["document_id"])
        metadata = dict(source.get("metadata") or {})
        metadata["structure_review_finalization_id"] = finalization_id
        document = ParsedDocument(
            document_id=document_id,
            title=str(source.get("title") or ""),
            file_type=str(source.get("file_type") or ""),
            source_path=str(source.get("source_path") or ""),
            source_url=str(source.get("source_url") or ""),
            metadata=metadata,
            blocks=block_objects,
            quality_report=build_quality_report(document_id, [], block_objects),
        )
        evaluation = evaluate_parsed_document(document)
        document_path = final_dir / "01_finalized_parsed_document.json"
        evaluation_path = final_dir / "02_finalized_evaluation.json"
        write_json(document_path, document)
        write_json(evaluation_path, evaluation)
        manifest = {
            "status": "completed",
            "finalization_id": finalization_id,
            "run_dir": str(self.run_dir),
            "decision_ids": decision_ids,
            "parsed_document_path": str(document_path.resolve()),
            "evaluation_path": str(evaluation_path.resolve()),
            "evaluation_status": evaluation.status,
            "block_count": len(blocks),
            "finalized_at": _now_iso(),
        }
        write_json(manifest_path, manifest)
        write_json(self.status_path, manifest)
        return manifest

    def _task_path(self, review_id: str) -> Path:
        path = self.tasks_dir / f"{review_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Structure review task not found: {review_id}")
        return path

    def _decisions(self) -> list[dict[str, Any]]:
        if not self.decisions_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.decisions_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def _apply_decision(block: dict[str, Any], task: dict[str, Any], decision: dict[str, Any]) -> None:
    action = decision["selected_action"]
    metadata = dict(block.get("metadata") or {})
    audit = {
        "review_id": task["review_id"],
        "decision_id": decision["decision_id"],
        "action": action,
        "reviewer": decision["reviewer"],
        "decided_at": decision["decided_at"],
        "note": decision.get("note") or "",
    }
    if action == "correct_text":
        audit["original_text"] = block.get("text")
        block["text"] = decision["corrected_text"]
    elif action == "exclude_block":
        metadata["excluded_from_retrieval"] = True
        metadata["exclusion_reason"] = "structure_review"
    flag = REVIEW_FLAG_BY_TYPE.get(str(task.get("review_type") or ""))
    if flag:
        block["quality_flags"] = [value for value in block.get("quality_flags") or [] if value != flag]
    metadata["structure_review"] = audit
    metadata["structure_review_status"] = "resolved"
    block["metadata"] = metadata


def _block_from_plain(value: dict[str, Any]) -> Block:
    table = value.get("table_data")
    image = value.get("image")
    trace = value.get("source_trace")
    return Block(
        block_id=str(value.get("block_id") or ""),
        document_id=str(value.get("document_id") or ""),
        block_type=str(value.get("block_type") or "paragraph"),
        text=value.get("text"),
        table_data=TableData(**table) if isinstance(table, dict) else None,
        image=ImageData(**image) if isinstance(image, dict) else None,
        page_start=value.get("page_start"),
        page_end=value.get("page_end"),
        bbox=value.get("bbox"),
        heading_path=list(value.get("heading_path") or []),
        metadata=dict(value.get("metadata") or {}),
        source_trace=SourceTrace(**trace) if isinstance(trace, dict) else None,
        quality_flags=list(value.get("quality_flags") or []),
        order=int(value.get("order") or 0),
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
