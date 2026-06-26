import asyncio
from html.parser import HTMLParser
import os
import re
from pathlib import Path

import pytest
import pdf_renderer.models as renderer_models

from app.config import Settings
from app.models import JobState
from app.pipeline import orchestrator
from app.pipeline.workflow import coerce_user_intent, validate_translation_plan_formula_refs
from app import runtime_config
from app.storage import Storage
from pdf_translator_schema import (
    BlockRole,
    BoundingBox,
    DocumentBlock,
    DocumentIR,
    DocumentPage,
    FormulaIR,
    PageSize,
    SourceBlock,
    TranslationBlockPlan,
    TranslationChunk,
    TranslationLayoutPlan,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT_TEST_PDF = REPO_ROOT / "test.pdf"
RUN_ROOT_TEST_PDF_GATE_ENV = "RUN_ROOT_TEST_PDF_ACCEPTANCE_GATE"


def test_translation_formula_ref_diagnostics_blocks_unknown_refs_but_allows_raw_tex() -> None:
    formula_token = "{{formula:f_known}}"
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=100, height=100),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=0, y0=0, x1=100, y1=20),
                        reading_order=0,
                        source_text=f"Energy {formula_token}.",
                    )
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="f_known",
                page_id="p1",
                anchor_block_id="b1",
                latex="E = mc^2",
                display_mode="inline",
            )
        ],
    )
    chunk = TranslationChunk(
        chunk_id="chunk_1",
        source_blocks=[
            SourceBlock(
                block_id="b1",
                role=BlockRole.PARAGRAPH,
                source_text=f"Energy {formula_token}.",
                preserve_tokens=[formula_token],
            )
        ],
    )
    plan = TranslationLayoutPlan(
        chunk_id="chunk_1",
        blocks=[
            TranslationBlockPlan(
                source_block_id="b1",
                translated_text=rf"能量 {formula_token} 且 $t\geq 0$。",
                role=BlockRole.PARAGRAPH,
            )
        ],
    )
    bad_plan = TranslationLayoutPlan(
        chunk_id="chunk_1",
        blocks=[
            TranslationBlockPlan(
                source_block_id="b1",
                translated_text="能量 {{formula:f_missing}}。",
                role=BlockRole.PARAGRAPH,
            )
        ],
    )

    diagnostics = validate_translation_plan_formula_refs(
        document=document,
        chunks=[chunk],
        plans=[plan],
    )
    bad_diagnostics = validate_translation_plan_formula_refs(
        document=document,
        chunks=[chunk],
        plans=[bad_plan],
    )

    assert diagnostics["status"] == "valid"
    assert diagnostics["raw_tex_detected_count"] == 1
    assert "raw_tex_detected" in diagnostics["quality_flags"]
    assert bad_diagnostics["status"] == "invalid"
    assert bad_diagnostics["unknown_formula_ref_count"] == 1


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


def _write_formula_smoke_pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page(width=420, height=520)
    page.insert_textbox(
        fitz.Rect(36, 36, 320, 64),
        "Formula Smoke Fixture",
        fontsize=16,
    )
    page.insert_textbox(
        fitz.Rect(36, 96, 340, 120),
        "The density n_{e} and citation 50^{-4} remain readable.",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(36, 150, 320, 174),
        "We solve E = mc^2 in the text and preserve it.",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(90, 220, 260, 244),
        r"\int f_s d\Omega",
        fontsize=12,
    )
    document.save(path)
    document.close()


def _write_numbered_formula_smoke_pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page(width=420, height=520)
    page.insert_textbox(
        fitz.Rect(36, 36, 320, 64),
        "Numbered Formula Fixture",
        fontsize=16,
    )
    page.insert_textbox(
        fitz.Rect(36, 96, 340, 120),
        "We keep a single GB/T equation number renderer.",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(72, 160, 320, 190),
        r"\int f_s d\Omega , (3) v_{n}",
        fontsize=12,
    )
    document.save(path)
    document.close()


