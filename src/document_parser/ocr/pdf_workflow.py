from __future__ import annotations

from pathlib import Path
from typing import Any
from collections.abc import Callable

import pypdfium2 as pdfium

from ..io_utils import write_json
from ..models import ParsedDocument
from ..ocr_policy import OCR_CANDIDATE_DECISIONS
from .review import OcrReviewStore
from .workflow import OcrCandidate, OcrProvider, OcrReviewWorkflow, OcrWorkflowResult


PdfOcrWorkflowResult = OcrWorkflowResult


class PdfOcrReviewWorkflow:
    def __init__(
        self,
        review_store: OcrReviewStore,
        execute_auto_ocr: bool = False,
        provider: OcrProvider | None = None,
        render_dpi: int = 144,
        renderer: Callable[[Path, int, Path, int], Path] | None = None,
    ) -> None:
        self.review_store = review_store
        self.execute_auto_ocr = execute_auto_ocr
        self.provider = provider
        self.render_dpi = render_dpi
        self.renderer = renderer or render_pdf_page
        self.workflow = OcrReviewWorkflow(
            review_store=review_store,
            execute_auto_ocr=execute_auto_ocr,
            provider=provider,
        )

    def run(
        self,
        document: ParsedDocument,
        raw_outputs: list[dict[str, Any]],
        parsed_document_path: Path,
        run_dir: Path,
    ) -> PdfOcrWorkflowResult:
        ocr_dir = run_dir / "ocr"
        renders_dir = ocr_dir / "04_page_renders"
        candidate_outputs = [
            item
            for item in raw_outputs
            if (item.get("ocr_decision") or {}).get("decision") in OCR_CANDIDATE_DECISIONS
        ]
        write_json(
            ocr_dir / "01_page_quality_features.json",
            [
                {
                    "page_number": item.get("page_number"),
                    **dict(item.get("page_quality") or {}),
                }
                for item in raw_outputs
            ],
        )
        write_json(
            ocr_dir / "02_pre_ocr_decisions.json",
            [
                {
                    "page_number": item.get("page_number"),
                    **dict(item.get("ocr_decision") or {}),
                }
                for item in raw_outputs
            ],
        )

        candidates: list[OcrCandidate] = []
        for item in candidate_outputs:
            page_number = int(item["page_number"])
            decision = dict(item["ocr_decision"])
            preview_path = renders_dir / f"page_{page_number:04d}.png"
            self.renderer(Path(document.source_path), page_number, preview_path, self.render_dpi)
            candidates.append(
                OcrCandidate(
                    candidate_id=f"p{page_number:04d}",
                    scope="page",
                    object_type="page",
                    object_id=f"page_{page_number:04d}",
                    decision=str(decision["decision"]),
                    reason_codes=list(decision.get("reason_codes") or []),
                    reason=str(decision.get("reason", "")),
                    preview_path=str(preview_path),
                    page_number=page_number,
                    metadata={"ocr_decision_details": decision},
                )
            )
        return self.workflow.run_candidates(
            document=document,
            parsed_document_path=parsed_document_path,
            run_dir=run_dir,
            candidates=candidates,
            summary_context={"page_count": len(raw_outputs), "scope": "pdf_page"},
        )


def render_pdf_page(pdf_path: Path, page_number: int, output_path: Path, dpi: int = 144) -> Path:
    if page_number < 1:
        raise ValueError("page_number must be >= 1")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_number > len(document):
            raise IndexError(f"PDF page {page_number} exceeds page count {len(document)}")
        page = document[page_number - 1]
        try:
            image = page.render(scale=dpi / 72.0).to_pil()
            image.save(output_path, format="PNG")
        finally:
            page.close()
    finally:
        document.close()
    return output_path
