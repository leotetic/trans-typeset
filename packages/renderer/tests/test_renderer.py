from __future__ import annotations

import builtins
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pdf_renderer.models as renderer_models
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


def test_katex_css_is_fully_self_contained() -> None:
    html = render_to_html(_render_source_bbox(_document([])))

    assert "fonts/KaTeX_" not in html
    assert "data:font/" in html or "data:application/font-" in html


def test_katex_display_margin_is_zeroed_to_prevent_formula_clipping() -> None:
    html = render_to_html(_render_source_bbox(_document([])))

    assert ".katex-display {\n        display: block;" in html
    assert "margin: 0;" in html
    assert "margin: 1em 0" not in html


def test_katex_render_to_string_handles_empty_stdout_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        renderer_models.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=None, stderr="boom"),
    )

    assert renderer_models._katex_render_to_string(r"\int f_s\,d\Omega", display=True) is None


def test_katex_strut_depth_counts_toward_formula_height() -> None:
    html = '<span class="strut" style="height:0.85em;vertical-align:-0.25em;"></span>'

    height = renderer_models._height_from_katex_html(html, font_size_pt=10)

    assert height == pytest.approx(11.0)


def test_formula_height_uses_heuristic_floor_without_extra_padding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        renderer_models,
        "_katex_html",
        lambda *args, **kwargs: (
            '<span class="strut" style="height:0.75em;vertical-align:-0.25em;"></span>'
        ),
    )

    height = renderer_models._formula_rendered_or_heuristic_height(
        r"\frac{\partial V}{\partial t} = \sum_i x_i",
        font_size_pt=10,
    )

    assert height == pytest.approx(29.0)


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


def test_pdf_export_page_diagnostics_collects_overflow_and_figure_checks() -> None:
    import asyncio

    class FakePage:
        def __init__(self) -> None:
            self.script = ""

        async def evaluate(self, script: str) -> dict[str, object]:
            self.script = script
            return {
                "block_overflow_count": 1,
                "block_overflows": [
                    {
                        "block_id": "p1_b1",
                        "page_id": "r0001",
                        "scroll_height": 20,
                        "client_height": 14,
                        "scroll_width": 100,
                        "client_width": 100,
                    }
                ],
                "figure_group_issue_count": 1,
                "figure_group_issues": [
                    {
                        "kind": "figure_group_separated",
                        "figure_group_id": "figure-group-a1",
                        "asset_id": "a1",
                        "caption_block_id": "c1",
                        "asset_page_id": "r0001",
                        "caption_page_id": "r0002",
                    }
                ],
            }

    page = FakePage()

    diagnostics = asyncio.run(renderer_module._collect_page_diagnostics(page))

    assert diagnostics["block_overflow_count"] == 1
    assert diagnostics["figure_group_issue_count"] == 1
    assert "scrollHeight > clientHeight" in page.script
    assert "data-figure-group-id" in page.script
    assert "asset_caption_mismatch" in page.script


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


def test_display_formula_height_does_not_double_count_padding() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        renderer_models,
        "_katex_html",
        lambda _latex, display=True: (
            '<span class="strut" style="height:1.8em;vertical-align:-0.6em;"></span>'
        ),
    )
    try:
        height = renderer_models._formula_rendered_or_heuristic_height(
            r"x = y + 1",
            font_size_pt=12,
        )
    finally:
        monkeypatch.undo()

    assert height == pytest.approx(28.8)


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


def test_corrupt_display_formula_prefers_image_fallback_even_if_latex_compiles() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=36, y0=72, x1=260, y1=108),
        source_text="{{formula:formula_corrupt}}",
    )
    formula_asset = Asset(
        asset_id="formula_asset",
        page_id="p1",
        kind="formula",
        bbox=BoundingBox(x0=36, y0=72, x1=260, y1=108),
        path="/api/documents/doc_1/assets/formula_asset.png",
        formula_id="formula_corrupt",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[formula],
                assets=[formula_asset],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_corrupt",
                page_id="p1",
                source_block_id="p1_formula",
                asset_id="formula_asset",
                latex=r"f_s = k^2",
                source_text="f 0 s=k2",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=["formula_text_layer_corrupt"],
            )
        ],
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_formula",
            translated_text="{{formula:formula_corrupt}}",
            role=BlockRole.FORMULA,
        )
    )

    render_document = RenderDocument.from_ir_and_plans(document, [plan], "zh-CN")
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()

    assert "formula-image-fallback" in html
    assert "formula_asset.png" in html
    assert diagnostics["quality_flag_counts"]["formula_image_fallback"] == 1


