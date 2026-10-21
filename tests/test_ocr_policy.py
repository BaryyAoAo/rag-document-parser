from document_parser.ocr_policy import decide_pdf_page_ocr
from document_parser.ocr.baidu_paddleocr import convert_parse_result_to_blocks


def test_document_footer_text_is_retained_for_retrieval() -> None:
    blocks = convert_parse_result_to_blocks(
        {"pages": [{"page_num": 3, "layouts": [
            {"layout_id": "footer-office", "type": "footer", "text": "嵊州市发展和改革局办公室", "position": [10, 10, 180, 20]},
            {"layout_id": "footer-date", "type": "footer", "text": "2025年12月29日印发", "position": [200, 10, 160, 20]},
            {"layout_id": "page-number", "type": "number", "text": "4", "position": [400, 10, 10, 20]},
        ]}]}, "doc_footer_test"
    )
    assert [block.text for block in blocks] == ["嵊州市发展和改革局办公室", "2025年12月29日印发", "4"]
    assert all(not block.metadata["excluded_from_retrieval"] for block in blocks[:2])
    assert all("excluded_layout_noise" not in block.quality_flags for block in blocks[:2])
    assert blocks[2].metadata["excluded_from_retrieval"]


def test_blank_page_skips_ocr() -> None:
    result = decide_pdf_page_ocr({})
    assert result["decision"] == "skip_ocr"
    assert result["reason_codes"] == ["BLANK_PAGE"]


def test_scanned_page_uses_automatic_ocr() -> None:
    result = decide_pdf_page_ocr(
        {"text_length": 2, "word_count": 1, "image_count": 1, "max_image_area_ratio": 0.9}
    )
    assert result["decision"] == "auto_ocr"
    assert result["reason_codes"] == ["SCANNED_PAGE"]


def test_mixed_page_requires_review_before_ocr() -> None:
    result = decide_pdf_page_ocr(
        {"text_length": 50, "word_count": 20, "image_count": 1, "max_image_area_ratio": 0.4}
    )
    assert result["decision"] == "review_before_ocr"
    assert result["reason_codes"] == ["MIXED_PAGE_LOW_TEXT"]
