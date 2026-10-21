from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifacts import ParsingArtifacts, resolve_parsing_artifacts
from .models import (
    Block,
    ImageData,
    ParsedDocument,
    QualityReport,
    QualityWarning,
    SourceTrace,
    TableData,
)
from .versions import SCHEMA_VERSION


class ParsedDocumentValidationError(ValueError):
    """Raised when a persisted ParsedDocument violates the public schema."""


def load_parsed_document(
    path: Path | str,
    *,
    expected_schema_version: str = SCHEMA_VERSION,
) -> ParsedDocument:
    """Load a persisted ParsedDocument with strict structural validation."""
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"ParsedDocument artifact not found: {source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise ParsedDocumentValidationError(f"Invalid ParsedDocument JSON: {source}") from error
    return parsed_document_from_dict(
        value,
        expected_schema_version=expected_schema_version,
        source=source,
    )


def load_resolved_parsed_document(
    run_dir: Path | str,
    *,
    expected_schema_version: str = SCHEMA_VERSION,
    require_review_complete: bool = True,
) -> ParsedDocument:
    """Resolve the effective artifact revision, validate review state, and load it."""
    artifacts = resolve_parsing_artifacts(Path(run_dir))
    document = load_parsed_document(
        artifacts.parsed_document_path,
        expected_schema_version=expected_schema_version,
    )
    evaluation = _load_object(artifacts.evaluation_path, "parsing evaluation")
    evaluation_document_id = _required_str(evaluation, "document_id", "evaluation")
    if evaluation_document_id != document.document_id:
        raise ParsedDocumentValidationError(
            "Evaluation document_id does not match ParsedDocument document_id"
        )
    if require_review_complete:
        _validate_review_complete(artifacts, evaluation)
    return document


def parsed_document_from_dict(
    value: Any,
    *,
    expected_schema_version: str = SCHEMA_VERSION,
    source: Path | None = None,
) -> ParsedDocument:
    context = f"ParsedDocument {source}" if source else "ParsedDocument"
    data = _object(value, context)
    document_id = _required_str(data, "document_id", context)
    metadata = _dict_field(data, "metadata", context)
    schema_version = metadata.get("schema_version")
    if schema_version != expected_schema_version:
        raise ParsedDocumentValidationError(
            f"{context}.metadata.schema_version must be {expected_schema_version!r}, "
            f"got {schema_version!r}"
        )

    raw_blocks = _list_field(data, "blocks", context)
    blocks = [
        _block_from_dict(item, document_id, f"{context}.blocks[{index}]")
        for index, item in enumerate(raw_blocks)
    ]
    block_ids = [block.block_id for block in blocks]
    if len(block_ids) != len(set(block_ids)):
        raise ParsedDocumentValidationError(f"{context}.blocks contains duplicate block_id values")

    quality = data.get("quality_report")
    quality_report = None if quality is None else _quality_report_from_dict(quality, document_id)
    return ParsedDocument(
        document_id=document_id,
        title=_required_str(data, "title", context),
        file_type=_required_str(data, "file_type", context),
        source_path=_required_str(data, "source_path", context),
        source_url=_optional_str(data, "source_url", context),
        metadata=metadata,
        blocks=blocks,
        quality_report=quality_report,
        parsed_at=_required_str(data, "parsed_at", context),
    )


def _block_from_dict(value: Any, document_id: str, context: str) -> Block:
    data = _object(value, context)
    block_document_id = _required_str(data, "document_id", context)
    if block_document_id != document_id:
        raise ParsedDocumentValidationError(
            f"{context}.document_id does not match parent document_id"
        )
    table = data.get("table_data")
    image = data.get("image")
    trace = data.get("source_trace")
    bbox = data.get("bbox")
    if bbox is not None:
        bbox = _number_list(bbox, f"{context}.bbox", exact_length=4)
    page_start = _optional_int(data, "page_start", context, minimum=1)
    page_end = _optional_int(data, "page_end", context, minimum=1)
    if page_start is not None and page_end is not None and page_end < page_start:
        raise ParsedDocumentValidationError(f"{context}.page_end cannot precede page_start")
    return Block(
        block_id=_required_str(data, "block_id", context),
        document_id=block_document_id,
        block_type=_required_str(data, "block_type", context),
        text=_nullable_str(data, "text", context),
        table_data=None if table is None else _table_from_dict(table, f"{context}.table_data"),
        image=None if image is None else _image_from_dict(image, f"{context}.image"),
        page_start=page_start,
        page_end=page_end,
        bbox=bbox,
        heading_path=_string_list_field(data, "heading_path", context),
        metadata=_dict_field(data, "metadata", context),
        source_trace=None if trace is None else _trace_from_dict(trace, f"{context}.source_trace"),
        quality_flags=_string_list_field(data, "quality_flags", context),
        order=_int_field(data, "order", context, minimum=0),
    )


