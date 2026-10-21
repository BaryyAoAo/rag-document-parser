from __future__ import annotations

import csv
import json
from pathlib import Path
import shutil
from threading import Barrier, Event
from types import SimpleNamespace

from document_parser.batch import parse_directory
from document_parser.ocr import OcrReviewStore
from document_parser.pipeline import DocumentParsingPipeline


def test_batch_cancel_before_scheduling_records_every_file(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("a.md", "b.md"):
        (input_dir / name).write_text("# sample", encoding="utf-8")
    cancel = Event()
    events: list[str] = []

    def progress(event: dict) -> None:
        events.append(event["type"])
        if event["type"] == "batch_started":
            cancel.set()

    report = parse_directory(
        DocumentParsingPipeline(tmp_path / "output"), input_dir,
        workers=2, progress=progress, cancel_event=cancel,
    )

    assert report["cancelled_count"] == 2
    assert report["finalized_count"] == 0
    assert [item["status"] for item in report["results"]] == ["cancelled", "cancelled"]
    assert events == ["batch_started", "file_completed", "file_completed", "batch_finished"]


def test_batch_runs_two_documents_at_the_same_time(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("a.md", "b.md"):
        (input_dir / name).write_text("# sample", encoding="utf-8")
    rendezvous = Barrier(2, timeout=3)

    class ConcurrentPipeline:
        output_root = tmp_path / "output"

        def new_worker(self) -> "ConcurrentPipeline":
            return self

        def parse(self, path: Path) -> SimpleNamespace:
            rendezvous.wait()
            return SimpleNamespace(
                status="finalized",
                parsed_document=SimpleNamespace(document_id=path.stem, blocks=[]),
                run_dir=str(tmp_path / path.stem),
            )

    report = parse_directory(ConcurrentPipeline(), input_dir, workers=2)
    assert report["finalized_count"] == 2


def test_parallel_batch_keeps_same_name_runs_separate_and_records_failures(
    project_root: Path, tmp_path: Path
) -> None:
    input_dir = tmp_path / "input"
    source = project_root / "fixtures" / "markdown" / "demo_water_faq.md"
    for folder in ("a", "b"):
        target = input_dir / folder / "same.md"
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
    (input_dir / "broken.pdf").write_bytes(b"not a PDF")
    output_root = tmp_path / "output"
    pipeline = DocumentParsingPipeline(output_root)

    report = parse_directory(pipeline, input_dir, workers=3)

    assert report["file_count"] == 3
    assert report["failed_count"] == 1
    assert [Path(item["source_path"]).name for item in report["results"]] == [
        "same.md", "same.md", "broken.pdf"
    ]
    successful = [item for item in report["results"] if item["status"] == "finalized"]
    assert len(successful) == 2
    assert len({item["run_dir"] for item in successful}) == 2
    failed = next(item for item in report["results"] if item["status"] == "failed")
    manifest = json.loads((Path(failed["run_dir"]) / "00_run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["status"] == "failed"
    assert manifest["summary"]["error_type"]
    assert Path(report["report_path"]).is_file()
    assert len((Path(report["report_path"]).parent / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 3

    next_report = parse_directory(pipeline, input_dir, workers=1)
    assert next_report["report_path"] != report["report_path"]
    assert Path(report["report_path"]).is_file()


def test_parallel_ocr_review_queue_keeps_every_task(project_root: Path, tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    source = project_root / "fixtures" / "pptx" / "demo_water_operations.pptx"
    for folder in ("a", "b"):
        target = input_dir / folder / "same.pptx"
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
    output_root = tmp_path / "output"

    report = parse_directory(DocumentParsingPipeline(output_root), input_dir, workers=2)

    assert report["failed_count"] == 0
    store = OcrReviewStore(output_root / "_ocr_review")
    states = store.list_task_states()
    with store.queue_path.open(encoding="utf-8-sig", newline="") as source_file:
        queue = list(csv.DictReader(source_file))
    assert len(states) >= 2
    assert {item["review_id"] for item in states} == {item["review_id"] for item in queue}
    assert len({item["metadata"]["run_dir"] for item in states}) == 2
