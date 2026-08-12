from __future__ import annotations

from document_parser.models import Block, TableData
from document_parser.repair import repair_continued_tables, repair_cross_page_paragraphs


def _paragraph(block_id: str, page: int, text: str, bbox: list[float]) -> Block:
    return Block(
        block_id=block_id,
        document_id="doc_test",
        block_type="paragraph",
        text=text,
        page_start=page,
        page_end=page,
        bbox=bbox,
        metadata={"page_height": 1000},
    )


def test_cross_page_paragraph_is_merged_only_at_page_boundary() -> None:
    blocks = [
        _paragraph("b1", 1, "This sentence continues", [50, 800, 500, 950]),
        _paragraph("b2", 2, "on the following page with enough text.", [50, 30, 500, 100]),
    ]
    repaired = repair_cross_page_paragraphs(blocks)

    assert len(repaired) == 1
    assert repaired[0].page_end == 2
    assert repaired[0].text == "This sentence continueson the following page with enough text."
    assert repaired[0].metadata["merged_block_ids"] == ["b2"]
    assert "merged_cross_page_paragraph" in repaired[0].quality_flags


def test_completed_chinese_sentence_is_not_merged_across_pages() -> None:
    blocks = [
        _paragraph("b1", 1, "本段已经结束。", [50, 800, 500, 950]),
        _paragraph("b2", 2, "这是下一页开始的一段完整正文。", [50, 30, 500, 100]),
    ]
    assert len(repair_cross_page_paragraphs(blocks)) == 2


def test_consecutive_tables_with_identical_headers_are_merged() -> None:
    first = Block(
        block_id="t1",
        document_id="doc_test",
        block_type="table",
        table_data=TableData(headers=["name", "value"], rows=[["a", "1"]]),
        page_start=1,
        page_end=1,
    )
    second = Block(
        block_id="t2",
        document_id="doc_test",
        block_type="table",
        table_data=TableData(headers=["name", "value"], rows=[["b", "2"]]),
        page_start=2,
        page_end=2,
    )
    repaired = repair_continued_tables([first, second])

    assert len(repaired) == 1
    assert repaired[0].table_data is not None
    assert repaired[0].table_data.rows == [["a", "1"], ["b", "2"]]
    assert repaired[0].metadata["merged_block_ids"] == ["t2"]
    assert "merged_continued_table" in repaired[0].quality_flags