def _table_from_dict(value: Any, context: str) -> TableData:
    data = _object(value, context)
    rows = _list_field(data, "rows", context)
    normalized_rows: list[list[str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or not all(isinstance(cell, str) for cell in row):
            raise ParsedDocumentValidationError(f"{context}.rows[{index}] must be a list of strings")
        normalized_rows.append(row)
    return TableData(
        caption=_nullable_str(data, "caption", context),
        headers=_string_list_field(data, "headers", context),
        rows=normalized_rows,
        notes=_string_list_field(data, "notes", context),
    )


def _image_from_dict(value: Any, context: str) -> ImageData:
    data = _object(value, context)
    return ImageData(
        image_id=_required_str(data, "image_id", context),
        caption=_nullable_str(data, "caption", context),
        ocr_text=_nullable_str(data, "ocr_text", context),
        image_path=_nullable_str(data, "image_path", context),
        width=_optional_number(data, "width", context),
        height=_optional_number(data, "height", context),
    )


def _trace_from_dict(value: Any, context: str) -> SourceTrace:
    data = _object(value, context)
    return SourceTrace(
        parser=_required_str(data, "parser", context),
        raw_object_type=_required_str(data, "raw_object_type", context),
        raw_object_index=_optional_int(data, "raw_object_index", context, minimum=0),
        raw_page_index=_optional_int(data, "raw_page_index", context, minimum=0),
        raw_sheet_name=_nullable_str(data, "raw_sheet_name", context),
        raw_slide_index=_optional_int(data, "raw_slide_index", context, minimum=0),
        raw_locator=_nullable_str(data, "raw_locator", context),
    )


def _quality_report_from_dict(value: Any, document_id: str) -> QualityReport:
    context = "ParsedDocument.quality_report"
    data = _object(value, context)
    quality_document_id = _required_str(data, "document_id", context)
    if quality_document_id != document_id:
        raise ParsedDocumentValidationError(
            f"{context}.document_id does not match parent document_id"
        )
    warnings = []
    for index, value in enumerate(_list_field(data, "warnings", context)):
        item_context = f"{context}.warnings[{index}]"
        item = _object(value, item_context)
        warnings.append(
            QualityWarning(
                level=_required_str(item, "level", item_context),
                warning_type=_required_str(item, "warning_type", item_context),
                message=_required_str(item, "message", item_context),
                page_number=_optional_int(item, "page_number", item_context, minimum=1),
                block_id=_nullable_str(item, "block_id", item_context),
                details=_dict_field(item, "details", item_context),
            )
        )
    return QualityReport(
        document_id=quality_document_id,
        summary=_dict_field(data, "summary", context),
        warnings=warnings,
    )


def _validate_review_complete(artifacts: ParsingArtifacts, evaluation: dict[str, Any]) -> None:
    status = _required_str(evaluation, "status", "evaluation")
    if status in {"failed", "blocked"}:
        raise ParsedDocumentValidationError(f"Parsing evaluation is not usable: {status}")
    errors = evaluation.get("error_count", 0)
    if not isinstance(errors, int) or isinstance(errors, bool):
        raise ParsedDocumentValidationError("evaluation.error_count must be an integer")
    if errors:
        raise ParsedDocumentValidationError("Parsing evaluation still contains errors")
    items = evaluation.get("manual_review_items", [])
    if not isinstance(items, list):
        raise ParsedDocumentValidationError("evaluation.manual_review_items must be a list")
    pending = [
        str(item.get("review_id") or "unknown")
        for item in items
        if isinstance(item, dict) and item.get("status") not in {"resolved", "applied", "excluded"}
    ]
    if pending:
        raise ParsedDocumentValidationError(
            "Parsing artifacts still have pending review items: " + ", ".join(pending)
        )
    if artifacts.source == "base_run" and status == "review_required":
        raise ParsedDocumentValidationError(
            "Base parsing artifacts require review; resolve OCR or structure review first"
        )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label.capitalize()} artifact not found: {path}")
    try:
        return _object(json.loads(path.read_text(encoding="utf-8-sig")), label)
    except json.JSONDecodeError as error:
        raise ParsedDocumentValidationError(f"Invalid {label} JSON: {path}") from error


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ParsedDocumentValidationError(f"{context} must be a JSON object")
    return value


def _required_str(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ParsedDocumentValidationError(f"{context}.{key} must be a non-empty string")
    return value


def _optional_str(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ParsedDocumentValidationError(f"{context}.{key} must be a string")
    return value


def _nullable_str(data: dict[str, Any], key: str, context: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ParsedDocumentValidationError(f"{context}.{key} must be a string or null")
    return value


def _dict_field(data: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ParsedDocumentValidationError(f"{context}.{key} must be an object")
    return value


def _list_field(data: dict[str, Any], key: str, context: str) -> list[Any]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ParsedDocumentValidationError(f"{context}.{key} must be a list")
    return value


def _string_list_field(data: dict[str, Any], key: str, context: str) -> list[str]:
    value = _list_field(data, key, context)
    if not all(isinstance(item, str) for item in value):
        raise ParsedDocumentValidationError(f"{context}.{key} must contain only strings")
    return value


def _int_field(data: dict[str, Any], key: str, context: str, minimum: int) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ParsedDocumentValidationError(f"{context}.{key} must be an integer >= {minimum}")
    return value


def _optional_int(
    data: dict[str, Any], key: str, context: str, minimum: int
) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ParsedDocumentValidationError(
            f"{context}.{key} must be null or an integer >= {minimum}"
        )
    return value


def _optional_number(data: dict[str, Any], key: str, context: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ParsedDocumentValidationError(f"{context}.{key} must be a number or null")
    return float(value)


def _number_list(value: Any, context: str, exact_length: int) -> list[float]:
    if not isinstance(value, list) or len(value) != exact_length:
        raise ParsedDocumentValidationError(
            f"{context} must be a list of {exact_length} numbers"
        )
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
        raise ParsedDocumentValidationError(f"{context} must contain only numbers")
    return [float(item) for item in value]
