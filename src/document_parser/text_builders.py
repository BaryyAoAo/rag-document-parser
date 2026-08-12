from __future__ import annotations

import re
from typing import Any

from .models import Block, TableData


def normalize_text(text: Any) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\u3000", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def compact_for_search(text: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(text))


def table_from_rows(rows: list[list[Any]], caption: str | None = None) -> TableData:
    cleaned = [[normalize_text(cell) for cell in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return TableData(caption=caption)
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    return TableData(caption=caption, headers=cleaned[0], rows=cleaned[1:])


def table_to_markdown(table: TableData) -> str:
    rows = []
    if table.headers:
        rows.append(table.headers)
    rows.extend(table.rows)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    separator = ["---"] * width
    body = rows[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def table_row_to_labeled_text(headers: list[str], row: list[str]) -> str:
    width = max(len(headers), len(row))
    headers = headers + [f"列{index + 1}" for index in range(len(headers), width)]
    row = row + [""] * (width - len(row))
    pairs = []
    for header, cell in zip(headers, row, strict=False):
        header = normalize_text(header)
        cell = normalize_text(cell)
        if header or cell:
            pairs.append(f"{header}: {cell}" if header else cell)
    return "；".join(pairs)


def block_main_text(block: Block) -> str:
    if block.block_type == "table" and block.table_data:
        return table_to_markdown(block.table_data)
    if block.block_type == "image" and block.image:
        return normalize_text(block.image.ocr_text or block.image.caption or "")
    return normalize_text(block.text)


def build_context_prefix(title: str, metadata: dict[str, Any], heading_path: list[str]) -> str:
    parts = []
    if title:
        parts.append(f"文档：{title}")
    city = metadata.get("city")
    category = metadata.get("category")
    year = metadata.get("year")
    if city:
        parts.append(f"城市：{city}")
    if category:
        parts.append(f"类别：{category}")
    if year:
        parts.append(f"年份：{year}")
    if heading_path:
        parts.append("章节：" + " > ".join(heading_path))
    return "。".join(parts)
