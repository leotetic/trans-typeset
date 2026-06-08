import pytest
from pydantic import ValidationError

from pdf_translator_schema import (
    Asset,
    AssetIR,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
<<<<<<< HEAD
    FormulaIR,
    FormulaRecognitionResult,
=======
    Formula,
>>>>>>> codex/freatrue_formula
    InputSource,
    OCRRecognitionResult,
    PageSize,
    LayoutIntentBlock,
    LayoutIntentPlan,
    SemanticBlockSignal,
    SemanticLayoutAnalysis,
    SourceBlock,
    TranslationBlockPlan,
    TranslationChunk,
    TranslationLayoutPlan,
    UserIntent,
    WorkflowRun,
    WorkflowStep,
    TypesettingStandard,
    validate_layout_intent_plan,
    validate_layout_plan,
)
from pdf_translator_schema.json_schema import export_schema
from pdf_translator_schema.models import DocumentBlock, InlineItem, TextLineIR, TextSpanIR
from pdf_translator_schema.validation import (
    LayoutIntentPlanValidationError,
    LayoutPlanValidationError,
)


def test_render_defaults_are_available_on_chunk() -> None:
    chunk = TranslationChunk(
        chunk_id="chunk_1",
        source_blocks=[
            SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Hello")
        ],
    )

    assert chunk.target_lang == "zh-CN"
    assert chunk.render_defaults.font_stack == [
        "Times New Roman",
        "SimSun",
        "Songti SC",
        "Noto Serif CJK SC",
        "Source Han Serif SC",
        "serif",
    ]
    assert chunk.render_defaults.layout_mode == "continuous_reflow"
    assert chunk.render_defaults.page_layout.width_pt == 595.28
    assert chunk.render_defaults.page_layout.margin_top_pt == 70.87
    assert chunk.render_defaults.role_styles.paragraph.font_size_pt == 12.0
    assert chunk.render_defaults.role_styles.paragraph.line_height == 1.5
    assert chunk.render_defaults.alignment.paragraph == "justify"
    assert chunk.render_defaults.overflow_policy.min_font_scale == 0.86
    assert chunk.render_defaults.overflow_policy.allow_continuation_page is True
    assert chunk.render_defaults.preserve_policy.whitespace == "allow_reflow"


def test_v2_workflow_contract_defaults_are_available() -> None:
    source = InputSource(
        source_id="source_1",
        input_type="text",
        size_bytes=12,
        sha256="a" * 64,
    )
    asset = AssetIR(
        asset_id="asset_1",
        source_id="source_1",
        kind="image",
        quality_flags=["deterministic_ocr_mock"],
    )
    intent = UserIntent(
        target_lang="zh-CN",
        output_kind="typeset_document",
        style_intent="academic",
        instruction="按照gb-GB/T 7713.1 进行排版",
    )
    run = WorkflowRun(
        workflow_id="workflow_1",
        doc_id="doc_1",
        input_sources=[source],
        user_intent=intent,
        steps=[
            WorkflowStep(
                step_id="step_1",
                name="read_input",
                status="completed",
                output_artifacts=["normalized-input"],
            )
        ],
    )

    assert source.input_type == "text"
    assert source.source_role == "content"
    assert asset.quality_flags == ["deterministic_ocr_mock"]
    assert intent.output_kind == "typeset_document"
    assert intent.typesetting_standard == TypesettingStandard.NONE
    assert intent.constraints.allow_continuation is True
    assert run.steps[0].name == "read_input"


def test_input_source_can_mark_layout_reference_role() -> None:
    source = InputSource(
        source_id="layout_source",
        input_type="pdf",
        source_role="layout_reference",
        filename="style.pdf",
    )

    assert source.source_role == "layout_reference"


def test_semantic_layout_analysis_contract_defaults_are_available() -> None:
    analysis = SemanticLayoutAnalysis(
        analysis_id="analysis_1",
        doc_id="doc_1",
        block_signals=[
            SemanticBlockSignal(
                source_block_id="b1",
                role_candidates=[BlockRole.TITLE, BlockRole.HEADING],
                section_hint="title",
                confidence=0.82,
            )
        ],
        section_hints=["title"],
        quality_flags=["deterministic_semantic_analysis"],
    )

    assert analysis.schema_version == "0.1"
    assert analysis.block_signals[0].source_block_id == "b1"
    assert analysis.block_signals[0].role_candidates == [
        BlockRole.TITLE,
        BlockRole.HEADING,
    ]
    assert analysis.quality_flags == ["deterministic_semantic_analysis"]