def test_corrupt_display_formula_uses_image_fallback_without_upstream_flags() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=36, y0=72, x1=260, y1=108),
        source_text="{{formula:formula_corrupt}}",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[formula],
                assets=[
                    Asset(
                        asset_id="formula_asset",
                        page_id="p1",
                        kind="formula",
                        bbox=BoundingBox(x0=36, y0=72, x1=260, y1=108),
                        path="/api/documents/doc_1/assets/formula_asset.png",
                        formula_id="formula_corrupt",
                    )
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_corrupt",
                page_id="p1",
                source_block_id="p1_formula",
                asset_id="formula_asset",
                latex=r"\partial fs=k2 @(kt)",
                source_text=r"\partial fs=k2 @(kt)",
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_formula",
            translated_text="{{formula:formula_corrupt}}",
            role=BlockRole.FORMULA,
        )
    )

    render_document = RenderDocument.from_ir_and_plans(document, [plan], "zh-CN")
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()
    block_html = render_document.pages[0].blocks[0].html or ""

    assert "formula-image-fallback" in html
    assert "formula_asset.png" in html
    assert r"\partial fs=k2" not in block_html
    assert diagnostics["quality_flag_counts"]["formula_image_fallback"] == 1


def test_clean_visual_formula_renders_latex_despite_corrupt_source_text() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=36, y0=72, x1=260, y1=108),
        source_text="{{formula:formula_visual}}",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[formula],
                assets=[
                    Asset(
                        asset_id="formula_asset",
                        page_id="p1",
                        kind="formula",
                        bbox=BoundingBox(x0=36, y0=72, x1=260, y1=108),
                        path="/api/documents/doc_1/assets/formula_asset.png",
                        formula_id="formula_visual",
                    )
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_visual",
                page_id="p1",
                source_block_id="p1_formula",
                asset_id="formula_asset",
                latex=r"\frac{\partial f_s}{\partial t}",
                source_text="@fs=@t þ f 0 s=k2",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=[
                    "formula_text_layer_corrupt",
                    "formula_slash_glyph_suspect",
                ],
            )
        ],
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_formula",
            translated_text="{{formula:formula_visual}}",
            role=BlockRole.FORMULA,
        )
    )

    render_document = RenderDocument.from_ir_and_plans(document, [plan], "zh-CN")
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()
    block_html = render_document.pages[0].blocks[0].html or ""

    assert "formula-image-fallback" not in block_html
    assert "formula_asset.png" not in block_html
    assert "katex" in html
    assert "formula_image_fallback" not in diagnostics["quality_flag_counts"]


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
            "reason": "vector_asset_not_rasterized",
            "quality_flags": ["vector_asset_not_rasterized"],
        }
    ]
    assert 'data-asset-id="vector_1"' not in html
    assert diagnostics["quality_flag_counts"]["vector_asset_not_rasterized"] == 1
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


