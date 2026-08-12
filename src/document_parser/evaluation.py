from __future__ import annotations

from collections import Counter
import hashlib

from .models import Block, ManualReviewItem, ParsedDocument, ParsingEvaluation
from .versions import PARSER_VERSION, SCHEMA_VERSION


REVIEW_FLAGS = {
    "merged_cross_page_paragraph": "cross_page_paragraph",
    "merged_continued_table": "continued_table",
    "manual_review": "parser_quality",
}


def evaluate_parsed_document(
    document: ParsedDocument,
    additional_reviews: list[ManualReviewItem] | None = None,
) -> ParsingEvaluation:
    by_type = Counter(block.block_type for block in document.blocks)
    flags = Counter(flag for block in document.blocks for flag in block.quality_flags)
    reviews = _block_reviews(document)
    existing = {item.review_id for item in reviews}
    reviews.extend(
        item for item in additional_reviews or [] if item.review_id not in existing
    )
    warnings = list(document.quality_report.warnings if document.quality_report else [])
    error_count = sum(warning.level == "error" for warning in warnings)
    warning_count = sum(warning.level == "warning" for warning in warnings)
    if error_count:
        status = "failed"
    elif reviews or warning_count:
        status = "review_required"
    else:
        status = "finalized"
    return ParsingEvaluation(
        document_id=document.document_id,
        status=status,
        parser_version=PARSER_VERSION,
        schema_version=SCHEMA_VERSION,
        block_count=len(document.blocks),
        blocks_by_type=dict(sorted(by_type.items())),
        warning_count=warning_count,
        error_count=error_count,
        manual_review_items=reviews,
        quality_flags=dict(sorted(flags.items())),
    )


def _block_reviews(document: ParsedDocument) -> list[ManualReviewItem]:
    reviews: list[ManualReviewItem] = []
    for block in document.blocks:
        for flag in block.quality_flags:
            review_type = REVIEW_FLAGS.get(flag)
            if not review_type:
                continue
            seed = f"{document.document_id}|{block.block_id}|{flag}"
            reviews.append(
                ManualReviewItem(
                    review_id="review_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12],
                    review_type=review_type,
                    severity="warning",
                    document_id=document.document_id,
                    page_number=block.page_start,
                    object_type="block",
                    object_id=block.block_id,
                    reason=_reason(flag),
                    suggested_action="Review the source and approve, correct, exclude, or reject.",
                    source_preview=_preview(block),
                )
            )
    return reviews


def _reason(flag: str) -> str:
    return {
        "merged_cross_page_paragraph": "A paragraph was merged across consecutive pages.",
        "merged_continued_table": "Tables on consecutive pages were merged.",
        "review_before_ocr": "The native content may require OCR review.",
        "auto_ocr_candidate": "The content was identified as an automatic OCR candidate.",
        "auto_ocr": "The content was identified as an automatic OCR candidate.",
        "manual_review": "The parser marked this block for manual review.",
    }.get(flag, flag)


def _preview(block: Block) -> str:
    if block.text:
        return " ".join(block.text.split())[:240]
    if block.table_data:
        return " | ".join(block.table_data.headers)[:240]
    if block.image:
        return str(block.image.caption or block.image.ocr_text or "[image]")[:240]
    return ""
