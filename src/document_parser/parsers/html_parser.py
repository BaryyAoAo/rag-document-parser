from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Tag

from ..io_utils import read_text_auto
from ..models import Block, ImageData, ParserResult, SourceFile, SourceTrace
from ..quality import build_quality_report
from ..text_builders import normalize_text, table_from_rows
from .base import BaseParser


class HtmlParser(BaseParser):
    parser_name = "beautifulsoup4"
    file_type = "html"

    def parse(self, source: SourceFile) -> ParserResult:
        path = Path(source.file_path)
        document_id = self.build_document_id(path)
        metadata = self.base_metadata(source, path)
        soup = BeautifulSoup(read_text_auto(path), "html.parser")
        for node in soup.select("script, style, nav, header, footer, noscript"):
            node.decompose()

        title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else path.stem
        root = soup.find("article") or soup.find("main") or soup.body or soup
        raw_outputs = [
            {
                "title": title,
                "root_tag": root.name if isinstance(root, Tag) else None,
                "heading_count": len(root.find_all(["h1", "h2", "h3", "h4"])),
                "paragraph_count": len(root.find_all("p")),
                "table_count": len(root.find_all("table")),
                "image_count": len(root.find_all("img")),
            }
        ]
        blocks: list[Block] = []
        order = 0

        for element_index, element in enumerate(root.find_all(["h1", "h2", "h3", "h4", "p", "li", "table", "img", "pre"], recursive=True), start=1):
            if element.find_parent("table") and element.name != "table":
                continue
            if element.name == "table":
                rows = []
                for tr in element.find_all("tr"):
                    rows.append([normalize_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])])
                table_data = table_from_rows(rows)
                if table_data.headers or table_data.rows:
                    order += 1
                    blocks.append(
                        Block(
                            block_id=f"{document_id}_html_tbl_{order:04d}",
                            document_id=document_id,
                            block_type="table",
                            table_data=table_data,
                            metadata={
                                "source_type": "html",
                                "html_tag": element.name,
                                "table_id": f"tbl_{order:04d}",
                            },
                            source_trace=SourceTrace(
                                parser=self.parser_name,
                                raw_object_type="html_table",
                                raw_object_index=element_index - 1,
                                raw_locator=self._short_css_locator(element),
                            ),
                            order=order,
                        )
                    )
                continue

            if element.name == "img":
                src = element.get("src")
                alt = normalize_text(element.get("alt"))
                order += 1
                blocks.append(
                    Block(
                        block_id=f"{document_id}_html_img_{order:04d}",
                        document_id=document_id,
                        block_type="image",
                        text=alt or None,
                        image=ImageData(
                            image_id=f"img_{order:04d}",
                            caption=alt,
                            image_path=src,
                            width=self._numeric_dimension(element.get("width")),
                            height=self._numeric_dimension(element.get("height")),
                        ),
                        metadata={
                            "source_type": "html",
                            "html_tag": "img",
                            "src": src,
                            "ocr_decision": "review_before_ocr" if src else "skip_ocr",
                            "ocr_status": "pending_review" if src else "not_required",
                            "ocr_reason": "HTML 正文图片可能包含业务文字或结构信息",
                            "ocr_reason_codes": ["HTML_CONTENT_IMAGE"] if src else [],
                        },
                        source_trace=SourceTrace(
                            parser=self.parser_name,
                            raw_object_type="html_image",
                            raw_object_index=element_index - 1,
                            raw_locator=self._short_css_locator(element),
                        ),
                        quality_flags=["review_before_ocr"] if src else [],
                        order=order,
                    )
                )
                continue

            text = normalize_text(element.get_text(" ", strip=True))
            if not text:
                continue
            order += 1
            if element.name in {"h1", "h2", "h3", "h4"}:
                block_type = "heading"
                heading_level = int(element.name[1])
            elif element.name == "li":
                block_type = "list_item"
                heading_level = None
            elif element.name == "pre":
                block_type = "code"
                heading_level = None
            else:
                block_type = "paragraph"
                heading_level = None
            blocks.append(
                Block(
                    block_id=f"{document_id}_html_txt_{order:04d}",
                    document_id=document_id,
                    block_type=block_type,
                    text=text,
                    metadata={
                        "source_type": "html",
                        "html_tag": element.name,
                        "heading_level": heading_level,
                    },
                    source_trace=SourceTrace(
                        parser=self.parser_name,
                        raw_object_type="html_element",
                        raw_object_index=element_index - 1,
                        raw_locator=self._short_css_locator(element),
                    ),
                    order=order,
                )
            )

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

    def _short_css_locator(self, element: Tag) -> str:
        ident = element.get("id")
        classes = element.get("class") or []
        suffix = f"#{ident}" if ident else "".join(f".{item}" for item in classes[:2])
        return f"{element.name}{suffix}"

    def _numeric_dimension(self, value: object) -> float | None:
        text = str(value or "").strip().lower().removesuffix("px")
        try:
            return float(text) if text else None
        except ValueError:
            return None