def _write_plan_v3_regression_pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page(width=460, height=760)
    page.insert_textbox(
        fitz.Rect(36, 36, 360, 64),
        "Plan V3 Formula Regression Fixture",
        fontsize=16,
    )
    page.insert_textbox(
        fitz.Rect(36, 92, 390, 120),
        "The density n_{e} and citation 50^{-4} remain readable.",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(36, 126, 390, 154),
        "We solve E = mc^2 in the text and preserve it.",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(72, 190, 388, 220),
        r"\frac{q_s n_s}{m_s} = \int f_s d\Omega",
        fontsize=12,
    )
    page.insert_textbox(
        fitz.Rect(72, 252, 388, 282),
        r"\sum_n q_s n_s = 0",
        fontsize=12,
    )
    page.insert_textbox(
        fitz.Rect(72, 314, 388, 344),
        r"\int f_s d\Omega , (3) v_{n}",
        fontsize=12,
    )
    page.insert_textbox(
        fitz.Rect(72, 376, 388, 406),
        r"\frac{\alpha}{\beta + 1} = q_s , (4)",
        fontsize=12,
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


def _visible_text_outside_formula_spans(html: str) -> str:
    class VisibleTextCollector(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.skip_stack: list[str] = []
            self.tag_stack: list[tuple[bool, bool]] = []
            self.formula_depth = 0
            self.text_parts: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attr_map = dict(attrs)
            classes = set((attr_map.get("class") or "").split())
            should_skip = tag in {"script", "style", "annotation"} or "katex-mathml" in classes
            is_formula = "formula" in classes
            self.tag_stack.append((is_formula, should_skip))
            if should_skip:
                self.skip_stack.append(tag)
                return
            if is_formula:
                self.formula_depth += 1

        def handle_endtag(self, tag: str) -> None:
            if not self.tag_stack:
                return
            is_formula, should_skip = self.tag_stack.pop()
            if should_skip:
                if self.skip_stack:
                    self.skip_stack.pop()
                return
            if is_formula and self.formula_depth > 0:
                self.formula_depth -= 1

        def handle_data(self, data: str) -> None:
            if self.skip_stack:
                return
            text = data.strip().replace("\u200b", "")
            if not text or self.formula_depth > 0:
                return
            self.text_parts.append(text)

    collector = VisibleTextCollector()
    collector.feed(html)
    return re.sub(r"\s+", " ", " ".join(collector.text_parts)).strip()


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


def test_local_pipeline_formula_smoke_pdf_preserves_formula_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            "agent_enable_vision_analysis": False,
            "ocr_provider_order": ["deterministic"],
        }
    )
    pdf_path = storage.uploads / "formula-smoke.pdf"
    _write_formula_smoke_pdf(pdf_path)

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert 'class="formula formula-display' in html
        output_path.write_bytes(b"%PDF-1.7\n% formula smoke fixture\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_formula_smoke",
            "doc_formula_smoke",
            pdf_path.name,
            pdf_path,
            "zh-CN",
        )
    )

    status = storage.load_status("job_formula_smoke")
    document = storage.load_document_ir("doc_formula_smoke")
    formula_diagnostics = storage.read_output_json("doc_formula_smoke", "formula-diagnostics.json")
    layout_trace = storage.read_output_json("doc_formula_smoke", "layout-trace.json")
    renderer_diagnostics = storage.read_output_json("doc_formula_smoke", "renderer-diagnostics.json")
    chunks = storage.read_output_json("doc_formula_smoke", "translation-chunks.json")
    html = storage.preview_html_path("doc_formula_smoke").read_text(encoding="utf-8")
    visible_outside_formulas = _visible_text_outside_formula_spans(html)

    assert status.status == JobState.COMPLETED
    assert formula_diagnostics["candidate_count"] == 3
    assert formula_diagnostics["accepted_count"] == 3
    assert formula_diagnostics["formula_recognition_mode"] == "pdf_primitive_replay"
    assert formula_diagnostics["display_count"] == 1
    assert formula_diagnostics["inline_count"] == 2
    assert formula_diagnostics["unresolved_placeholders"] == []

    assert renderer_diagnostics["layout_issues"] == []
    assert renderer_diagnostics["unresolved_formula_placeholders"] == []
    assert renderer_diagnostics["formula_rendered_count"] == 3
    assert renderer_diagnostics["quality_flag_counts"].get("formula_render_failed", 0) == 0
    assert renderer_diagnostics["quality_flag_counts"].get("text_script_marker_rendered", 0) == 1
    assert all(formula.pdf_formula is not None for formula in document.formulas)

    display_formula = next(
        formula for formula in document.formulas if formula.display_mode == "display"
    )
    source_display_block = next(
        block
        for page in document.pages
        for block in page.blocks
        if block.block_id == display_formula.source_block_id
    )
    rendered_display_block = next(
        block for block in layout_trace["blocks"] if block["source_block_id"] == display_formula.source_block_id
    )
    source_display_height = source_display_block.bbox.y1 - source_display_block.bbox.y0
    rendered_display_height = (
        rendered_display_block["bbox"]["y1"] - rendered_display_block["bbox"]["y0"]
    )
    rendered_formula_html = renderer_models._katex_html(display_formula.latex, display=True)
    rendered_formula_height = renderer_models._height_from_katex_html(
        rendered_formula_html,
        12.0,
    )

    assert rendered_formula_height is not None
    assert rendered_display_height > source_display_height
    assert rendered_display_height >= rendered_formula_height

    chunk_blocks = [
        source_block
        for chunk in chunks
        for source_block in chunk["source_blocks"]
    ]
    assert len(chunks) == 2
    assert chunk_blocks[1]["source_text"].startswith("The density {{formula:")
    assert chunk_blocks[2]["source_text"].startswith("We solve {{formula:")
    assert chunk_blocks[3]["role"] == "formula"
    assert chunk_blocks[3]["requires_translation"] is False

    assert "{{formula:" not in html
    assert "@@FORMULA_" not in html
    assert '<span class="formula-plaintext-fallback">' not in html
    assert html.count('class="formula-image-fallback"') >= 3
    assert 'data-pdf-formula="true"' in html
    assert "50<sup>-4</sup>" in html
    assert "_{" not in visible_outside_formulas
    assert "^{" not in visible_outside_formulas
    assert "citation 50 -4 remain readable" in visible_outside_formulas
    assert storage.output_pdf_path("doc_formula_smoke").read_bytes().startswith(b"%PDF-")


