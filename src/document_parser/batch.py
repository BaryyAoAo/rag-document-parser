from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .file_type import FILE_TYPE_BY_SUFFIX
from .io_utils import write_json
from .pipeline import DocumentParsingPipeline


def parse_directory(
    pipeline: DocumentParsingPipeline,
    input_dir: Path,
    recursive: bool = True,
) -> dict[str, Any]:
    input_dir = input_dir.resolve()
    iterator = input_dir.rglob("*") if recursive else input_dir.glob("*")
    files = sorted(
        path for path in iterator if path.is_file() and path.suffix.lower() in FILE_TYPE_BY_SUFFIX
    )
    results = []
    for path in files:
        try:
            result = pipeline.parse(path)
            results.append(
                {
                    "source_path": str(path),
                    "status": result.status,
                    "document_id": result.parsed_document.document_id,
                    "block_count": len(result.parsed_document.blocks),
                    "run_dir": result.run_dir,
                    "error": "",
                }
            )
        except Exception as error:
            results.append(
                {
                    "source_path": str(path),
                    "status": "failed",
                    "document_id": "",
                    "block_count": 0,
                    "run_dir": "",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_dir": str(input_dir),
        "file_count": len(files),
        "finalized_count": sum(item["status"] == "finalized" for item in results),
        "review_required_count": sum(item["status"] == "review_required" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "results": results,
    }
    output_path = pipeline.output_root / "batch_report.json"
    write_json(output_path, report)
    report["report_path"] = str(output_path)
    return report