def test_rejects_invalid_bbox() -> None:
    with pytest.raises(ValidationError):
        BoundingBox(x0=20, y0=10, x1=10, y1=30)


def test_document_rejects_duplicate_block_ids() -> None:
    block = DocumentBlock(
        block_id="b1",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=0, y0=0, x1=10, y1=10),
        reading_order=0,
    )
    with pytest.raises(ValidationError):
        DocumentIR(
            doc_id="doc_1",
            pages=[
                DocumentPage(
                    page_id="p1",
                    size=PageSize(width=100, height=100),
                    blocks=[block, block],
                )
            ],
        )


def test_document_rejects_duplicate_page_asset_and_reading_order_ids() -> None:
    block_1 = DocumentBlock(
        block_id="b1",
        page_id="p1",
        bbox=BoundingBox(x0=0, y0=0, x1=10, y1=10),
        reading_order=0,
    )
    block_2 = DocumentBlock(
        block_id="b2",
        page_id="p1",
        bbox=BoundingBox(x0=0, y0=20, x1=10, y1=30),
        reading_order=0,
    )
    with pytest.raises(ValidationError):
        DocumentPage(
            page_id="p1",
            size=PageSize(width=100, height=100),
            blocks=[block_1, block_2],
        )

    page = DocumentPage(page_id="p1", size=PageSize(width=100, height=100))
    with pytest.raises(ValidationError):
        DocumentIR(doc_id="doc_1", pages=[page, page])

    asset = Asset(
        asset_id="asset_1",
        page_id="p1",
        bbox=BoundingBox(x0=0, y0=0, x1=10, y1=10),
    )
    with pytest.raises(ValidationError):
        DocumentIR(
            doc_id="doc_1",
            pages=[
                DocumentPage(
                    page_id="p1",
                    size=PageSize(width=100, height=100),
                    assets=[asset],
                ),
                DocumentPage(
                    page_id="p2",
                    size=PageSize(width=100, height=100),
                    assets=[
                        Asset(
                            asset_id="asset_1",
                            page_id="p2",
                            bbox=BoundingBox(x0=0, y0=0, x1=10, y1=10),
                        )
                    ],
                ),
            ],
        )


<<<<<<< HEAD
def test_document_can_reference_formula_ir_from_block_and_asset() -> None:
    block = DocumentBlock(
        block_id="b_formula",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=0, y0=0, x1=100, y1=30),
        reading_order=0,
        source_text="x = y + 1",
        formula_id="formula_1",
    )
    asset = Asset(
        asset_id="asset_formula",
        page_id="p1",
        kind="formula",
        bbox=BoundingBox(x0=0, y0=0, x1=100, y1=30),
        formula_id="formula_1",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=100, height=100),
                blocks=[block],
                assets=[asset],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_1",
                page_id="p1",
                source_block_id="b_formula",
                asset_id="asset_formula",
                latex="x = y + 1",
                display_mode="display",
                source_kind="text_layer",
                confidence=0.95,
            )
        ],
    )

    assert document.formulas_by_id()["formula_1"].latex == "x = y + 1"
    assert document.pages[0].blocks[0].formula_id == "formula_1"
    assert document.pages[0].assets[0].formula_id == "formula_1"


