from __future__ import annotations

import tomllib
from pathlib import Path

from pdf_renderer import RenderDocument, render_to_html
from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    LayoutIntentBlock,
    LayoutIntentPlan,
    TranslationBlockPlan,
    TranslationLayoutPlan,
)
from pdf_translator_schema.models import DocumentBlock, RenderDefaults, StyleSeed

RENDERER_ROOT = Path(__file__).resolve().parents[1]


def _block(
    block_id: str,
    role: BlockRole,
    bbox: BoundingBox,
    source_text: str = "Source text",
    font_size: float = 10,
    reading_order: int = 0,
) -> DocumentBlock:
    return DocumentBlock(
        block_id=block_id,
        page_id="p1",
        role=role,
        bbox=bbox,
        reading_order=reading_order,
        source_text=source_text,
        style_seed=StyleSeed(font_size=font_size),
    )


def _document(blocks: list[DocumentBlock], assets: list[Asset] | None = None) -> DocumentIR:
    return DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=blocks,
                assets=assets or [],
            )
        ],
    )


def _plan(*blocks: TranslationBlockPlan) -> TranslationLayoutPlan:
    return TranslationLayoutPlan(chunk_id="chunk_1", blocks=list(blocks))


def test_render_to_html_uses_original_page_size() -> None:
    block = _block(
        "p1_b1",
        BlockRole.TITLE,
        BoundingBox(x0=72, y0=72, x1=300, y1=110),
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(_document([block]), [], "zh-CN"))

    assert "size: 612.0pt 792.0pt" in html
    assert "--page-width-pt: 612.0pt" in html
    assert "--page-height-pt: 792.0pt" in html


def test_render_to_html_uses_schema_default_chinese_font_stack() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(_document([block]), [], "zh-CN"))

    assert (
        'font-family: "Noto Sans CJK SC", "Source Han Sans SC", '
        '"Arial Unicode MS", sans-serif'
    ) in html


def test_render_to_html_uses_configured_render_defaults() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        font_stack=["Custom CJK", "serif"],
        line_height=1.55,
        paragraph_spacing_em=0.25,
    )

    html = render_to_html(
        RenderDocument.from_ir_and_plans(
            _document([block]),
            [],
            "zh-CN",
            render_defaults=defaults,
        )
    )

    assert 'font-family: "Custom CJK", serif' in html
    assert "line-height: 1.55" in html


def test_render_to_html_includes_translated_text_and_block_id() -> None:
    block = _block(
        "p1_b1",
        BlockRole.TITLE,
        BoundingBox(x0=72, y0=72, x1=300, y1=110),
        source_text="A Paper",
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text="一篇论文",
            role=BlockRole.TITLE,
        )
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(_document([block]), [plan], "zh-CN"))

    assert "一篇论文" in html
    assert 'data-block-id="p1_b1"' in html


def test_missing_translation_falls_back_to_source_text_and_quality_flag() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
        source_text="Fallback source",
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(_document([block]), [], "zh-CN"))

    assert "Fallback source" in html
    assert "quality-missing-translation" in html


def test_role_class_is_stable_and_comes_from_source_block() -> None:
    block = _block(
        "p1_b1",
        BlockRole.HEADING,
        BoundingBox(x0=72, y0=120, x1=420, y1=150),
        source_text="Heading",
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text="标题",
            role=BlockRole.PARAGRAPH,
        )
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(_document([block]), [plan], "zh-CN"))

    assert 'class="block role-heading intent-normal quality-role-mismatch"' in html
    assert 'class="block role-paragraph' not in html


def test_structured_roles_have_dedicated_rendering_rules() -> None:
    table = _block(
        "p1_table",
        BlockRole.TABLE,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
        source_text="A  B  C  D",
        reading_order=0,
    )
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=72, y0=200, x1=420, y1=230),
        source_text="x = y + 1",
        reading_order=1,
    )
    footnote = _block(
        "p1_footnote",
        BlockRole.FOOTNOTE,
        BoundingBox(x0=72, y0=700, x1=420, y1=730),
        source_text="1 A footnote.",
        reading_order=2,
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(_document([table, formula, footnote]), [], "zh-CN"))

    assert 'class="block role-table intent-normal quality-missing-translation"' in html
    assert 'class="block role-formula intent-normal quality-missing-translation"' in html
    assert 'class="block role-footnote intent-normal quality-missing-translation"' in html
    assert ".role-table" in html
    assert "ui-monospace" in html
    assert ".role-formula" in html
    assert "Cambria Math" in html
    assert ".role-footnote" in html


