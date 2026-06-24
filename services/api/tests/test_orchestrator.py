import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sqlite3
import shutil
import sys

import pytest
from app import runtime_config
from app.config import Settings
from app.models import JobState
from app.pipeline import orchestrator
from app.pipeline.translator import DeterministicTranslator
from app.pipeline.article_brief import ArticleBriefError
from app.pipeline.workflow import (
    build_layout_intent_plan,
    coerce_user_intent,
    render_evaluation_summary,
)
from app.pipeline.agents.nodes import route_after_validation
from app.pipeline.parser import UnsupportedPdfError
from app.storage import Storage
from pdf_renderer import RenderDocument
from pdf_translator_schema import (
    ArticleBrief,
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    SourceBlock,
    TranslationBlockPlan,
    TranslationChunk,
    TranslationLayoutPlan,
    DocumentStructureCandidate,
    SemanticBlockSignal,
    SemanticLayoutAnalysis,
    SectionKind,
    UserIntent,
)
from pdf_translator_schema.models import (
    DocumentBlock,
    FormulaRecognitionResult,
    OCRRecognitionResult,
    RenderDefaults,
)


async def fake_article_brief(*_args, **_kwargs) -> ArticleBrief:
    return ArticleBrief(
        title="Test Paper",
        field="translation systems",
        background="A local test document exercises the translation workflow.",
        main_idea="The pipeline translates academic blocks with stable artifacts.",
        contribution="It keeps terminology consistent.",
        key_terms={"local pipeline": "本地流水线"},
        quality_flags=["test_article_brief"],
    )


def test_render_evaluation_blocks_browser_overflow() -> None:
    evaluation = render_evaluation_summary(
        {
            "quality_flag_counts": {},
            "layout_issues": [],
            "browser_block_overflow_count": 2,
            "browser_validation_unavailable": False,
        }
    )

    assert evaluation["accepted"] is False
    assert evaluation["blocking_flags"] == {"browser_overflow": 2}
    assert evaluation["repair_recommended"] is True
    assert evaluation["manual_action_required"] is False


def test_workflow_mode_routes_translation_and_typeset_paths() -> None:
    assert (
        route_after_validation(
            {"user_intent": UserIntent(workflow_mode="translate_only").model_dump(mode="json")}
        )
        == "translate_chunks"
    )
    assert (
        route_after_validation(
            {
                "user_intent": UserIntent(
                    workflow_mode="typeset_only",
                    output_kind="typeset_document",
                ).model_dump(mode="json")
            }
        )
        == "build_source_plans"
    )


def test_render_evaluation_marks_browser_unavailable_without_repair_loop() -> None:
    evaluation = render_evaluation_summary(
        {
            "quality_flag_counts": {"browser_validation_unavailable": 1},
            "layout_issues": [],
            "browser_block_overflow_count": 0,
            "browser_validation_unavailable": True,
        }
    )

    assert evaluation["accepted"] is False
    assert evaluation["blocking_flags"] == {}
    assert evaluation["repair_recommended"] is False
    assert evaluation["manual_action_required"] is True


def test_render_evaluation_blocks_underfilled_non_final_reflow_page() -> None:
    page_utilization = [
        {
            "page_id": "r0001",
            "combined_area_ratio": 0.08,
            "bottom_whitespace_ratio": 0.55,
        }
    ]

    evaluation = render_evaluation_summary(
        {
            "quality_flag_counts": {"underfilled_reflow_page": 1},
            "layout_issues": [],
            "underfilled_reflow_pages": ["r0001"],
            "page_utilization": page_utilization,
            "browser_block_overflow_count": 0,
            "browser_validation_unavailable": False,
        }
    )

    assert evaluation["accepted"] is False
    assert evaluation["blocking_flags"] == {"underfilled_reflow_page": 1}
    assert evaluation["repair_recommended"] is True
    assert evaluation["underfilled_reflow_pages"] == ["r0001"]
    assert evaluation["page_utilization"] == page_utilization


def test_render_evaluation_blocks_column_flow_anomalies() -> None:
    evaluation = render_evaluation_summary(
        {
            "quality_flag_counts": {
                "right_column_page_start": 1,
                "left_column_underfilled_before_right_column": 1,
            },
            "layout_issues": [],
            "right_column_start_pages": ["r0003"],
            "left_column_underfilled_pages": ["r0002"],
            "browser_block_overflow_count": 0,
            "browser_validation_unavailable": False,
        }
    )

    assert evaluation["accepted"] is False
    assert evaluation["blocking_flags"] == {
        "right_column_page_start": 1,
        "left_column_underfilled_before_right_column": 1,
    }
    assert evaluation["repair_recommended"] is True
    assert evaluation["right_column_start_pages"] == ["r0003"]
    assert evaluation["left_column_underfilled_pages"] == ["r0002"]


def test_render_evaluation_allows_short_final_reflow_page() -> None:
    page_utilization = [
        {
            "page_id": "r0003",
            "combined_area_ratio": 0.06,
            "bottom_whitespace_ratio": 0.62,
        }
    ]

    evaluation = render_evaluation_summary(
        {
            "quality_flag_counts": {},
            "layout_issues": [],
            "low_utilization_pages": ["r0003"],
            "underfilled_reflow_pages": [],
            "page_utilization": page_utilization,
            "browser_block_overflow_count": 0,
            "browser_validation_unavailable": False,
        }
    )

    assert evaluation["accepted"] is True
    assert evaluation["blocking_flags"] == {}
    assert evaluation["repair_recommended"] is False
    assert evaluation["underfilled_reflow_pages"] == []
    assert evaluation["page_utilization"] == page_utilization


def test_layout_repair_ignores_nonblocking_renderer_quality_flags() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=36, y0=72, x1=264, y1=120),
                        reading_order=0,
                        source_text="Formula-heavy paragraph.",
                    )
                ],
            )
        ],
    )

    repaired = build_layout_intent_plan(
        document,
        coerce_user_intent("zh-CN"),
        attempt=2,
        diagnostics={
            "pages": [
                {
                    "flagged_blocks": [
                        {
                            "block_id": "b1",
                            "quality_flags": [
                                "formula_height_adjusted",
                                "formula_height_risk",
                                "compact_reflow",
                            ],
                        }
                    ]
                }
            ]
        },
    )

    assert repaired.blocks[0].render_intent == "normal"
    assert "repair_compact_intent" not in repaired.blocks[0].quality_flags


