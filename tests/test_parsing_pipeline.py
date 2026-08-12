from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path

import pytest

from document_parser.pipeline import DocumentParsingPipeline
from document_parser.models import to_plain_data


CASES = [
    ("docx/demo_water_service_guide.docx", "docx", 9, "finalized"),
    ("html/demo_water_price_policy.html", "html", 7, "finalized"),
    ("markdown/demo_water_faq.md", "markdown", 9, "finalized"),
    ("pdf/demo_water_report.pdf", "pdf", 129, "review_required"),
    ("pptx/demo_water_operations.pptx", "pptx", 7, "review_required"),
    ("xlsx/demo_water_statistics.xlsx", "xlsx", 2, "finalized"),
]


@pytest.mark.parametrize("relative_path,file_type,block_count,status", CASES)
def test_six_formats_produce_observable_parsed_documents(
    project_root: Path,
    output_root: Path,
    relative_path: str,
    file_type: str,
    block_count: int,
    status: str,
) -> None:
    result = DocumentParsingPipeline(output_root).parse(project_root / "fixtures" / relative_path)

    assert result.parsed_document.file_type == file_type
    assert len(result.parsed_document.blocks) == block_count
    assert result.status == status
    run_dir = Path(result.run_dir)
    assert (run_dir / "02_raw_parser_output.json").is_file()
    assert (run_dir / "03_blocks_before_clean.json").is_file()
    assert (run_dir / "04_blocks_after_clean.json").is_file()
    assert (run_dir / "05_blocks_after_repair.json").is_file()
    assert (run_dir / "06_parsed_document.json").is_file()
    assert (run_dir / "07_parsing_evaluation.json").is_file()
    assert not list(run_dir.glob("*chunk*"))
    assert not list(run_dir.glob("*ingest*"))


def test_pdf_matches_extraction_baseline(project_root: Path, output_root: Path) -> None:
    result = DocumentParsingPipeline(output_root).parse(
        project_root / "fixtures" / "pdf" / "demo_water_report.pdf"
    )
    baseline = json.loads(
        (project_root / "migration" / "baseline_manifest.json").read_text(encoding="utf-8")
    )
    fixture = baseline["pdf_fixture"]
    assert len(result.parsed_document.blocks) == fixture["block_count"]
    fields = fixture["semantic_block_fields"]
    canonical = [
        {
            **{field: to_plain_data(getattr(block, field)) for field in fields},
            "image": _semantic_image(block.image),
        }
        for block in result.parsed_document.blocks
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert digest == fixture["semantic_blocks_sha256"]

    for block in result.parsed_document.blocks:
        if block.bbox is None:
            continue
        assert len(block.bbox) == 4
        assert all(math.isfinite(value) for value in block.bbox)
        x0, top, x1, bottom = block.bbox
        assert x0 <= x1
        assert top <= bottom


def _semantic_image(image: object) -> dict[str, object] | None:
    if image is None:
        return None
    return {
        "image_id": image.image_id,
        "caption": image.caption,
        "ocr_text": image.ocr_text,
    }