def test_continuous_reflow_allocates_required_paragraph_height() -> None:
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=90, x1=250, y1=133.74),
        source_text="Source paragraph.",
        font_size=12,
        reading_order=0,
    )
    translated = "天地玄黄宇宙洪荒" * 16
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_body",
            translated_text=translated,
            role=BlockRole.PARAGRAPH,
            render_intent="compact",
        )
    )
    defaults = RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow")

    render_document = RenderDocument.from_ir_and_plans(
        _document([paragraph]),
        [plan],
        "zh-CN",
        render_defaults=defaults,
    )
    block = render_document.pages[0].blocks[0]
    trace = render_document.layout_trace["blocks"][0]
    diagnostics = render_document.diagnostics()

    assert trace["estimated_lines"] == 4
    assert trace["required_height_pt"] > 43.74
    assert trace["allocated_height_pt"] >= trace["required_height_pt"]
    assert block.bbox.y1 - block.bbox.y0 == pytest.approx(trace["allocated_height_pt"])
    assert "overflow_clipped" not in block.quality_flags
    assert not [
        issue
        for issue in diagnostics["layout_issues"]
        if issue["kind"] == "overflow_clipped"
    ]


def test_continuous_reflow_keeps_figure_images_with_captions() -> None:
    lead = _block(
        "p1_lead",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=40, x1=320, y1=95),
        source_text="Lead paragraph.",
        reading_order=0,
    )
    fig1_caption = _block(
        "p1_fig1_caption",
        BlockRole.CAPTION,
        BoundingBox(x0=50, y0=230, x1=320, y1=252),
        source_text="Fig. 1. First figure.",
        reading_order=1,
    )
    fig2_caption = _block(
        "p1_fig2_caption",
        BlockRole.CAPTION,
        BoundingBox(x0=50, y0=390, x1=320, y1=412),
        source_text="Fig. 2. Second figure.",
        reading_order=2,
    )
    fig1 = Asset(
        asset_id="fig1_image",
        page_id="p1",
        kind="image",
        bbox=BoundingBox(x0=52, y0=140, x1=318, y1=220),
        path="/api/documents/doc_1/assets/fig1_image.png",
        alt_text="Figure 1",
    )
    fig2 = Asset(
        asset_id="fig2_image",
        page_id="p1",
        kind="image",
        bbox=BoundingBox(x0=52, y0=300, x1=318, y1=380),
        path="/api/documents/doc_1/assets/fig2_image.png",
        alt_text="Figure 2",
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_lead",
            translated_text="引导段落。" * 50,
            role=BlockRole.PARAGRAPH,
        ),
        TranslationBlockPlan(
            source_block_id="p1_fig1_caption",
            translated_text="图 1. 第一幅图。",
            role=BlockRole.CAPTION,
        ),
        TranslationBlockPlan(
            source_block_id="p1_fig2_caption",
            translated_text="图 2. 第二幅图。",
            role=BlockRole.CAPTION,
        ),
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        layout_mode="continuous_reflow",
    ).model_copy(
        update={
            "page_layout": RenderDefaults(
                target_lang="zh-CN",
                layout_mode="continuous_reflow",
            ).page_layout.model_copy(
                update={
                    "width_pt": 300.0,
                    "height_pt": 220.0,
                    "margin_top_pt": 18.0,
                    "margin_right_pt": 18.0,
                    "margin_bottom_pt": 18.0,
                    "margin_left_pt": 18.0,
                }
            )
        },
        deep=True,
    )

    render_document = RenderDocument.from_ir_and_plans(
        _document([lead, fig1_caption, fig2_caption], [fig1, fig2]),
        [plan],
        "zh-CN",
        render_defaults=defaults,
    )
    group_trace = render_document.layout_trace["figure_groups"]
    diagnostics = render_document.diagnostics()

    assert [
        (group["asset_id"], group["caption_block_id"])
        for group in group_trace
    ] == [
        ("fig1_image", "p1_fig1_caption"),
        ("fig2_image", "p1_fig2_caption"),
    ]
    assert all(group["output_page_id"] for group in group_trace)
    assert group_trace[0]["output_page_id"] == group_trace[0]["caption_output_page_id"]
    assert group_trace[1]["output_page_id"] == group_trace[1]["caption_output_page_id"]
    assert group_trace[0]["order_index"] < group_trace[1]["order_index"]
    assert "figure_group_separated" not in diagnostics["quality_flag_counts"]
    assert "asset_caption_mismatch" not in diagnostics["quality_flag_counts"]


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
    assert render_block.bbox.y1 - render_block.bbox.y0 > 30.0
    assert render_document.layout_trace["blocks"][0]["estimated_lines"] > 1
    assert "formula_height_adjusted" in render_block.quality_flags
    assert 'class="katex-display"' in html
    assert '--h-pt: 16.2pt' not in html