def test_local_pipeline_numbered_formula_smoke_preserves_single_equation_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            "agent_enable_vision_analysis": False,
            "ocr_provider_order": ["deterministic"],
        }
    )
    pdf_path = storage.uploads / "numbered-formula-smoke.pdf"
    _write_numbered_formula_smoke_pdf(pdf_path)

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert 'class="formula-equation-number"' in html
        output_path.write_bytes(b"%PDF-1.7\n% numbered formula smoke fixture\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_numbered_formula_smoke",
            "doc_numbered_formula_smoke",
            pdf_path.name,
            pdf_path,
            "zh-CN",
            coerce_user_intent(
                "zh-CN",
                output_kind="typeset_document",
                instruction="按 GB/T 7713.1 标准排版",
            ),
        )
    )

    status = storage.load_status("job_numbered_formula_smoke")
    formula_diagnostics = storage.read_output_json(
        "doc_numbered_formula_smoke",
        "formula-diagnostics.json",
    )
    renderer_diagnostics = storage.read_output_json(
        "doc_numbered_formula_smoke",
        "renderer-diagnostics.json",
    )
    html = storage.preview_html_path("doc_numbered_formula_smoke").read_text(encoding="utf-8")

    assert status.status == JobState.COMPLETED
    assert formula_diagnostics["display_count"] == 1
    assert formula_diagnostics["unresolved_placeholders"] == []
    assert renderer_diagnostics["layout_issues"] == []
    assert renderer_diagnostics["formula_rendered_count"] == 1
    assert renderer_diagnostics["formula_number_source_preserved_count"] == 1
    assert renderer_diagnostics["formula_numbered_count"] == 0
    assert html.count('class="formula-equation-number"') == 1
    assert 'data-formula-number="(3)"' in html
    assert 'data-latex="\\int f_s d\\Omega \\tag{3}"' not in html
    assert storage.output_pdf_path("doc_numbered_formula_smoke").read_bytes().startswith(
        b"%PDF-"
    )


