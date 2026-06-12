import asyncio
import os
import re
from pathlib import Path

import pytest

from app.config import Settings
from app.models import JobState
from app.pipeline import orchestrator
from app import runtime_config
from app.storage import Storage


REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT_TEST_PDF = REPO_ROOT / "test.pdf"
RUN_ROOT_TEST_PDF_GATE_ENV = "RUN_ROOT_TEST_PDF_ACCEPTANCE_GATE"


def _write_digital_pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page(width=360, height=420)
    page.insert_textbox(
        fitz.Rect(36, 36, 324, 70),
        "A Digital Paper Fixture",
        fontsize=16,
    )
    page.insert_textbox(
        fitz.Rect(36, 88, 324, 126),
        "Abstract This paper studies local PDF translation [1].",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(36, 144, 324, 194),
        "1 Introduction The method preserves Eq. 1 and references Smith et al. 2024.",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(36, 212, 324, 246),
        "E = mc^2",
        fontsize=10,
    )
    document.save(path)
    document.close()


def _crop_pdf_pages(source_path: Path, output_path: Path, page_count: int) -> None:
    import fitz

    source = fitz.open(source_path)
    try:
        if source.page_count < page_count:
            pytest.skip(
                f"{source_path.name} has {source.page_count} pages; "
                f"{page_count} pages are required for the acceptance gate"
            )
        cropped = fitz.open()
        try:
            cropped.insert_pdf(source, from_page=0, to_page=page_count - 1)
            cropped.save(output_path)
        finally:
            cropped.close()
    finally:
        source.close()