def test_katex_height_counts_strut_depth() -> None:
    html = (
        '<span class="katex-display"><span class="katex">'
        '<span class="strut" style="height:1.8em;vertical-align:-0.6em;"></span>'
        "</span></span>"
    )

    height = renderer_models._height_from_katex_html(html, 10.0)

    assert height == pytest.approx(24.0)


def test_rendered_formula_height_uses_heuristic_floor() -> None:
    latex = (
        r"\frac{\partial V}{\partial t} = \nabla^2 V + \sum_i x_i + "
        r"\int_0^1 \frac{a_i}{b_i}\,dx"
    )
    expected_height = renderer_models._formula_latex_heuristic_height(latex, 12.0)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        renderer_models,
        "_katex_html",
        lambda _latex, display=True: (
            '<span class="strut" style="height:1.8em;vertical-align:-0.9em;"></span>'
        ),
    )
    try:
        height = renderer_models._formula_rendered_or_heuristic_height(latex, 12.0)
    finally:
        monkeypatch.undo()

    assert height == pytest.approx(expected_height)
    assert height > 12.0 * (1.8 + 0.9)


def test_formula_only_block_uses_visual_formula_height_for_long_single_line_latex() -> None:
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
                latex=" + ".join([f"x_{{{index}}}" for index in range(1, 40)]),
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )
    defaults = RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow")
    style = renderer_models._style_for_role(defaults, BlockRole.FORMULA)
    visual_height = 12.0 * (1.8 + 0.6 + renderer_models._FORMULA_LIKE_VERTICAL_MARGIN_EM)
    estimation_text = renderer_models._formula_estimation_text(
        formula.source_text,
        document,
        formula,
    )
    estimated_formula_lines = renderer_models._estimated_line_count(
        estimation_text,
        200.0,
        12.0,
    )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        renderer_models,
        "_katex_html",
        lambda _latex, display=True: (
            '<span class="strut" style="height:1.8em;vertical-align:-0.6em;"></span>'
        ),
    )
    try:
        visual_only_height = renderer_models._estimated_formula_aware_height(
            formula.source_text,
            200.0,
            12.0,
            style.line_height,
            document=document,
            block=formula,
        )
    finally:
        monkeypatch.undo()

    assert estimated_formula_lines > 1
    assert visual_only_height == pytest.approx(visual_height)
    assert visual_only_height < estimated_formula_lines * 12.0 * max(style.line_height, 1.2)


def test_continuous_reflow_tracks_multi_display_formula_height_diagnostics() -> None:
    formula = _block(
        "p1_formula",
        BlockRole.FORMULA,
        BoundingBox(x0=50, y0=90, x1=250, y1=120),
        source_text=(
            "{{formula:formula_1}} {{formula:formula_2}} {{formula:formula_3}}"
        ),
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
                latex=r"\partial f_s / \partial t",
                display_mode="display",
                source_kind="text_layer",
            ),
            FormulaIR(
                formula_id="formula_2",
                page_id="p1",
                source_block_id="p1_formula",
                latex=r"= \sum_n",
                display_mode="display",
                source_kind="text_layer",
            ),
            FormulaIR(
                formula_id="formula_3",
                page_id="p1",
                source_block_id="p1_formula",
                latex=r"\int g_{sn}\,d\Omega",
                display_mode="display",
                source_kind="text_layer",
            ),
        ],
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow"),
    )
    render_block = render_document.pages[0].blocks[0]
    diagnostics = render_document.diagnostics()
    html = render_to_html(render_document)

    assert render_block.bbox.y1 - render_block.bbox.y0 > 60
    assert "formula_height_adjusted" in render_block.quality_flags
    assert "formula_multi_display_block" in render_block.quality_flags
    assert "formula_height_risk" in render_block.quality_flags
    assert diagnostics["formula_height_adjusted_count"] == 1
    assert diagnostics["formula_multi_display_block_count"] == 1
    assert diagnostics["formula_block_overflow_count"] == 1
    assert diagnostics["formula_dom_estimation_strategy"] == (
        "display_node_count_and_formula_aware_height"
    )
    assert html.count('class="formula formula-display formula-ir"') == 3