def test_process_document_job_persists_frontend_visible_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)

    def fail_parse(pdf_path: Path, doc_id: str, asset_output_dir: Path | None = None):
        raise ValueError("parse failed clearly")

    monkeypatch.setattr(orchestrator, "parse_pdf", fail_parse)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            tmp_path / "paper.pdf",
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    assert status.status == JobState.FAILED
    assert status.error == "parse failed clearly"
    assert status.message == "Failed"


def test_typeset_document_continuation_skips_translator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=10, x1=220, y1=40),
                        reading_order=0,
                        source_text="Alpha source [1].",
                    )
                ],
            )
        ],
    )
    storage.save_document_ir(document)

    def fail_build_translator(*_args, **_kwargs):
        raise AssertionError("translator should not be built for source-only typesetting")

    async def fake_render_preview(doc_id, document, plans, *_args, **_kwargs):
        assert plans[0].blocks[0].translated_text == "Alpha source [1]."
        assert "translation_skipped" in plans[0].blocks[0].quality_flags
        storage.save_preview_html(doc_id, "<html>preview</html>")
        return "<html>preview</html>", {
            "quality_flag_counts": {},
            "layout_issues": [],
            "browser_block_overflow_count": 0,
            "browser_validation_unavailable": False,
        }

    async def fake_render_pdf(html, output_path, **_kwargs):
        output_path.write_bytes(b"%PDF-1.4\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "build_translator", fail_build_translator)
    monkeypatch.setattr(orchestrator, "_render_preview_artifacts", fake_render_preview)
    monkeypatch.setattr(orchestrator, "_render_pdf_with_optional_diagnostics", fake_render_pdf)

    asyncio.run(
        orchestrator.process_document_continuation_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            None,
            "zh-CN",
            UserIntent(output_kind="typeset_document"),
        )
    )

    status = storage.load_status("job_1")
    assert status.status == JobState.COMPLETED
    plans = storage.read_output_json("doc_1", "translation-plans.json")
    assert plans[0]["blocks"][0]["translated_text"] == "Alpha source [1]."
    assert "source_text_preserved" in plans[0]["blocks"][0]["quality_flags"]
    progress = storage.read_output_json("doc_1", "translation-progress.json")
    assert progress[0]["status"] == "skipped"
    assert storage.read_output_json("doc_1", "edit-scope.json")["mode"] == "all"


def test_coerce_user_intent_detects_gbt_7713_standard() -> None:
    intent = coerce_user_intent(
        "zh-CN",
        output_kind="typeset_document",
        instruction="按照 GB/T 7713.1 标准进行排版",
    )

    assert intent.typesetting_standard == "gb_t_7713_1_2025"


def test_coerce_user_intent_detects_column_layout_phrases() -> None:
    two_column = coerce_user_intent("zh-CN", instruction="我需要双栏排版")
    typo_double_column = coerce_user_intent("zh-CN", instruction="double colume")
    typo_two_column = coerce_user_intent("zh-CN", instruction="two colume")
    digit_two_column = coerce_user_intent("zh-CN", instruction="2 colume")
    single_column = coerce_user_intent(
        "zh-CN",
        instruction="Use double column first, then switch to single column.",
    )
    typo_single_override = coerce_user_intent(
        "zh-CN",
        instruction="Use double colume first, then switch to single colum.",
    )
    body_two_column = coerce_user_intent(
        "zh-CN",
        instruction="正文双栏，栏距 8mm，平衡双栏",
    )
    unbalanced = coerce_user_intent("zh-CN", instruction="双栏，但不平衡栏")

    assert two_column.column_layout.column_count == 2
    assert typo_double_column.column_layout.column_count == 2
    assert typo_two_column.column_layout.column_count == 2
    assert digit_two_column.column_layout.column_count == 2
    assert single_column.column_layout.column_count == 1
    assert typo_single_override.column_layout.column_count == 1
    assert body_two_column.column_layout.scope == "body"
    assert body_two_column.column_layout.column_gap_pt == 22.68
    assert body_two_column.column_layout.balance_columns is True
    assert unbalanced.column_layout.balance_columns is False


def test_layout_intent_plan_uses_semantic_analysis_to_rescue_unknown_blocks() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.UNKNOWN,
                        bbox=BoundingBox(x0=10, y0=10, x1=280, y1=40),
                        reading_order=0,
                        source_text="1 Introduction",
                    ),
                    DocumentBlock(
                        block_id="b2",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=50, x1=280, y1=90),
                        reading_order=1,
                        source_text="Alpha beta.",
                    ),
                ],
            )
        ],
    )
    intent = coerce_user_intent(
        "zh-CN",
        output_kind="typeset_document",
        instruction="按照 GB/T 7713.1 标准排版",
    )
    analysis = SemanticLayoutAnalysis(
        analysis_id="analysis_1",
        doc_id="doc_1",
        block_signals=[
            SemanticBlockSignal(
                source_block_id="b1",
                role_candidates=[BlockRole.HEADING],
                section_hint="heading: Introduction",
                confidence=0.9,
            ),
            SemanticBlockSignal(
                source_block_id="b2",
                role_candidates=[BlockRole.PARAGRAPH],
                confidence=0.88,
            ),
        ],
        structure_candidates=[
            DocumentStructureCandidate(
                section_id="intro",
                kind=SectionKind.HEADING,
                title="1 Introduction",
                level=1,
                source_block_ids=["b1", "b2"],
                confidence=0.86,
            )
        ],
    )

    plan = build_layout_intent_plan(document, intent, semantic_analysis=analysis)

    block_by_id = {block.source_block_id: block for block in plan.blocks}
    assert block_by_id["b1"].role == BlockRole.HEADING
    assert block_by_id["b1"].render_intent == "emphasis"
    assert "semantic_role_inferred_heading" in block_by_id["b1"].quality_flags
    assert plan.structure_plan.sections[0].section_id == "intro"
    assert plan.structure_plan.sections[0].source_block_ids == ["b1", "b2"]
    assert "semantic_structure_candidate" in plan.structure_plan.sections[0].quality_flags
    assert "semantic_analysis_considered" in plan.quality_flags


