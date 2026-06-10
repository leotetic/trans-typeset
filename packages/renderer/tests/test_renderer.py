from __future__ import annotations

import builtins
import json
import tomllib
from pathlib import Path

import pdf_renderer.renderer as renderer_module
import pytest
from pdf_renderer import RenderDocument, render_to_html, render_to_pdf
from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    FormulaIR,
    LayoutIntentBlock,
    LayoutIntentPlan,
    PageSize,
    TranslationBlockPlan,
    TranslationLayoutPlan,
)
from pdf_translator_schema.models import DocumentBlock, Formula, RenderDefaults, StyleSeed

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


def _source_bbox_defaults(**updates: object) -> RenderDefaults:
    return RenderDefaults(target_lang="zh-CN", layout_mode="source_bbox", **updates)


def _render_source_bbox(
    document: DocumentIR,
    plans: list[TranslationLayoutPlan] | None = None,
    *,
    layout_intent_plan: LayoutIntentPlan | None = None,
    defaults: RenderDefaults | None = None,
) -> RenderDocument:
    return RenderDocument.from_ir_and_plans(
        document,
        plans or [],
        "zh-CN",
        render_defaults=defaults or _source_bbox_defaults(),
        layout_intent_plan=layout_intent_plan,
    )


def test_render_to_html_uses_original_page_size() -> None:
    block = _block(
        "p1_b1",
        BlockRole.TITLE,
        BoundingBox(x0=72, y0=72, x1=300, y1=110),
    )

    html = render_to_html(_render_source_bbox(_document([block])))

    assert "size: 612.0pt 792.0pt" in html
    assert "--page-width-pt: 612.0pt" in html
    assert "--page-height-pt: 792.0pt" in html


def test_render_to_html_uses_schema_default_gbt_font_stack() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(_document([block]), [], "zh-CN"))

    assert (
        'font-family: "Times New Roman", "SimSun", "Songti SC", '
        '"Noto Serif CJK SC", "Source Han Serif SC", serif'
    ) in html
    assert "Noto Sans CJK SC" not in html


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
        layout_mode="source_bbox",
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
    assert "line-height: var(--line-height, 1.55)" in html


def test_render_to_pdf_falls_back_when_playwright_driver_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright.async_api":
            raise RuntimeError("driver unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    output_path = tmp_path / "fallback.pdf"
    import asyncio

    asyncio.run(render_to_pdf("<html><body><p>Translated text</p></body></html>", output_path))

    assert output_path.read_bytes().startswith(b"%PDF-")


def test_render_to_pdf_writes_diagnostics_for_driver_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright.async_api":
            raise RuntimeError("Connection closed while reading from the driver")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "/custom/node")
    output_path = tmp_path / "fallback.pdf"
    diagnostics_path = tmp_path / "pdf-export-diagnostics.json"

    import asyncio

    asyncio.run(
        render_to_pdf(
            "<html><body><p>Translated text</p></body></html>",
            output_path,
            diagnostics_path=diagnostics_path,
        )
    )

    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["kind"] == "pdf_export"
    assert diagnostics["status"] == "fallback_pdf"
    assert diagnostics["playwright_nodejs_path"] == "/custom/node"
    assert diagnostics["playwright_nodejs_path_source"] == "env"
    assert "PLAYWRIGHT_NODEJS_PATH" in diagnostics["error"]
    assert diagnostics["output_bytes"] > 0


def test_pdf_export_inlines_api_asset_sources(tmp_path: Path) -> None:
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / "asset_1.png").write_bytes(b"image-bytes")
    diagnostics = {
        "asset_rewrites": {
            "inlined": 0,
            "missing": [],
        }
    }
    html = '<img src="/api/documents/doc_1/assets/asset_1.png" alt="figure" />'

    rewritten = renderer_module._inline_api_asset_sources(html, asset_dir, diagnostics)

    assert 'src="data:image/png;base64,' in rewritten
    assert diagnostics["asset_rewrites"]["inlined"] == 1
    assert diagnostics["asset_rewrites"]["missing"] == []


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

    html = render_to_html(_render_source_bbox(_document([table, formula, footnote])))

    assert 'class="block role-table intent-normal quality-missing-translation"' in html
    assert 'class="block role-formula intent-normal quality-missing-translation"' in html
    assert 'class="block role-footnote intent-normal quality-missing-translation"' in html
    assert ".role-table" in html
    assert "ui-monospace" not in html
    assert ".role-formula" in html
    assert ".role-footnote" in html