def test_continuous_reflow_compacts_formula_like_fragments_into_single_block() -> None:
    block_a = _block(
        "p1_formula_a",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=90, x1=150, y1=110),
        source_text="{{formula:formula_a}}",
        reading_order=0,
    )
    block_b = _block(
        "p1_formula_b",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=158, y0=92, x1=270, y1=110),
        source_text="{{formula:formula_b}}",
        reading_order=1,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[block_a, block_b],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_a",
                page_id="p1",
                anchor_block_id="p1_formula_a",
                latex="x = y",
                display_mode="display",
                source_kind="text_layer",
            ),
            FormulaIR(
                formula_id="formula_b",
                page_id="p1",
                anchor_block_id="p1_formula_b",
                latex="= 0",
                display_mode="display",
                source_kind="text_layer",
            ),
        ],
    )
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_formula_a",
            translated_text="{{formula:formula_a}}",
            role=BlockRole.PARAGRAPH,
            quality_flags=["formula_like_repaired"],
        ),
        TranslationBlockPlan(
            source_block_id="p1_formula_b",
            translated_text="{{formula:formula_b}}",
            role=BlockRole.PARAGRAPH,
            quality_flags=["formula_like_repaired"],
        ),
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [plan],
        "zh-CN",
        render_defaults=RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow"),
    )
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()

    assert len(render_document.pages[0].blocks) == 1
    assert diagnostics["formula_reflow_cluster_count"] >= 1
    assert diagnostics["formula_like_block_count"] >= 1
    assert 'data-formula-id="formula_a"' in html
    assert 'data-formula-id="formula_b"' in html


def test_continuous_reflow_keeps_inline_formula_paragraph_as_regular_text_block() -> None:
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=90, x1=280, y1=140),
        source_text="Body {{formula:formula_inline}} text.",
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
                latex="E = mc^2",
                display_mode="inline",
                source_kind="inline_text",
            )
        ],
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow"),
    )
    diagnostics = render_document.diagnostics()

    assert len(render_document.pages[0].blocks) == 1
    assert diagnostics["formula_reflow_cluster_count"] == 0


def _display_formula_document(
    formulas: list[tuple[str, str, str]],
) -> DocumentIR:
    """Build a doc with one display-formula block per (block_id, formula_id, latex)."""
    blocks = [
        DocumentBlock(
            block_id=block_id,
            page_id="p1",
            role=BlockRole.FORMULA,
            bbox=BoundingBox(x0=50, y0=90 + index * 80, x1=280, y1=120 + index * 80),
            reading_order=index,
            source_text=f"{{{{formula:{formula_id}}}}}",
            style_seed=StyleSeed(font_size=10),
            formula_id=formula_id,
        )
        for index, (block_id, formula_id, _latex) in enumerate(formulas)
    ]
    return DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=blocks,
            )
        ],
        formulas=[
            FormulaIR(
                formula_id=formula_id,
                page_id="p1",
                source_block_id=block_id,
                latex=latex,
                display_mode="display",
                source_kind="text_layer",
            )
            for block_id, formula_id, latex in formulas
        ],
    )