@pytest.mark.parametrize(
    ("instruction", "document_kind", "citation_style", "has_docx"),
    [
        ("本科论文，参考文献采用 GB/T 7714，同时输出 Word 和 PDF", "undergraduate_thesis", "gb_t_7714", True),
        ("实验报告，使用 APA 格式", "lab_report", "apa", False),
        ("开题报告，MLA references", "proposal_report", "mla", False),
        ("普通作业，保持默认格式", "homework", "auto", False),
    ],
)
def test_coerce_user_intent_detects_v02_academic_intent(
    instruction: str,
    document_kind: str,
    citation_style: str,
    has_docx: bool,
) -> None:
    intent = coerce_user_intent(
        "zh-CN",
        output_kind="typeset_document",
        instruction=instruction,
    )

    assert intent.schema_version == "0.2"
    assert intent.task_intent.document_kind == document_kind
    assert intent.bibliography_preference.citation_style == citation_style
    assert {target.format for target in intent.output_targets} >= {"html_preview", "pdf"}
    assert ("docx" in {target.format for target in intent.output_targets}) is has_docx


def test_coerce_user_intent_detects_template_sources() -> None:
    school = coerce_user_intent("zh-CN", instruction="按照学校模板排版")
    course = coerce_user_intent("zh-CN", instruction="遵循课程要求")

    assert school.template_profile.source == "school_template"
    assert school.template_profile.fallback_used is False
    assert course.template_profile.source == "course_requirement"
    assert course.template_profile.fallback_used is False


def test_process_document_job_honors_existing_cancel_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    storage.save_status(
        orchestrator.JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="paper.pdf",
            target_lang="zh-CN",
            status=JobState.CANCELED,
            progress=1,
            message="Canceled",
        )
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("parse_pdf should not run after cancellation")

    monkeypatch.setattr(orchestrator, "parse_pdf", fail_if_called)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            tmp_path / "paper.pdf",
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    assert status.status == JobState.CANCELED
    assert status.message == "Canceled"


def test_process_document_job_persists_chunk_progress_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    render_defaults = RenderDefaults(
        target_lang="zh-CN",
        font_stack=["Configured Sans", "serif"],
        line_height=1.58,
    ).model_dump()
    render_defaults["overflow_policy"]["min_font_scale"] = 0.77
    storage.write_runtime_config(
        {
            "translation_concurrency": 2,
            "translator_max_attempts": 3,
            "translation_chunk_max_chars": 1800,
            "render_defaults": render_defaults,
        }
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="Alpha",
                    ),
                    DocumentBlock(
                        block_id="b2",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=50, x1=120, y1=80),
                        reading_order=1,
                        source_text="Beta",
                    ),
                ],
            )
        ],
    )
    chunks = [
        TranslationChunk(
            chunk_id="chunk_1",
            source_blocks=[
                SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Alpha")
            ],
        ),
        TranslationChunk(
            chunk_id="chunk_2",
            source_blocks=[
                SourceBlock(block_id="b2", role=BlockRole.PARAGRAPH, source_text="Beta")
            ],
        ),
    ]

    class FakeTranslator:
        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            block = chunk.source_blocks[0]
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id=block.block_id,
                        translated_text=f"translated {block.source_text}",
                        role=block.role,
                        quality_flags=["repaired_layout_plan"]
                        if chunk.chunk_id == "chunk_2"
                        else [],
                    )
                ],
            )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    captured: dict[str, object] = {}

    def fake_build_chunks(*_args, **kwargs):
        captured["chunk_render_defaults"] = kwargs["render_defaults"]
        captured["chunk_max_chars"] = kwargs["max_chars"]
        return chunks

    real_from_ir_and_plans = RenderDocument.from_ir_and_plans

    def fake_from_ir_and_plans(*args, **kwargs):
        captured["renderer_render_defaults"] = kwargs["render_defaults"]
        return real_from_ir_and_plans(*args, **kwargs)

    async def fake_render_preview_with_browser_layout(
        document_arg,
        plans_arg,
        target_lang_arg,
        *,
        render_defaults=None,
        layout_intent_plan=None,
        asset_base_path=None,
        max_iterations=3,
    ):
        render_document = orchestrator.RenderDocument.from_ir_and_plans(
            document_arg,
            plans_arg,
            target_lang_arg,
            render_defaults=render_defaults,
            layout_intent_plan=layout_intent_plan,
        )
        return "<html></html>", render_document, render_document.diagnostics()

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda _path, _doc_id, _asset_dir: document)
    monkeypatch.setattr(orchestrator, "build_chunks", fake_build_chunks)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: FakeTranslator())
    monkeypatch.setattr(orchestrator.RenderDocument, "from_ir_and_plans", fake_from_ir_and_plans)
    monkeypatch.setattr(
        orchestrator,
        "render_preview_with_browser_layout",
        fake_render_preview_with_browser_layout,
    )
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            tmp_path / "paper.pdf",
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    progress = storage.read_output_json("doc_1", "translation-progress.json")
    formula_recognition = storage.read_output_json("doc_1", "formula-recognition.json")
    formula_diagnostics = storage.read_output_json("doc_1", "formula-diagnostics.json")

    assert status.status == JobState.COMPLETED
    assert [chunk.status for chunk in status.chunks] == ["completed", "completed"]
    assert progress[0]["chunk_id"] == "chunk_1"
    assert progress[1]["quality_flags"] == ["repaired_layout_plan"]
    assert storage.output_pdf_path("doc_1").read_bytes().startswith(b"%PDF-")
    assert captured["chunk_render_defaults"].font_stack == ["Configured Sans", "serif"]
    assert captured["chunk_max_chars"] == 1800
    assert captured["chunk_render_defaults"].page_layout.width_pt == 300
    assert captured["chunk_render_defaults"].page_layout.height_pt == 400
    assert captured["chunk_render_defaults"].page_layout.margin_top_pt == 36
    assert captured["renderer_render_defaults"].page_layout.width_pt == 300
    assert captured["renderer_render_defaults"].page_layout.margin_left_pt == 36
    assert captured["renderer_render_defaults"].line_height == 1.58
    assert captured["renderer_render_defaults"].overflow_policy.min_font_scale == 0.77
    assert formula_recognition == []
    assert formula_diagnostics["kind"] == "formula_diagnostics"
    assert formula_diagnostics["recognizer_type"] == "deterministic"
    assert formula_diagnostics["visual_formula_recognition_enabled"] is True
    parser_diagnostics = storage.read_output_json("doc_1", "parser-diagnostics.json")
    assert "pdf_parse_ms" in parser_diagnostics
    assert parser_diagnostics["formula_recognizer_type"] == "deterministic"


