from __future__ import annotations

from collections import Counter
from typing import Any

from .models import Block, QualityReport, QualityWarning
from .ocr_policy import OCR_CANDIDATE_DECISIONS


def build_quality_report(
    document_id: str,
    raw_outputs: list[dict[str, Any]],
    blocks: list[Block],
    parser_warnings: list[QualityWarning] | None = None,
) -> QualityReport:
    block_counts = Counter(block.block_type for block in blocks)
    page_numbers = {
        block.page_start for block in blocks if block.page_start is not None
    }
    warnings = list(parser_warnings or [])

    aggregated_flags = {
        "table_of_contents",
        "manual_review",
        "auto_ocr_candidate",
        "review_before_ocr",
        "auto_ocr",
    }
    aggregated_seen: set[tuple[str, int | None]] = set()
    for block in blocks:
        for flag in block.quality_flags:
            if flag in aggregated_flags:
                aggregate_key = (flag, block.page_start)
                if aggregate_key in aggregated_seen:
                    continue
                aggregated_seen.add(aggregate_key)
                warnings.append(
                    QualityWarning(
                        level="warning",
                        warning_type=flag,
                        message=f"Page {block.page_start} contains blocks with quality flag: {flag}",
                        page_number=block.page_start,
                        details={"scope": "page"},
                    )
                )
                continue
            warnings.append(
                QualityWarning(
                    level="warning",
                    warning_type=flag,
                    message=f"Block {block.block_id} has quality flag: {flag}",
                    page_number=block.page_start,
                    block_id=block.block_id,
                )
            )

    deduplicated: list[QualityWarning] = []
    seen_warning_keys: set[tuple[str, int | None, str | None]] = set()
    for warning in warnings:
        key = (warning.warning_type, warning.page_number, warning.block_id)
        if key in seen_warning_keys:
            continue
        seen_warning_keys.add(key)
        deduplicated.append(warning)

    summary = {
        "raw_output_count": len(raw_outputs),
        "page_count": max(page_numbers) if page_numbers else None,
        "block_count": len(blocks),
        "block_counts": dict(block_counts),
        "table_count": block_counts.get("table", 0),
        "image_count": block_counts.get("image", 0),
        "ocr_candidate_count": sum(
            1
            for block in blocks
            if block.metadata.get("ocr_decision") in OCR_CANDIDATE_DECISIONS
        ),
        "excluded_from_retrieval_count": sum(
            1 for block in blocks if block.metadata.get("excluded_from_retrieval")
        ),
        "warning_count": len(deduplicated),
    }
    return QualityReport(document_id=document_id, summary=summary, warnings=deduplicated)