def test_continuous_reflow_numbers_display_formulas_per_gbt() -> None:
    document = _display_formula_document(
        [
            ("p1_f1", "formula_1", "E = mc^2"),
            ("p1_f2", "formula_2", "a^2 + b^2 = c^2"),
        ]
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        layout_mode="continuous_reflow",
        formula_numbering="parenthesized",
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()

    numbered = [block for page in render_document.pages for block in page.blocks]
    assert [block.formula_number for block in numbered] == ["(1)", "(2)"]
    assert all("gbt_formula_numbered" in block.quality_flags for block in numbered)
    assert html.count('class="formula-equation-number"') == 2
    assert 'data-formula-number="(1)"' in html
    assert 'data-formula-number="(2)"' in html
    assert diagnostics["formula_numbered_count"] == 2
    assert diagnostics["formula_number_source_preserved_count"] == 0


def test_continuous_reflow_preserves_existing_source_equation_numbers() -> None:
    document = _display_formula_document([("p1_f1", "formula_1", "E = mc^2")])
    plan = _plan(
        TranslationBlockPlan(
            source_block_id="p1_f1",
            translated_text="{{formula:formula_1}} (12)",
            role=BlockRole.FORMULA,
        )
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        layout_mode="continuous_reflow",
        formula_numbering="parenthesized",
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [plan],
        "zh-CN",
        render_defaults=defaults,
    )
    diagnostics = render_document.diagnostics()
    html = render_to_html(render_document)

    block = render_document.pages[0].blocks[0]
    assert block.text == "{{formula:formula_1}}"
    assert block.formula_number == "(12)"
    assert "formula_number_source_preserved" in block.quality_flags
    assert html.count('class="formula-equation-number"') == 1
    assert 'data-formula-number="(12)"' in html
    assert diagnostics["formula_numbered_count"] == 0
    assert diagnostics["formula_number_source_preserved_count"] == 1


def test_continuous_reflow_strips_latex_tag_and_uses_single_renderer_number() -> None:
    document = _display_formula_document(
        [("p1_f1", "formula_1", r"E = mc^2 \tag{7}")]
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        layout_mode="continuous_reflow",
        formula_numbering="parenthesized",
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)
    block = render_document.pages[0].blocks[0]

    assert block.formula_number == "(7)"
    assert "formula_number_source_preserved" in block.quality_flags
    assert "gbt_formula_numbered" not in block.quality_flags
    assert html.count('class="formula-equation-number"') == 1
    assert 'data-formula-number="(7)"' in html
    assert 'data-latex="E = mc^2"' in html
    assert 'data-latex="E = mc^2 \\tag{7}"' not in html


def test_continuous_reflow_uses_formula_source_number_as_renderer_span() -> None:
    document = _display_formula_document(
        [("p1_f1", "formula_1", r"\int f_s\,d\Omega")]
    )
    document = document.model_copy(
        update={
            "formulas": [
                document.formulas[0].model_copy(
                    update={"source_text": r"\int f_s\,d\Omega , (3) v_{n}"}
                )
            ]
        },
        deep=True,
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        layout_mode="continuous_reflow",
        formula_numbering="parenthesized",
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)
    block = render_document.pages[0].blocks[0]

    assert block.formula_number == "(3)"
    assert "formula_number_source_preserved" in block.quality_flags
    assert "gbt_formula_numbered" not in block.quality_flags
    assert html.count('class="formula-equation-number"') == 1
    assert 'data-formula-number="(3)"' in html


def test_continuous_reflow_advances_fallback_numbering_after_preserved_number() -> None:
    document = _display_formula_document(
        [
            ("p1_f1", "formula_1", r"\int f_s\,d\Omega"),
            ("p1_f2", "formula_2", r"\partial f_s / \partial t"),
        ]
    )
    document = document.model_copy(
        update={
            "formulas": [
                document.formulas[0].model_copy(
                    update={"source_text": r"\int f_s\,d\Omega , (4) v_{n}"}
                ),
                document.formulas[1],
            ]
        },
        deep=True,
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        layout_mode="continuous_reflow",
        formula_numbering="parenthesized",
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=defaults,
    )

    blocks = [block for page in render_document.pages for block in page.blocks]
    assert [block.formula_number for block in blocks] == ["(4)", "(5)"]


def test_continuous_reflow_strips_preserved_number_from_formula_latex_markup() -> None:
    document = _display_formula_document(
        [("p1_f1", "formula_1", r"\int f_s\,d\Omega")]
    )
    document = document.model_copy(
        update={
            "formulas": [
                document.formulas[0].model_copy(
                    update={"source_text": r"\int f_s\,d\Omega : (4) v'_{n}"}
                )
            ]
        },
        deep=True,
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        layout_mode="continuous_reflow",
        formula_numbering="parenthesized",
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)

    assert 'data-formula-number="(4)"' in html
    assert "(4) v&#x27;_{n}" not in html
    assert "(4) v'_{n}" not in html
    assert html.count('class="formula-equation-number"') == 1


def test_renderer_converts_leftover_text_subscript_and_superscript_markers() -> None:
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=90, x1=330, y1=140),
        source_text="Densities n_{e}, x_{1}, and citations 50^{–54} remain readable.",
        reading_order=0,
    )

    render_document = RenderDocument.from_ir_and_plans(
        _document([paragraph]),
        [],
        "zh-CN",
        render_defaults=RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow"),
    )
    html = render_to_html(render_document)
    block = render_document.pages[0].blocks[0]

    assert "text_script_marker_rendered" in block.quality_flags
    assert "n<sub>e</sub>" in html
    assert "x<sub>1</sub>" in html
    assert "50<sup>–54</sup>" in html
    assert "n_{" not in html
    assert "50^{" not in html