def test_process_document_job_gbt_intent_uses_gbt_render_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    render_defaults = RenderDefaults(
        target_lang="zh-CN",
        font_stack=[
            "Noto Sans CJK SC",
            "Source Han Sans SC",
            "Arial Unicode MS",
            "sans-serif",
        ],
    ).model_dump()
    storage.write_runtime_config(
        {
            "translation_concurrency": 1,
            "translator_max_attempts": 1,
            "render_defaults": render_defaults,
        }
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="Alpha",
                    )
                ],
            )
        ],
    )
    chunks = [
        TranslationChunk(
            chunk_id="chunk_1",
            source_blocks=[
                SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Alpha")
            ],
        )
    ]

    class FakeTranslator:
        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id="b1",
                        translated_text="阿尔法",
                        role=BlockRole.PARAGRAPH,
                    )
                ],
            )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    captured: dict[str, object] = {}

    def fake_build_chunks(*_args, **kwargs):
        captured["chunk_render_defaults"] = kwargs["render_defaults"]
        captured["chunk_max_chars"] = kwargs["max_chars"]
        return chunks

    def fake_from_ir_and_plans(*_args, **kwargs):
        captured["renderer_render_defaults"] = kwargs["render_defaults"]
        return RenderDocument(doc_id="doc_1", target_lang="zh-CN", pages=[])

    async def fake_render_preview_with_browser_layout(
        document_arg,
        plans_arg,
        target_lang_arg,
        *,
        render_defaults=None,
        layout_intent_plan=None,
        asset_base_path=None,
        max_iterations=3,
    ):
        render_document = orchestrator.RenderDocument.from_ir_and_plans(
            document_arg,
            plans_arg,
            target_lang_arg,
            render_defaults=render_defaults,
            layout_intent_plan=layout_intent_plan,
        )
        return "<html></html>", render_document, render_document.diagnostics()

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda _path, _doc_id, _asset_dir: document)
    monkeypatch.setattr(orchestrator, "build_chunks", fake_build_chunks)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: FakeTranslator())
    monkeypatch.setattr(orchestrator.RenderDocument, "from_ir_and_plans", fake_from_ir_and_plans)
    monkeypatch.setattr(
        orchestrator,
        "render_preview_with_browser_layout",
        fake_render_preview_with_browser_layout,
    )
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            tmp_path / "paper.pdf",
            "zh-CN",
            coerce_user_intent(
                "zh-CN",
                output_kind="typeset_document",
                instruction="按照 GB/T 7713.1 标准排版",
            ),
        )
    )

    assert captured["chunk_render_defaults"].layout_mode == "continuous_reflow"
    assert captured["renderer_render_defaults"].layout_mode == "continuous_reflow"
    assert captured["chunk_render_defaults"].page_layout.width_pt == 595.28
    assert captured["renderer_render_defaults"].page_layout.margin_top_pt == 70.87
    assert captured["renderer_render_defaults"].formula_numbering == "parenthesized"
    assert captured["chunk_render_defaults"].font_stack == [
        "Times New Roman",
        "SimSun",
        "Songti SC",
        "Noto Serif CJK SC",
        "Source Han Serif SC",
        "serif",
    ]


def test_process_document_job_fails_with_unrecoverable_translation_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    storage.write_runtime_config(
        {
            "translation_concurrency": 1,
            "translator_max_attempts": 2,
            "agent_max_repair_attempts": 0,
        }
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="Alpha",
                    ),
                    DocumentBlock(
                        block_id="b2",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=50, x1=120, y1=80),
                        reading_order=1,
                        source_text="Beta [1]",
                    ),
                ],
            )
        ],
    )
    chunks = [
        TranslationChunk(
            chunk_id="chunk_1",
            source_blocks=[
                SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Alpha")
            ],
        ),
        TranslationChunk(
            chunk_id="chunk_2",
            source_blocks=[
                SourceBlock(
                    block_id="b2",
                    role=BlockRole.PARAGRAPH,
                    source_text="Beta [1]",
                    preserve_tokens=["[1]"],
                )
            ],
        ),
    ]

    class FakeTranslator:
        def __init__(self) -> None:
            self._diagnostics: list[dict[str, object]] = []

        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            block = chunk.source_blocks[0]
            if chunk.chunk_id == "chunk_2":
                self._diagnostics.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "attempt": 2,
                        "error_type": "UnparseableTranslationResponseError",
                        "content_type": "str",
                        "content_length": 42,
                        "response_preview_length": 24,
                        "sanitized_response_preview": "plain text without JSON",
                        "quality_flags": [
                            "translator_response_unparseable",
                            "translator_unrecoverable_response",
                        ],
                    }
                )
                raise RuntimeError("translator_unrecoverable_response")
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id=block.block_id,
                        translated_text="translated Alpha",
                        role=block.role,
                    )
                ],
            )

        def drain_diagnostics(self) -> list[dict[str, object]]:
            diagnostics = list(self._diagnostics)
            self._diagnostics.clear()
            return diagnostics

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda _path, _doc_id, _asset_dir: document)
    monkeypatch.setattr(orchestrator, "build_chunks", lambda *_args, **_kwargs: chunks)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: FakeTranslator())

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            tmp_path / "paper.pdf",
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    progress = storage.read_output_json("doc_1", "translation-progress.json")
    diagnostics = storage.read_output_json("doc_1", "translation-diagnostics.json")

    assert status.status == JobState.FAILED
    assert "translation chunk(s) failed" in status.error
    assert [chunk.status for chunk in status.chunks] == ["completed", "failed"]
    assert progress[1]["status"] == "failed"
    assert progress[1]["error"] == "translator_unrecoverable_response"
    assert progress[1]["quality_flags"] == [
        "translator_response_unparseable",
        "translator_unrecoverable_response",
    ]
    assert diagnostics == [
        {
            "chunk_id": "chunk_2",
            "attempt": 2,
            "error_type": "UnparseableTranslationResponseError",
            "content_type": "str",
            "content_length": 42,
            "response_preview_length": 24,
            "sanitized_response_preview": "plain text without JSON",
            "quality_flags": [
                "translator_response_unparseable",
                "translator_unrecoverable_response",
            ],
        }
    ]


