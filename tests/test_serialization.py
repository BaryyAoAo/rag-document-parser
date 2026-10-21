from __future__ import annotations

import json
from pathlib import Path

import pytest

from document_parser import (
    ParsedDocumentValidationError,
    load_parsed_document,
    load_resolved_parsed_document,
)
from document_parser.io_utils import write_json
from document_parser.amendments import amend_final_document
from document_parser.artifacts import resolve_parsing_artifacts


def _document() -> dict:
    return {
        "document_id": "doc_test",
        "title": "测试文件",
        "file_type": "pdf",
        "source_path": "test.pdf",
        "source_url": "",
        "metadata": {"schema_version": "1.0", "parser_version": "1.2"},
        "blocks": [
            {
                "block_id": "block_1",
                "document_id": "doc_test",
                "block_type": "paragraph",
                "text": "有效正文",
                "table_data": None,
                "image": None,
                "page_start": 1,
                "page_end": 1,
                "bbox": [1, 2, 3, 4],
                "heading_path": ["第一条"],
                "metadata": {},
                "source_trace": {
                    "parser": "fixture",
                    "raw_object_type": "text",
                    "raw_object_index": 0,
                    "raw_page_index": 0,
                    "raw_sheet_name": None,
                    "raw_slide_index": None,
                    "raw_locator": None,
                },
                "quality_flags": [],
                "order": 1,
            }
        ],
        "quality_report": {
            "document_id": "doc_test",
            "summary": {"block_count": 1},
            "warnings": [],
        },
        "parsed_at": "2026-01-01T00:00:00+00:00",
    }


def test_load_parsed_document_builds_public_models(output_root: Path) -> None:
    path = output_root / "document.json"
    write_json(path, _document())

    document = load_parsed_document(path)

    assert document.document_id == "doc_test"
    assert document.blocks[0].source_trace is not None
    assert document.blocks[0].bbox == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["metadata"].update(schema_version="2.0"), "schema_version"),
        (lambda value: value["blocks"][0].update(document_id="other"), "parent document_id"),
        (lambda value: value["blocks"][0].update(bbox=[1, 2]), "list of 4 numbers"),
        (lambda value: value["blocks"].append(dict(value["blocks"][0])), "duplicate block_id"),
    ],
    ids=["schema-version", "document-id", "bbox", "duplicate-block-id"],
)
def test_load_parsed_document_rejects_invalid_contract(
    output_root: Path, mutate, message: str
) -> None:
    value = _document()
    mutate(value)
    path = output_root / "invalid.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ParsedDocumentValidationError, match=message):
        load_parsed_document(path)


def test_resolved_loader_rejects_pending_base_review(output_root: Path) -> None:
    write_json(output_root / "06_parsed_document.json", _document())
    write_json(
        output_root / "07_parsing_evaluation.json",
        {
            "document_id": "doc_test",
            "status": "review_required",
            "error_count": 0,
            "manual_review_items": [
                {"review_id": "review_1", "status": "pending"}
            ],
        },
    )

    with pytest.raises(ParsedDocumentValidationError, match="pending review items"):
        load_resolved_parsed_document(output_root)


def test_resolved_loader_accepts_final_revision_with_only_filtered_warnings(
    output_root: Path,
) -> None:
    final_dir = output_root / "ocr" / "finalized" / "final_1"
    document_path = final_dir / "01_finalized_parsed_document.json"
    evaluation_path = final_dir / "02_finalized_evaluation.json"
    write_json(document_path, _document())
    write_json(
        evaluation_path,
        {
            "document_id": "doc_test",
            "status": "review_required",
            "error_count": 0,
            "manual_review_items": [],
            "quality_flags": {"excluded_layout_noise": 1},
        },
    )
    write_json(
        output_root / "ocr" / "finalized" / "00_finalization_status.json",
        {
            "status": "completed",
            "finalization_id": "final_1",
            "parsed_document_path": str(document_path),
            "evaluation_path": str(evaluation_path),
        },
    )

    document = load_resolved_parsed_document(output_root)

    assert document.document_id == "doc_test"


def test_manual_amendment_keeps_previous_revision_and_adds_verified_text(output_root: Path) -> None:
    original = _document()
    original["blocks"][0]["quality_flags"] = ["excluded_layout_noise"]
    original["blocks"][0]["metadata"]["excluded_from_retrieval"] = True
    final_dir = output_root / "ocr" / "finalized" / "final_1"
    document_path = final_dir / "01_finalized_parsed_document.json"
    evaluation_path = final_dir / "02_finalized_evaluation.json"
    write_json(document_path, original)
    write_json(evaluation_path, {"document_id": "doc_test", "status": "review_required",
                                  "error_count": 0, "manual_review_items": []})
    write_json(output_root / "ocr" / "finalized" / "00_finalization_status.json", {
        "status": "completed", "parsed_document_path": str(document_path),
        "evaluation_path": str(evaluation_path),
    })
    status = amend_final_document(
        output_root, reviewer="tester", reason="页脚有效文字误排除", evidence="源 PDF 第 4 页已核对",
        include_block_ids=["block_1"],
        add_text_blocks=[{"page": 4, "text": "2025年12月29日印发"}],
    )
    revised = load_resolved_parsed_document(output_root)
    assert [block.text for block in revised.blocks] == ["有效正文", "2025年12月29日印发"]
    assert not revised.blocks[0].metadata["excluded_from_retrieval"]
    assert "excluded_layout_noise" not in revised.blocks[0].quality_flags
    assert revised.blocks[1].source_trace.parser == "manual_verified_from_source"
    assert status["previous_document_path"] == str(document_path)
    assert json.loads(document_path.read_text(encoding="utf-8")) == original
    next_review_path = output_root / "ocr" / "finalized" / "final_2" / "01_finalized_parsed_document.json"
    write_json(next_review_path, original)
    write_json(output_root / "ocr" / "finalized" / "00_finalization_status.json", {
        "status": "completed", "parsed_document_path": str(next_review_path),
        "evaluation_path": str(evaluation_path),
    })
    assert resolve_parsing_artifacts(output_root).source == "ocr_review"