def test_renderer_converts_bare_script_markers_without_base() -> None:
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=90, x1=360, y1=140),
        source_text="Detached ^{3}, ^{50–54}, and _{tail} stay readable.",
        reading_order=0,
    )

    render_document = RenderDocument.from_ir_and_plans(
        _document([paragraph]),
        [],
        "zh-CN",
        render_defaults=RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow"),
    )
    html = render_to_html(render_document)
    block = render_document.pages[0].blocks[0]

    assert "text_script_marker_rendered" in block.quality_flags
    assert "<sup>3</sup>" in html
    assert "<sup>50–54</sup>" in html
    assert "<sub>tail</sub>" in html
    assert "^{3}" not in html
    assert "_{tail}" not in html


def test_continuous_reflow_formula_numbering_defaults_to_none() -> None:
    document = _display_formula_document([("p1_f1", "formula_1", "E = mc^2")])
    defaults = RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow")

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)

    assert render_document.pages[0].blocks[0].formula_number is None
    assert 'class="formula-equation-number"' not in html
    assert 'data-formula-number=' not in html


def test_continuous_reflow_safely_splits_formula_bearing_paragraphs() -> None:
    intro = _block(
        "intro",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=40, x1=320, y1=80),
        source_text="Prelude text. Prelude text. Prelude text.",
        reading_order=0,
    )
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=90, x1=320, y1=160),
        source_text=(
            "Body {{formula:formula_1}} text continues with more explanation and "
            "{{formula:formula_2}} references that should wrap across pages "
            "without forcing the entire paragraph onto the next page. "
            "Body {{formula:formula_1}} text continues with more explanation and "
            "{{formula:formula_2}} references that should wrap across pages "
            "without forcing the entire paragraph onto the next page."
        ),
        reading_order=1,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[intro, paragraph],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_1",
                page_id="p1",
                anchor_block_id="p1_body",
                latex="E = mc^2",
                display_mode="inline",
                source_kind="inline_text",
            ),
            FormulaIR(
                formula_id="formula_2",
                page_id="p1",
                anchor_block_id="p1_body",
                latex=r"\alpha_{e} = \beta^{2}",
                display_mode="inline",
                source_kind="inline_text",
            ),
        ],
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        layout_mode="continuous_reflow",
    ).model_copy(
        update={
            "page_layout": RenderDefaults(
                target_lang="zh-CN",
                layout_mode="continuous_reflow",
            ).page_layout.model_copy(
                update={
                    "width_pt": 240.0,
                    "height_pt": 150.0,
                    "margin_top_pt": 18.0,
                    "margin_right_pt": 18.0,
                    "margin_bottom_pt": 18.0,
                    "margin_left_pt": 18.0,
                }
            )
        },
        deep=True,
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)
    blocks = [block for page in render_document.pages for block in page.blocks]
    formula_blocks = [block for block in blocks if block.block_id.startswith("p1_body")]

    assert len(render_document.pages) > 1
    assert len(blocks) > 1
    assert render_document.pages[0].blocks[0].block_id == "intro"
    assert formula_blocks[0].block_id == "p1_body__reflow_01"
    assert "reflow_split" in formula_blocks[0].quality_flags
    assert formula_blocks[0].html is not None
    assert any("reflow_continued" in block.quality_flags for block in formula_blocks[1:])
    assert sum(1 for block in formula_blocks if block.html is not None) >= 2
    assert html.count('data-formula-id="formula_1"') >= 1
    assert html.count('data-formula-id="formula_2"') >= 1