def test_translate_chunks_reuses_cached_successes_on_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    chunks = [
        TranslationChunk(
            chunk_id="chunk_1",
            source_blocks=[
                SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Alpha")
            ],
        ),
        TranslationChunk(
            chunk_id="chunk_2",
            source_blocks=[
                SourceBlock(block_id="b2", role=BlockRole.PARAGRAPH, source_text="Beta")
            ],
        ),
    ]

    def plan_for(chunk: TranslationChunk) -> TranslationLayoutPlan:
        block = chunk.source_blocks[0]
        return TranslationLayoutPlan(
            chunk_id=chunk.chunk_id,
            blocks=[
                TranslationBlockPlan(
                    source_block_id=block.block_id,
                    translated_text=f"translated {block.source_text}",
                    role=block.role,
                )
            ],
        )

    class FailingSecondTranslator:
        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            if chunk.chunk_id == "chunk_2":
                raise RuntimeError("network timeout")
            return plan_for(chunk)

    first_progress = orchestrator._initial_chunk_progress(chunks)
    with pytest.raises(RuntimeError, match="network timeout"):
        asyncio.run(
            orchestrator._translate_chunks(
                job_id="job_1",
                filename="paper.pdf",
                target_lang="zh-CN",
                doc_id="doc_1",
                chunks=chunks,
                chunk_progress=first_progress,
                translator=FailingSecondTranslator(),
                translation_concurrency=1,
            )
        )
    cache = storage.read_output_json("doc_1", "translation-plan-cache.json")
    assert "chunk_1" in cache["entries"]
    assert "chunk_2" not in cache["entries"]

    class RecordingTranslator:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            self.calls.append(chunk.chunk_id)
            return plan_for(chunk)

    continuation_translator = RecordingTranslator()
    second_progress = orchestrator._initial_chunk_progress(chunks)
    plans = asyncio.run(
        orchestrator._translate_chunks(
            job_id="job_2",
            filename="paper.pdf",
            target_lang="zh-CN",
            doc_id="doc_1",
            chunks=chunks,
            chunk_progress=second_progress,
            translator=continuation_translator,
            translation_concurrency=1,
            reuse_cached_plans=True,
        )
    )

    assert [plan.chunk_id for plan in plans] == ["chunk_1", "chunk_2"]
    assert continuation_translator.calls == ["chunk_2"]
    assert second_progress[0].message == "Reused"
    assert "translation_cache_reused" in second_progress[0].quality_flags


def test_process_document_job_persists_pdf_export_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="Alpha",
                    ),
                ],
            )
        ],
    )
    chunks = [
        TranslationChunk(
            chunk_id="chunk_1",
            source_blocks=[
                SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Alpha")
            ],
        ),
    ]

    class FakeTranslator:
        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id="b1",
                        translated_text="translated Alpha",
                        role=BlockRole.PARAGRAPH,
                    )
                ],
            )

    async def fake_render_to_pdf(
        html: str,
        output_path: Path,
        *,
        diagnostics_path: Path | None = None,
        asset_base_path: Path | None = None,
    ) -> Path:
        assert "translated Alpha" in html
        assert asset_base_path == storage.asset_dir("doc_1")
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        if diagnostics_path is not None:
            diagnostics_path.write_text(
                '{"kind":"pdf_export","status":"completed","output_bytes":14}',
                encoding="utf-8",
            )
        return output_path

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda _path, _doc_id, _asset_dir: document)
    monkeypatch.setattr(orchestrator, "build_chunks", lambda *_args, **_kwargs: chunks)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: FakeTranslator())
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            tmp_path / "paper.pdf",
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    diagnostics = storage.read_output_json("doc_1", "pdf-export-diagnostics.json")
    workflow = storage.read_output_json("doc_1", "workflow-run.json")

    assert status.status == JobState.COMPLETED
    assert diagnostics["status"] == "completed"
    complete_steps = [step for step in workflow["steps"] if step["name"] == "complete"]
    assert "pdf-export-diagnostics" in complete_steps[-1]["output_artifacts"]


def test_formula_enrichment_does_not_build_openai_formula_provider_unless_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    storage.write_runtime_config(
        {
            "openai_base_url": "https://models.example.test/v1",
            "openai_api_key": "secret-key",
            "openai_model": "paper-model",
            "vision_analyzer_model": "vision-model",
            "agent_enable_vision_analysis": False,
            "ocr_provider_order": ["deterministic"],
        }
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="formula_1",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="E = mc^2",
                    )
                ],
            )
        ],
    )

    class ForbiddenRecognizer:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("vision recognizer should not be constructed")

    monkeypatch.setattr(orchestrator, "OpenAIFormulaRecognizer", ForbiddenRecognizer)

    result = asyncio.run(orchestrator._enrich_document_formulas(document, doc_id="doc_1"))

    assert result.diagnostics["recognizer_type"] == "deterministic"
    assert result.diagnostics["visual_formula_recognition_enabled"] is False
    assert "visual_formula_recognition_disabled" in result.diagnostics["quality_flags"]


def test_formula_enrichment_openai_formula_ocr_is_decoupled_from_agent_vision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    storage.write_runtime_config(
        {
            "openai_base_url": "https://models.example.test/v1",
            "openai_api_key": "secret-key",
            "openai_model": "paper-model",
            "vision_analyzer_model": "vision-model",
            "agent_enable_vision_analysis": False,
            "ocr_provider_order": ["openai_vision", "deterministic"],
        }
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="formula_1",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="E = mc^2",
                    )
                ],
            )
        ],
    )
    constructed: list[dict] = []

    class FakeRecognizer:
        def __init__(self, **kwargs) -> None:
            constructed.append(kwargs)

        async def recognize(self, candidate):
            return FormulaRecognitionResult(
                latex="E = mc^2",
                display_mode="display",
                confidence=0.91,
                quality_flags=[],
            )

    monkeypatch.setattr(orchestrator, "OpenAIFormulaRecognizer", FakeRecognizer)

    result = asyncio.run(orchestrator._enrich_document_formulas(document, doc_id="doc_1"))

    assert constructed
    assert result.diagnostics["recognizer_type"] == "deterministic"
    assert result.diagnostics["visual_formula_recognition_enabled"] is True


def test_formula_enrichment_reports_progress_and_falls_back_from_pix2text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    storage.save_status(
        orchestrator.JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="paper.pdf",
            target_lang="zh-CN",
            status=JobState.PARSING,
            progress=0.17,
            message="Recognizing formulas",
        )
    )
    storage.write_runtime_config(
        {
            "ocr_provider_order": ["pix2text", "deterministic"],
            "ocr_provider_timeout_seconds": 1,
        }
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="formula_1",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="@fs=@t þ f 0 s=k2 (4)",
                    )
                ],
                assets=[
                    Asset(
                        asset_id="formula_asset",
                        page_id="p1",
                        kind="formula",
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        path="/api/documents/doc_1/assets/formula_asset.png",
                    )
                ],
            )
        ],
    )
    storage.asset_dir("doc_1").mkdir(parents=True, exist_ok=True)
    (storage.asset_dir("doc_1") / "formula_asset.png").write_bytes(b"fake-image")

    class EmptyPix2TextProvider:
        name = "pix2text"

        def __init__(self, **_kwargs) -> None:
            pass

        async def recognize_formula(self, candidate, *, image_path=None):
            return OCRRecognitionResult(
                region_kind="formula",
                provider="pix2text",
                confidence=0,
                quality_flags=["pix2text_formula_ocr_empty"],
            )

    monkeypatch.setattr(orchestrator, "Pix2TextOCRProvider", EmptyPix2TextProvider)

    result = asyncio.run(
        orchestrator._enrich_document_formulas(
            document,
            doc_id="doc_1",
            job_id="job_1",
            filename="paper.pdf",
            target_lang="zh-CN",
        )
    )

    status = storage.load_status("job_1")
    assert status.message == "Recognizing formulas 1/1"
    assert result.formulas[0].ocr_provider == "deterministic"
    ocr_records = storage.read_output_json("doc_1", "ocr-recognition.json")
    assert any(record["provider"] == "pix2text" for record in ocr_records)


