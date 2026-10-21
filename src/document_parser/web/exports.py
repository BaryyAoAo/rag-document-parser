"""Human-readable export formats derived from the effective ParsedDocument."""

from __future__ import annotations

from ..models import ParsedDocument
from ..text_builders import block_main_text


def document_markdown(document: ParsedDocument) -> str:
    parts: list[str] = []
    if document.title:
        parts.append(f"# {document.title}")
    for block in sorted(document.blocks, key=lambda item: (item.order, item.page_start or 0)):
        if block.metadata.get("excluded_from_retrieval"):
            continue
        content = block_main_text(block)
        if not content:
            continue
        if block.block_type == "heading":
            level = max(2, min(int(block.metadata.get("heading_level") or 2), 6))
            parts.append(f"{'#' * level} {content}")
        elif block.block_type == "list_item":
            parts.append(f"- {content}")
        elif block.block_type == "image":
            parts.append(f"[图片：{content}]")
        else:
            parts.append(content)
    return "\n\n".join(parts).rstrip() + "\n"
