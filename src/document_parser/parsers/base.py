from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
import re

from ..io_utils import sha256_file
from ..models import ParserResult, QualityReport, SourceFile
from ..text_builders import normalize_text
from ..versions import PARSER_VERSION, PIPELINE_VERSION, SCHEMA_VERSION


class BaseParser(ABC):
    parser_name = "base"
    file_type = "unknown"

    @abstractmethod
    def parse(self, source: SourceFile) -> ParserResult:
        raise NotImplementedError

    def build_document_id(self, path: Path) -> str:
        digest = sha256_file(path)[:12]
        return f"doc_{self.file_type}_{digest}"

    def base_metadata(self, source: SourceFile, path: Path) -> dict:
        metadata = dict(source.metadata)
        metadata.update(
            {
                "sha256": sha256_file(path),
                "original_filename": source.original_filename,
                "parser_version": PARSER_VERSION,
                "pipeline_version": PIPELINE_VERSION,
                "schema_version": SCHEMA_VERSION,
            }
        )
        return metadata

    def empty_quality_report(self, document_id: str) -> QualityReport:
        return QualityReport(document_id=document_id, summary={}, warnings=[])

    def resolve_title(self, parser_title: str, metadata: dict, path: Path) -> str:
        reported = normalize_text(parser_title) or path.stem
        inventory_title = normalize_text(metadata.get("inventory_title"))
        metadata["parser_reported_title"] = reported

        if inventory_title:
            metadata["resolved_title"] = inventory_title
            metadata["title_source"] = "inventory_title"
            return inventory_title
        if not self._looks_like_filename_title(reported, path):
            metadata["resolved_title"] = reported
            metadata["title_source"] = "parser_reported_title"
            return reported
        metadata["resolved_title"] = path.stem
        metadata["title_source"] = "filename_fallback"
        return path.stem

    def _looks_like_filename_title(self, title: str, path: Path) -> bool:
        normalized = title.lower().strip()
        if normalized in {path.stem.lower(), path.name.lower(), "untitled", "document"}:
            return True
        return bool(re.search(r"(?:seed_\d+|\.(?:pdf|docx?|xlsx?|pptx?|html?|md)$)", normalized))
