from __future__ import annotations

from typing import Any

from ..models import Block, ImageData, SourceTrace, TableData


HEADING_TYPES = {"doc_title", "paragraph_title", "title"}
PARAGRAPH_TYPES = {
    "abstract",
    "algorithm",
    "aside_text",
    "display_formula",
    "footnote",
    "formula_number",
    "inline_formula",
    "reference",
    "reference_content",
    "text",
    "vertical_text",
}
EXCLUDED_TYPES = {"content", "footer", "footer_image", "header", "header_image", "number"}


def convert_parse_result_to_blocks(
    parse_result: dict[str, Any],
    document_id: str,
) -> list[Block]:
    blocks: list[Block] = []
    order = 0
    for page in parse_result.get("pages", []):
        page_number = int(page.get("page_num", 0)) + 1
        layouts = page.get("layouts", [])
        table_lookup = {
            str(item.get("layout_id")): item for item in page.get("tables", [])
        }
        image_lookup = {
            str(item.get("layout_id")): item for item in page.get("images", [])
        }
        associations = _build_layout_associations(layouts)
        for layout in layouts:
            layout_id = str(layout.get("layout_id", ""))
            layout_type = str(layout.get("type", "text"))
            association = associations.get(layout_id, {})
            order += 1
            common = {
                "block_id": f"{document_id}_p{page_number:04d}_ocr_{order:04d}",
                "document_id": document_id,
                "page_start": page_number,
                "page_end": page_number,
                "bbox": _xywh_to_xyxy(layout.get("position")),
                "metadata": {
                    "source_type": "paddleocr_vl",
                    "ocr_layout_id": layout_id,
                    "ocr_layout_type": layout_type,
                    "ocr_sub_type": layout.get("sub_type") or None,
                    "polygon": layout.get("polygon") or [],
                    "span_boxes": layout.get("span_boxes") or [],
                    "page_width": (page.get("meta") or {}).get("page_width"),
                    "page_height": (page.get("meta") or {}).get("page_height"),
                    "excluded_from_retrieval": layout_type in EXCLUDED_TYPES,
                    "context_for_layout_id": association.get("target_layout_id"),
                },
                "source_trace": SourceTrace(
                    parser="baidu_paddleocr_vl_1_6",
                    raw_object_type=layout_type,
                    raw_object_index=order - 1,
                    raw_page_index=page_number - 1,
                    raw_locator=layout_id,
                ),
                "order": order,
            }
            if layout_type == "table":
                table = table_lookup.get(layout_id, {})
                context = associations.get(layout_id, {})
                blocks.append(
                    Block(
                        **common,
                        block_type="table",
                        table_data=_table_data(
                            table,
                            caption=context.get("caption"),
                            notes=context.get("notes") or [],
                        ),
                    )
                )
            elif layout_type in {"image", "chart", "seal", "footer_image", "header_image"}:
                image = image_lookup.get(layout_id, {})
                context = associations.get(layout_id, {})
                size = _xywh_size(layout.get("position"))
                blocks.append(
                    Block(
                        **common,
                        block_type="image",
                        image=ImageData(
                            image_id=layout_id or common["block_id"],
                            caption=context.get("caption") or _clean_text(layout.get("text")) or None,
                            ocr_text=_image_description(image),
                            image_path=image.get("data_url"),
                            width=size[0] if size else None,
                            height=size[1] if size else None,
                        ),
                    )
                )
            else:
                text = _clean_text(layout.get("text"))
                if not text:
                    order -= 1
                    continue
                blocks.append(
                    Block(
                        **common,
                        block_type=_text_block_type(layout_type, text, association),
                        text=text,
                        quality_flags=["excluded_layout_noise"] if layout_type in EXCLUDED_TYPES else [],
                    )
                )
    return blocks


