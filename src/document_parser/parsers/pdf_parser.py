from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import pdfplumber

from ..models import Block, ImageData, ParserResult, QualityWarning, SourceFile, SourceTrace
from ..ocr_policy import decide_pdf_page_ocr
from ..ocr_policy import OCR_CANDIDATE_DECISIONS
from ..quality import build_quality_report
from ..text_builders import normalize_text, table_from_rows
from .base import BaseParser


class PdfParser(BaseParser):
    parser_name = "pdfplumber"
    file_type = "pdf"

    def parse(self, source: SourceFile) -> ParserResult:
        path = Path(source.file_path)
        document_id = self.build_document_id(path)
        metadata = self.base_metadata(source, path)
        raw_outputs: list[dict[str, Any]] = []
        blocks: list[Block] = []
        warnings: list[QualityWarning] = []
        title = path.stem
        order = 0

        with pdfplumber.open(path) as pdf:
            metadata["page_count"] = len(pdf.pages)
            if pdf.metadata and pdf.metadata.get("Title"):
                title = normalize_text(pdf.metadata.get("Title")) or title

            for page_index, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                words = page.extract_words() or []
                tables = page.find_tables() or []
                images = page.images or []
                table_bboxes = [table.bbox for table in tables if table.bbox]
                text_lines, excluded_word_count = self._build_text_lines_from_words(page, words, table_bboxes)
                image_groups = self._group_images(images)
                page_quality = self._inspect_page(page, text, words, tables, image_groups)
                ocr_decision = decide_pdf_page_ocr(page_quality)

                raw_outputs.append(
                    {
                        "page_number": page_index,
                        "width": page.width,
                        "height": page.height,
                        "text": text,
                        "word_count": len(words),
                        "excluded_table_word_count": excluded_word_count,
                        "words_sample": words[:80],
                        "bbox_text_lines_sample": text_lines[:30],
                        "tables": [self._raw_table_summary(table) for table in tables],
                        "images": [self._raw_image_summary(image) for image in images],
                        "grouped_images": image_groups,
                        "page_quality": page_quality,
                        "ocr_decision": ocr_decision,
                    }
                )

                text_blocks, order = self._build_text_blocks_from_lines(
                    document_id=document_id,
                    page_index=page_index,
                    page_height=float(page.height),
                    text_lines=text_lines,
                    start_order=order,
                )
                blocks.extend(text_blocks)

                for table_index, table in enumerate(tables, start=1):
                    rows = table.extract() or []
                    caption, notes = self._find_table_context(text_lines, table.bbox)
                    table_data = table_from_rows(rows, caption=caption)
                    table_data.notes.extend(notes)
                    if not table_data.headers and not table_data.rows:
                        continue
                    order += 1
                    blocks.append(
                        Block(
                            block_id=f"{document_id}_p{page_index:04d}_tbl_{table_index:04d}",
                            document_id=document_id,
                            block_type="table",
                            table_data=table_data,
                            page_start=page_index,
                            page_end=page_index,
                            bbox=[float(item) for item in table.bbox] if table.bbox else None,
                            metadata={
                                "source_type": "pdf",
                                "page_height": float(page.height),
                                "table_id": f"tbl_p{page_index:04d}_{table_index:04d}",
                                "row_count": len(table_data.rows),
                                "column_count": len(table_data.headers),
                            },
                            source_trace=SourceTrace(
                                parser=self.parser_name,
                                raw_page_index=page_index - 1,
                                raw_object_type="table",
                                raw_object_index=table_index - 1,
                            ),
                            order=order,
                        )
                    )

                for image_index, image in enumerate(image_groups, start=1):
                    order += 1
                    image_id = f"img_p{page_index:04d}_{image_index:04d}"
                    image_area = float(image.get("width", 0) or 0) * float(image.get("height", 0) or 0)
                    page_area = float(page.width * page.height) if page.width and page.height else 1.0
                    area_ratio = image_area / page_area
                    blocks.append(
                        Block(
                            block_id=f"{document_id}_p{page_index:04d}_{image_id}",
                            document_id=document_id,
                            block_type="image",
                            image=ImageData(
                                image_id=image_id,
                                width=float(image.get("width", 0) or 0),
                                height=float(image.get("height", 0) or 0),
                            ),
                            page_start=page_index,
                            page_end=page_index,
                            bbox=self._image_bbox(image),
                            metadata={
                                "source_type": "pdf",
                                "page_height": float(page.height),
                                "source_image_count": image.get("source_image_count", 1),
                                "ocr_decision": ocr_decision["decision"],
                                "ocr_status": ocr_decision["status"],
                                "ocr_reason": ocr_decision["reason"],
                                "ocr_reason_codes": ocr_decision.get("reason_codes", []),
                                "ocr_policy_version": ocr_decision.get("policy_version"),
                                "image_area_ratio": round(area_ratio, 6),
                                "semantic_role": "image_group",
                                "excluded_from_retrieval": True,
                                "exclusion_reason": "image_has_no_ocr_or_caption_text",
                            },
                            source_trace=SourceTrace(
                                parser=self.parser_name,
                                raw_page_index=page_index - 1,
                                raw_object_type="image",
                                raw_object_index=image_index - 1,
                            ),
                            quality_flags=[ocr_decision["decision"]]
                            if ocr_decision["decision"] in OCR_CANDIDATE_DECISIONS
                            else [],
                            order=order,
                        )
                    )

                if ocr_decision["decision"] in OCR_CANDIDATE_DECISIONS:
                    warnings.append(
                        QualityWarning(
                            level="warning",
                            warning_type=ocr_decision["decision"],
                            message=ocr_decision["reason"],
                            page_number=page_index,
                        )
                    )

        if self._is_noisy_pdf_title(title):
            cover_heading = next(
                (
                    block.text
                    for block in blocks
                    if block.page_start == 1 and block.block_type == "heading" and block.text
                ),
                None,
            )
            if cover_heading:
                metadata["parser_reported_title"] = title
                metadata["title_source"] = "cover_heading_fallback"
                title = cover_heading

        quality_report = build_quality_report(document_id, raw_outputs, blocks, warnings)
        return ParserResult(
            document_id=document_id,
            title=title,
            file_type=self.file_type,
            source_path=str(path),
            source_url=source.source_url,
            metadata=metadata,
            raw_outputs=raw_outputs,
            blocks=blocks,
            quality_report=quality_report,
        )

    def _is_noisy_pdf_title(self, title: str) -> bool:
        normalized = normalize_text(title).lower()
        return (
            normalized.startswith("microsoft word")
            or normalized.endswith((".doc", ".docx", ".pdf"))
            or normalized in {"untitled", "document"}
        )

    def _inspect_page(self, page: Any, text: str, words: list[dict], tables: list[Any], images: list[dict]) -> dict[str, Any]:
        page_area = float(page.width * page.height) if page.width and page.height else 1.0
        normalized_text = normalize_text(text)
        image_area_ratios = []
        for image in images:
            width = float(image.get("width", 0) or 0)
            height = float(image.get("height", 0) or 0)
            image_area_ratios.append((width * height) / page_area)
        table_cells: list[str] = []
        table_numeric_cells = 0
        for table in tables:
            for row in table.extract() or []:
                for cell in row:
                    value = normalize_text(cell)
                    table_cells.append(value)
                    if value and re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", value):
                        table_numeric_cells += 1
        empty_table_cells = sum(not cell for cell in table_cells)
        cid_marker_count = len(re.findall(r"\(cid:\d+\)", normalized_text, flags=re.IGNORECASE))
        replacement_char_count = normalized_text.count("�")
        garbled_char_count = cid_marker_count + replacement_char_count
        word_bbox_area = sum(
            max(float(word.get("x1", 0)) - float(word.get("x0", 0)), 0)
            * max(float(word.get("bottom", 0)) - float(word.get("top", 0)), 0)
            for word in words
        )
        risk_tags = []
        if table_numeric_cells:
            risk_tags.append("numeric_table")
        if images and len(normalized_text) >= 100:
            risk_tags.append("mixed_text_image")
        return {
            "text_length": len(normalized_text),
            "word_count": len(words),
            "table_count": len(tables),
            "image_count": len(images),
            "max_image_area_ratio": max(image_area_ratios) if image_area_ratios else 0.0,
            "text_bbox_area_ratio": round(min(word_bbox_area / page_area, 1.0), 6),
            "cid_marker_count": cid_marker_count,
            "replacement_char_count": replacement_char_count,
            "garbled_char_ratio": round(garbled_char_count / max(len(normalized_text), 1), 6),
            "table_cell_count": len(table_cells),
            "table_empty_cell_ratio": round(empty_table_cells / max(len(table_cells), 1), 6),
            "table_numeric_cell_count": table_numeric_cells,
            "risk_tags": risk_tags,
        }

    def _raw_table_summary(self, table: Any) -> dict[str, Any]:
        rows = table.extract() or []
        return {
            "bbox": [float(item) for item in table.bbox] if table.bbox else None,
            "cell_count": len(getattr(table, "cells", []) or []),
            "row_count": len(rows),
            "rows_sample": rows[:5],
        }

    def _raw_image_summary(self, image: dict[str, Any]) -> dict[str, Any]:
        keys = ["x0", "top", "x1", "bottom", "width", "height", "name", "stream"]
        summary = {key: image.get(key) for key in keys if key in image}
        if "stream" in summary:
            summary["stream"] = "<pdf_stream>"
        return summary

    def _image_bbox(self, image: dict[str, Any]) -> list[float] | None:
        try:
            return [
                float(image["x0"]),
                float(image["top"]),
                float(image["x1"]),
                float(image["bottom"]),
            ]
        except KeyError:
            return None

    def _build_text_lines_from_words(
        self,
        page: Any,
        words: list[dict[str, Any]],
        table_bboxes: list[tuple[float, float, float, float]],
    ) -> tuple[list[dict[str, Any]], int]:
        candidate_words = []
        excluded_word_count = 0
        for word in words:
            if self._word_in_any_bbox(word, table_bboxes, padding=2.0):
                excluded_word_count += 1
                continue
            candidate_words.append(word)

        candidate_words.sort(key=lambda item: (float(item.get("top", 0)), float(item.get("x0", 0))))
        grouped_lines: list[list[dict[str, Any]]] = []
        line_tolerance = 3.0
        for word in candidate_words:
            if not grouped_lines:
                grouped_lines.append([word])
                continue
            last_line = grouped_lines[-1]
            last_top = sum(float(item.get("top", 0)) for item in last_line) / len(last_line)
            if abs(float(word.get("top", 0)) - last_top) <= line_tolerance:
                last_line.append(word)
            else:
                grouped_lines.append([word])

        lines = []
        for line_index, line_words in enumerate(grouped_lines, start=1):
            line_words.sort(key=lambda item: float(item.get("x0", 0)))
            text = self._join_words_as_line(line_words)
            if not text:
                continue
            bbox = [
                min(float(item.get("x0", 0)) for item in line_words),
                min(float(item.get("top", 0)) for item in line_words),
                max(float(item.get("x1", 0)) for item in line_words),
                max(float(item.get("bottom", 0)) for item in line_words),
            ]
            lines.append(
                {
                    "line_index": line_index,
                    "text": text,
                    "bbox": bbox,
                    "word_count": len(line_words),
                    "is_header_footer": self._is_header_or_footer_line(text, bbox, float(page.height)),
                }
            )
        return lines, excluded_word_count

    def _build_text_blocks_from_lines(
        self,
        document_id: str,
        page_index: int,
        page_height: float,
        text_lines: list[dict[str, Any]],
        start_order: int,
    ) -> tuple[list[Block], int]:
        blocks: list[Block] = []
        paragraph_buffer: list[dict[str, Any]] = []
        order = start_order

        def flush_paragraph() -> None:
            nonlocal order
            if not paragraph_buffer:
                return
            order += 1
            text = self._join_paragraph_lines(paragraph_buffer)
            bbox = self._union_bbox([line["bbox"] for line in paragraph_buffer])
            first_line_index = paragraph_buffer[0]["line_index"]
            blocks.append(
                Block(
                    block_id=f"{document_id}_p{page_index:04d}_para_{first_line_index:04d}",
                    document_id=document_id,
                    block_type="paragraph",
                    text=text,
                    page_start=page_index,
                    page_end=page_index,
                    bbox=bbox,
                    metadata={
                        "source_type": "pdf",
                        "page_height": page_height,
                        "line_start": first_line_index,
                        "line_end": paragraph_buffer[-1]["line_index"],
                        "line_count": len(paragraph_buffer),
                    },
                    source_trace=SourceTrace(
                        parser=self.parser_name,
                        raw_page_index=page_index - 1,
                        raw_object_type="word_paragraph",
                        raw_object_index=first_line_index - 1,
                    ),
                    order=order,
                )
            )
            paragraph_buffer.clear()

        for line in text_lines:
            text = normalize_text(line["text"])
            if not text or line.get("is_header_footer"):
                continue
            line_type, extra_metadata = self._classify_text_line(text)
            if line_type == "skip":
                continue
            if line_type == "paragraph":
                if paragraph_buffer and self._should_start_new_paragraph(paragraph_buffer[-1], line):
                    flush_paragraph()
                paragraph_buffer.append(line)
                continue

            flush_paragraph()
            order += 1
            block_type = "heading" if line_type == "heading" else line_type
            blocks.append(
                Block(
                    block_id=f"{document_id}_p{page_index:04d}_{block_type}_{line['line_index']:04d}",
                    document_id=document_id,
                    block_type=block_type,
                    text=text,
                    page_start=page_index,
                    page_end=page_index,
                    bbox=line["bbox"],
                    metadata={
                        "source_type": "pdf",
                        "page_height": page_height,
                        "line_start": line["line_index"],
                        "line_end": line["line_index"],
                        **extra_metadata,
                    },
                    source_trace=SourceTrace(
                        parser=self.parser_name,
                        raw_page_index=page_index - 1,
                        raw_object_type="word_line",
                        raw_object_index=line["line_index"] - 1,
                    ),
                    order=order,
                )
            )

        flush_paragraph()
        return blocks, order

    def _word_in_any_bbox(
        self,
        word: dict[str, Any],
        bboxes: list[tuple[float, float, float, float]],
        padding: float = 0.0,
    ) -> bool:
        x = (float(word.get("x0", 0)) + float(word.get("x1", 0))) / 2
        y = (float(word.get("top", 0)) + float(word.get("bottom", 0))) / 2
        for x0, top, x1, bottom in bboxes:
            if x0 - padding <= x <= x1 + padding and top - padding <= y <= bottom + padding:
                return True
        return False

    def _join_words_as_line(self, words: list[dict[str, Any]]) -> str:
        pieces: list[str] = []
        previous_word: dict[str, Any] | None = None
        for word in words:
            current_text = normalize_text(word.get("text", ""))
            if not current_text:
                continue
            if previous_word is not None:
                gap = float(word.get("x0", 0)) - float(previous_word.get("x1", 0))
                if gap > 3 and self._needs_space(previous_word.get("text", ""), current_text):
                    pieces.append(" ")
            pieces.append(current_text)
            previous_word = word
        return normalize_text("".join(pieces))

    def _needs_space(self, previous_text: str, current_text: str) -> bool:
        if not previous_text or not current_text:
            return False
        previous_last = previous_text[-1]
        current_first = current_text[0]
        if self._is_ascii_token(previous_last) or self._is_ascii_token(current_first):
            return True
        return False

    def _is_ascii_token(self, char: str) -> bool:
        return bool(re.match(r"[A-Za-z0-9%./()+-]", char))

    def _is_header_or_footer_line(self, text: str, bbox: list[float], page_height: float) -> bool:
        top = bbox[1]
        bottom = bbox[3]
        if top < 76 and len(text) <= 40:
            return True
        if bottom > page_height - 80 and re.fullmatch(r"\d+", text):
            return True
        return False

    def _classify_text_line(self, text: str) -> tuple[str, dict[str, Any]]:
        if text in {"目录"}:
            return "skip", {}
        if re.match(r"^表\s*\d+\s*[-－]\s*\d+", text):
            return "table_caption", {"caption_type": "table"}
        if re.match(r"^图\s*\d+\s*[-－]\s*\d+", text):
            return "figure_caption", {"caption_type": "figure"}
        if re.match(r"^单位[:：]", text):
            return "table_note", {"note_type": "unit"}
        if re.match(r"^[（(]?[一二三四五六七八九十]+[）)、.．]", text):
            return "heading", {"heading_level": 2}
        if re.match(r"^第[一二三四五六七八九十0-9]+[章节]", text):
            return "heading", {"heading_level": 1}
        if (
            len(text) <= 30
            and not re.search(r"[，。；：,.;:《》]", text)
            and any(keyword in text for keyword in ("附表", "政策", "办法", "指南", "公报", "报告", "概况"))
        ):
            return "heading", {"heading_level": 1}
        return "paragraph", {}

    def _should_start_new_paragraph(self, previous_line: dict[str, Any], current_line: dict[str, Any]) -> bool:
        previous_text = normalize_text(previous_line["text"])
        current_bbox = current_line["bbox"]
        previous_bbox = previous_line["bbox"]
        vertical_gap = current_bbox[1] - previous_bbox[3]
        if vertical_gap > 12:
            return True
        if previous_text.endswith(("。", "！", "？", ".", "!", "?")):
            return True
        return False

    def _join_paragraph_lines(self, lines: list[dict[str, Any]]) -> str:
        text = ""
        for line in lines:
            line_text = normalize_text(line["text"])
            if not text:
                text = line_text
                continue
            if text.endswith(("-", "－")):
                text = text[:-1] + line_text
            elif self._needs_space(text[-1], line_text[0]):
                text += " " + line_text
            else:
                text += line_text
        return normalize_text(text)

    def _find_table_context(
        self,
        text_lines: list[dict[str, Any]],
        table_bbox: tuple[float, float, float, float] | None,
    ) -> tuple[str | None, list[str]]:
        if not table_bbox:
            return None, []
        table_top = float(table_bbox[1])
        nearby_lines = [
            line
            for line in text_lines
            if not line.get("is_header_footer")
            and 0 <= table_top - float(line["bbox"][3]) <= 80
        ]
        caption = None
        notes: list[str] = []
        for line in nearby_lines:
            text = normalize_text(line["text"])
            line_type, _ = self._classify_text_line(text)
            if line_type == "table_caption":
                caption = text
            elif line_type == "table_note":
                notes.append(text)
        return caption, notes

    def _group_images(self, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
        image_items = []
        for image in images:
            bbox = self._image_bbox(image)
            if not bbox:
                continue
            image_items.append({"bbox": bbox, "items": [image]})
        image_items.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))

        groups: list[dict[str, Any]] = []
        for item in image_items:
            matching_indexes = [
                index
                for index, group in enumerate(groups)
                if self._should_merge_image_group(group["bbox"], item["bbox"])
            ]
            if not matching_indexes:
                groups.append(item)
                continue

            target_index = matching_indexes[0]
            groups[target_index]["bbox"] = self._union_bbox([groups[target_index]["bbox"], item["bbox"]])
            groups[target_index]["items"].extend(item["items"])
            for merge_index in reversed(matching_indexes[1:]):
                groups[target_index]["bbox"] = self._union_bbox(
                    [groups[target_index]["bbox"], groups[merge_index]["bbox"]]
                )
                groups[target_index]["items"].extend(groups[merge_index]["items"])
                groups.pop(merge_index)

        grouped_images = []
        for group in groups:
            bbox = group["bbox"]
            grouped_images.append(
                {
                    "x0": bbox[0],
                    "top": bbox[1],
                    "x1": bbox[2],
                    "bottom": bbox[3],
                    "width": bbox[2] - bbox[0],
                    "height": bbox[3] - bbox[1],
                    "source_image_count": len(group["items"]),
                    "object_type": "image_group",
                }
            )
        return grouped_images

    def _should_merge_image_group(self, previous_bbox: list[float], current_bbox: list[float]) -> bool:
        horizontal_overlap = min(previous_bbox[2], current_bbox[2]) - max(previous_bbox[0], current_bbox[0])
        min_width = min(previous_bbox[2] - previous_bbox[0], current_bbox[2] - current_bbox[0])
        overlap_ratio = horizontal_overlap / min_width if min_width > 0 else 0
        vertical_gap = max(current_bbox[1] - previous_bbox[3], previous_bbox[1] - current_bbox[3], 0)
        return overlap_ratio >= 0.7 and vertical_gap <= 4

    def _union_bbox(self, bboxes: list[list[float]]) -> list[float]:
        return [
            min(bbox[0] for bbox in bboxes),
            min(bbox[1] for bbox in bboxes),
            max(bbox[2] for bbox in bboxes),
            max(bbox[3] for bbox in bboxes),
        ]
