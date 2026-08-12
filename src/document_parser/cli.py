from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .batch import parse_directory
from .io_utils import write_json
from .models import to_plain_data
from .ocr import ALLOWED_ACTIONS, OcrReviewStore, convert_parse_result_to_blocks
from .ocr.providers import BaiduPaddleOcrProvider
from .pipeline import DocumentParsingPipeline, RuntimeParseOptions
from .review import STRUCTURE_ACTIONS, StructureReviewStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="document-parser")
    commands = parser.add_subparsers(dest="command", required=True)

    parse = commands.add_parser("parse", help="Parse one document into ParsedDocument")
    parse.add_argument("file")
    parse.add_argument("--output", default="outputs/runs")
    parse.add_argument("--source-url", default="")
    parse.add_argument("--metadata-json", default="{}")
    _add_ocr_options(parse)
    parse.set_defaults(handler=_parse_one)

    batch = commands.add_parser("batch", help="Parse every supported document in a directory")
    batch.add_argument("input_dir")
    batch.add_argument("--output", default="outputs/batch")
    batch.add_argument("--no-recursive", action="store_true")
    _add_ocr_options(batch)
    batch.set_defaults(handler=_parse_batch)

    structure = commands.add_parser("review-structure", help="Manage structure review tasks")
    structure.add_argument("run_dir")
    structure_commands = structure.add_subparsers(dest="structure_command", required=True)
    create = structure_commands.add_parser("create")
    create.set_defaults(handler=_structure_create)
    listing = structure_commands.add_parser("list")
    listing.set_defaults(handler=_structure_list)
    decide = structure_commands.add_parser("decide")
    decide.add_argument("review_id")
    decide.add_argument("--action", choices=STRUCTURE_ACTIONS, required=True)
    decide.add_argument("--reviewer", required=True)
    decide.add_argument("--note", default="")
    decide.add_argument("--corrected-text", default="")
    decide.set_defaults(handler=_structure_decide)
    finalize = structure_commands.add_parser("finalize")
    finalize.set_defaults(handler=_structure_finalize)

    ocr = commands.add_parser("review-ocr", help="Manage OCR review tasks")
    ocr.add_argument("review_root", help="The _ocr_review directory under an output root")
    ocr_commands = ocr.add_subparsers(dest="ocr_command", required=True)
    ocr_list = ocr_commands.add_parser("list")
    ocr_list.add_argument(
        "--status",
        choices=["pending", "decided", "ocr_requested", "ocr_result_ready", "applied"],
    )
    ocr_list.set_defaults(handler=_ocr_list)
    ocr_show = ocr_commands.add_parser("show")
    ocr_show.add_argument("review_id")
    ocr_show.set_defaults(handler=_ocr_show)
    ocr_decide = ocr_commands.add_parser("decide")
    ocr_decide.add_argument("review_id")
    ocr_decide.add_argument("--action", choices=ALLOWED_ACTIONS, required=True)
    ocr_decide.add_argument("--reviewer", required=True)
    ocr_decide.add_argument("--note", default="")
    ocr_decide.add_argument("--native-block-ids", default="")
    ocr_decide.add_argument("--ocr-block-ids", default="")
    ocr_decide.set_defaults(handler=_ocr_decide)
    ocr_execute = ocr_commands.add_parser("execute")
    ocr_execute.add_argument("review_id")
    ocr_execute.add_argument("--env-file", default=".env")
    ocr_execute.set_defaults(handler=_ocr_execute)
    ocr_apply = ocr_commands.add_parser("apply")
    ocr_apply.add_argument("review_id")
    ocr_apply.set_defaults(handler=_ocr_apply)
    ocr_finalize = ocr_commands.add_parser("finalize")
    ocr_finalize.add_argument("run_dir")
    ocr_finalize.set_defaults(handler=_ocr_finalize)
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    result = args.handler(args)
    print(json.dumps(to_plain_data(result), ensure_ascii=False, indent=2))


def _pipeline(args: argparse.Namespace) -> DocumentParsingPipeline:
    provider = None
    if args.execute_auto_ocr:
        provider = BaiduPaddleOcrProvider.from_env_file(Path(args.env_file))
    return DocumentParsingPipeline(
        output_root=Path(args.output),
        options=RuntimeParseOptions(execute_auto_ocr=args.execute_auto_ocr),
        ocr_provider=provider,
    )


def _parse_one(args: argparse.Namespace) -> Any:
    metadata = json.loads(args.metadata_json)
    if not isinstance(metadata, dict):
        raise ValueError("--metadata-json must contain a JSON object")
    return _pipeline(args).parse(Path(args.file), metadata, args.source_url)


def _parse_batch(args: argparse.Namespace) -> Any:
    return parse_directory(_pipeline(args), Path(args.input_dir), not args.no_recursive)