def _build_layout_associations(layouts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Associate captions and notes with their neighboring table or figure layout."""
    associations: dict[str, dict[str, Any]] = {}

    def layout_id_at(index: int) -> str:
        return str(layouts[index].get("layout_id", ""))

    for index, layout in enumerate(layouts):
        layout_id = layout_id_at(index)
        layout_type = str(layout.get("type", "text"))
        text = _clean_text(layout.get("text"))

        if layout_type == "figure_title" and text.startswith("表"):
            target_index = _find_neighbor_layout(
                layouts, index, direction=1, accepted_types={"table"}, max_distance=3
            )
            if target_index is not None:
                target_id = layout_id_at(target_index)
                associations[layout_id] = {
                    "role": "table_caption",
                    "target_layout_id": target_id,
                }
                associations.setdefault(target_id, {})["caption"] = text

        elif layout_type == "figure_title" and text.startswith("图"):
            target_index = _find_neighbor_layout(
                layouts,
                index,
                direction=-1,
                accepted_types={"image", "chart"},
                max_distance=3,
            )
            if target_index is not None:
                target_id = layout_id_at(target_index)
                associations[layout_id] = {
                    "role": "figure_caption",
                    "target_layout_id": target_id,
                }
                associations.setdefault(target_id, {})["caption"] = text

        elif layout_type == "vision_footnote":
            target_index = _find_neighbor_layout(
                layouts, index, direction=1, accepted_types={"table"}, max_distance=2
            )
            if target_index is None:
                target_index = _find_neighbor_layout(
                    layouts, index, direction=-1, accepted_types={"table"}, max_distance=2
                )
            if target_index is not None:
                target_id = layout_id_at(target_index)
                associations[layout_id] = {
                    "role": "table_note",
                    "target_layout_id": target_id,
                }
                associations.setdefault(target_id, {}).setdefault("notes", []).append(text)

    return associations


def _find_neighbor_layout(
    layouts: list[dict[str, Any]],
    start_index: int,
    direction: int,
    accepted_types: set[str],
    max_distance: int,
) -> int | None:
    for distance in range(1, max_distance + 1):
        index = start_index + direction * distance
        if index < 0 or index >= len(layouts):
            break
        if str(layouts[index].get("type", "")) in accepted_types:
            return index
    return None


def _text_block_type(
    layout_type: str,
    text: str,
    association: dict[str, Any],
) -> str:
    role = association.get("role")
    if role in {"table_caption", "table_note", "figure_caption"}:
        return str(role)
    if layout_type == "figure_title":
        return "table_caption" if text.startswith("表") else "figure_caption"
    if layout_type == "vision_footnote" and text.startswith(("单位", "注")):
        return "table_note"
    return "heading" if layout_type in HEADING_TYPES else "paragraph"


def _xywh_to_xyxy(position: Any) -> list[float] | None:
    if not isinstance(position, list) or len(position) != 4:
        return None
    x, y, width, height = (float(value) for value in position)
    return [x, y, x + width, y + height]


def _xywh_size(position: Any) -> tuple[float, float] | None:
    if not isinstance(position, list) or len(position) != 4:
        return None
    return float(position[2]), float(position[3])


def _table_data(
    table: dict[str, Any],
    caption: str | None = None,
    notes: list[str] | None = None,
) -> TableData:
    cells = table.get("cells") or []
    matrix = table.get("matrix") or []
    rows: list[list[str]] = []
    for matrix_row in matrix:
        row: list[str] = []
        for cell_index in matrix_row:
            try:
                cell = cells[int(cell_index)]
            except (IndexError, TypeError, ValueError):
                row.append("")
                continue
            row.append(_clean_text(cell.get("text")))
        rows.append(row)
    if rows:
        return TableData(caption=caption, headers=rows[0], rows=rows[1:], notes=notes or [])
    markdown = _clean_text(table.get("markdown"))
    fallback_notes = list(notes or [])
    if markdown:
        fallback_notes.append(markdown)
    return TableData(caption=caption, notes=fallback_notes)


def _image_description(image: dict[str, Any]) -> str | None:
    description = image.get("image_description")
    if isinstance(description, str):
        return description.strip() or None
    return None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())