def test_process_document_job_rerenders_preview_after_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="Alpha",
                    ),
                ],
            )
        ],
    )
    chunks = [
        TranslationChunk(
            chunk_id="chunk_1",
            source_blocks=[
                SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Alpha")
            ],
        ),
    ]

    class FakeTranslator:
        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id="b1",
                        translated_text="translated Alpha",
                        role=BlockRole.PARAGRAPH,
                    )
                ],
            )

    render_calls = 0
    real_render_to_html = orchestrator.render_to_html

    async def fake_render_preview_with_browser_layout(
        document_arg,
        plans_arg,
        target_lang_arg,
        *,
        render_defaults=None,
        layout_intent_plan=None,
        asset_base_path=None,
        max_iterations=3,
    ):
        nonlocal render_calls
        render_calls += 1
        suffix = "repaired" if render_calls == 2 else "initial"
        render_document = orchestrator.RenderDocument.from_ir_and_plans(
            document_arg,
            plans_arg,
            target_lang_arg,
            render_defaults=render_defaults,
            layout_intent_plan=layout_intent_plan,
        )
        return (
            f"{real_render_to_html(render_document)}<!-- {suffix} -->",
            render_document,
            render_document.diagnostics(),
        )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "<!-- repaired -->" in html
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda _path, _doc_id, _asset_dir: document)
    monkeypatch.setattr(orchestrator, "build_chunks", lambda *_args, **_kwargs: chunks)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: FakeTranslator())
    monkeypatch.setattr(
        orchestrator,
        "render_preview_with_browser_layout",
        fake_render_preview_with_browser_layout,
    )
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)
    monkeypatch.setattr(
        orchestrator,
        "render_evaluation_summary",
        lambda diagnostics: {
            "kind": "render_evaluation",
            "accepted": render_calls >= 2,
            "quality_flag_counts": diagnostics.get("quality_flag_counts", {}),
            "layout_issue_count": len(diagnostics.get("layout_issues", [])),
            "blocking_flags": {} if render_calls >= 2 else {"overflow_clipped": 1},
            "repair_recommended": render_calls < 2,
        },
    )

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            tmp_path / "paper.pdf",
            "zh-CN",
        )
    )

    assert render_calls == 2
    assert "<!-- repaired -->" in storage.preview_html_path("doc_1").read_text(encoding="utf-8")
    workflow = storage.read_output_json("doc_1", "workflow-run.json")
    repair_step = [step for step in workflow["steps"] if step["name"] == "repair"][-1]
    assert "preview" in repair_step["output_artifacts"]


def test_process_document_job_persists_scanned_pdf_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)

    def fail_with_ocr_required(
        pdf_path: Path,
        doc_id: str,
        asset_output_dir: Path | None = None,
    ) -> DocumentIR:
        raise UnsupportedPdfError(
            "Scanned or image-only PDF requires OCR, which is not implemented yet",
            {
                "kind": "unsupported_scanned_pdf",
                "reason": "ocr_required",
                "text_block_count": 0,
                "recoverable": True,
            },
        )

    monkeypatch.setattr(orchestrator, "parse_pdf", fail_with_ocr_required)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "scan.pdf",
            tmp_path / "scan.pdf",
            "zh-CN",
            UserIntent(workflow_mode="typeset_only", output_kind="typeset_document"),
        )
    )

    status = storage.load_status("job_1")
    diagnostics = storage.read_output_json("doc_1", "parser-diagnostics.json")

    assert status.status == JobState.COMPLETED
    assert diagnostics["kind"] == "scanned_pdf_ocr_parser_diagnostics"
    assert diagnostics["ocr_fallback_used"] is True


def test_text_workflow_runs_to_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(openai_api_key="", openai_api_key_from_env=False),
    )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "【译】" in html
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_text_document_job(
            "job_1",
            "doc_1",
            "text-input.txt",
            "Paper Title\n\nAbstract This is a text workflow [1].",
            "zh-CN",
            coerce_user_intent("zh-CN", instruction="double colume"),
        )
    )

    status = storage.load_status("job_1")
    workflow = storage.read_output_json("doc_1", "workflow-run.json")
    normalized = storage.read_output_json("doc_1", "normalized-input.json")
    semantic = storage.read_output_json("doc_1", "semantic-analysis.json")
    layout_plan = storage.read_output_json("doc_1", "layout-intent-plan.json")
    layout_trace = storage.read_output_json("doc_1", "layout-trace.json")
    user_intent = storage.read_output_json("doc_1", "user-intent.json")
    article_brief = storage.read_output_json("doc_1", "article-brief.json")
    chunks = storage.read_output_json("doc_1", "translation-chunks.json")

    assert status.status == JobState.COMPLETED
    assert workflow["status"] == "completed"
    assert "semantic_recognize" in {step["name"] for step in workflow["steps"]}
    assert "export_pdf" in {step["name"] for step in workflow["steps"]}
    assert normalized["input_sources"][0]["input_type"] == "text"
    assert semantic["schema_version"] == "0.2"
    assert semantic["structure_candidates"]
    assert semantic["block_section_mappings"]
    assert semantic["quality_flags"]
    assert user_intent["schema_version"] == "0.2"
    assert article_brief["schema_version"] == "0.1"
    assert "article_brief_model_skipped_deterministic_mode" in article_brief["quality_flags"]
    assert chunks[0]["article_brief"]["title"] == article_brief["title"]
    assert user_intent["column_layout"]["column_count"] == 2
    assert user_intent["output_targets"][0]["format"] == "html_preview"
    assert user_intent["output_targets"][1]["format"] == "pdf"
    assert layout_plan["schema_version"] == "0.2"
    assert layout_plan["column_layout"]["column_count"] == 2
    assert layout_trace["column_layout"]["column_count"] == 2
    assert layout_plan["document_profile"]["document_kind"] == "generic_academic"
    assert layout_plan["structure_plan"]["sections"]
    assert "numbering_plan" in layout_plan
    assert "bibliography_plan" in layout_plan
    assert layout_plan["blocks"]
    assert "semantic_analysis_considered" in layout_plan["quality_flags"]
    assert "planner_fallback" in layout_plan["quality_flags"]
    assert storage.preview_html_path("doc_1").exists()
    assert storage.output_pdf_path("doc_1").read_bytes().startswith(b"%PDF-")
    assert (tmp_path / "checkpoints" / "langgraph.sqlite").exists()


