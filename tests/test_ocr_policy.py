from document_parser.ocr_policy import decide_pdf_page_ocr


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
