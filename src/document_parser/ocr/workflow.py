from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any, Protocol

from ..io_utils import write_json
from ..models import ManualReviewItem, ParsedDocument, to_plain_data
from .baidu_paddleocr import convert_parse_result_to_blocks
from .review import OcrReviewStore, OcrReviewTask


class OcrProvider(Protocol):
    provider_name: str

    def parse_image(self, image_path: Path) -> dict[str, Any]: ...


@dataclass
class OcrCandidate:
    candidate_id: str
    scope: str
    object_type: str
    object_id: str
    decision: str
    reason_codes: list[str]
    reason: str
    preview_path: str
    page_number: int | None = None
    target_block_ids: list[str] = field(default_factory=list)
    bbox: list[float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OcrWorkflowResult:
    manual_reviews: list[ManualReviewItem] = field(default_factory=list)
    review_task_ids: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


class OcrReviewWorkflow:
    """File-type-neutral OCR execution, review task creation and audit output."""

    def __init__(
        self,
        review_store: OcrReviewStore,
        execute_auto_ocr: bool = False,
        provider: OcrProvider | None = None,
    ) -> None:
        self.review_store = review_store
        self.execute_auto_ocr = execute_auto_ocr
        self.provider = provider

    def run_candidates(
        self,
        document: ParsedDocument,
        parsed_document_path: Path,
        run_dir: Path,
        candidates: list[OcrCandidate],
        summary_context: dict[str, Any] | None = None,
    ) -> OcrWorkflowResult:
        ocr_dir = run_dir / "ocr"
        provider_dir = ocr_dir / "05_provider_results"
        blocks_dir = ocr_dir / "06_ocr_blocks"
        reviews: list[ManualReviewItem] = []
        task_ids: list[str] = []
        executed_count = 0
        execution_failures = 0

        for candidate in candidates:
            preview_path = Path(candidate.preview_path)
            ocr_blocks_path = ""
            stage = "pre_ocr"
            suggested_action = "run_ocr" if candidate.decision == "auto_ocr" else "approve_native"
            allowed_actions = ["run_ocr", "approve_native", "exclude_page", "reject_document"]
            reason_codes = list(candidate.reason_codes)
            reason = candidate.reason

            if candidate.decision == "auto_ocr" and self.execute_auto_ocr:
                if self.provider is None:
                    raise RuntimeError("execute_auto_ocr requires an OCR provider")
                if not preview_path.is_file():
                    execution_failures += 1
                    reason_codes.append("OCR_ASSET_NOT_AVAILABLE")
                    reason += " OCR 资源尚未保存为本地文件。"
                else:
                    try:
                        provider_result = self.provider.parse_image(preview_path)
                        parse_result = provider_result["parse_result"]
                        provider_path = provider_dir / f"{candidate.candidate_id}.json"
                        ocr_blocks_file = blocks_dir / f"{candidate.candidate_id}.json"
                        write_json(provider_path, provider_result)
                        blocks = convert_parse_result_to_blocks(
                            parse_result,
                            document_id=f"{document.document_id}_ocr_{candidate.candidate_id}",
                        )
                        write_json(ocr_blocks_file, to_plain_data(blocks))
                        ocr_blocks_path = str(ocr_blocks_file)
                        stage = "post_ocr"
                        suggested_action = "approve_ocr"
                        allowed_actions = [
                            "approve_native",
                            "approve_ocr",
                            "merge_results",
                            "retry_ocr",
                            "exclude_page",
                            "reject_document",
                        ]
                        executed_count += 1
                    except Exception as error:
                        execution_failures += 1
                        reason_codes.append("OCR_EXECUTION_FAILED")
                        reason += f" OCR 执行失败：{type(error).__name__}。"

            review_id = self._review_id(document.document_id, candidate.candidate_id, run_dir.name)
            task = OcrReviewTask(
                review_id=review_id,
                review_stage=stage,
                document_id=document.document_id,
                page_number=candidate.page_number,
                reason_codes=reason_codes,
                reason=reason,
                suggested_action=suggested_action,
                source_preview_path=candidate.preview_path,
                native_blocks_path=str(parsed_document_path),
                ocr_blocks_path=ocr_blocks_path,
                scope=candidate.scope,
                bbox=candidate.bbox,
                target_block_ids=list(candidate.target_block_ids),
                allowed_actions=allowed_actions,
                metadata={
                    "run_dir": str(run_dir),
                    "candidate_id": candidate.candidate_id,
                    "file_type": document.file_type,
                    "ocr_decision": candidate.decision,
                    "provider": getattr(self.provider, "provider_name", None),
                    **candidate.metadata,
                },
            )
            self.review_store.create_task(task)
            task_ids.append(review_id)
            reviews.append(
                ManualReviewItem(
                    review_id=review_id,
                    review_type="ocr",
                    severity="warning",
                    document_id=document.document_id,
                    page_number=candidate.page_number,
                    object_type=candidate.object_type,
                    object_id=candidate.object_id,
                    reason=reason,
                    suggested_action=(
                        f"执行 `{suggested_action}`；使用 review_ocr.py 查看证据并记录决定。"
                    ),
                    source_preview=candidate.preview_path,
                )
            )

        summary = {
            **dict(summary_context or {}),
            "candidate_count": len(candidates),
            "review_task_count": len(task_ids),
            "auto_ocr_execution_enabled": self.execute_auto_ocr,
            "auto_ocr_executed_count": executed_count,
            "auto_ocr_failure_count": execution_failures,
            "review_task_ids": task_ids,
        }
        write_json(ocr_dir / "03_review_tasks.json", [to_plain_data(item) for item in reviews])
        write_json(ocr_dir / "10_ocr_summary.json", summary)
        return OcrWorkflowResult(reviews, task_ids, summary)

    def _review_id(self, document_id: str, candidate_id: str, run_id: str) -> str:
        digest = hashlib.sha1(f"{run_id}|{candidate_id}".encode("utf-8")).hexdigest()[:8]
        return f"ocr_review_{document_id}_{candidate_id}_{digest}"