def test_text_workflow_fails_before_translation_when_model_article_brief_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(
            openai_base_url="https://models.example.test/v1",
            openai_api_key="fake-key",
            openai_api_key_from_env=True,
            openai_model="paper-model",
            ocr_provider_order=("deterministic",),
        ),
    )

    async def fail_article_brief(*_args, **_kwargs) -> ArticleBrief:
        raise ArticleBriefError("brief model unavailable")

    class ForbiddenTranslator:
        async def translate(self, _chunk: TranslationChunk) -> TranslationLayoutPlan:
            raise AssertionError("translator should not run after article brief failure")

    monkeypatch.setattr(orchestrator, "build_article_brief", fail_article_brief)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: ForbiddenTranslator())

    asyncio.run(
        orchestrator.process_text_document_job(
            "job_1",
            "doc_1",
            "text-input.txt",
            "Paper Title\n\nAbstract This is a text workflow [1].",
            "zh-CN",
            coerce_user_intent("zh-CN"),
        )
    )

    status = storage.load_status("job_1")

    assert status.status == JobState.FAILED
    assert "brief model unavailable" in (status.error or "")
    assert not storage.output_json_path("doc_1", "translation-chunks.json").exists()


def test_text_workflow_persists_minimax_intent_column_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    storage = Storage(tmp_path)
    chat_kwargs: list[dict] = []
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(
            openai_base_url="https://api.minimaxi.com/v1",
            openai_api_key="fake-key",
            openai_api_key_from_env=True,
            openai_model="MiniMax-M3",
            minimax_api_key="fake-key",
            minimax_endpoint="https://api.minimaxi.com/v1/chat/completions",
            minimax_model="MiniMax-M3",
            layout_planner_model="MiniMax-M3",
            vision_analyzer_model="MiniMax-M3",
            ocr_provider_order=("deterministic",),
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_translator",
        lambda *args, **kwargs: DeterministicTranslator(),
    )
    monkeypatch.setattr(orchestrator, "build_article_brief", fake_article_brief)

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "【译】" in html
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    class FakeChatOpenAI:
        def __init__(self, *args, **kwargs) -> None:
            chat_kwargs.append(kwargs)

        def with_structured_output(self, _schema_cls):
            raise AssertionError("MiniMax should use LangChain JSON invocation")

        async def ainvoke(self, messages):
            user_payload = json.loads(messages[1][1])
            if "json_schema" in user_payload:
                return {
                    "content": json.dumps(
                        {
                            "schema_version": "0.2",
                            "target_lang": "zh-CN",
                            "output_kind": "typeset_document",
                            "style_intent": "academic",
                            "instruction": "double colume",
                            "column_layout": {"column_count": 2},
                        }
                    )
                }
            if "source_block_ids" in user_payload:
                return {
                    "content": json.dumps(
                        {
                            "schema_version": "0.2",
                            "plan_id": "doc_model_plan",
                            "doc_id": "doc_model",
                            "target_lang": "zh-CN",
                            "column_layout": {"column_count": 1},
                            "blocks": [
                                {
                                    "source_block_id": block_id,
                                    "role": "paragraph",
                                    "priority": 3,
                                    "render_intent": "normal",
                                }
                                for block_id in user_payload["source_block_ids"]
                            ],
                        }
                    )
                }
            return {
                "content": json.dumps(
                    {
                        "schema_version": "0.2",
                        "analysis_id": "doc_model_analysis",
                        "doc_id": "doc_model",
                        "target_lang": "zh-CN",
                        "quality_flags": ["model_semantic_analysis"],
                    }
                )
            }

    class FakeLangChainOpenAI:
        ChatOpenAI = FakeChatOpenAI

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)
    monkeypatch.setitem(sys.modules, "langchain_openai", FakeLangChainOpenAI)

    asyncio.run(
        orchestrator.process_text_document_job(
            "job_model",
            "doc_model",
            "text-input.txt",
            "Paper Title\n\nAbstract This is a text workflow [1].",
            "zh-CN",
            coerce_user_intent("zh-CN", instruction="double colume"),
        )
    )

    workflow = storage.read_output_json("doc_model", "workflow-run.json")
    user_intent = storage.read_output_json("doc_model", "user-intent.json")
    layout_plan = storage.read_output_json("doc_model", "layout-intent-plan.json")
    layout_trace = storage.read_output_json("doc_model", "layout-trace.json")
    intent_step = next(
        step for step in workflow["steps"] if step["name"] == "analyze_intent"
    )

    assert len(chat_kwargs) >= 3
    assert all(kwargs["extra_body"]["reasoning_split"] is True for kwargs in chat_kwargs)
    assert all(
        kwargs["extra_body"]["thinking"] == {"type": "disabled"}
        for kwargs in chat_kwargs
    )
    assert user_intent["column_layout"]["column_count"] == 2
    assert layout_plan["column_layout"]["column_count"] == 2
    assert layout_trace["column_layout"]["column_count"] == 2
    assert intent_step["diagnostics"]["intent_model_used"] is True
    assert intent_step["diagnostics"]["intent_model_provider"] == "minimax_langchain_json"