def test_document_can_store_text_line_and_span_metadata_for_inline_formulas() -> None:
    span = TextSpanIR(
        span_id="s1",
        page_id="p1",
        block_id="b1",
        line_id="l1",
        text="E = mc^2",
        bbox=BoundingBox(x0=20, y0=10, x1=60, y1=20),
        font_name="Cambria Math",
        font_size=9,
        flags=0,
        origin=(20, 18),
    )
    line = TextLineIR(
        line_id="l1",
        page_id="p1",
        block_id="b1",
        text="where E = mc^2 holds",
        bbox=BoundingBox(x0=0, y0=8, x1=120, y1=22),
        span_ids=["s1"],
=======
def test_formula_contract_defaults_on_document_and_chunk_blocks() -> None:
    formula = Formula(
        formula_id="Fabc123",
        placeholder="@@FORMULA_Fabc123@@",
        kind="inline",
        source_text="x = y + 1",
        latex="x = y + 1",
        confidence=0.8,
>>>>>>> codex/freatrue_formula
    )
    block = DocumentBlock(
        block_id="b1",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
<<<<<<< HEAD
        bbox=BoundingBox(x0=0, y0=0, x1=160, y1=40),
        reading_order=0,
        source_text="where {{formula:f_inline}} holds",
        lines=[line],
        spans=[span],
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=200, height=200),
                blocks=[block],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="f_inline",
                page_id="p1",
                anchor_block_id="b1",
                latex="E = mc^2",
                source_text="E = mc^2",
                source_text_range=(6, 14),
                span_ids=["s1"],
                display_mode="inline",
                source_kind="inline_text",
                ocr_provider="deterministic",
                ocr_confidence=0.96,
            )
        ],
    )

    assert document.pages[0].blocks[0].spans[0].font_name == "Cambria Math"
    assert document.formulas_by_id()["f_inline"].display_mode == "inline"


def test_document_rejects_invalid_formula_refs() -> None:
    block = DocumentBlock(
        block_id="b1",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=0, y0=0, x1=100, y1=30),
        reading_order=0,
        formula_id="missing_formula",
    )

    with pytest.raises(ValidationError):
        DocumentIR(
            doc_id="doc_1",
            pages=[
                DocumentPage(
                    page_id="p1",
                    size=PageSize(width=100, height=100),
                    blocks=[block],
                )
            ],
        )

    with pytest.raises(ValidationError):
        DocumentIR(
            doc_id="doc_1",
            pages=[
                DocumentPage(
                    page_id="p1",
                    size=PageSize(width=100, height=100),
                )
            ],
            formulas=[
                FormulaIR(
                    formula_id="formula_1",
                    page_id="p1",
                    source_block_id="missing_block",
                    latex="x",
                )
            ],
        )
=======
        bbox=BoundingBox(x0=0, y0=0, x1=10, y1=10),
        reading_order=0,
        source_text="We use x = y + 1.",
        text_for_translation="We use @@FORMULA_Fabc123@@.",
        formulas=[formula],
    )
    source = SourceBlock(
        block_id="b1",
        role=BlockRole.PARAGRAPH,
        source_text=block.text_for_translation,
        preserve_tokens=[formula.placeholder],
    )

    assert block.formulas[0].placeholder == "@@FORMULA_Fabc123@@"
    assert block.text_for_translation == "We use @@FORMULA_Fabc123@@."
    assert source.requires_translation is True
    assert SourceBlock(
        block_id="formula",
        role=BlockRole.FORMULA,
        source_text="@@FORMULA_Fdisplay@@",
        requires_translation=False,
    ).requires_translation is False
>>>>>>> codex/freatrue_formula


def test_chunk_rejects_duplicate_source_block_ids() -> None:
    block = SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Hello")
    with pytest.raises(ValidationError):
        TranslationChunk(chunk_id="chunk_1", source_blocks=[block, block])


def test_layout_plan_requires_all_known_blocks() -> None:
    chunk = TranslationChunk(
        chunk_id="chunk_1",
        source_blocks=[
            SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Hello"),
            SourceBlock(block_id="b2", role=BlockRole.PARAGRAPH, source_text="World"),
        ],
    )
    plan = TranslationLayoutPlan(
        chunk_id="chunk_1",
        blocks=[
            TranslationBlockPlan(
                source_block_id="b1",
                translated_text="你好",
                role=BlockRole.PARAGRAPH,
            )
        ],
    )

    with pytest.raises(LayoutPlanValidationError):
        validate_layout_plan(chunk, plan)


def test_layout_plan_requires_preserve_tokens() -> None:
    chunk = TranslationChunk(
        chunk_id="chunk_1",
        source_blocks=[
            SourceBlock(
                block_id="b1",
                role=BlockRole.PARAGRAPH,
                source_text="See [1].",
                preserve_tokens=["[1]"],
            )
        ],
    )

    missing = TranslationLayoutPlan(
        chunk_id="chunk_1",
        blocks=[
            TranslationBlockPlan(
                source_block_id="b1",
                translated_text="参见文献。",
                role=BlockRole.PARAGRAPH,
            )
        ],
    )
    with pytest.raises(LayoutPlanValidationError):
        validate_layout_plan(chunk, missing)

    valid = TranslationLayoutPlan(
        chunk_id="chunk_1",
        blocks=[
            TranslationBlockPlan(
                source_block_id="b1",
                translated_text="参见文献。",
                role=BlockRole.PARAGRAPH,
                inline_items=[InlineItem(kind="citation", text="[1]", source_token="[1]")],
            )
        ],
    )
    assert validate_layout_plan(chunk, valid) is valid


