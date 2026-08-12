from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


OCR_POLICY_VERSION = "2.0"
OCR_CANDIDATE_DECISIONS = {
    "auto_ocr",
    "review_before_ocr",
    # Backward compatibility for historical outputs.
    "auto_ocr_candidate",
    "manual_review",
}


@dataclass(frozen=True)
class PdfOcrPolicy:
    scanned_max_text_chars: int = 30
    scanned_max_word_count: int = 6
    scanned_min_image_ratio: float = 0.60
    mixed_max_text_chars: int = 100
    mixed_min_image_ratio: float = 0.20
    garbled_ratio_auto_ocr: float = 0.08
    cid_count_auto_ocr: int = 3
    table_empty_ratio_review: float = 0.45


def decide_pdf_page_ocr(
    page_quality: dict[str, Any],
    policy: PdfOcrPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or PdfOcrPolicy()
    text_length = int(page_quality.get("text_length", 0) or 0)
    word_count = int(page_quality.get("word_count", 0) or 0)
    max_image_area_ratio = float(page_quality.get("max_image_area_ratio", 0.0) or 0.0)
    image_count = int(page_quality.get("image_count", 0) or 0)
    garbled_char_ratio = float(page_quality.get("garbled_char_ratio", 0.0) or 0.0)
    cid_count = int(page_quality.get("cid_marker_count", 0) or 0)
    table_count = int(page_quality.get("table_count", 0) or 0)
    table_empty_ratio = float(page_quality.get("table_empty_cell_ratio", 0.0) or 0.0)

    common = {
        "policy_version": OCR_POLICY_VERSION,
        "scope": "page",
        "policy": asdict(policy),
    }
    if text_length == 0 and image_count == 0 and table_count == 0:
        return {
            **common,
            "decision": "skip_ocr",
            "status": "not_required",
            "confidence": 0.98,
            "reason_codes": ["BLANK_PAGE"],
            "reason": "页面没有原生文字、图片或表格，按空白页处理。",
        }
    if cid_count >= policy.cid_count_auto_ocr or garbled_char_ratio >= policy.garbled_ratio_auto_ocr:
        return {
            **common,
            "decision": "auto_ocr",
            "status": "pending_ocr",
            "confidence": 0.95,
            "reason_codes": ["NATIVE_TEXT_GARBLED"],
            "reason": "原生文字层存在大量 CID、替换字符或乱码，自动 OCR 后再进行结果门禁。",
        }
    if (
        text_length <= policy.scanned_max_text_chars
        and word_count <= policy.scanned_max_word_count
        and max_image_area_ratio >= policy.scanned_min_image_ratio
    ):
        return {
            **common,
            "decision": "auto_ocr",
            "status": "pending_ocr",
            "confidence": 0.96,
            "reason_codes": ["SCANNED_PAGE"],
            "reason": "页面几乎没有原生文字，同时存在覆盖大部分页面的图片，符合扫描页特征。",
        }
    if table_count and table_empty_ratio >= policy.table_empty_ratio_review:
        return {
            **common,
            "decision": "review_before_ocr",
            "status": "pending_review",
            "confidence": 0.75,
            "reason_codes": ["NATIVE_TABLE_LOW_QUALITY"],
            "reason": "原生表格存在较高空单元格比例，需要人工判断是否使用 OCR 修复结构。",
        }
    if (
        text_length < policy.mixed_max_text_chars
        and image_count > 0
        and max_image_area_ratio >= policy.mixed_min_image_ratio
    ):
        return {
            **common,
            "decision": "review_before_ocr",
            "status": "pending_review",
            "confidence": 0.70,
            "reason_codes": ["MIXED_PAGE_LOW_TEXT"],
            "reason": "原生文字较少且存在较大图片，需确认图片是否包含业务文字、表格或流程图。",
        }
    return {
        **common,
        "decision": "skip_ocr",
        "status": "not_required",
        "confidence": 0.92,
        "reason_codes": ["NATIVE_PARSE_USABLE"],
        "reason": "原生文字和结构可用；页面中的图片不足以证明需要 OCR。",
    }