def test_continuous_reflow_formula_paragraph_keeps_number_only_on_first_fragment() -> None:
    intro = _block(
        "intro",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=40, x1=320, y1=80),
        source_text="Prelude text. Prelude text. Prelude text.",
        reading_order=0,
    )
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=90, x1=320, y1=160),
        source_text=(
            "{{formula:formula_1}} (4) with explanatory text that is intentionally "
            "long enough to split across pages while preserving only one equation "
            "number on the first fragment. The continuation should remain plain prose "
            "after the display formula and still reflow safely across pages."
        ),
        reading_order=1,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[intro, paragraph],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="formula_1",
                page_id="p1",
                anchor_block_id="p1_body",
                latex="E = mc^2",
                source_text="E = mc^2",
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )
    defaults = RenderDefaults(
        target_lang="zh-CN",
        layout_mode="continuous_reflow",
        formula_numbering="parenthesized",
    ).model_copy(
        update={
            "page_layout": RenderDefaults(
                target_lang="zh-CN",
                layout_mode="continuous_reflow",
            ).page_layout.model_copy(
                update={
                    "width_pt": 240.0,
                    "height_pt": 150.0,
                    "margin_top_pt": 18.0,
                    "margin_right_pt": 18.0,
                    "margin_bottom_pt": 18.0,
                    "margin_left_pt": 18.0,
                }
            )
        },
        deep=True,
    )

    render_document = RenderDocument.from_ir_and_plans(
        document,
        [],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)
    blocks = [block for page in render_document.pages for block in page.blocks]
    formula_blocks = [block for block in blocks if block.block_id.startswith("p1_body")]

    assert len(formula_blocks) > 1
    assert formula_blocks[0].formula_number == "(4)"
    assert all(block.formula_number is None for block in formula_blocks[1:])
    assert "reflow_continued" in formula_blocks[1].quality_flags
    assert html.count('class="formula-equation-number"') == 1


def test_continuous_reflow_headings_use_gbt_heiti_font_stack() -> None:
    heading = _block(
        "p1_heading",
        BlockRole.HEADING,
        BoundingBox(x0=50, y0=90, x1=280, y1=120),
        source_text="1 Introduction",
        reading_order=0,
    )
    paragraph = _block(
        "p1_body",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=50, y0=130, x1=280, y1=180),
        source_text="Body text.",
        reading_order=1,
    )
    defaults = RenderDefaults(target_lang="zh-CN", layout_mode="continuous_reflow")

    render_document = RenderDocument.from_ir_and_plans(
        _document([heading, paragraph]),
        [],
        "zh-CN",
        render_defaults=defaults,
    )
    html = render_to_html(render_document)

    heading_block = render_document.pages[0].blocks[0]
    body_block = render_document.pages[0].blocks[1]
    assert heading_block.font_stack is not None
    assert "SimHei" in heading_block.font_stack
    assert body_block.font_stack is None
    assert "--block-font-family:" in html
    assert "SimHei" in html


def test_render_to_html_escapes_raw_block_text() -> None:
    block = _block(
        "p1_b1",
        BlockRole.PARAGRAPH,
        BoundingBox(x0=72, y0=120, x1=420, y1=180),
        source_text='<script>alert("x")</script> & <b>bold</b>',
    )

    html = render_to_html(_render_source_bbox(_document([block])))

    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


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
