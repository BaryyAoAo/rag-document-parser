from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cleaner import clean_blocks
from .evaluation import evaluate_parsed_document
from .file_type import detect_file_type
from .models import ParseRunResult, ParsedDocument, SourceFile, to_plain_data
from .ocr import OcrReviewStore, PdfOcrReviewWorkflow
from .ocr.image_workflow import BlockImageOcrWorkflow
from .ocr.pdf_workflow import OcrProvider
from .ocr.workflow import OcrReviewWorkflow
from .parsers.base import BaseParser
from .parsers.docx_parser import DocxParser
from .parsers.html_parser import HtmlParser
from .parsers.markdown_parser import MarkdownParser
from .parsers.pdf_parser import PdfParser
from .parsers.pptx_parser import PptxParser
from .parsers.xlsx_parser import XlsxParser
from .quality import build_quality_report
from .repair import repair_blocks
from .run_context import RunContext


PARSER_REGISTRY: dict[str, type[BaseParser]] = {
    "pdf": PdfParser,
    "docx": DocxParser,
    "xlsx": XlsxParser,
    "html": HtmlParser,
    "markdown": MarkdownParser,
    "pptx": PptxParser,
}

REVIEW_FIELDS = [
    "review_id",
    "review_type",
    "severity",
    "document_id",
    "page_number",
    "object_type",
    "object_id",
    "reason",
    "suggested_action",
    "source_preview",
    "status",
]


@dataclass(frozen=True)
class RuntimeParseOptions:
    execute_auto_ocr: bool = False
    keep_raw_parser_output: bool = True


class DocumentParsingPipeline:
    """Convert one source file into an observable, reviewable ParsedDocument."""

    def __init__(
        self,
        output_root: Path,
        options: RuntimeParseOptions | None = None,
        ocr_provider: OcrProvider | None = None,
        parser_registry: dict[str, type[BaseParser]] | None = None,
    ) -> None:
        self.output_root = output_root.resolve()
        self.options = options or RuntimeParseOptions()
        self.parser_registry = parser_registry or PARSER_REGISTRY
        review_store = OcrReviewStore(self.output_root / "_ocr_review")
        self.pdf_ocr = PdfOcrReviewWorkflow(
            review_store=review_store,
            execute_auto_ocr=self.options.execute_auto_ocr,
            provider=ocr_provider,
        )
        self.image_ocr = BlockImageOcrWorkflow(
            OcrReviewWorkflow(
                review_store=review_store,
                execute_auto_ocr=self.options.execute_auto_ocr,
                provider=ocr_provider,
            )
        )

    def parse(
        self,
        file_path: Path,
        metadata: dict[str, Any] | None = None,
        source_url: str = "",
    ) -> ParseRunResult:
        file_path = file_path.resolve()
        file_type = detect_file_type(file_path)
        context = RunContext(self.output_root, file_path, file_type)
        context.log("start document parsing pipeline")
        source = SourceFile(
            source_id=f"src_{file_path.stem}",
            file_path=str(file_path),
            file_type=file_type,
            original_filename=file_path.name,
            source_url=source_url,
            metadata=dict(metadata or {}),
        )
        context.write_stage("01_source_file.json", source)

        parser = self._parser(file_type)
        parser_result = parser.parse(source)
        parser_result.title = parser.resolve_title(
            parser_result.title, parser_result.metadata, file_path
        )
        if self.options.keep_raw_parser_output:
            context.write_stage(
                "02_raw_parser_output.json",
                parser_result.raw_outputs,
                {"raw_output_count": len(parser_result.raw_outputs)},
            )
        context.write_stage(
            "03_blocks_before_clean.json",
            parser_result.blocks,
            {"block_count": len(parser_result.blocks)},
        )

        cleaned = clean_blocks(parser_result.blocks)
        context.write_stage("04_blocks_after_clean.json", cleaned, {"block_count": len(cleaned)})
        repaired = repair_blocks(cleaned)
        context.write_stage("05_blocks_after_repair.json", repaired, {"block_count": len(repaired)})

        quality_report = build_quality_report(
            parser_result.document_id,
            parser_result.raw_outputs,
            repaired,
            parser_result.quality_report.warnings,
        )
        document = ParsedDocument(
            document_id=parser_result.document_id,
            title=parser_result.title,
            file_type=parser_result.file_type,
            source_path=parser_result.source_path,
            source_url=parser_result.source_url,
            metadata=parser_result.metadata,
            blocks=repaired,
            quality_report=quality_report,
        )
        document_path = context.run_dir / "06_parsed_document.json"
        context.write_stage(
            document_path.name,
            document,
            {"document_id": document.document_id, "block_count": len(document.blocks)},
        )

        if file_type == "pdf":
            ocr_result = self.pdf_ocr.run(
                document, parser_result.raw_outputs, document_path, context.run_dir
            )
        else:
            ocr_result = self.image_ocr.run(
                document, parser_result.raw_outputs, document_path, context.run_dir
            )

        evaluation = evaluate_parsed_document(document, ocr_result.manual_reviews)
        evaluation_path = context.run_dir / "07_parsing_evaluation.json"
        context.write_stage(evaluation_path.name, evaluation, {"status": evaluation.status})
        context.write_csv_stage(
            "08_manual_review.csv",
            [to_plain_data(item) for item in evaluation.manual_review_items],
            REVIEW_FIELDS,
            {"manual_review_count": len(evaluation.manual_review_items)},
        )
        context.finish(
            {
                "document_id": document.document_id,
                "parser": parser.parser_name,
                "status": evaluation.status,
                "block_count": len(document.blocks),
                "manual_review_count": len(evaluation.manual_review_items),
            }
        )
        context.log(f"finished parsing: {context.run_dir}")
        artifacts = {
            "parsed_document": str(document_path),
            "evaluation": str(evaluation_path),
            "manual_review": str(context.run_dir / "08_manual_review.csv"),
            "manifest": str(context.run_dir / "00_run_manifest.json"),
        }
        return ParseRunResult(
            run_id=context.run_id,
            run_dir=str(context.run_dir),
            status=evaluation.status,
            parsed_document=document,
            evaluation=evaluation,
            artifact_paths=artifacts,
        )

    def _parser(self, file_type: str) -> BaseParser:
        parser_class = self.parser_registry.get(file_type)
        if parser_class is None:
            raise ValueError(f"No parser registered for file type: {file_type}")
        return parser_class()
