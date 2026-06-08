from app.pipeline.parser import (
    UnsupportedPdfError,
    build_parser_diagnostics,
    _extract_assets,
    _filter_header_footer_blocks,
    _header_footer_keys,
    _order_text_blocks,
    _reading_sort_key,
    _stable_asset_id,
    _stable_block_id,
    classify_role,
)
from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
)
from pdf_translator_schema.models import DocumentBlock


def test_classify_role_is_testable_for_common_pdf_blocks() -> None:
    assert (
        classify_role("Paper Title", page_index=0, block_index=0, font_size=18)
        == BlockRole.TITLE
    )
    assert (
        classify_role("Abstract", page_index=0, block_index=1, font_size=11)
        == BlockRole.ABSTRACT
    )
    assert (
        classify_role("1 Introduction", page_index=0, block_index=2, font_size=12)
        == BlockRole.HEADING
    )
    assert (
        classify_role("Fig. 2. Result overview.", page_index=1, block_index=0, font_size=9)
        == BlockRole.CAPTION
    )
    assert (
        classify_role("Table 1. Results overview.", page_index=1, block_index=1, font_size=9)
        == BlockRole.CAPTION
    )
    assert (
        classify_role("[1] Smith, A. 2024.", page_index=5, block_index=0, font_size=9)
        == BlockRole.REFERENCE
    )
    assert (
        classify_role("x = y + 1", page_index=1, block_index=2, font_size=10)
        == BlockRole.FORMULA
    )
    assert (
        classify_role("Method  Score  Time  Notes", page_index=1, block_index=3, font_size=9)
        == BlockRole.TABLE
    )
    assert (
        classify_role(
            "1 A short explanatory footnote.",
            page_index=1,
            block_index=8,
            font_size=7.5,
            bbox=(40, 650, 420, 690),
            page_height=800,
        )
        == BlockRole.FOOTNOTE
    )


def test_stable_block_id_is_independent_of_extraction_index() -> None:
    first = _stable_block_id("p0001", "Alpha beta", (10.0, 20.0, 100.0, 40.0))
    second = _stable_block_id("p0001", "Alpha   beta", (10.02, 20.01, 100.02, 40.01))

    assert first == second


def test_reading_sort_key_is_top_to_bottom_then_left_to_right() -> None:
    blocks = [
        {"bbox": (200, 20, 250, 40)},
        {"bbox": (10, 80, 150, 100)},
        {"bbox": (10, 20, 150, 40)},
    ]

    ordered = sorted(blocks, key=_reading_sort_key)

    assert ordered == [blocks[2], blocks[0], blocks[1]]


def test_extract_assets_writes_image_and_records_api_path(tmp_path) -> None:
    page_dict = {
        "blocks": [
            {
                "type": 1,
                "bbox": (10, 20, 110, 90),
                "ext": "png",
                "image": b"image-bytes",
            }
        ]
    }

    assets = _extract_assets("doc_1", "p0001", page_dict, tmp_path)

    assert len(assets) == 1
    asset = assets[0]
    assert asset.asset_id == _stable_asset_id("p0001", 1, (10, 20, 110, 90))
    assert asset.kind == "image"
    assert asset.path == f"/api/documents/doc_1/assets/{asset.asset_id}.png"
    assert (tmp_path / f"{asset.asset_id}.png").read_bytes() == b"image-bytes"


def _text_block(text: str, bbox: tuple[float, float, float, float]) -> dict:
    return {
        "type": 0,
        "bbox": bbox,
        "lines": [{"spans": [{"text": text}]}],
    }


def test_order_text_blocks_is_column_aware_for_two_column_papers() -> None:
    blocks = [
        _text_block("left top", (40, 80, 250, 110)),
        _text_block("right top", (330, 80, 540, 110)),
        _text_block("left bottom", (40, 130, 250, 160)),
        _text_block("right bottom", (330, 130, 540, 160)),
    ]

    ordered = _order_text_blocks(blocks, page_width=600)

    assert [block["lines"][0]["spans"][0]["text"] for block in ordered] == [
        "left top",
        "left bottom",
        "right top",
        "right bottom",
    ]


def test_repeated_header_footer_blocks_are_filtered() -> None:
    page_1 = {
        "height": 800,
        "blocks": [
            _text_block("Conference Header", (40, 12, 400, 32)),
            _text_block("Body one", (40, 120, 400, 150)),
            _text_block("1", (300, 774, 320, 790)),
        ],
    }
    page_2 = {
        "height": 800,
        "blocks": [
            _text_block("Conference Header", (40, 12, 400, 32)),
            _text_block("Body two", (40, 120, 400, 150)),
            _text_block("1", (300, 774, 320, 790)),
        ],
    }

    repeated = _header_footer_keys([page_1, page_2])
    filtered = _filter_header_footer_blocks(page_1, repeated)

    assert repeated == {"conference header", "1"}
    assert [block["lines"][0]["spans"][0]["text"] for block in filtered] == ["Body one"]


def test_parser_diagnostics_reports_structured_content_fallbacks() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="table_1",
                        page_id="p1",
                        role=BlockRole.TABLE,
                        bbox=BoundingBox(x0=10, y0=20, x1=200, y1=70),
                        reading_order=0,
                        source_text="A  B  C  D",
                    ),
                    DocumentBlock(
                        block_id="formula_1",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=10, y0=90, x1=200, y1=110),
                        reading_order=1,
                        source_text="x = y + 1",
                    ),
                ],
                assets=[
                    Asset(
                        asset_id="asset_1",
                        page_id="p1",
                        kind="image",
                        bbox=BoundingBox(x0=20, y0=140, x1=120, y1=220),
                    ),
                    Asset(
                        asset_id="vector_1",
                        page_id="p1",
                        kind="figure",
                        bbox=BoundingBox(x0=130, y0=140, x1=240, y1=220),
                    ),
                ],
            )
        ],
    )

    diagnostics = build_parser_diagnostics(document)

    assert diagnostics["role_counts"]["table"] == 1
    assert diagnostics["role_counts"]["formula"] == 1
    assert diagnostics["asset_counts"]["image"] == 1
    assert diagnostics["asset_counts"]["figure"] == 1
    assert "table_text_fallback" in diagnostics["fallback_flags"]
    assert "formula_text_fallback" in diagnostics["fallback_flags"]
    assert "vector_asset_placeholder" in diagnostics["fallback_flags"]
    assert "vector_assets_not_rasterized" in diagnostics["fallback_flags"]
    assert diagnostics["unsupported_features"][0]["status"] == "placeholder_only"


def test_unsupported_pdf_error_carries_scanned_pdf_diagnostics() -> None:
    error = UnsupportedPdfError(
        "OCR required",
        {
            "kind": "unsupported_scanned_pdf",
            "reason": "ocr_required",
            "text_block_count": 0,
            "recoverable": True,
        },
    )

    assert str(error) == "OCR required"
    assert error.diagnostics["kind"] == "unsupported_scanned_pdf"
    assert error.diagnostics["recoverable"] is True