def test_text_workflow_falls_back_to_memory_checkpointer_when_sqlite_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_root = Path.cwd() / ".tmp" / "test_orchestrator_sqlite_fallback"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    storage = Storage(tmp_root)
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(openai_api_key="", openai_api_key_from_env=False),
    )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "<html" in html
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    class FailingSqliteSaver:
        async def setup(self) -> None:
            raise sqlite3.OperationalError("disk I/O error")

    @asynccontextmanager
    async def fake_from_conn_string(_path: str):
        yield FailingSqliteSaver()

    from langgraph.checkpoint.sqlite import aio as sqlite_aio

    monkeypatch.setattr(sqlite_aio.AsyncSqliteSaver, "from_conn_string", fake_from_conn_string)
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_text_document_job(
            "job_1",
            "doc_1",
            "text-input.txt",
            "Paper Title\n\nAbstract This is a text workflow [1].",
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    workflow = storage.read_output_json("doc_1", "workflow-run.json")

    assert status.status == JobState.COMPLETED
    assert workflow["status"] == "completed"
    assert storage.output_pdf_path("doc_1").read_bytes().startswith(b"%PDF-")


def test_pdf_workflow_records_content_and_layout_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(openai_api_key="", openai_api_key_from_env=False),
    )
    content_pdf = tmp_path / "content.pdf"
    layout_pdf = tmp_path / "layout.pdf"
    content_pdf.write_bytes(b"%PDF-1.7\n%%EOF")
    layout_pdf.write_bytes(b"%PDF-1.7\n%%EOF")
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="Alpha",
                    ),
                ],
            )
        ],
    )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda *_args: document)
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "content.pdf",
            content_pdf,
            "zh-CN",
            layout_pdf_path=layout_pdf,
            layout_filename="layout.pdf",
        )
    )

    status = storage.load_status("job_1")
    normalized = storage.read_output_json("doc_1", "normalized-input.json")
    workflow = storage.read_output_json("doc_1", "workflow-run.json")
    semantic = storage.read_output_json("doc_1", "semantic-analysis.json")

    assert status.status == JobState.COMPLETED
    assert [source["source_role"] for source in normalized["input_sources"]] == [
        "content",
        "layout_reference",
    ]
    assert normalized["layout_reference"]["filename"] == "layout.pdf"
    assert "layout_reference_source_available" in semantic["quality_flags"]
    assert [source["source_role"] for source in workflow["input_sources"]] == [
        "content",
        "layout_reference",
    ]


def test_pdf_workflow_marks_layout_source_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(openai_api_key="", openai_api_key_from_env=False),
    )
    content_pdf = tmp_path / "content.pdf"
    content_pdf.write_bytes(b"%PDF-1.7\n%%EOF")
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="Alpha",
                    ),
                ],
            )
        ],
    )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda *_args: document)
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "content.pdf",
            content_pdf,
            "zh-CN",
        )
    )

    normalized = storage.read_output_json("doc_1", "normalized-input.json")
    semantic = storage.read_output_json("doc_1", "semantic-analysis.json")

    assert normalized["input_sources"][1]["source_role"] == "layout_reference"
    assert "layout_source_fallback_to_content" in normalized["quality_flags"]
    assert "layout_source_fallback_to_content" in semantic["quality_flags"]


def test_image_workflow_uses_deterministic_ocr_mock_and_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(openai_api_key="", openai_api_key_from_env=False),
    )
    image_path = tmp_path / "layout.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "Deterministic OCR fallback" in html
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_image_document_job(
            "job_1",
            "doc_1",
            "layout.png",
            image_path,
            "zh-CN",
            "image/png",
        )
    )

    status = storage.load_status("job_1")
    asset_ir = storage.read_output_json("doc_1", "asset-ir.json")
    semantic = storage.read_output_json("doc_1", "semantic-analysis.json")
    diagnostics = storage.read_output_json("doc_1", "parser-diagnostics.json")
    html = storage.preview_html_path("doc_1").read_text(encoding="utf-8")

    assert status.status == JobState.COMPLETED
    assert "deterministic_ocr_mock" in asset_ir[0]["quality_flags"]
    assert "vision_ocr_unconfigured" in asset_ir[0]["quality_flags"]
    assert "vision_analysis_disabled" in semantic["quality_flags"]
    assert diagnostics["kind"] == "image_adapter_diagnostics"
    assert diagnostics["ocr_provider"] == "deterministic"
    assert 'data-asset-id="doc_1_asset_0001"' in html


def test_docx_workflow_converts_then_runs_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    docx_path = tmp_path / "paper.docx"
    docx_path.write_bytes(b"PK\x03\x04docx")
    converted_pdf = tmp_path / "paper.pdf"
    converted_pdf.write_bytes(b"%PDF-1.7\n%%EOF")
    captured: dict[str, object] = {}

    def fake_convert_docx_to_pdf(**_kwargs):
        return converted_pdf, {
            "kind": "docx_conversion",
            "status": "completed",
            "converted_pdf_path": str(converted_pdf),
        }

    async def fake_run_graph_job(**kwargs):
        captured.update(kwargs)
        orchestrator.update_status(
            kwargs["job_id"],
            kwargs["filename"],
            kwargs["target_lang"],
            JobState.COMPLETED,
            1,
            "Completed",
            kwargs["doc_id"],
        )

    monkeypatch.setattr(orchestrator, "convert_docx_to_pdf", fake_convert_docx_to_pdf)
    monkeypatch.setattr(orchestrator, "_run_graph_job", fake_run_graph_job)

    asyncio.run(
        orchestrator.process_docx_document_job(
            "job_1",
            "doc_1",
            "paper.docx",
            docx_path,
            "zh-CN",
            UserIntent(workflow_mode="translate_and_typeset"),
        )
    )

    assert storage.load_status("job_1").status == JobState.COMPLETED
    assert captured["input_kind"] == "docx"
    assert captured["source_path"] == docx_path
    assert captured["content_source_path"] == converted_pdf
    assert storage.read_output_json("doc_1", "docx-conversion.json")["status"] == "completed"


def test_docx_workflow_fails_when_libreoffice_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    docx_path = tmp_path / "paper.docx"
    docx_path.write_bytes(b"PK\x03\x04docx")

    def fake_convert_docx_to_pdf(**_kwargs):
        raise orchestrator.DocxConversionError(
            "LibreOffice/soffice was not found.",
            {
                "kind": "docx_conversion",
                "status": "failed",
                "error": "LibreOffice/soffice was not found.",
                "recoverable": True,
            },
        )

    monkeypatch.setattr(orchestrator, "convert_docx_to_pdf", fake_convert_docx_to_pdf)

    asyncio.run(
        orchestrator.process_docx_document_job(
            "job_1",
            "doc_1",
            "paper.docx",
            docx_path,
            "zh-CN",
            UserIntent(workflow_mode="typeset_only", output_kind="typeset_document"),
        )
    )

    status = storage.load_status("job_1")
    diagnostics = storage.read_output_json("doc_1", "docx-conversion.json")
    assert status.status == JobState.FAILED
    assert "LibreOffice" in (status.error or "")
    assert diagnostics["status"] == "failed"
    assert diagnostics["recoverable"] is True
