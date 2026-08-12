from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..io_utils import write_json
from ..models import ParsedDocument
from ..ocr_policy import OCR_CANDIDATE_DECISIONS
from .assets import ImageAssetMaterializer
from .image_policy import ImageOcrPolicy
from .workflow import OcrCandidate, OcrReviewWorkflow, OcrWorkflowResult


class BlockImageOcrWorkflow:
    def __init__(
        self,
        workflow: OcrReviewWorkflow,
        materializer: ImageAssetMaterializer | None = None,
        policy: ImageOcrPolicy | None = None,
    ) -> None:
        self.workflow = workflow
        self.materializer = materializer or ImageAssetMaterializer()
        self.policy = policy or ImageOcrPolicy()

    def run(
        self,
        document: ParsedDocument,
        raw_outputs: list[dict[str, Any]],
        parsed_document_path: Path,
        run_dir: Path,
    ) -> OcrWorkflowResult:
        del raw_outputs
        assets_dir = run_dir / "ocr" / "04_image_assets"
        image_blocks = [block for block in document.blocks if block.block_type == "image" and block.image]
        candidates: list[OcrCandidate] = []
        asset_records = []

        for block in image_blocks:
            policy_decision = self.policy.decide(block)
            decision = policy_decision.decision
            if decision not in OCR_CANDIDATE_DECISIONS:
                asset_records.append(
                    {
                        "block_id": block.block_id,
                        "image_id": block.image.image_id,
                        "decision": decision,
                        "asset_status": "not_materialized",
                        "asset_path": "",
                        "reason_code": ",".join(policy_decision.reason_codes),
                        "reason": policy_decision.reason,
                    }
                )
                continue
            materialized = self.materializer.materialize(document, block, assets_dir)
            reason_codes = list(policy_decision.reason_codes)
            reason = policy_decision.reason
            if materialized.reason_code:
                reason_codes.append(materialized.reason_code)
                reason += " " + materialized.reason
            preview = materialized.path or str(block.image.image_path or document.source_path)
            candidate_id = "img_" + hashlib.sha1(block.block_id.encode("utf-8")).hexdigest()[:12]
            candidates.append(
                OcrCandidate(
                    candidate_id=candidate_id,
                    scope="image",
                    object_type="block",
                    object_id=block.block_id,
                    decision=decision,
                    reason_codes=reason_codes,
                    reason=reason,
                    preview_path=preview,
                    page_number=block.page_start,
                    target_block_ids=[block.block_id],
                    bbox=block.bbox,
                    metadata={
                        "asset_status": materialized.status,
                        "asset_metadata": materialized.metadata or {},
                    },
                )
            )
            asset_records.append(
                {
                    "block_id": block.block_id,
                    "image_id": block.image.image_id,
                    "decision": decision,
                    "asset_status": materialized.status,
                    "asset_path": materialized.path,
                    "reason_code": materialized.reason_code,
                    "reason": materialized.reason,
                }
            )

        write_json(run_dir / "ocr" / "01_image_asset_features.json", asset_records)
        write_json(
            run_dir / "ocr" / "02_pre_ocr_decisions.json",
            [
                {
                    "candidate_id": item.candidate_id,
                    "scope": item.scope,
                    "target_block_ids": item.target_block_ids,
                    "decision": item.decision,
                    "reason_codes": item.reason_codes,
                    "reason": item.reason,
                }
                for item in candidates
            ],
        )
        return self.workflow.run_candidates(
            document=document,
            parsed_document_path=parsed_document_path,
            run_dir=run_dir,
            candidates=candidates,
            summary_context={
                "image_block_count": len(image_blocks),
                "scope": "image_block",
            },
        )