def _assert_no_preview_formula_failures(html: str) -> None:
    forbidden_fragments = [
        "@@FORMULA_",
        "{{formula:",
        "\ufffd",
        "¼",
        "þ",
        "ð",
        "quality-formula-render-failed",
        "quality-formula-missing-latex",
        "quality-unresolved-formula-placeholder",
        "color: red",
        "color:red",
        "#ff0000",
        "rgb(255, 0, 0)",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in html
    assert 'class="formula-render-failed' not in html
    assert " formula-render-failed" not in html
    forbidden_patterns = [
        r"(?<![A-Za-z])=k2(?![A-Za-z])",
        r"\bf\s*0\s*s\b",
        r"\bf\s*0\s*n\b",
        r"\\partial\s+fs\s*=\s*k2",
    ]
    for pattern in forbidden_patterns:
        assert re.search(pattern, html) is None, pattern
    assert re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", html) is None


def test_root_test_pdf_first_four_pages_deterministic_acceptance_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.getenv(RUN_ROOT_TEST_PDF_GATE_ENV) != "1":
        pytest.skip(
            f"set {RUN_ROOT_TEST_PDF_GATE_ENV}=1 to run the root test.pdf "
            "first-four-pages acceptance gate"
        )
    if not ROOT_TEST_PDF.exists():
        pytest.skip(f"root acceptance fixture is missing: {ROOT_TEST_PDF}")

    storage = Storage(tmp_path / "storage")
    cropped_pdf = tmp_path / "test-pages-1-4.pdf"
    _crop_pdf_pages(ROOT_TEST_PDF, cropped_pdf, page_count=4)

    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    monkeypatch.setattr(orchestrator, "storage", storage)
    no_provider_config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(runtime_config, "settings", no_provider_config)
    storage.write_runtime_config(
        {
            "openai_api_key": "",
            "translation_concurrency": 2,
            "translator_max_attempts": 2,
            "agent_enable_vision_analysis": False,
            "ocr_provider_order": ["deterministic"],
        }
    )

    asyncio.run(
        orchestrator.process_document_job(
            "job_test_pdf_first_four_pages",
            "doc_test_pdf_first_four_pages",
            cropped_pdf.name,
            cropped_pdf,
            "zh-CN",
        )
    )

    status = storage.load_status("job_test_pdf_first_four_pages")
    formula_diagnostics = storage.read_output_json(
        "doc_test_pdf_first_four_pages",
        "formula-diagnostics.json",
    )
    renderer_diagnostics = storage.read_output_json(
        "doc_test_pdf_first_four_pages",
        "renderer-diagnostics.json",
    )
    render_evaluation = storage.read_output_json(
        "doc_test_pdf_first_four_pages",
        "render-evaluation.json",
    )
    html = storage.preview_html_path("doc_test_pdf_first_four_pages").read_text(
        encoding="utf-8"
    )
    translated_pdf = storage.output_pdf_path("doc_test_pdf_first_four_pages")

    assert status.status == JobState.COMPLETED
    assert formula_diagnostics.get("candidate_count", 0) > 0, formula_diagnostics
    assert formula_diagnostics.get("latex_success_count", 0) > 0, formula_diagnostics
    assert formula_diagnostics["unresolved_placeholders"] == []

    renderer_quality_counts = renderer_diagnostics["quality_flag_counts"]
    assert renderer_diagnostics["layout_issues"] == []
    assert renderer_diagnostics["unresolved_formula_placeholders"] == []
    assert renderer_diagnostics["formula_rendered_count"] > 0
    for blocking_flag in (
        "formula_render_failed",
        "formula_missing_latex",
        "missing_translation",
    ):
        assert renderer_quality_counts.get(blocking_flag, 0) == 0

    assert render_evaluation["accepted"] is True
    assert render_evaluation["blocking_flags"] == {}

    _assert_no_preview_formula_failures(html)
    assert translated_pdf.exists()
    assert translated_pdf.stat().st_size > 0


def test_local_pipeline_runs_digital_pdf_to_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    no_provider_config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(runtime_config, "settings", no_provider_config)
    storage.write_runtime_config(
        {
            "openai_api_key": "",
            "translation_concurrency": 2,
            "translator_max_attempts": 2,
        }
    )
    pdf_path = storage.uploads / "doc_1.pdf"
    _write_digital_pdf(pdf_path)

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "【译】" in html
        output_path.write_bytes(b"%PDF-1.7\n% local pipeline fixture\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "fixture.pdf",
            pdf_path,
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    document_ir = storage.load_document_ir("doc_1")
    chunks = storage.read_output_json("doc_1", "translation-chunks.json")
    plans = storage.read_output_json("doc_1", "translation-plans.json")
    progress = storage.read_output_json("doc_1", "translation-progress.json")
    parser_diagnostics = storage.read_output_json("doc_1", "parser-diagnostics.json")
    renderer_diagnostics = storage.read_output_json("doc_1", "renderer-diagnostics.json")
    layout_trace = storage.read_output_json("doc_1", "layout-trace.json")
    workflow_run = storage.read_output_json("doc_1", "workflow-run.json")
    normalized_input = storage.read_output_json("doc_1", "normalized-input.json")
    user_intent = storage.read_output_json("doc_1", "user-intent.json")
    layout_intent_plan = storage.read_output_json("doc_1", "layout-intent-plan.json")
    render_evaluation = storage.read_output_json("doc_1", "render-evaluation.json")
    validation_and_repair = storage.read_output_json("doc_1", "validation-and-repair.json")
    formula_recognition = storage.read_output_json("doc_1", "formula-recognition.json")
    formula_diagnostics = storage.read_output_json("doc_1", "formula-diagnostics.json")
    html = storage.preview_html_path("doc_1").read_text(encoding="utf-8")

    source_block_ids = {
        block.block_id
        for page in document_ir.pages
        for block in page.blocks
        if block.source_text.strip()
    }
    planned_block_ids = {
        block["source_block_id"]
        for plan in plans
        for block in plan["blocks"]
    }
    preserve_tokens = {
        token
        for chunk in chunks
        for block in chunk["source_blocks"]
        for token in block["preserve_tokens"]
    }

    assert status.status == JobState.COMPLETED
    assert status.progress == 1
    assert status.chunks
    assert all(chunk.status == "completed" for chunk in status.chunks)
    assert document_ir.doc_id == "doc_1"
    assert len(source_block_ids) >= 2
    assert source_block_ids == planned_block_ids
    assert "[1]" in preserve_tokens
    assert any("mock_translation" in block["quality_flags"] for plan in plans for block in plan["blocks"])
    assert parser_diagnostics["kind"] == "parser_diagnostics"
    assert parser_diagnostics["text_block_count"] == len(source_block_ids)
    assert renderer_diagnostics["doc_id"] == "doc_1"
    assert renderer_diagnostics["page_count"] >= 1
    assert layout_trace["kind"] == "layout_trace"
    assert layout_trace["layout_mode"] == "continuous_reflow"
    assert layout_trace["output"]["page_count"] >= 1
    assert isinstance(formula_recognition, list)
    assert formula_diagnostics["kind"] == "formula_diagnostics"
    assert workflow_run["status"] == "completed"
    assert {step["name"] for step in workflow_run["steps"]} >= {
        "read_input",
        "analyze_intent",
        "build_plan",
        "validate_plan",
        "translate",
        "render",
        "evaluate_render",
        "complete",
    }
    assert normalized_input["kind"] == "normalized_input"
    assert user_intent["target_lang"] == "zh-CN"
    assert layout_intent_plan["doc_id"] == "doc_1"
    assert render_evaluation["kind"] == "render_evaluation"
    assert validation_and_repair["layout_intent_plan"]["status"] == "valid"
    assert len(progress) == len(chunks)
    assert "【译】" in html
    assert storage.output_pdf_path("doc_1").read_bytes().startswith(b"%PDF-")
