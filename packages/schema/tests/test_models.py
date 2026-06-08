import pytest
from pydantic import ValidationError

from pdf_translator_schema import (
    Asset,
    AssetIR,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    InputSource,
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
from pdf_translator_schema.models import DocumentBlock, InlineItem
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
    assert chunk.render_defaults.font_stack[0] == "Noto Sans CJK SC"
    assert chunk.render_defaults.layout_mode == "source_bbox"
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
