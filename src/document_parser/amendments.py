"""Audited corrections to an already finalized ParsedDocument."""

from __future__ import annotations

from pathlib import Path
import json
from uuid import uuid4

from .artifacts import resolve_parsing_artifacts
from .evaluation import evaluate_parsed_document
from .io_utils import write_json
from .models import Block, ParsedDocument, SourceTrace, to_plain_data, utc_now_iso
from .quality import build_quality_report
from .serialization import load_resolved_parsed_document


def amend_final_document(
    run_dir: Path,
    *,
    reviewer: str,
    reason: str,
    evidence: str,
    include_block_ids: list[str] | None = None,
    add_text_blocks: list[dict] | None = None,
) -> dict:
    """Write a new effective revision, retaining every previous artifact and audit trail."""
    if not reviewer.strip() or not reason.strip() or not evidence.strip():
        raise ValueError("Reviewer, reason and evidence are required")
    run_dir = run_dir.resolve()
    previous = resolve_parsing_artifacts(run_dir)
    if previous.source == "base_run":
        raise ValueError("Manual amendments require a finalized review revision")
    document = load_resolved_parsed_document(run_dir)
    blocks = document.blocks
    by_id = {block.block_id: block for block in blocks}
    for block_id in include_block_ids or []:
        if block_id not in by_id:
            raise ValueError(f"Block not found: {block_id}")
        block = by_id[block_id]
        block.metadata["excluded_from_retrieval"] = False
        block.metadata["manual_inclusion_reason"] = reason
        block.quality_flags = [flag for flag in block.quality_flags if flag != "excluded_layout_noise"]
    next_order = max((block.order for block in blocks), default=0)
    for addition in add_text_blocks or []:
        text = str(addition.get("text") or "").strip()
        page = addition.get("page")
        if not text or not isinstance(page, int) or page < 1:
            raise ValueError("Added text requires nonempty text and positive page")
        next_order += 1
        blocks.append(Block(
            block_id=f"{document.document_id}_manual_{uuid4().hex[:12]}",
            document_id=document.document_id,
            block_type="paragraph",
            text=text,
            page_start=page,
            page_end=page,
            bbox=addition.get("bbox"),
            metadata={"source_type": "manual_revision", "evidence": evidence},
            source_trace=SourceTrace(parser="manual_verified_from_source", raw_object_type="text", raw_page_index=page - 1),
            order=next_order,
        ))
    revision_id = f"manual_{uuid4().hex[:12]}"
    if previous.source == "manual_revision":
        previous_status = json.loads((run_dir / "manual_revision" / "finalized" / "00_finalization_status.json").read_text(encoding="utf-8-sig"))
        base_review_document_path = previous_status.get("base_review_document_path") or previous_status["previous_document_path"]
    else:
        base_review_document_path = str(previous.parsed_document_path)
    output_dir = run_dir / "manual_revision" / "finalized" / revision_id
    revised = ParsedDocument(
        document_id=document.document_id,
        title=document.title,
        file_type=document.file_type,
        source_path=document.source_path,
        source_url=document.source_url,
        metadata={**document.metadata, "effective_revision": revision_id},
        blocks=blocks,
        quality_report=build_quality_report(document.document_id, [], blocks),
    )
    evaluation = evaluate_parsed_document(revised)
    document_path = output_dir / "01_finalized_parsed_document.json"
    evaluation_path = output_dir / "02_finalized_evaluation.json"
    write_json(document_path, revised)
    write_json(evaluation_path, evaluation)
    status = {
        "status": "completed", "finalization_id": revision_id,
        "parsed_document_path": str(document_path), "evaluation_path": str(evaluation_path),
        "previous_source": previous.source,
        "previous_document_path": str(previous.parsed_document_path),
        "base_review_document_path": base_review_document_path,
        "reviewer": reviewer, "reason": reason, "evidence": evidence,
        "included_block_ids": include_block_ids or [],
        "added_text_blocks": add_text_blocks or [],
        "finalized_at": utc_now_iso(),
    }
    write_json(output_dir / "03_amendment_manifest.json", status)
    write_json(run_dir / "manual_revision" / "finalized" / "00_finalization_status.json", status)
    return to_plain_data(status)