def test_formula_block_renders_internal_latex_markup() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=72, y0=200, x1=420, y1=230),
        source_text="{{formula:formula_1}}",
        reading_order=0,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[formula],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_1",
                page_id="p1",
                source_block_id="p1_formula",
                latex=r"E = mc^2",
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(document, [], "zh-CN"))

    assert 'class="katex-display"' in html
    assert 'data-latex="E = mc^2"' in html
    assert "{{formula:formula_1}}" not in html


def test_small_bbox_display_formula_expands_before_font_scaling() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=72, y0=200, x1=420, y1=212),
        source_text="{{formula:formula_1}}",
        font_size=12,
        reading_order=0,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[formula],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_1",
                page_id="p1",
                source_block_id="p1_formula",
                latex=r"\frac{\partial V}{\partial t} = \nabla^2 V + \sum_i x_i",
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )

    render_document = _render_source_bbox(document)
    render_block = render_document.pages[0].blocks[0]
    html = render_to_html(render_document)

    assert render_block.bbox.y1 > formula.bbox.y1
    assert render_block.font_scale == pytest.approx(1.0)
    assert render_block.font_size_pt == pytest.approx(12.0)
    assert "box_expanded" in render_block.quality_flags
    assert "font_scaled" not in render_block.quality_flags
    assert 'class="katex-display"' in html
    assert "quality-box-expanded" in html
    assert "--h-pt: 12.0pt" not in html


def test_inline_formula_ref_renders_inside_paragraph() -> None:
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
        source_text="Energy {{formula:formula_inline}} is preserved.",
        reading_order=0,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[paragraph],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_inline",
                page_id="p1",
                anchor_block_id="p1_body",
                latex=r"E = mc^2",
                display_mode="inline",
                source_kind="inline_text",
            )
        ],
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(document, [], "zh-CN"))

    assert 'class="katex"' in html
    assert 'class="katex-display"' not in html
    assert "Energy " in html
    assert "{{formula:formula_inline}}" not in html


def test_invalid_formula_latex_falls_back_with_quality_flag() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=72, y0=200, x1=420, y1=230),
        source_text="{{formula:formula_bad}}",
        reading_order=0,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[formula],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_bad",
                page_id="p1",
                source_block_id="p1_formula",
                latex=r"\frac{x",
                source_text="x / ?",
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )

    render_document = RenderDocument.from_ir_and_plans(document, [], "zh-CN")
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()
    block_html = render_document.pages[0].blocks[0].html or ""

    assert "quality-formula-plaintext-fallback" in html
    assert '<span class="formula-plaintext-fallback">x / ?</span>' in html
    assert diagnostics["quality_flag_counts"]["formula_plaintext_fallback"] == 1
    assert "quality-formula-render-failed" not in html
    assert "formula-render-failed" not in html
    assert "katex-error" not in block_html
    assert "{{formula:formula_bad}}" not in html


def test_missing_formula_ref_does_not_render_raw_placeholder() -> None:
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
        source_text="Missing {{formula:formula_missing}} should be diagnostic.",
        reading_order=0,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[paragraph],
            )
        ],
    )

    render_document = _render_source_bbox(document)
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()

    assert "{{formula:formula_missing}}" not in html
    assert 'data-unresolved-formula-id="formula_missing"' in html
    assert '<span class="formula-plaintext-fallback">formula formula_missing</span>' in html
    assert diagnostics["quality_flag_counts"]["unresolved_formula_placeholder"] == 1
    assert diagnostics["unresolved_formula_placeholders"] == [
        {
            "page_id": "p1",
            "block_id": "p1_body",
            "formula_id": "formula_missing",
            "placeholder": "{{formula:formula_missing}}",
        }
    ]


def test_mixed_known_and_unknown_formula_refs_do_not_leak_raw_placeholders() -> None:
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=190),
        source_text="Known {{formula:formula_inline}} and missing {{formula:formula_missing}}.",
        reading_order=0,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[paragraph],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_inline",
                page_id="p1",
                anchor_block_id="p1_body",
                latex=r"E = mc^2",
                display_mode="inline",
                source_kind="inline_text",
            )
        ],
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_body",
            translated_text=(
                "已知 {{formula:formula_inline}}，缺失 {{formula:formula_missing}}。"
            ),
            role=BlockRole.PARAGRAPH,
        )
    )

    render_document = _render_source_bbox(document, [plan])
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()

    assert "{{formula:" not in html
    assert 'data-formula-id="formula_inline"' in html
    assert 'data-unresolved-formula-id="formula_missing"' in html
    assert diagnostics["formula_rendered_count"] == 1
    assert diagnostics["quality_flag_counts"]["unresolved_formula_placeholder"] == 1


