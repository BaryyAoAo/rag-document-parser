from __future__ import annotations

from pathlib import Path

from ..io_utils import read_text_auto
from ..models import Block, ImageData, ParserResult, SourceFile, SourceTrace
from ..quality import build_quality_report
from ..text_builders import normalize_text, table_from_rows
from .base import BaseParser


class MarkdownParser(BaseParser):
    parser_name = "simple-markdown-parser"
    file_type = "markdown"

    def parse(self, source: SourceFile) -> ParserResult:
        path = Path(source.file_path)
        document_id = self.build_document_id(path)
        metadata = self.base_metadata(source, path)
        lines = read_text_auto(path).splitlines()
        title = path.stem
        raw_outputs = [{"line_count": len(lines), "lines_sample": lines[:40]}]
        blocks: list[Block] = []
        paragraph_buffer: list[str] = []
        order = 0
        index = 0

        def flush_paragraph(line_number: int) -> None:
            nonlocal order
            text = normalize_text(" ".join(paragraph_buffer))
            paragraph_buffer.clear()
            if not text:
                return
            order += 1
            blocks.append(
                Block(
                    block_id=f"{document_id}_md_txt_{order:04d}",
                    document_id=document_id,
                    block_type="paragraph",
                    text=text,
                    metadata={"source_type": "markdown", "line_end": line_number},
                    source_trace=SourceTrace(
                        parser=self.parser_name,
                        raw_object_type="markdown_paragraph",
                        raw_object_index=order - 1,
                        raw_locator=f"line:{line_number}",
                    ),
                    order=order,
                )
            )

        while index < len(lines):
            raw_line = lines[index]
            line = raw_line.rstrip()
            stripped = line.strip()
            line_number = index + 1
            if not stripped:
                flush_paragraph(line_number)
                index += 1
                continue
            if stripped.startswith("#"):
                flush_paragraph(line_number)
                level = len(stripped) - len(stripped.lstrip("#"))
                text = normalize_text(stripped[level:])
                if text and title == path.stem:
                    title = text
                order += 1
                blocks.append(
                    Block(
                        block_id=f"{document_id}_md_head_{order:04d}",
                        document_id=document_id,
                        block_type="heading",
                        text=text,
                        metadata={"source_type": "markdown", "heading_level": level, "line_start": line_number},
                        source_trace=SourceTrace(
                            parser=self.parser_name,
                            raw_object_type="markdown_heading",
                            raw_object_index=order - 1,
                            raw_locator=f"line:{line_number}",
                        ),
                        order=order,
                    )
                )
                index += 1
                continue
            if stripped.startswith("```"):
                flush_paragraph(line_number)
                language = stripped.strip("`").strip()
                code_lines = []
                start_line = line_number
                index += 1
                while index < len(lines) and not lines[index].strip().startswith("```"):
                    code_lines.append(lines[index])
                    index += 1
                index += 1
                order += 1
                blocks.append(
                    Block(
                        block_id=f"{document_id}_md_code_{order:04d}",
                        document_id=document_id,
                        block_type="code",
                        text="\n".join(code_lines),
                        metadata={"source_type": "markdown", "language": language, "line_start": start_line},
                        source_trace=SourceTrace(
                            parser=self.parser_name,
                            raw_object_type="markdown_code",
                            raw_object_index=order - 1,
                            raw_locator=f"line:{start_line}",
                        ),
                        order=order,
                    )
                )
                continue
            if stripped.startswith("|") and "|" in stripped[1:]:
                flush_paragraph(line_number)
                table_lines = []
                start_line = line_number
                while index < len(lines) and lines[index].strip().startswith("|"):
                    table_lines.append(lines[index].strip())
                    index += 1
                rows = [
                    [normalize_text(cell) for cell in item.strip("|").split("|")]
                    for item in table_lines
                    if "---" not in item
                ]
                table_data = table_from_rows(rows)
                order += 1
                blocks.append(
                    Block(
                        block_id=f"{document_id}_md_tbl_{order:04d}",
                        document_id=document_id,
                        block_type="table",
                        table_data=table_data,
                        metadata={"source_type": "markdown", "line_start": start_line, "table_id": f"tbl_{order:04d}"},
                        source_trace=SourceTrace(
                            parser=self.parser_name,
                            raw_object_type="markdown_table",
                            raw_object_index=order - 1,
                            raw_locator=f"line:{start_line}",
                        ),
                        order=order,
                    )
                )
                continue
            if stripped.startswith("![") and "](" in stripped:
                flush_paragraph(line_number)
                alt = stripped.split("](", 1)[0].lstrip("![")
                src = stripped.split("](", 1)[1].rstrip(")")
                order += 1
                blocks.append(
                    Block(
                        block_id=f"{document_id}_md_img_{order:04d}",
                        document_id=document_id,
                        block_type="image",
                        text=normalize_text(alt) or None,
                        image=ImageData(image_id=f"img_{order:04d}", caption=normalize_text(alt), image_path=src),
                        metadata={
                            "source_type": "markdown",
                            "line_start": line_number,
                            "ocr_decision": "review_before_ocr",
                            "ocr_status": "pending_review",
                            "ocr_reason": "Markdown 图片可能包含业务文字或结构信息",
                            "ocr_reason_codes": ["MARKDOWN_CONTENT_IMAGE"],
                        },
                        source_trace=SourceTrace(
                            parser=self.parser_name,
                            raw_object_type="markdown_image",
                            raw_object_index=order - 1,
                            raw_locator=f"line:{line_number}",
                        ),
                        quality_flags=["review_before_ocr"],
                        order=order,
                    )
                )
                index += 1
                continue
            paragraph_buffer.append(stripped)
            index += 1

        flush_paragraph(len(lines))
        quality_report = build_quality_report(document_id, raw_outputs, blocks)
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
