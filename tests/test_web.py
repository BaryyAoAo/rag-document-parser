from __future__ import annotations

from pathlib import Path
from dataclasses import fields
import json
import shutil
from threading import Event, Lock, Thread
import time

from fastapi.testclient import TestClient

from document_parser.web.app import create_app
from document_parser.ocr.providers import BaiduPaddleOcrProvider
from document_parser.ocr.review import OcrReviewStore, OcrReviewTask
from document_parser.io_utils import write_json
from document_parser.web.preview import office_preview_html


def _wait_for_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"completed", "cancelled", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError("Local parsing job did not complete")


def test_local_web_directory_job_and_result_survive_refresh(
    project_root: Path, tmp_path: Path
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    shutil.copyfile(
        project_root / "fixtures" / "markdown" / "demo_water_faq.md",
        input_dir / "faq.md",
    )
    output_root = tmp_path / "output"
    app = create_app(output_root)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        response = client.post("/api/jobs/directory", json={"path": str(input_dir), "workers": 2})
        assert response.status_code == 202
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["completed_count"] == 1
        assert job["finalized_count"] == 1
        assert Path(job["report_path"]).is_file()
        detail = client.get(f"/api/jobs/{job['job_id']}/documents/0")
        assert detail.status_code == 200
        assert detail.json()["block_count"] > 0
        assert client.get(f"/api/jobs/{job['job_id']}/source/0").status_code == 200
        assert client.post(f"/api/jobs/{job['job_id']}/retry").status_code == 400
        history = client.get("/api/history").json()
        assert len(history) == 1
        assert history[0]["job_id"] == job["job_id"]
        exported = client.get(f"/api/jobs/{job['job_id']}/documents/0/export?format=json")
        assert exported.status_code == 200
        assert exported.json()["document_id"] == job["files"][0]["document_id"]
        markdown = client.get(f"/api/jobs/{job['job_id']}/documents/0/export?format=md")
        assert markdown.status_code == 200
        assert "# " in markdown.text
        target = tmp_path / "exports"
        target.mkdir()
        request = {"entries": [{"job_id": job["job_id"], "index": 0}],
                   "target_folder": str(target), "format": "json"}
        first = client.post("/api/history/export", json=request)
        second = client.post("/api/history/export", json=request)
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["exported_count"] == 1
        assert first.json()["exported"][0]["path"] != second.json()["exported"][0]["path"]
        assert json.loads(Path(first.json()["exported"][0]["path"]).read_text(encoding="utf-8"))["document_id"] == job["files"][0]["document_id"]
    app.state.jobs.close()

    reopened = create_app(output_root)
    with TestClient(reopened) as client:
        saved = client.get(f"/api/jobs/{job['job_id']}").json()
        assert saved["status"] == "completed"
        assert saved["files"][0]["document_id"]
    reopened.state.jobs.close()


def test_local_web_upload_and_ocr_review(project_root: Path, tmp_path: Path) -> None:
    app = create_app(tmp_path / "output")
    source = project_root / "fixtures" / "pptx" / "demo_water_operations.pptx"
    with TestClient(app) as client:
        with source.open("rb") as file:
            response = client.post(
                "/api/jobs/upload",
                files={"files": (source.name, file, "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
                data={"workers": "2"},
            )
        assert response.status_code == 202
        job = _wait_for_job(client, response.json()["job_id"])
        preview = client.get(f"/api/jobs/{job['job_id']}/source/0/preview")
        assert preview.status_code == 200
        assert "第 1 页" in preview.text
        assert preview.headers["content-security-policy"].startswith("default-src 'none'")
        assert job["review_required_count"] == 1
        reviews = client.get("/api/reviews/ocr").json()
        assert reviews
        review = reviews[0]
        assert review["source_filename"] == source.name
        assert review["web_job_id"] == job["job_id"]
        assert review["metadata"]["run_dir"] == job["files"][0]["run_dir"]
        if len(reviews) == 1:
            pending = {field.name: review[field.name] for field in fields(OcrReviewTask)}
            pending["review_id"] += "_pending_second_page"
            pending["page_number"] = int(review["page_number"] or 1) + 1
            OcrReviewStore(tmp_path / "output" / "_ocr_review").create_task(OcrReviewTask(**pending))
            reviews = client.get("/api/reviews/ocr").json()
        prefix = f"/api/jobs/{job['job_id']}/documents/0"
        initial_progress = client.get(prefix).json()["ocr_progress"]
        assert initial_progress["task_count"] == len(reviews)
        assert initial_progress["applied_count"] == 0
        assert client.get(f"/api/reviews/ocr/{review['review_id']}/preview").status_code == 200
        native = client.get(f"/api/reviews/ocr/{review['review_id']}/blocks")
        assert native.status_code == 200
        assert native.json()["native_count"] >= 0
        result = client.post(
            f"/api/reviews/ocr/{review['review_id']}/decision",
            json={"action": "approve_native", "reviewer": "local_tester", "note": "fixture checked"},
        )
        assert result.status_code == 200
        assert result.json()["status"] == "decided"
        applied = client.post(f"/api/reviews/ocr/{review['review_id']}/apply")
        assert applied.status_code == 200
        detail = client.get(prefix).json()
        assert detail["ocr_progress"]["applied_count"] == 1
        assert detail["ocr_progress"]["pending_count"] == len(reviews) - 1
        assert detail["ocr_progress"]["applied_pages"] == [review["page_number"]]
        assert detail["artifact_source"] == "base_run"
    app.state.jobs.close()


def test_office_previews_show_source_content(project_root: Path) -> None:
    for relative in ("docx/demo_water_service_guide.docx", "xlsx/demo_water_statistics.xlsx"):
        html = office_preview_html(project_root / "fixtures" / relative)
        assert "<table>" in html or "<p>" in html
        assert "<h1>" in html


def test_job_list_uses_completed_ocr_revision(project_root: Path, tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    app = create_app(output_root)
    source = project_root / "fixtures" / "pptx" / "demo_water_operations.pptx"
    with TestClient(app) as client:
        with source.open("rb") as file:
            response = client.post("/api/jobs/upload", files={"files": (source.name, file)})
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["files"][0]["status"] == "review_required"
        run_dir = Path(job["files"][0]["run_dir"])
        base = json.loads((run_dir / "06_parsed_document.json").read_text(encoding="utf-8"))
        revised = json.loads(json.dumps(base))
        revised["blocks"].append({**revised["blocks"][0], "block_id": "extra_ocr_block"})
        final_dir = run_dir / "ocr" / "finalized" / "final_test"
        document_path = final_dir / "01_finalized_parsed_document.json"
        evaluation_path = final_dir / "02_finalized_evaluation.json"
        status_path = run_dir / "ocr" / "finalized" / "00_finalization_status.json"
        write_json(document_path, revised)
        write_json(evaluation_path, {
            "document_id": revised["document_id"], "status": "review_required",
            "warning_count": 4, "error_count": 0, "manual_review_items": [],
        })
        write_json(status_path, {
            "status": "completed", "finalization_id": "final_test",
            "parsed_document_path": str(document_path), "evaluation_path": str(evaluation_path),
        })
        for current in (
            client.get(f"/api/jobs/{job['job_id']}").json(),
            next(item for item in client.get("/api/jobs").json() if item["job_id"] == job["job_id"]),
        ):
            assert current["files"][0]["status"] == "finalized"
            assert current["files"][0]["artifact_source"] == "ocr_review"
            assert current["files"][0]["block_count"] == len(base["blocks"]) + 1
            assert current["files"][0]["quality_warning_count"] == 4
            assert current["finalized_count"] == 1
            assert current["review_required_count"] == 0
        assert client.get(f"/api/jobs/{job['job_id']}/documents/0").json()["file"]["status"] == "finalized"
        assert client.get(f"/api/jobs/{job['job_id']}/documents/0/export").status_code == 200
        saved = json.loads((output_root / "web_jobs" / f"{job['job_id']}.json").read_text(encoding="utf-8"))
        assert saved["files"][0]["status"] == "review_required"

        write_json(evaluation_path, {
            "document_id": revised["document_id"], "status": "review_required",
            "warning_count": 4, "error_count": 0,
            "manual_review_items": [{"review_id": "unresolved", "status": "pending"}],
        })
        assert client.get(f"/api/jobs/{job['job_id']}").json()["files"][0]["status"] == "review_required"
        assert client.get(f"/api/jobs/{job['job_id']}/documents/0/export").status_code == 409
        write_json(evaluation_path, {
            "document_id": revised["document_id"], "status": "review_required",
            "warning_count": 4, "error_count": 0, "manual_review_items": [],
        })
    app.state.jobs.close()

    reopened = create_app(output_root)
    with TestClient(reopened) as client:
        assert client.get(f"/api/jobs/{job['job_id']}").json()["files"][0]["status"] == "finalized"
    reopened.state.jobs.close()


def test_ocr_execution_appears_in_active_list(
    project_root: Path, tmp_path: Path, monkeypatch
) -> None:
    app = create_app(tmp_path / "output")
    source = project_root / "fixtures" / "pptx" / "demo_water_operations.pptx"
    started = Event()
    release = Event()

    class FakeProvider:
        provider_name = "test_provider"

        def parse_image(self, image_path: Path) -> dict:
            started.set()
            assert release.wait(timeout=5)
            return {"parse_result": {"pages": []}}

    monkeypatch.setattr(
        BaiduPaddleOcrProvider, "from_env_file",
        classmethod(lambda cls, path: FakeProvider()),
    )
    with TestClient(app) as client:
        with source.open("rb") as file:
            response = client.post("/api/jobs/upload", files={"files": (source.name, file)})
        job = _wait_for_job(client, response.json()["job_id"])
        review_id = client.get("/api/reviews/ocr").json()[0]["review_id"]
        approved = client.post(
            f"/api/reviews/ocr/{review_id}/decision",
            json={"action": "run_ocr", "reviewer": "tester"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "ocr_requested"
        responses: list = []
        thread = Thread(target=lambda: responses.append(client.post(f"/api/reviews/ocr/{review_id}/execute")))
        thread.start()
        try:
            assert started.wait(timeout=5)
            executing = client.get("/api/reviews/ocr").json()
            assert next(item for item in executing if item["review_id"] == review_id)["status"] == "executing"
            assert client.post(f"/api/reviews/ocr/{review_id}/execute").status_code == 400
        finally:
            release.set()
            thread.join(timeout=5)
        assert responses[0].status_code == 200
        assert responses[0].json()["status"] == "ocr_result_ready"
        preview = client.get(f"/api/reviews/ocr/{review_id}/blocks").json()
        assert preview["ocr_count"] == 0
    app.state.jobs.close()


def test_bulk_ocr_approval_and_background_execution(
    project_root: Path, tmp_path: Path, monkeypatch
) -> None:
    app = create_app(tmp_path / "output")
    source = project_root / "fixtures" / "pptx" / "demo_water_operations.pptx"
    call_lock = Lock()
    active_calls = 0
    peak_calls = 0

    class FakeProvider:
        provider_name = "test_provider"

        def parse_image(self, image_path: Path) -> dict:
            nonlocal active_calls, peak_calls
            with call_lock:
                active_calls += 1
                peak_calls = max(peak_calls, active_calls)
            time.sleep(0.05)
            with call_lock:
                active_calls -= 1
            return {"parse_result": {"pages": []}}

    monkeypatch.setattr(
        BaiduPaddleOcrProvider, "from_env_file",
        classmethod(lambda cls, path: FakeProvider()),
    )
    with TestClient(app) as client:
        with source.open("rb") as file:
            response = client.post("/api/jobs/upload", files={"files": (source.name, file)})
        job = _wait_for_job(client, response.json()["job_id"])
        reviews = client.get("/api/reviews/ocr").json()
        assert reviews
        if len(reviews) < 3:
            original = reviews[0]
            store = OcrReviewStore(tmp_path / "output" / "_ocr_review")
            for index in range(3 - len(reviews)):
                task = {field.name: original[field.name] for field in fields(OcrReviewTask)}
                task["review_id"] += f"_bulk_{index}"
                task["page_number"] = int(original["page_number"] or 1) + index + 1
                store.create_task(OcrReviewTask(**task))
            reviews = client.get("/api/reviews/ocr").json()
        run_dir = job["files"][0]["run_dir"]
        approved = client.post("/api/reviews/ocr/bulk/approve", json={
            "run_dir": run_dir, "reviewer": "tester", "note": "batch checked",
        })
        assert approved.status_code == 200
        assert approved.json()["approved_count"] == len(reviews)
        assert {item["status"] for item in client.get("/api/reviews/ocr").json()} == {"ocr_requested"}
        started = client.post("/api/reviews/ocr/bulk/execute", json={"run_dir": run_dir})
        assert started.status_code == 202
        execution_id = started.json()["execution_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            executions = client.get("/api/reviews/ocr/bulk/executions").json()
            execution = next(item for item in executions if item["execution_id"] == execution_id)
            if execution["status"] == "completed":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Batch OCR did not complete")
        assert execution["completed_count"] == len(reviews)
        assert execution["failed_count"] == 0
        assert peak_calls <= 2
        assert {item["status"] for item in client.get("/api/reviews/ocr").json()} == {"ocr_result_ready"}
    app.state.jobs.close()


def test_local_web_structure_review_and_effective_block_comparison(
    project_root: Path, tmp_path: Path
) -> None:
    app = create_app(tmp_path / "output")
    source = project_root / "fixtures" / "markdown" / "demo_water_faq.md"
    with TestClient(app) as client:
        response = client.post("/api/jobs/directory", json={"path": str(source)})
        job = _wait_for_job(client, response.json()["job_id"])
        run_dir = Path(job["files"][0]["run_dir"])
        document_path = run_dir / "06_parsed_document.json"
        evaluation_path = run_dir / "07_parsing_evaluation.json"
        document = json.loads(document_path.read_text(encoding="utf-8"))
        block = document["blocks"][0]
        block["quality_flags"] = ["merged_cross_page_paragraph"]
        document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        evaluation["status"] = "review_required"
        evaluation["manual_review_items"] = [{
            "review_id": "review_web_test", "review_type": "cross_page_paragraph",
            "severity": "warning", "document_id": document["document_id"],
            "page_number": 1, "object_type": "block", "object_id": block["block_id"],
            "reason": "check merge", "suggested_action": "review",
            "source_preview": block.get("text") or "", "status": "pending",
        }]
        evaluation_path.write_text(json.dumps(evaluation, ensure_ascii=False), encoding="utf-8")

        prefix = f"/api/jobs/{job['job_id']}/documents/0"
        prepared = client.post(f"{prefix}/structure/prepare")
        assert prepared.status_code == 200
        assert len(prepared.json()["tasks"]) == 1
        assert prepared.json()["tasks"][0]["status"] == "pending"
        assert client.post(f"{prefix}/structure/finalize").status_code == 400
        decision = client.post(
            f"{prefix}/structure/review_web_test/decision",
            json={"action": "correct_text", "reviewer": "tester", "note": "source checked",
                  "corrected_text": "corrected in web"},
        )
        assert decision.status_code == 200
        assert decision.json()["tasks"][0]["status"] == "decided"
        finalized = client.post(f"{prefix}/structure/finalize")
        assert finalized.status_code == 200
        assert finalized.json()["evaluation_status"] == "finalized"
        detail = client.get(prefix).json()
        assert detail["artifact_source"] == "structure_review"
        blocks = client.get(f"{prefix}/blocks", params={"block_type": block["block_type"]}).json()
        pair = next(item for item in blocks["items"] if item["base"]["block_id"] == block["block_id"])
        assert pair["base"]["text"] != pair["effective"]["text"]
        assert pair["effective"]["text"] == "corrected in web"
    app.state.jobs.close()


def test_failed_file_detail_keeps_manifest_visible(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a PDF")
    app = create_app(tmp_path / "output")
    with TestClient(app) as client:
        response = client.post("/api/jobs/directory", json={"path": str(broken)})
        job = _wait_for_job(client, response.json()["job_id"])
        assert job["failed_count"] == 1
        detail = client.get(f"/api/jobs/{job['job_id']}/documents/0")
        assert detail.status_code == 200
        assert detail.json()["manifest"]["summary"]["status"] == "failed"
        assert detail.json()["block_count"] == 0
    app.state.jobs.close()