def test_unresolved_legacy_formula_placeholder_does_not_leak_raw_token() -> None:
    placeholder = "@@FORMULA_Fmissing@@"
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
        source_text=f"We solve {placeholder}.",
    ).model_copy(
        update={"text_for_translation": f"We solve {placeholder}."},
        deep=True,
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text=f"我们求解 {placeholder}。",
            role=BlockRole.PARAGRAPH,
        )
    )

    render_document = _render_source_bbox(_document([block]), [plan])
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()

    assert "@@FORMULA_" not in html
    assert 'data-unresolved-formula-id="Fmissing"' in html
    assert '<span class="formula-plaintext-fallback">formula Fmissing</span>' in html
    assert diagnostics["quality_flag_counts"]["unresolved_formula_placeholder"] == 1


def test_invalid_legacy_formula_placeholder_uses_plaintext_fallback() -> None:
    placeholder = "@@FORMULA_Fbad@@"
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
        source_text="We solve @bad.",
    ).model_copy(
        update={
            "text_for_translation": f"We solve {placeholder}.",
            "formulas": [
                Formula(
                    formula_id="Fbad",
                    placeholder=placeholder,
                    kind="inline",
                    source_text="x / ?",
                    latex=r"\frac{x",
                )
            ],
        },
        deep=True,
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text=f"我们求解 {placeholder}。",
            role=BlockRole.PARAGRAPH,
        )
    )

    render_document = _render_source_bbox(_document([block]), [plan])
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()
    block_html = render_document.pages[0].blocks[0].html or ""

    assert "@@FORMULA_" not in html
    assert '<span class="formula-plaintext-fallback">x / ?</span>' in html
    assert diagnostics["quality_flag_counts"]["formula_plaintext_fallback"] == 1
    assert "quality-formula-render-failed" not in html
    assert "formula-render-failed" not in html
    assert "katex-error" not in block_html


def test_formula_renderer_structures_common_latex_commands() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=72, y0=200, x1=420, y1=230),
        source_text="{{formula:formula_1}}",
        reading_order=0,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[formula],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_1",
                page_id="p1",
                source_block_id="p1_formula",
                latex=r"\frac{\alpha^2}{\sqrt{x}}",
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )

    html = render_to_html(RenderDocument.from_ir_and_plans(document, [], "zh-CN"))

    assert 'data-latex="\\frac{\\alpha^2}{\\sqrt{x}}"' in html
    assert 'class="katex-frac"' in html or "<mfrac>" in html
    assert "α<sup>2</sup>" in html or "<mi>α</mi>" in html
    assert 'class="katex-sqrt"' in html or "<msqrt>" in html


def test_formula_placeholders_are_rendered_as_formula_nodes() -> None:
    placeholder = "@@FORMULA_Fabc123@@"
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
        source_text="We solve @fs=@t.",
    ).model_copy(
        update={
            "text_for_translation": f"We solve {placeholder}.",
            "formulas": [
                Formula(
                    formula_id="Fabc123",
                    placeholder=placeholder,
                    kind="inline",
                    source_text="@fs=@t",
                    latex=r"\partial fs = \partial t",
                )
            ],
        },
        deep=True,
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_b1",
            translated_text=f"我们求解 {placeholder}。",
            role=BlockRole.PARAGRAPH,
        )
    )

    render_document = RenderDocument.from_ir_and_plans(_document([block]), [plan], "zh-CN")
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()

    assert "@@FORMULA_" not in html
    assert 'class="formula formula-inline"' in html
    assert 'data-formula-id="Fabc123"' in html
    assert r"\partial fs = \partial t" in html
    assert "window.katex" in html
    assert "katex.render" in html
    assert diagnostics["formula_rendered_count"] == 1
    assert diagnostics["unresolved_formula_placeholders"] == []


def test_original_bbox_is_a_hard_constraint() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
    )

    html = render_to_html(_render_source_bbox(_document([block])))

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

    render_document = _render_source_bbox(_document([block]), [plan])
    render_block = render_document.pages[0].blocks[0]
    html = render_to_html(render_document)

    assert render_block.font_scale < 1
    assert render_block.font_size_pt < 12.0
    assert render_block.bbox.y1 > block.bbox.y1
    assert "quality-box-expanded" in html