def test_layout_intent_plan_requires_all_document_blocks() -> None:
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
                        bbox=BoundingBox(x0=0, y0=0, x1=10, y1=10),
                        reading_order=0,
                    ),
                    DocumentBlock(
                        block_id="b2",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=0, y0=20, x1=10, y1=30),
                        reading_order=1,
                    ),
                ],
            )
        ],
    )
    plan = LayoutIntentPlan(
        plan_id="plan_1",
        doc_id="doc_1",
        blocks=[
            LayoutIntentBlock(
                source_block_id="b1",
                role=BlockRole.PARAGRAPH,
            )
        ],
    )

    with pytest.raises(LayoutIntentPlanValidationError):
        validate_layout_intent_plan(document, plan)

    valid = LayoutIntentPlan(
        plan_id="plan_1",
        doc_id="doc_1",
        blocks=[
            LayoutIntentBlock(source_block_id="b1", role=BlockRole.PARAGRAPH),
            LayoutIntentBlock(source_block_id="b2", role=BlockRole.PARAGRAPH),
        ],
    )
    assert validate_layout_intent_plan(document, valid) is valid


@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        (
            InlineItem,
            {
                "kind": "text",
                "text": "你好",
                "bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
            },
        ),
        (
            TranslationBlockPlan,
            {
                "source_block_id": "b1",
                "translated_text": "你好",
                "role": BlockRole.PARAGRAPH,
                "page": 1,
            },
        ),
        (
            TranslationLayoutPlan,
            {
                "chunk_id": "chunk_1",
                "blocks": [
                    {
                        "source_block_id": "b1",
                        "translated_text": "你好",
                        "role": BlockRole.PARAGRAPH,
                    }
                ],
                "x": 10,
            },
        ),
        (
            LayoutIntentBlock,
            {
                "source_block_id": "b1",
                "role": BlockRole.PARAGRAPH,
                "x0": 10,
            },
        ),
        (
            LayoutIntentPlan,
            {
                "plan_id": "plan_1",
                "doc_id": "doc_1",
                "blocks": [
                    {"source_block_id": "b1", "role": BlockRole.PARAGRAPH}
                ],
                "page_number": 1,
            },
        ),
        (
            SemanticLayoutAnalysis,
            {
                "analysis_id": "analysis_1",
                "doc_id": "doc_1",
                "block_signals": [
                    {"source_block_id": "b1", "role_candidates": [BlockRole.TITLE]}
                ],
                "bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
            },
        ),
        (
            FormulaRecognitionResult,
            {
                "latex": "x = y + 1",
                "display_mode": "display",
                "page": 1,
            },
        ),
        (
            OCRRecognitionResult,
            {
                "text": "x = y + 1",
                "latex": "x = y + 1",
                "region_kind": "formula",
                "bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
            },
        ),
    ],
)
def test_layout_plan_rejects_layout_coordinates(model_cls: type, payload: dict) -> None:
    with pytest.raises(ValidationError):
        model_cls.model_validate(payload)


def test_json_schema_export_includes_metadata(tmp_path) -> None:
    export_schema(tmp_path)

    for filename in (
        "document-ir.schema.json",
        "input-source.schema.json",
        "asset-ir.schema.json",
        "formula-recognition.schema.json",
        "ocr-recognition.schema.json",
        "user-intent.schema.json",
        "workflow-run.schema.json",
        "layout-intent-plan.schema.json",
        "semantic-layout-analysis.schema.json",
        "translation-chunk.schema.json",
        "translation-layout-plan.schema.json",
    ):
        schema_text = (tmp_path / filename).read_text(encoding="utf-8")
        assert '"$schema": "https://json-schema.org/draft/2020-12/schema"' in schema_text
        assert '"x-schema-version": "0.1"' in schema_text