def _structure_store(args: argparse.Namespace) -> StructureReviewStore:
    return StructureReviewStore(Path(args.run_dir))


def _structure_create(args: argparse.Namespace) -> Any:
    return _structure_store(args).create_from_run()


def _structure_list(args: argparse.Namespace) -> Any:
    return _structure_store(args).list_states()


def _structure_decide(args: argparse.Namespace) -> Any:
    return _structure_store(args).record_decision(
        args.review_id,
        args.action,
        args.reviewer,
        args.note,
        args.corrected_text,
    )


def _structure_finalize(args: argparse.Namespace) -> Any:
    return _structure_store(args).finalize()


def _ocr_store(args: argparse.Namespace) -> OcrReviewStore:
    return OcrReviewStore(Path(args.review_root))


def _ocr_list(args: argparse.Namespace) -> Any:
    return _ocr_store(args).list_task_states(args.status)


def _ocr_show(args: argparse.Namespace) -> Any:
    state = _ocr_store(args).get_task_state(args.review_id)
    return {
        "task": state,
        "native_block_preview": _block_preview(
            Path(state["native_blocks_path"]),
            state.get("page_number"),
            list(state.get("target_block_ids") or []),
        ),
        "ocr_block_preview": (
            _block_preview(Path(state["ocr_blocks_path"]), None, [])
            if state.get("ocr_blocks_path")
            else []
        ),
    }


def _ocr_decide(args: argparse.Namespace) -> Any:
    return _ocr_store(args).record_decision(
        review_id=args.review_id,
        action=args.action,
        reviewer=args.reviewer,
        note=args.note,
        native_block_ids=_csv_values(args.native_block_ids),
        ocr_block_ids=_csv_values(args.ocr_block_ids),
    )


def _ocr_execute(args: argparse.Namespace) -> Any:
    store = _ocr_store(args)
    state = store.get_task_state(args.review_id)
    if state["status"] != "ocr_requested":
        raise ValueError(f"Task is not waiting for OCR execution: {state['status']}")
    preview_path = Path(state["source_preview_path"])
    if not preview_path.is_file():
        raise FileNotFoundError(f"OCR source image is not a local file: {preview_path}")
    provider = BaiduPaddleOcrProvider.from_env_file(Path(args.env_file))
    decision_id = state["latest_decision"]["decision_id"]
    attempt_dir = store.root / "ocr_attempts" / args.review_id / decision_id
    provider_path = attempt_dir / "01_provider_result.json"
    blocks_path = attempt_dir / "02_ocr_blocks.json"
    provider_result = provider.parse_image(preview_path)
    write_json(provider_path, provider_result)
    blocks = convert_parse_result_to_blocks(
        provider_result["parse_result"],
        document_id=f"{state['document_id']}_ocr_{args.review_id[-12:]}",
    )
    write_json(blocks_path, blocks)
    result = store.record_ocr_result(
        review_id=args.review_id,
        provider=provider.provider_name,
        provider_result_path=provider_path,
        ocr_blocks_path=blocks_path,
    )
    return {**result, "task_state": store.get_task_state(args.review_id)["status"]}


def _ocr_apply(args: argparse.Namespace) -> Any:
    return _ocr_store(args).apply(args.review_id)


def _ocr_finalize(args: argparse.Namespace) -> Any:
    return _ocr_store(args).finalize_run(Path(args.run_dir))


def _block_preview(
    path: Path,
    page_number: int | None,
    target_block_ids: list[str],
) -> list[dict[str, str]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    blocks = value.get("blocks", []) if isinstance(value, dict) else value
    if target_block_ids:
        selected = set(target_block_ids)
        blocks = [block for block in blocks if str(block.get("block_id")) in selected]
    elif page_number is not None:
        blocks = [
            block
            for block in blocks
            if int(block.get("page_start") or 0)
            <= int(page_number)
            <= int(block.get("page_end") or 0)
        ]
    return [
        {
            "block_id": str(block.get("block_id") or ""),
            "block_type": str(block.get("block_type") or ""),
            "preview": _block_text(block)[:240],
        }
        for block in blocks[:30]
    ]


def _block_text(block: dict[str, Any]) -> str:
    if block.get("text"):
        return " ".join(str(block["text"]).split())
    table = block.get("table_data") or {}
    if table:
        return " ".join(
            value
            for value in [
                str(table.get("caption") or ""),
                " | ".join(table.get("headers") or []),
            ]
            if value
        )
    image = block.get("image") or {}
    return str(image.get("caption") or image.get("ocr_text") or "[image]")


def _csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _add_ocr_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execute-auto-ocr", action="store_true")
    parser.add_argument("--env-file", default=".env")
