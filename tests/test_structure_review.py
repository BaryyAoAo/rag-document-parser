from __future__ import annotations

import json
from pathlib import Path

import pytest

from document_parser.io_utils import write_json
from document_parser.review import StructureReviewStore


def _create_reviewable_run(run_dir: Path) -> None:
    write_json(
        run_dir / "06_parsed_document.json",
        {
            "document_id": "doc_test",
            "title": "Test document",
            "file_type": "pdf",
            "source_path": "fixture.pdf",
            "source_url": "",
            "metadata": {},
            "blocks": [
                {
                    "block_id": "block_1",
                    "document_id": "doc_test",
                    "block_type": "paragraph",
                    "text": "incorrect text",
                    "table_data": None,
                    "image": None,
                    "page_start": 1,
                    "page_end": 2,
                    "bbox": [10, 700, 500, 900],
                    "heading_path": [],
                    "metadata": {},
                    "source_trace": None,
                    "quality_flags": ["merged_cross_page_paragraph"],
                    "order": 0
                }
            ],
            "quality_report": None,
            "parsed_at": "2026-01-01T00:00:00+00:00"
        },
    )
    write_json(
        run_dir / "07_parsing_evaluation.json",
        {
            "document_id": "doc_test",
            "status": "review_required",
            "manual_review_items": [
                {
                    "review_id": "review_1",
                    "review_type": "cross_page_paragraph",
                    "severity": "warning",
                    "document_id": "doc_test",
                    "page_number": 1,
                    "object_type": "block",
                    "object_id": "block_1",
                    "reason": "cross-page merge",
                    "suggested_action": "review",
                    "source_preview": "incorrect text",
                    "status": "pending"
                }
            ]
        },
    )


def test_structure_review_requires_all_decisions_and_preserves_audit(output_root: Path) -> None:
    run_dir = output_root / "run"
    _create_reviewable_run(run_dir)
    store = StructureReviewStore(run_dir)
    states = store.create_from_run()

    assert states[0]["status"] == "pending"
    with pytest.raises(ValueError, match="pending tasks"):
        store.finalize()

    store.record_decision(
        "review_1",
        "correct_text",
        reviewer="tester",
        note="compared with source",
        corrected_text="correct text",
    )
    manifest = store.finalize()
    document = json.loads(Path(manifest["parsed_document_path"]).read_text(encoding="utf-8"))
    block = document["blocks"][0]

    assert manifest["status"] == "completed"
    assert manifest["evaluation_status"] == "finalized"
    assert block["text"] == "correct text"
    assert block["quality_flags"] == []
    assert block["metadata"]["structure_review"]["original_text"] == "incorrect text"
    assert block["metadata"]["structure_review"]["reviewer"] == "tester"