def test_original_bbox_is_a_hard_constraint() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(_document([block]), [], "zh-CN"))

    assert "--x-pt: 72.0pt" in html
    assert "--y-pt: 120.0pt" in html
    assert "--w-pt: 348.0pt" in html
    assert "--h-pt: 60.0pt" in html
    assert "height: var(--h-pt)" in html


def test_compact_intent_scales_font_and_expands_box_before_continuation() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=132, y1=140),
        font_size=12,
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text="这是一段很长很长很长很长很长很长很长的译文，必须留在原始块内。",
            role=BlockRole.PARAGRAPH,
            render_intent="compact",
        )
    )

    render_document = RenderDocument.from_ir_and_plans(_document([block]), [plan], "zh-CN")
    render_block = render_document.pages[0].blocks[0]
    html = render_to_html(render_document)

    assert render_block.font_size_pt < block.style_seed.font_size
    assert render_block.bbox.y1 > block.bbox.y1
    assert "quality-box-expanded" in html


def test_layout_intent_plan_applies_semantic_intent_without_overriding_bbox() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=132, y1=140),
        font_size=12,
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text="这是一段很长很长很长很长很长很长很长的译文，必须留在原始块内。",
            role=BlockRole.PARAGRAPH,
            render_intent="normal",
        )
    )
    layout_intent = LayoutIntentPlan(
        plan_id="plan_1",
        doc_id="doc_1",
        blocks=[
            LayoutIntentBlock(
                source_block_id="p1_b1",
                role=BlockRole.PARAGRAPH,
                render_intent="compact",
                quality_flags=["repair_compact_intent"],
            )
        ],
    )

    render_document = RenderDocument.from_ir_and_plans(
        _document([block]),
        [plan],
        "zh-CN",
        layout_intent_plan=layout_intent,
    )
    render_block = render_document.pages[0].blocks[0]

    assert render_block.render_intent == "compact"
    assert render_block.bbox.x0 == block.bbox.x0
    assert "repair_compact_intent" in render_block.quality_flags


def test_normal_intent_scales_font_when_overflow_risk_is_detected() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=132, y1=140),
        font_size=12,
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text="这是一段很长很长很长很长很长很长很长的译文，必须留在原始块内。",
            role=BlockRole.PARAGRAPH,
        )
    )

    render_document = RenderDocument.from_ir_and_plans(_document([block]), [plan], "zh-CN")
    render_block = render_document.pages[0].blocks[0]

    assert render_block.font_size_pt < block.style_seed.font_size
    assert "font_scaled" in render_block.quality_flags


def test_overflow_scaling_uses_configured_min_font_scale() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=132, y1=140),
        font_size=12,
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text="这是一段很长很长很长很长很长很长很长的译文，必须留在原始块内。",
            role=BlockRole.PARAGRAPH,
        )
    )
    defaults = RenderDefaults(target_lang="zh-CN")
    defaults.overflow_policy.min_font_scale = 0.7

    render_document = RenderDocument.from_ir_and_plans(
        _document([block]),
        [plan],
        "zh-CN",
        render_defaults=defaults,
    )
    render_block = render_document.pages[0].blocks[0]

    assert render_block.font_scale == 0.7


def test_overflow_creates_continuation_page_after_scaling_and_expansion() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=720, x1=132, y1=735),
        font_size=12,
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text=" ".join(["long translated sentence"] * 80),
            role=BlockRole.PARAGRAPH,
        )
    )

    render_document = RenderDocument.from_ir_and_plans(_document([block]), [plan], "zh-CN")
    html = render_to_html(render_document)

    assert len(render_document.pages) > 1
    assert render_document.pages[1].page_id.startswith("p1_cont_")
    assert render_document.pages[0].blocks[0].quality_flags == [
        "font_scaled",
        "continued_on_next_page",
    ]
    assert "quality-continuation-page" in html
    assert 'data-block-id="p1_b1__cont_01"' in html


