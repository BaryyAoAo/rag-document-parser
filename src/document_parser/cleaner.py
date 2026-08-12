from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
import re

from .models import Block
from .text_builders import normalize_text


NOISE_TEXTS = {
    "首页",
    "政务公开",
    "互动交流",
    "网站地图",
    "友情链接",
    "版权声明",
}


def clean_blocks(blocks: list[Block]) -> list[Block]:
    cleaned: list[Block] = []
    seen_short_text: set[str] = set()
    for block in blocks:
        new_block = deepcopy(block)
        if new_block.text is not None:
            new_block.text = normalize_text(new_block.text)
        if new_block.table_data:
            new_block.table_data.caption = normalize_text(new_block.table_data.caption)
            new_block.table_data.headers = [normalize_text(cell) for cell in new_block.table_data.headers]
            new_block.table_data.rows = [
                [normalize_text(cell) for cell in row] for row in new_block.table_data.rows
            ]
            new_block.table_data.notes = [normalize_text(note) for note in new_block.table_data.notes]

        visible_text = new_block.text or ""
        if new_block.block_type in {"paragraph", "heading", "list_item"}:
            if not visible_text or visible_text in NOISE_TEXTS:
                continue
            if len(visible_text) <= 40 and visible_text in seen_short_text:
                new_block.quality_flags.append("duplicate_short_text_removed")
                continue
            seen_short_text.add(visible_text)

        cleaned.append(new_block)
    _mark_table_of_contents(cleaned)
    return cleaned


TOC_ENTRY_PATTERN = re.compile(r"\.{4,}\s*\d+\s*$")


def _mark_table_of_contents(blocks: list[Block]) -> None:
    """Keep TOC blocks for audit, but exclude them from retrieval chunking."""
    page_blocks: dict[int, list[Block]] = defaultdict(list)
    for block in blocks:
        if block.page_start is not None:
            page_blocks[block.page_start].append(block)

    for page_number, candidates in page_blocks.items():
        text_blocks = [
            block
            for block in candidates
            if block.block_type in {"heading", "paragraph", "list_item"} and block.text
        ]
        toc_entries = [block for block in text_blocks if TOC_ENTRY_PATTERN.search(block.text or "")]
        if len(toc_entries) < 3 or len(toc_entries) / max(len(text_blocks), 1) < 0.5:
            continue
        for block in candidates:
            block.metadata["semantic_role"] = "table_of_contents"
            block.metadata["excluded_from_retrieval"] = True
            block.metadata["exclusion_reason"] = "table_of_contents_page"
            if "table_of_contents" not in block.quality_flags:
                block.quality_flags.append("table_of_contents")
