from __future__ import annotations

import tomllib
from pathlib import Path

from pdf_renderer import RenderDocument, render_to_html
from pdf_translator_schema import (
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    TranslationBlockPlan,
    TranslationLayoutPlan,
)
from pdf_translator_schema.models import DocumentBlock, StyleSeed

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


def _document(blocks: list[DocumentBlock]) -> DocumentIR:
    return DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=blocks,
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


def test_compact_intent_scales_font_and_overflow_is_clipped() -> None:
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
    assert render_block.bbox == block.bbox
    assert "quality-overflow-clipped" in html


def test_normal_intent_keeps_style_seed_font_size_when_clipped() -> None:
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

    assert render_block.font_size_pt == block.style_seed.font_size
    assert render_block.quality_flags == ["overflow_clipped"]


def test_jinja_templates_are_declared_as_package_data() -> None:
    pyproject = tomllib.loads((RENDERER_ROOT / "pyproject.toml").read_text())

    assert pyproject["tool"]["setuptools"]["package-data"]["pdf_renderer"] == [
        "templates/*.j2"
    ]