def test_render_document_diagnostics_reports_quality_flags() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=132, y1=140),
        source_text="Source fallback",
        font_size=12,
    )

    diagnostics = RenderDocument.from_ir_and_plans(
        _document([block]), [], "zh-CN"
    ).diagnostics()

    assert diagnostics["doc_id"] == "doc_1"
    assert diagnostics["page_count"] == 1
    assert diagnostics["block_count"] == 1
    assert diagnostics["quality_flag_counts"]["missing_translation"] == 1
    assert diagnostics["pages"][0]["flagged_blocks"][0]["block_id"] == "p1_b1"


def test_render_to_html_preserves_image_assets_at_document_bbox() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=160, x1=420, y1=190),
    )
    asset = Asset(
        asset_id="asset_1",
        page_id="p1",
        kind="image",
        bbox=BoundingBox(x0=90, y0=80, x1=240, y1=140),
        path="/api/documents/doc_1/assets/asset_1.png",
        alt_text="Figure asset",
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(_document([block], [asset]), [], "zh-CN"))

    assert 'data-asset-id="asset_1"' in html
    assert 'src="/api/documents/doc_1/assets/asset_1.png"' in html
    assert "--x-pt: 90.0pt" in html
    assert "--w-pt: 150.0pt" in html


def test_missing_asset_path_is_diagnostic_not_silent() -> None:
    asset = Asset(
        asset_id="asset_1",
        page_id="p1",
        kind="image",
        bbox=BoundingBox(x0=90, y0=80, x1=240, y1=140),
    )

    render_document = RenderDocument.from_ir_and_plans(_document([], [asset]), [], "zh-CN")
    diagnostics = render_document.diagnostics()
    html = render_to_html(render_document)

    assert diagnostics["quality_flag_counts"]["asset_missing_path"] == 1
    assert "quality-asset-missing-path" in html
    assert '<div class="asset-placeholder">image</div>' in html


def test_vector_placeholder_asset_preserves_bbox_and_reports_missing_path() -> None:
    asset = Asset(
        asset_id="vector_1",
        page_id="p1",
        kind="figure",
        bbox=BoundingBox(x0=80, y0=90, x1=260, y1=210),
        alt_text="PDF vector drawing placeholder",
    )

    render_document = RenderDocument.from_ir_and_plans(_document([], [asset]), [], "zh-CN")
    html = render_to_html(render_document)

    assert 'data-asset-id="vector_1"' in html
    assert 'data-asset-kind="figure"' in html
    assert "--x-pt: 80.0pt" in html
    assert "--w-pt: 180.0pt" in html
    assert "quality-asset-missing-path" in html
    assert '<div class="asset-placeholder">figure</div>' in html


def test_layout_issues_detect_overlap_and_outside_page() -> None:
    first = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=180, y1=170),
        source_text="First",
        reading_order=0,
    )
    second = _block(
        "p1_b2",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=90, y0=130, x1=190, y1=180),
        source_text="Second",
        reading_order=1,
    )
    asset = Asset(
        asset_id="asset_1",
        page_id="p1",
        kind="image",
        bbox=BoundingBox(x0=580, y0=760, x1=640, y1=820),
    )

    diagnostics = RenderDocument.from_ir_and_plans(
        _document([first, second], [asset]), [], "zh-CN"
    ).diagnostics()

    issue_kinds = {issue["kind"] for issue in diagnostics["layout_issues"]}
    assert "overlap" in issue_kinds
    assert "bbox_outside_page" in issue_kinds


def test_jinja_templates_are_declared_as_package_data() -> None:
    pyproject = tomllib.loads((RENDERER_ROOT / "pyproject.toml").read_text())

    assert pyproject["tool"]["setuptools"]["package-data"]["pdf_renderer"] == [
        "templates/*.j2"
    ]