def test_source_bbox_uses_role_styles_for_typography() -> None:
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
        font_size=8,
    )
    title = _block(
        "p1_title",
        BlockRole.TITLE,
        BoundingBox(x0=72, y0=72, x1=420, y1=108),
        font_size=9,
        reading_order=1,
    )

    render_document = _render_source_bbox(_document([paragraph, title]))
    rendered = {block.block_id: block for block in render_document.pages[0].blocks}

    assert rendered["p1_body"].font_size_pt == pytest.approx(12.0)
    assert rendered["p1_body"].line_height == pytest.approx(1.5)
    assert rendered["p1_body"].text_align == "justify"
    assert rendered["p1_body"].first_line_indent_em == pytest.approx(2.0)
    assert rendered["p1_title"].font_size_pt == pytest.approx(18.0)
    assert rendered["p1_title"].font_weight == 700
    assert rendered["p1_title"].text_align == "center"


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

    render_document = _render_source_bbox(
        _document([block]),
        [plan],
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

    render_document = _render_source_bbox(_document([block]), [plan])
    render_block = render_document.pages[0].blocks[0]

    assert render_block.font_scale < 1
    assert render_block.font_size_pt < 12.0
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
    defaults = _source_bbox_defaults()
    defaults.overflow_policy.min_font_scale = 0.7

    render_document = _render_source_bbox(
        _document([block]),
        [plan],
        defaults=defaults,
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

    render_document = _render_source_bbox(_document([block]), [plan])
    html = render_to_html(render_document)

    assert len(render_document.pages) > 1
    assert render_document.pages[1].page_id.startswith("p1_cont_")
    assert render_document.pages[0].blocks[0].quality_flags == [
        "font_scaled",
        "continued_on_next_page",
    ]
    assert "quality-continuation-page" in html
    assert 'data-block-id="p1_b1__cont_01"' in html


def test_continuous_reflow_uses_gbt_page_layout_without_source_bbox_continuations() -> None:
    title = _block(
        "p1_title",
        BlockRole.TITLE,
        BoundingBox(x0=12, y0=12, x1=40, y1=24),
        source_text="Tiny source title bbox",
        reading_order=0,
    )
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=12, y0=30, x1=42, y1=42),
        source_text="Small source body bbox",
        reading_order=1,
    )
    vector_placeholder = Asset(
        asset_id="vector_1",
        page_id="p1",
        kind="figure",
        bbox=BoundingBox(x0=20, y0=60, x1=180, y1=120),
        alt_text="PDF vector drawing placeholder",
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_title",
            translated_text="论文题名",
            role=BlockRole.TITLE,
        ),
        TranslationBlockPlan(
            source_block_id="p1_body",
            translated_text="。".join(["这是一段用于测试连续重排的中文译文"] * 260),
            role=BlockRole.PARAGRAPH,
        ),
    )
    defaults = RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow")

    render_document = RenderDocument.from_ir_and_plans(
        _document([title, paragraph], [vector_placeholder]),
        [plan],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()

    assert render_document.layout_mode == "continuous_reflow"
    assert render_document.pages[0].size.width == pytest.approx(595.28)
    assert render_document.pages[0].blocks[0].bbox.x0 == pytest.approx(70.87)
    assert render_document.pages[0].blocks[0].bbox.x0 != title.bbox.x0
    assert all("_cont_" not in page.page_id for page in render_document.pages)
    assert "quality-continuation-page" not in html
    assert diagnostics["single_fragment_pages"] == []
    assert render_document.layout_trace["standard"] == "gb_t_7713_1_2025"
    assert render_document.layout_trace["suppressed_artifacts"] == [
        {
            "kind": "asset_ignored",
            "asset_id": "vector_1",
            "source_page_id": "p1",
            "reason": "vector_placeholder_without_renderable_asset",
            "quality_flags": ["asset_suppressed_placeholder"],
        }
    ]
    assert 'data-asset-id="vector_1"' not in html
    assert "page-footer" in html


def test_continuous_reflow_preserves_raster_assets_in_reading_flow() -> None:
    block = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=90, x1=250, y1=130),
        source_text="Body before figure.",
        reading_order=0,
    )
    asset = Asset(
        asset_id="asset_1",
        page_id="p1",
        kind="image",
        bbox=BoundingBox(x0=50, y0=140, x1=250, y1=260),
        path="/api/documents/doc_1/assets/asset_1.png",
        alt_text="Figure asset",
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_body",
            translated_text="图前正文。",
            role=BlockRole.PARAGRAPH,
        )
    )
    defaults = RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow")

    render_document = RenderDocument.from_ir_and_plans(
        _document([block], [asset]),
        [plan],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)

    assert 'data-asset-id="asset_1"' in html
    assert render_document.pages[0].assets[0].quality_flags == ["reflow_asset"]
    assert render_document.layout_trace["assets"][0]["asset_id"] == "asset_1"


