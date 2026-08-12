from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_plain_data(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_plain_data(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [to_plain_data(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


@dataclass
class SourceFile:
    source_id: str
    file_path: str
    file_type: str
    original_filename: str
    source_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceTrace:
    parser: str
    raw_object_type: str
    raw_object_index: int | None = None
    raw_page_index: int | None = None
    raw_sheet_name: str | None = None
    raw_slide_index: int | None = None
    raw_locator: str | None = None


@dataclass
class TableData:
    caption: str | None = None
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ImageData:
    image_id: str
    caption: str | None = None
    ocr_text: str | None = None
    image_path: str | None = None
    width: float | None = None
    height: float | None = None


@dataclass
class Block:
    block_id: str
    document_id: str
    block_type: str
    text: str | None = None
    table_data: TableData | None = None
    image: ImageData | None = None
    page_start: int | None = None
    page_end: int | None = None
    bbox: list[float] | None = None
    heading_path: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_trace: SourceTrace | None = None
    quality_flags: list[str] = field(default_factory=list)
    order: int = 0


@dataclass
class QualityWarning:
    level: str
    warning_type: str
    message: str
    page_number: int | None = None
    block_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityReport:
    document_id: str
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[QualityWarning] = field(default_factory=list)


@dataclass
class ParsedDocument:
    document_id: str
    title: str
    file_type: str
    source_path: str
    source_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    quality_report: QualityReport | None = None
    parsed_at: str = field(default_factory=utc_now_iso)


@dataclass
class ParserResult:
    document_id: str
    title: str
    file_type: str
    source_path: str
    source_url: str
    metadata: dict[str, Any]
    raw_outputs: list[dict[str, Any]]
    blocks: list[Block]
    quality_report: QualityReport


@dataclass
class ManualReviewItem:
    review_id: str
    review_type: str
    severity: str
    document_id: str
    page_number: int | None
    object_type: str
    object_id: str | None
    reason: str
    suggested_action: str
    source_preview: str = ""
    status: str = "pending"


@dataclass
class ParsingEvaluation:
    document_id: str
    status: str
    parser_version: str
    schema_version: str
    block_count: int
    blocks_by_type: dict[str, int] = field(default_factory=dict)
    warning_count: int = 0
    error_count: int = 0
    manual_review_items: list[ManualReviewItem] = field(default_factory=list)
    quality_flags: dict[str, int] = field(default_factory=dict)
    evaluated_at: str = field(default_factory=utc_now_iso)


@dataclass
class ParseRunResult:
    run_id: str
    run_dir: str
    status: str
    parsed_document: ParsedDocument
    evaluation: ParsingEvaluation
    artifact_paths: dict[str, str] = field(default_factory=dict)
