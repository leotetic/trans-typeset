import pytest
from pydantic import ValidationError

from pdf_translator_schema import (
    AcademicRequirement,
    ArticleBrief,
    Asset,
    AssetIR,
    BlockRole,
    BoundingBox,
    BibliographyPlan,
    CitationStyle,
    ColumnLayoutDefaults,
    DocumentIR,
    DocumentPage,
    DocumentKind,
    DocumentProfile,
    DocumentStructurePlan,
    DocumentStructureSection,
    EditScope,
    Formula,
    FormulaIR,
    FormulaRecognitionResult,
    InputKind,
    InputSource,
    OCRRecognitionResult,
    PageSize,
    LayoutIntentBlock,
    LayoutIntentPlan,
    NumberingPlan,
    NumberingRule,
    SemanticBlockSignal,
    SemanticLayoutAnalysis,
    SectionKind,
    SourceBlock,
    TaskIntent,
    TranslationBlockPlan,
    TranslationChunk,
    TranslationLayoutPlan,
    UserIntent,
    WorkflowMode,
    WorkflowRun,
    WorkflowStep,
    TypesettingStandard,
    all_schemas,
    schema_for,
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
        source_blocks=[SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Hello")],
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
    assert chunk.render_defaults.formula_numbering == "none"
    assert chunk.render_defaults.column_layout.column_count == 1
    assert chunk.render_defaults.column_layout.column_gap_pt == 18.0
    assert chunk.render_defaults.column_layout.scope == "body"
    assert chunk.render_defaults.column_layout.balance_columns is False
    assert chunk.render_defaults.role_styles.paragraph.font_stack is None
    assert chunk.render_defaults.role_styles.heading.font_stack is not None
    assert "SimHei" in chunk.render_defaults.role_styles.heading.font_stack
    assert "SimHei" in chunk.render_defaults.role_styles.title.font_stack


def test_article_brief_is_available_on_translation_chunk_and_schema() -> None:
    brief = ArticleBrief(
        title="A Paper",
        field="machine learning",
        background="The paper studies retrieval.",
        main_idea="A local pipeline improves translation.",
        contribution="It preserves academic terms.",
        key_terms={"retrieval augmented generation": "检索增强生成"},
    )
    chunk = TranslationChunk(
        chunk_id="chunk_1",
        source_blocks=[SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Hello")],
        article_brief=brief,
    )

    assert chunk.article_brief is not None
    assert chunk.article_brief.schema_version == "0.1"
    assert chunk.article_brief.key_terms["retrieval augmented generation"] == "检索增强生成"
    assert "article-brief.schema.json" in all_schemas()
    assert schema_for("article-brief")["properties"]["title"]["type"] == "string"
    assert "article_brief" in schema_for("translation-chunk")["properties"]


def test_render_defaults_accept_gbt_formula_numbering_and_role_fonts() -> None:
    from pdf_translator_schema.models import RenderDefaults, RoleStyleDefaults

    defaults = RenderDefaults(formula_numbering="parenthesized")
    assert defaults.formula_numbering == "parenthesized"

    style = RoleStyleDefaults(font_size_pt=14.0, font_stack=["SimHei", "sans-serif"])
    assert style.font_stack == ["SimHei", "sans-serif"]

    with pytest.raises(ValidationError):
        RenderDefaults(formula_numbering="roman")
    with pytest.raises(ValidationError):
        RoleStyleDefaults(font_size_pt=14.0, font_stack=[])


def test_column_layout_defaults_reject_unsupported_column_counts() -> None:
    assert ColumnLayoutDefaults(column_count=2).column_count == 2

    with pytest.raises(ValidationError):
        ColumnLayoutDefaults(column_count=3)


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


def test_workflow_mode_and_docx_input_are_first_class() -> None:
    intent = UserIntent(workflow_mode="typeset_only", output_kind="typeset_document")
    source = InputSource(
        source_id="docx_source",
        input_type="docx",
        filename="paper.docx",
    )
    plan = LayoutIntentPlan(plan_id="layout_1", doc_id="doc_1", workflow_mode="translate_only")

    assert intent.workflow_mode == WorkflowMode.TYPESET_ONLY
    assert source.input_type == InputKind.DOCX
    assert plan.workflow_mode == WorkflowMode.TRANSLATE_ONLY


def test_input_source_can_mark_layout_reference_role() -> None:
    source = InputSource(
        source_id="layout_source",
        input_type="pdf",
        source_role="layout_reference",
        filename="style.pdf",
    )

    assert source.source_role == "layout_reference"


def test_edit_scope_validates_pages_and_blocks() -> None:
    assert EditScope().mode == "all"
    assert EditScope(mode="pages", page_numbers=[1, 3]).page_numbers == [1, 3]
    assert EditScope(mode="blocks", block_ids=["b1", "b2"]).block_ids == ["b1", "b2"]

    with pytest.raises(ValidationError):
        EditScope(mode="pages")
    with pytest.raises(ValidationError):
        EditScope(mode="pages", page_numbers=[0])
    with pytest.raises(ValidationError):
        EditScope(mode="blocks", block_ids=["b1", "b1"])
    with pytest.raises(ValidationError):
        EditScope(mode="blocks", page_numbers=[1], block_ids=["b1"])


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

    assert analysis.schema_version == "0.2"
    assert analysis.block_signals[0].source_block_id == "b1"
    assert analysis.block_signals[0].role_candidates == [
        BlockRole.TITLE,
        BlockRole.HEADING,
    ]
    assert analysis.quality_flags == ["deterministic_semantic_analysis"]


def test_v02_user_intent_defaults_are_compatible() -> None:
    intent = UserIntent(target_lang="zh-CN")

    assert intent.schema_version == "0.2"
    assert intent.task_intent.document_kind == DocumentKind.GENERIC_ACADEMIC
    assert [target.format for target in intent.output_targets] == [
        "html_preview",
        "pdf",
    ]
    assert intent.template_profile.source == "default_academic"
    assert intent.bibliography_preference.citation_style == CitationStyle.AUTO
    assert intent.requirements == []


def test_academic_requirements_flow_through_intent_analysis_and_plan() -> None:
    requirement = AcademicRequirement(
        requirement_id="cover_page",
        label="Cover page",
        category="structure",
        section_kinds=[SectionKind.COVER],
        evidence=["cover_page_keyword"],
    )
    intent = UserIntent(requirements=[requirement])
    analysis = SemanticLayoutAnalysis(
        analysis_id="analysis_1",
        doc_id="doc_1",
        recognized_requirements=[requirement],
    )
    plan = LayoutIntentPlan(
        plan_id="plan_1",
        doc_id="doc_1",
        requirements=[requirement],
    )

    assert intent.requirements[0].requirement_id == "cover_page"
    assert analysis.recognized_requirements[0].section_kinds == [SectionKind.COVER]
    assert plan.requirements[0].evidence == ["cover_page_keyword"]


def test_v01_user_intent_payload_still_validates() -> None:
    intent = UserIntent.model_validate(
        {
            "schema_version": "0.1",
            "target_lang": "zh-CN",
            "output_kind": "translation",
            "style_intent": "academic",
        }
    )

    assert intent.schema_version == "0.1"
    assert intent.task_intent.document_kind == DocumentKind.GENERIC_ACADEMIC
    assert intent.output_targets[0].format == "html_preview"


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
    )
    block = DocumentBlock(
        block_id="b1",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
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


def test_formula_contract_defaults_on_document_and_chunk_blocks() -> None:
    formula = Formula(
        formula_id="Fabc123",
        placeholder="@@FORMULA_Fabc123@@",
        kind="inline",
        source_text="x = y + 1",
        latex="x = y + 1",
        confidence=0.8,
    )
    block = DocumentBlock(
        block_id="b1",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
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
    assert (
        SourceBlock(
            block_id="formula",
            role=BlockRole.FORMULA,
            source_text="@@FORMULA_Fdisplay@@",
            requires_translation=False,
        ).requires_translation
        is False
    )


def test_chunk_rejects_duplicate_source_block_ids() -> None:
    block = SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Hello")
    with pytest.raises(ValidationError):
        TranslationChunk(chunk_id="chunk_1", source_blocks=[block, block])


@pytest.mark.parametrize("tokens", [[""], ["[1]", "[1]"]])
def test_source_block_rejects_invalid_preserve_tokens(tokens: list[str]) -> None:
    with pytest.raises(ValidationError):
        SourceBlock(
            block_id="b1",
            role=BlockRole.PARAGRAPH,
            source_text="See [1].",
            preserve_tokens=tokens,
        )


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


def test_v02_layout_intent_plan_structure_defaults_and_validation() -> None:
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
                        role=BlockRole.TITLE,
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
        document_profile=DocumentProfile(
            document_kind=DocumentKind.UNDERGRADUATE_THESIS,
            citation_style=CitationStyle.GB_T_7714,
        ),
        structure_plan=DocumentStructurePlan(
            sections=[
                DocumentStructureSection(
                    section_id="title_01",
                    kind=SectionKind.TITLE,
                    source_block_ids=["b1"],
                ),
                DocumentStructureSection(
                    section_id="body_01",
                    kind=SectionKind.BODY,
                    source_block_ids=["b2"],
                ),
            ]
        ),
        numbering_plan=NumberingPlan(
            heading_numbering=NumberingRule(
                enabled=True,
                style="arabic",
                section_ids=["body_01"],
            )
        ),
        bibliography_plan=BibliographyPlan(citation_style="gb_t_7714"),
        blocks=[
            LayoutIntentBlock(source_block_id="b1", role=BlockRole.TITLE),
            LayoutIntentBlock(source_block_id="b2", role=BlockRole.PARAGRAPH),
        ],
    )

    assert plan.schema_version == "0.2"
    assert validate_layout_intent_plan(document, plan) is plan


def test_layout_intent_plan_rejects_unknown_structure_block_id() -> None:
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
                    )
                ],
            )
        ],
    )
    plan = LayoutIntentPlan(
        plan_id="plan_1",
        doc_id="doc_1",
        structure_plan=DocumentStructurePlan(
            sections=[
                DocumentStructureSection(
                    section_id="body_01",
                    kind=SectionKind.BODY,
                    source_block_ids=["missing"],
                )
            ]
        ),
        blocks=[LayoutIntentBlock(source_block_id="b1", role=BlockRole.PARAGRAPH)],
    )

    with pytest.raises(LayoutIntentPlanValidationError, match="unknown source_block_id"):
        validate_layout_intent_plan(document, plan)


