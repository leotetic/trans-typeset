import pytest
from pydantic import ValidationError

from pdf_translator_schema import (
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
    validate_layout_plan,
)
from pdf_translator_schema.json_schema import export_schema
from pdf_translator_schema.models import DocumentBlock, InlineItem
from pdf_translator_schema.validation import LayoutPlanValidationError


def test_render_defaults_are_available_on_chunk() -> None:
    chunk = TranslationChunk(
        chunk_id="chunk_1",
        source_blocks=[
            SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Hello")
        ],
    )

    assert chunk.target_lang == "zh-CN"
    assert chunk.render_defaults.font_stack[0] == "Noto Sans CJK SC"
    assert chunk.render_defaults.alignment.paragraph == "justify"
    assert chunk.render_defaults.overflow_policy.min_font_scale == 0.86
    assert chunk.render_defaults.overflow_policy.allow_continuation_page is True
    assert chunk.render_defaults.preserve_policy.whitespace == "allow_reflow"


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
    ],
)
def test_layout_plan_rejects_layout_coordinates(model_cls: type, payload: dict) -> None:
    with pytest.raises(ValidationError):
        model_cls.model_validate(payload)


def test_json_schema_export_includes_metadata(tmp_path) -> None:
    export_schema(tmp_path)

    for filename in (
        "document-ir.schema.json",
        "translation-chunk.schema.json",
        "translation-layout-plan.schema.json",
    ):
        schema_text = (tmp_path / filename).read_text(encoding="utf-8")
        assert '"$schema": "https://json-schema.org/draft/2020-12/schema"' in schema_text
        assert '"x-schema-version": "0.1"' in schema_text
