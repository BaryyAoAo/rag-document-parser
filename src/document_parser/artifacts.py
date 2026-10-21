from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class ParsingArtifacts:
    source: str
    parsed_document_path: Path
    evaluation_path: Path
    revision_id: str = ""


def resolve_parsing_artifacts(run_dir: Path, include_structure: bool = True) -> ParsingArtifacts:
    run_dir = run_dir.resolve()
    review_artifacts = None
    if include_structure:
        review_artifacts = _from_status(
            run_dir / "structure_review" / "finalized" / "00_finalization_status.json",
            "structure_review",
        )
    if not review_artifacts:
        review_artifacts = _from_status(
            run_dir / "ocr" / "finalized" / "00_finalization_status.json",
            "ocr_review",
        )
    if not review_artifacts:
        review_artifacts = ParsingArtifacts(
            source="base_run",
            parsed_document_path=run_dir / "06_parsed_document.json",
            evaluation_path=run_dir / "07_parsing_evaluation.json",
        )
    manual_status_path = run_dir / "manual_revision" / "finalized" / "00_finalization_status.json"
    manual = _from_status(manual_status_path, "manual_revision")
    if manual:
        status = json.loads(manual_status_path.read_text(encoding="utf-8-sig"))
        expected_review_path = status.get("base_review_document_path") or status.get("previous_document_path")
        if expected_review_path and Path(expected_review_path).resolve() == review_artifacts.parsed_document_path.resolve():
            return manual
    return review_artifacts


def _from_status(path: Path, source: str) -> ParsingArtifacts | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("status") != "completed":
        return None
    document = Path(str(value["parsed_document_path"]))
    evaluation = Path(str(value["evaluation_path"]))
    if not document.is_file() or not evaluation.is_file():
        raise FileNotFoundError(f"Finalized parsing artifacts are incomplete: {path}")
    return ParsingArtifacts(
        source=source,
        parsed_document_path=document,
        evaluation_path=evaluation,
        revision_id=str(value.get("finalization_id") or ""),
    )