def test_layout_intent_plan_rejects_unknown_numbering_section_id() -> None:
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
                    )
                ],
            )
        ],
    )
    plan = LayoutIntentPlan(
        plan_id="plan_1",
        doc_id="doc_1",
        structure_plan=DocumentStructurePlan(
            sections=[
                DocumentStructureSection(
                    section_id="body_01",
                    kind=SectionKind.BODY,
                    source_block_ids=["b1"],
                )
            ]
        ),
        numbering_plan=NumberingPlan(
            heading_numbering=NumberingRule(section_ids=["missing_section"])
        ),
        blocks=[LayoutIntentBlock(source_block_id="b1", role=BlockRole.PARAGRAPH)],
    )

    with pytest.raises(LayoutIntentPlanValidationError, match="unknown section_id"):
        validate_layout_intent_plan(document, plan)


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
                "blocks": [{"source_block_id": "b1", "role": BlockRole.PARAGRAPH}],
                "page_number": 1,
            },
        ),
        (
            SemanticLayoutAnalysis,
            {
                "analysis_id": "analysis_1",
                "doc_id": "doc_1",
                "block_signals": [{"source_block_id": "b1", "role_candidates": [BlockRole.TITLE]}],
                "bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1},
            },
        ),
        (
            TaskIntent,
            {
                "document_kind": "generic_academic",
                "page": 1,
            },
        ),
        (
            DocumentStructureSection,
            {
                "section_id": "body_01",
                "kind": "body",
                "source_block_ids": ["b1"],
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
    expected_filenames = (
        "article-brief.schema.json",
        "document-ir.schema.json",
        "input-source.schema.json",
        "asset-ir.schema.json",
        "edit-scope.schema.json",
        "formula-recognition.schema.json",
        "ocr-recognition.schema.json",
        "user-intent.schema.json",
        "workflow-run.schema.json",
        "layout-intent-plan.schema.json",
        "semantic-layout-analysis.schema.json",
        "translation-chunk.schema.json",
        "translation-layout-plan.schema.json",
    )

    exported = export_schema(tmp_path)
    assert tuple(exported) == expected_filenames

    for filename in expected_filenames:
        schema_text = (tmp_path / filename).read_text(encoding="utf-8")
        assert '"$schema": "https://json-schema.org/draft/2020-12/schema"' in schema_text
        assert '"x-schema-version": "0.2"' in schema_text
        assert exported[filename] == tmp_path / filename


def test_json_schema_helpers_include_contract_models() -> None:
    schemas = all_schemas()
    layout_plan_schema = schema_for("translation-layout-plan")

    assert layout_plan_schema["title"] == "TranslationLayoutPlan"
    assert schemas["translation-layout-plan.schema.json"] == layout_plan_schema
    assert sorted(schemas) == [
        "article-brief.schema.json",
        "asset-ir.schema.json",
        "document-ir.schema.json",
        "edit-scope.schema.json",
        "formula-recognition.schema.json",
        "input-source.schema.json",
        "layout-intent-plan.schema.json",
        "ocr-recognition.schema.json",
        "semantic-layout-analysis.schema.json",
        "translation-chunk.schema.json",
        "translation-layout-plan.schema.json",
        "user-intent.schema.json",
        "workflow-run.schema.json",
    ]
    assert layout_plan_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert layout_plan_schema["x-schema-version"] == "0.2"
    assert "column_layout" in schema_for("user-intent")["properties"]
    assert "column_layout" in schema_for("layout-intent-plan")["properties"]
    assert (
        "column_layout" in schema_for("translation-chunk")["$defs"]["RenderDefaults"]["properties"]
    )


def test_json_schema_helper_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="unknown schema"):
        schema_for("missing")
