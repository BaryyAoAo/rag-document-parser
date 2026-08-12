from __future__ import annotations

from copy import deepcopy

from .models import Block


SENTENCE_ENDINGS = ("。", "！", "？", ".", "!", "?", "；", ";", "：", ":")


def sort_blocks_by_position(blocks: list[Block]) -> list[Block]:
    return sorted(
        blocks,
        key=lambda block: (
            block.page_start if block.page_start is not None else 10**9,
            block.bbox[1] if block.bbox else block.order,
            block.bbox[0] if block.bbox else 0,
            block.order,
        ),
    )


def repair_cross_page_paragraphs(blocks: list[Block]) -> list[Block]:
    repaired: list[Block] = []
    for block in blocks:
        current = deepcopy(block)
        if (
            repaired
            and current.block_type == "paragraph"
            and repaired[-1].block_type == "paragraph"
            and repaired[-1].page_end is not None
            and current.page_start is not None
            and current.page_start == repaired[-1].page_end + 1
            and repaired[-1].text
            and current.text
            and repaired[-1].bbox
            and current.bbox
            and _blocks_touch_page_boundary(repaired[-1], current)
            and not repaired[-1].text.endswith(SENTENCE_ENDINGS)
            and len(current.text) > 10
        ):
            repaired[-1].text = repaired[-1].text.rstrip() + current.text.lstrip()
            repaired[-1].page_end = current.page_end
            repaired[-1].quality_flags.append("merged_cross_page_paragraph")
            merged_ids = repaired[-1].metadata.setdefault("merged_block_ids", [])
            merged_ids.append(current.block_id)
            continue
        repaired.append(current)
    return repaired


def _blocks_touch_page_boundary(previous: Block, current: Block) -> bool:
    previous_page_height = float(previous.metadata.get("page_height", 0) or 0)
    current_page_height = float(current.metadata.get("page_height", 0) or 0)
    if previous_page_height <= 0 or current_page_height <= 0 or not previous.bbox or not current.bbox:
        return False
    previous_near_bottom = previous.bbox[3] >= previous_page_height * 0.70
    current_near_top = current.bbox[1] <= current_page_height * 0.25
    return previous_near_bottom and current_near_top


def repair_continued_tables(blocks: list[Block]) -> list[Block]:
    repaired: list[Block] = []
    for block in blocks:
        current = deepcopy(block)
        if (
            repaired
            and current.block_type == "table"
            and repaired[-1].block_type == "table"
            and current.table_data
            and repaired[-1].table_data
            and current.page_start is not None
            and repaired[-1].page_end is not None
            and current.page_start == repaired[-1].page_end + 1
            and current.table_data.headers
            and current.table_data.headers == repaired[-1].table_data.headers
        ):
            repaired[-1].table_data.rows.extend(current.table_data.rows)
            repaired[-1].page_end = current.page_end
            repaired[-1].metadata["is_continued_table"] = True
            repaired[-1].quality_flags.append("merged_continued_table")
            merged_ids = repaired[-1].metadata.setdefault("merged_block_ids", [])
            merged_ids.append(current.block_id)
            continue
        repaired.append(current)
    return repaired


def assign_heading_path(blocks: list[Block]) -> list[Block]:
    assigned: list[Block] = []
    heading_path: list[str] = []
    for block in blocks:
        current = deepcopy(block)
        if current.metadata.get("excluded_from_retrieval"):
            current.heading_path = heading_path.copy()
            assigned.append(current)
            continue
        if current.block_type == "heading" and current.text:
            level = int(current.metadata.get("heading_level", 1))
            heading_path = heading_path[: max(level - 1, 0)]
            heading_path.append(current.text)
            current.heading_path = heading_path.copy()
        elif not current.heading_path:
            current.heading_path = heading_path.copy()
        assigned.append(current)
    return assigned


def repair_blocks(blocks: list[Block]) -> list[Block]:
    repaired = sort_blocks_by_position(blocks)
    repaired = repair_cross_page_paragraphs(repaired)
    repaired = repair_continued_tables(repaired)
    repaired = assign_heading_path(repaired)
    return repaired