def test_local_pipeline_plan_v3_formula_regression_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            "agent_enable_vision_analysis": False,
            "ocr_provider_order": ["deterministic"],
        }
    )
    pdf_path = storage.uploads / "plan-v3-regression.pdf"
    _write_plan_v3_regression_pdf(pdf_path)

    async def fake_render_to_pdf(
        html: str,
        output_path: Path,
        *,
        diagnostics_path: Path | None = None,
        asset_base_path: Path | None = None,
    ) -> Path:
        assert html.count('class="formula formula-display') >= 4
        output_path.write_bytes(b"%PDF-1.7\n% plan v3 regression fixture\n%%EOF")
        if diagnostics_path is not None:
            diagnostics_path.write_text(
                '{"kind":"pdf_export","status":"completed","output_bytes":39}',
                encoding="utf-8",
            )
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_plan_v3_regression",
            "doc_plan_v3_regression",
            pdf_path.name,
            pdf_path,
            "zh-CN",
            coerce_user_intent(
                "zh-CN",
                output_kind="typeset_document",
                instruction="按 GB/T 7713.1 标准排版",
            ),
        )
    )

    status = storage.load_status("job_plan_v3_regression")
    document = storage.load_document_ir("doc_plan_v3_regression")
    formula_diagnostics = storage.read_output_json("doc_plan_v3_regression", "formula-diagnostics.json")
    layout_trace = storage.read_output_json("doc_plan_v3_regression", "layout-trace.json")
    renderer_diagnostics = storage.read_output_json(
        "doc_plan_v3_regression",
        "renderer-diagnostics.json",
    )
    html = storage.preview_html_path("doc_plan_v3_regression").read_text(encoding="utf-8")
    visible_outside_formulas = _visible_text_outside_formula_spans(html)

    assert status.status == JobState.COMPLETED
    assert formula_diagnostics["accepted_count"] == 6
    assert formula_diagnostics["formula_recognition_mode"] == "pdf_primitive_replay"
    assert formula_diagnostics["display_count"] == 4
    assert formula_diagnostics["inline_count"] == 2
    assert formula_diagnostics["unresolved_placeholders"] == []

    assert renderer_diagnostics["layout_issues"] == []
    assert renderer_diagnostics["unresolved_formula_placeholders"] == []
    assert renderer_diagnostics["formula_rendered_count"] == 6
    assert renderer_diagnostics["quality_flag_counts"].get("formula_render_failed", 0) == 0
    assert renderer_diagnostics["quality_flag_counts"].get("formula_missing_latex", 0) == 0
    assert (
        renderer_diagnostics["formula_numbered_count"]
        + renderer_diagnostics["formula_number_source_preserved_count"]
        == 4
    )
    assert renderer_diagnostics["formula_number_source_preserved_count"] == 2
    assert renderer_diagnostics["formula_numbered_count"] == 2

    assert "{{formula:" not in html
    assert "@@FORMULA_" not in html
    assert html.count('class="formula-equation-number"') == 4
    assert '<span class="formula-plaintext-fallback">' not in html
    assert html.count('class="formula-image-fallback"') >= 6
    assert 'data-pdf-formula="true"' in html
    assert "50<sup>-4</sup>" in html
    assert "_{" not in visible_outside_formulas
    assert "^{" not in visible_outside_formulas
    assert storage.output_pdf_path("doc_plan_v3_regression").read_bytes().startswith(
        b"%PDF-"
    )

    display_formulas = [formula for formula in document.formulas if formula.display_mode == "display"]
    assert len(display_formulas) == 4
    assert all(formula.pdf_formula is not None for formula in document.formulas)

    for display_formula in display_formulas:
        source_display_block = next(
            block
            for page in document.pages
            for block in page.blocks
            if block.block_id == display_formula.source_block_id
        )
        rendered_display_block = next(
            block
            for block in layout_trace["blocks"]
            if block["source_block_id"] == display_formula.source_block_id
        )
        source_display_height = source_display_block.bbox.y1 - source_display_block.bbox.y0
        rendered_display_height = (
            rendered_display_block["bbox"]["y1"] - rendered_display_block["bbox"]["y0"]
        )
        rendered_formula_html = renderer_models._katex_html(display_formula.latex, display=True)
        rendered_formula_height = renderer_models._height_from_katex_html(
            rendered_formula_html,
            12.0,
        )

        assert rendered_formula_height is not None
        assert rendered_display_height > source_display_height
        assert rendered_display_height >= rendered_formula_height