def test_continuous_reflow_suppresses_formula_assets_but_preserves_figures() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=50, y0=90, x1=250, y1=120),
        source_text="{{formula:formula_1}}",
        reading_order=0,
    )
    formula_asset = Asset(
        asset_id="formula_asset",
        page_id="p1",
        kind="formula",
        bbox=BoundingBox(x0=50, y0=90, x1=250, y1=120),
        path="/api/documents/doc_1/assets/formula_asset.png",
        formula_id="formula_1",
    )
    figure_asset = Asset(
        asset_id="figure_asset",
        page_id="p1",
        kind="image",
        bbox=BoundingBox(x0=50, y0=140, x1=250, y1=260),
        path="/api/documents/doc_1/assets/figure_asset.png",
        alt_text="Figure asset",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[formula],
                assets=[formula_asset, figure_asset],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_1",
                page_id="p1",
                source_block_id="p1_formula",
                asset_id="formula_asset",
                latex="E = mc^2",
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow"),
    )
    html = render_to_html(render_document)

    assert 'data-asset-id="formula_asset"' not in html
    assert 'data-asset-id="figure_asset"' in html
    assert {
        "kind": "asset_ignored",
        "asset_id": "formula_asset",
        "source_page_id": "p1",
        "reason": "formula_rendered_from_text",
        "quality_flags": ["formula_asset_suppressed"],
    } in render_document.layout_trace["suppressed_artifacts"]


def test_continuous_reflow_allocates_display_formula_height() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=50, y0=90, x1=250, y1=120),
        source_text="{{formula:formula_1}}",
        font_size=12,
        reading_order=0,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[formula],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_1",
                page_id="p1",
                source_block_id="p1_formula",
                latex=r"\frac{\partial V}{\partial t} = \nabla^2 V + \sum_i x_i",
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow"),
    )
    render_block = render_document.pages[0].blocks[0]
    html = render_to_html(render_document)

    assert render_block.role == BlockRole.FORMULA
    assert render_block.bbox.y1 - render_block.bbox.y0 > 12.0 * 1.35
    assert render_document.layout_trace["blocks"][0]["estimated_lines"] > 1
    assert 'class="katex-display"' in html
    assert '--h-pt: 16.2pt' not in html


def test_continuous_reflow_suppresses_vertical_timestamp_artifacts() -> None:
    artifact = _block(
        "p1_timestamp",
        BlockRole.HEADING,
        BoundingBox(x0=562, y0=390, x1=568, y1=453),
        source_text="25 April 2025 00:08:47",
        reading_order=0,
    )
    defaults = RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow")

    render_document = RenderDocument.from_ir_and_plans(
        _document([artifact]),
        [],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)

    assert "25 April 2025" not in html
    assert render_document.layout_trace["suppressed_artifacts"] == [
        {
            "kind": "source_block_suppressed",
            "source_block_id": "p1_timestamp",
            "source_page_id": "p1",
            "reason": "running_header_footer_or_pdf_artifact",
        }
    ]


def test_render_document_diagnostics_reports_quality_flags() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=132, y1=140),
        source_text="Source fallback",
        font_size=12,
    )

    diagnostics = _render_source_bbox(_document([block])).diagnostics()

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

    html = render_to_html(_render_source_bbox(_document([block], [asset])))

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

    render_document = _render_source_bbox(_document([], [asset]))
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

    render_document = _render_source_bbox(_document([], [asset]))
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

    diagnostics = _render_source_bbox(_document([first, second], [asset])).diagnostics()

    issue_kinds = {issue["kind"] for issue in diagnostics["layout_issues"]}
    assert "overlap" in issue_kinds
    assert "bbox_outside_page" in issue_kinds


def test_jinja_templates_are_declared_as_package_data() -> None:
    pyproject = tomllib.loads((RENDERER_ROOT / "pyproject.toml").read_text())

    assert pyproject["tool"]["setuptools"]["package-data"]["pdf_renderer"] == [
        "templates/*.j2"
    ]
