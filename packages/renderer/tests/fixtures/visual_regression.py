from __future__ import annotations

from dataclasses import dataclass

from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    TranslationBlockPlan,
    TranslationLayoutPlan,
)
from pdf_translator_schema.models import DocumentBlock, StyleSeed


@dataclass(frozen=True)
class VisualRegressionFixture:
    document: DocumentIR
    plans: list[TranslationLayoutPlan]
    source_block_ids: set[str]
    expected_page_count: int
    expected_quality_flag_counts: dict[str, int]
    expected_layout_issues: list[dict[str, object]]
    expected_html_tokens: tuple[str, ...]


def _block(
    block_id: str,
    role: BlockRole,
    bbox: BoundingBox,
    source_text: str,
    *,
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


def build_visual_regression_fixture() -> VisualRegressionFixture:
    """Build a deterministic layout-risk sample without checked-in artifacts."""

    blocks = [
        _block(
            "p1_heading",
            BlockRole.HEADING,
            BoundingBox(x0=36, y0=32, x1=320, y1=58),
            "1 Introduction",
            font_size=14,
            reading_order=0,
        ),
        _block(
            "p1_overlap_a",
            BlockRole.PARAGRAPH,
            BoundingBox(x0=40, y0=80, x1=180, y1=120),
            "The first paragraph occupies the upper left column.",
            reading_order=1,
        ),
        _block(
            "p1_overlap_b",
            BlockRole.PARAGRAPH,
            BoundingBox(x0=90, y0=96, x1=220, y1=136),
            "The second paragraph should be detected as overlapping.",
            reading_order=2,
        ),
        _block(
            "p1_missing",
            BlockRole.PARAGRAPH,
            BoundingBox(x0=40, y0=150, x1=300, y1=190),
            "This source text must remain visible when translation is missing.",
            reading_order=3,
        ),
        _block(
            "p1_table",
            BlockRole.TABLE,
            BoundingBox(x0=40, y0=198, x1=200, y1=228),
            "A  B\n1  2",
            reading_order=4,
        ),
        _block(
            "p1_formula",
            BlockRole.FORMULA,
            BoundingBox(x0=220, y0=198, x1=320, y1=228),
            "E = mc^2",
            reading_order=5,
        ),
        _block(
            "p1_footnote",
            BlockRole.FOOTNOTE,
            BoundingBox(x0=40, y0=285, x1=300, y1=304),
            "1 A footnote with compact typography.",
            reading_order=6,
        ),
        _block(
            "p1_empty",
            BlockRole.PARAGRAPH,
            BoundingBox(x0=260, y0=80, x1=320, y1=86),
            "",
            reading_order=7,
        ),
        _block(
            "p1_overflow",
            BlockRole.PARAGRAPH,
            BoundingBox(x0=40, y0=320, x1=120, y1=334),
            "Overflow source text.",
            font_size=12,
            reading_order=8,
        ),
    ]
    assets = [
        Asset(
            asset_id="asset_image",
            page_id="p1",
            kind="image",
            bbox=BoundingBox(x0=240, y0=230, x1=330, y1=270),
            path="/api/documents/visual_regression/assets/asset_image.png",
            alt_text="Raster figure",
        ),
        Asset(
            asset_id="asset_vector_placeholder",
            page_id="p1",
            kind="figure",
            bbox=BoundingBox(x0=240, y0=272, x1=330, y1=282),
            alt_text="Vector drawing placeholder",
        ),
        Asset(
            asset_id="asset_outside_page",
            page_id="p1",
            kind="image",
            bbox=BoundingBox(x0=330, y0=330, x1=390, y1=390),
            alt_text="Out-of-page asset",
        ),
    ]
    document = DocumentIR(
        doc_id="visual_regression",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=360, height=360),
                blocks=blocks,
                assets=assets,
            )
        ],
    )
    plan = TranslationLayoutPlan(
        chunk_id="visual_regression_chunk",
        blocks=[
            TranslationBlockPlan(
                source_block_id="p1_heading",
                translated_text="1 引言",
                role=BlockRole.HEADING,
            ),
            TranslationBlockPlan(
                source_block_id="p1_overlap_a",
                translated_text="第一段位于左上栏。",
                role=BlockRole.PARAGRAPH,
            ),
            TranslationBlockPlan(
                source_block_id="p1_overlap_b",
                translated_text="第二段用于触发重叠诊断。",
                role=BlockRole.PARAGRAPH,
            ),
            TranslationBlockPlan(
                source_block_id="p1_table",
                translated_text="A  B\n1  2",
                role=BlockRole.TABLE,
            ),
            TranslationBlockPlan(
                source_block_id="p1_formula",
                translated_text="E = mc^2",
                role=BlockRole.FORMULA,
            ),
            TranslationBlockPlan(
                source_block_id="p1_footnote",
                translated_text="1 紧凑排版脚注。",
                role=BlockRole.FOOTNOTE,
            ),
            TranslationBlockPlan(
                source_block_id="p1_overflow",
                translated_text="long translated sentence " * 30,
                role=BlockRole.PARAGRAPH,
            ),
        ],
    )

    return VisualRegressionFixture(
        document=document,
        plans=[plan],
        source_block_ids={block.block_id for block in blocks},
        expected_page_count=2,
        expected_quality_flag_counts={
            "asset_missing_path": 2,
            "continued_from_overflow": 1,
            "continued_on_next_page": 1,
            "continuation_page": 1,
            "font_scaled": 1,
            "missing_translation": 2,
        },
        expected_layout_issues=[
            {
                "kind": "bbox_outside_page",
                "page_id": "p1",
                "item_id": "asset_outside_page",
                "item_type": "asset",
            },
            {
                "kind": "empty_render_block",
                "page_id": "p1",
                "item_id": "p1_empty",
                "item_type": "block",
            },
            {
                "kind": "overlap",
                "page_id": "p1",
                "item_id": "p1_overlap_a",
                "item_type": "block",
                "other_id": "p1_overlap_b",
                "other_type": "block",
                "overlap_ratio": 0.4154,
            },
        ],
        expected_html_tokens=(
            'data-block-id="p1_overflow__cont_01"',
            'data-asset-id="asset_vector_placeholder"',
            'data-asset-id="asset_outside_page"',
            "quality-continuation-page",
            "quality-missing-translation",
            "quality-asset-missing-path",
            "role-table",
            "role-formula",
            "role-footnote",
        ),
    )
