from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import Block


DECORATIVE_NAME_TOKENS = {
    "logo",
    "icon",
    "avatar",
    "button",
    "btn",
    "qrcode",
    "qr_code",
    "spacer",
    "banner",
}


@dataclass
class ImageOcrDecision:
    decision: str
    reason_codes: list[str]
    reason: str


class ImageOcrPolicy:
    def __init__(self, small_side_threshold: float = 80, small_area_threshold: float = 16_384) -> None:
        self.small_side_threshold = small_side_threshold
        self.small_area_threshold = small_area_threshold

    def decide(self, block: Block) -> ImageOcrDecision:
        image = block.image
        if image is None:
            return ImageOcrDecision("skip_ocr", ["NO_IMAGE_DATA"], "Block 没有图片数据。")
        declared = str(block.metadata.get("ocr_decision") or "").strip()
        if declared == "skip_ocr":
            return ImageOcrDecision("skip_ocr", ["PARSER_DECLARED_SKIP"], "解析器已标记无需 OCR。")

        name = Path(str(image.image_path or "")).name.lower()
        if any(token in name for token in DECORATIVE_NAME_TOKENS):
            return ImageOcrDecision(
                "skip_ocr",
                ["DECORATIVE_IMAGE_NAME"],
                "文件名显示该图片更可能是 Logo、图标、按钮或二维码。",
            )

        width = float(image.width or 0)
        height = float(image.height or 0)
        if width > 0 and height > 0 and (
            min(width, height) <= self.small_side_threshold
            or width * height <= self.small_area_threshold
        ):
            return ImageOcrDecision(
                "skip_ocr",
                ["SMALL_DECORATIVE_IMAGE"],
                "图片尺寸较小，更可能是图标或装饰资源。",
            )

        reason_codes = list(block.metadata.get("ocr_reason_codes") or ["CONTENT_IMAGE_REVIEW"])
        reason = str(block.metadata.get("ocr_reason") or "图片可能包含业务文字或结构信息。")
        return ImageOcrDecision("review_before_ocr", reason_codes, reason)
