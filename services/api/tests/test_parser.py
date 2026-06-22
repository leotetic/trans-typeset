from app.pipeline.parser import (
    UnsupportedPdfError,
    build_parser_diagnostics,
    _block_text,
    _extract_assets,
    _extract_assets_from_page,
    _filter_header_footer_blocks,
    _header_footer_keys,
    _order_text_blocks,
    _repair_stacked_formula_text,
    _reading_sort_key,
    _stable_asset_id,
    _stable_block_id,
    classify_role,
)
from app.pipeline.formulas.normalization import normalize_pdf_text
from app.pipeline.formula_processing import build_formula_diagnostics, normalize_document_formulas
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
        classify_role(
            "Figure 4 shows the effective propagation velocity.",
            page_index=1,
            block_index=2,
            font_size=9,
        )
        == BlockRole.PARAGRAPH
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
        classify_role(
            r"\frac{\alpha}{\beta + 1} = q_s , (4)",
            page_index=1,
            block_index=3,
            font_size=12,
        )
        == BlockRole.FORMULA
    )
    assert (
        classify_role(
            "We solve E = mc^2 in the text and preserve it.",
            page_index=1,
            block_index=2,
            font_size=10,
        )
        == BlockRole.PARAGRAPH
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


def test_extract_assets_from_page_uses_image_info_without_text_dict_payload(tmp_path) -> None:
    class FakeDocument:
        def extract_image(self, xref: int) -> dict:
            assert xref == 7
            return {"image": b"image-bytes", "ext": "png"}

    class FakePage:
        parent = FakeDocument()

        def get_image_info(self, xrefs: bool = False) -> list[dict]:
            assert xrefs is True
            return [{"xref": 7, "bbox": (10, 20, 110, 90)}]

    assets = _extract_assets_from_page("doc_1", "p0001", FakePage(), tmp_path)

    assert len(assets) == 1
    asset = assets[0]
    assert asset.asset_id == _stable_asset_id("p0001", 1, (10, 20, 110, 90))
    assert asset.path == f"/api/documents/doc_1/assets/{asset.asset_id}.png"
    assert (tmp_path / f"{asset.asset_id}.png").read_bytes() == b"image-bytes"


def _text_block(text: str, bbox: tuple[float, float, float, float]) -> dict:
    return {
        "type": 0,
        "bbox": bbox,
        "lines": [{"spans": [{"text": text}]}],
    }


def _span(
    text: str,
    bbox: tuple[float, float, float, float],
    *,
    font: str = "AdvOT7d6df7ab.I",
    size: float = 9,
) -> dict:
    return {"text": text, "bbox": bbox, "font": font, "size": size}


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


def test_order_text_blocks_ignores_wide_page_artifacts_when_detecting_columns() -> None:
    blocks = [
        _text_block("Journal banner", (40, 20, 560, 45)),
        _text_block("left top", (50, 100, 295, 150)),
        _text_block("right top", (315, 105, 560, 155)),
        _text_block("left equation", (80, 180, 295, 205)),
        _text_block("right equation", (330, 170, 560, 195)),
        _text_block("left bottom", (50, 220, 295, 260)),
        _text_block("right bottom", (315, 215, 560, 260)),
        _text_block("footer", (35, 740, 575, 750)),
    ]

    ordered = _order_text_blocks(blocks, page_width=612)

    assert [block["lines"][0]["spans"][0]["text"] for block in ordered] == [
        "Journal banner",
        "left top",
        "left equation",
        "left bottom",
        "right top",
        "right equation",
        "right bottom",
        "footer",
    ]


def test_rawdict_geometry_recovers_aip_scripts_and_stacked_fractions() -> None:
    block = {
        "type": 0,
        "bbox": (80, 450, 245, 475),
        "lines": [
            {
                "bbox": (84, 451, 245, 460),
                "spans": [
                    _span("f", (90, 451, 93, 460)),
                    _span("s", (93, 455, 96, 461), size=6.3),
                    _span("2", (96, 448, 99, 454), font="AdvOT1ef757c0", size=6.3),
                    _span(" = ", (100, 451, 112, 460), font="AdvP4C4E51"),
                    _span("@", (114, 451, 119, 460), font="AdvP4C4E51"),
                    _span("f", (120, 451, 123, 460)),
                    _span("s", (123, 455, 126, 461), size=6.3),
                    _span("@", (130, 451, 135, 460), font="AdvP4C4E51"),
                    _span("t", (136, 451, 140, 460)),
                    _span(" + q", (145, 451, 164, 460)),
                    _span("s", (164, 455, 167, 461), size=6.3),
                ],
            },
            {
                "bbox": (151, 462, 158, 471),
                "spans": [
                    _span("m", (151, 462, 156, 471)),
                    _span("s", (156, 466, 159, 472), size=6.3),
                ],
            },
        ],
    }

    text = _block_text(block)
    repaired = _repair_stacked_formula_text(text, block)

    assert "f_{s}^{2}" in text
    assert " / " in text
    assert "∂f_{s}" in text
    assert r"\frac{∂f_s}{∂t}" in repaired
    assert r"\frac{q_s}{m_s}" in repaired


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


def test_publication_boilerplate_blocks_are_filtered_from_body_flow() -> None:
    page = {
        "height": 800,
        "blocks": [
            _text_block("Body one", (40, 120, 400, 150)),
            _text_block("© Author(s) 2025", (40, 754, 130, 763)),
            _text_block("© 作者 2025", (140, 754, 210, 763)),
            _text_block(
                "J. Appl. Phys. 137, 163302 (2025); doi: 10.1063/5.0260925",
                (35, 741, 575, 750),
            ),
            _text_block("25 April 2025 00:08:47", (562, 390, 568, 453)),
            _text_block("1 A short explanatory footnote.", (40, 690, 420, 710)),
        ],
    }

    filtered = _filter_header_footer_blocks(page, set())

    assert [block["lines"][0]["spans"][0]["text"] for block in filtered] == [
        "Body one",
        "1 A short explanatory footnote.",
    ]


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


def test_formula_normalization_detects_inline_and_display_formulas() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="para",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=20, x1=280, y1=60),
                        reading_order=0,
                        source_text="The scaled law is α[E] ¼ α[B] ¼ −1.",
                    ),
                    DocumentBlock(
                        block_id="formula",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=10, y0=90, x1=280, y1=120),
                        reading_order=1,
                        source_text="@fs=@t + ∇·(fs v)=0",
                    ),
                ],
            )
        ],
    )

    normalized = normalize_document_formulas(document)
    para, display = normalized.pages[0].blocks
    diagnostics = build_formula_diagnostics(normalized)

    assert "{{formula:" in para.text_for_translation
    assert para.formulas[0].kind == "inline"
    assert "\\alpha" in para.formulas[0].latex
    assert display.text_for_translation == f"{{{{formula:{display.formula_id}}}}}"
    assert display.formulas[0].kind == "display"
    assert "\\nabla" in display.formulas[0].latex
    assert diagnostics["formula_count"] == 2
    assert diagnostics["inline_count"] == 1
    assert diagnostics["display_count"] == 1


def test_formula_normalization_repairs_malformed_placeholders_in_diagnostics() -> None:
    block = DocumentBlock(
        block_id="para",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=10, y0=20, x1=280, y1=60),
        reading_order=0,
        source_text="The transport term is preserved.",
    ).model_copy(
        update={"text_for_translation": "The term {formula:formula_known}} is preserved."},
        deep=True,
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[block],
            )
        ],
        formulas=[
            {
                "formula_id": "formula_known",
                "page_id": "p1",
                "anchor_block_id": "para",
                "latex": r"\nabla \cdot \Gamma_\epsilon",
                "display_mode": "inline",
                "source_kind": "inline_text",
            }
        ],
    )

    normalized = normalize_document_formulas(document)
    diagnostics = build_formula_diagnostics(normalized)

    assert normalized.pages[0].blocks[0].text_for_translation == (
        "The term {{formula:formula_known}} is preserved."
    )
    assert diagnostics["malformed_placeholder_repaired_count"] == 1
    assert diagnostics["quality_flag_counts"]["formula_placeholder_syntax_repaired"] == 1


def test_formula_normalization_merges_adjacent_formula_fragments_into_cluster() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="formula_a",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=40, y0=90, x1=120, y1=108),
                        reading_order=0,
                        source_text="@fs=@t",
                    ),
                    DocumentBlock(
                        block_id="formula_b",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=128, y0=92, x1=260, y1=110),
                        reading_order=1,
                        source_text="f = 0",
                    ),
                ],
            )
        ],
    )

    normalized = normalize_document_formulas(document)
    diagnostics = build_formula_diagnostics(normalized)

    assert len(normalized.pages[0].blocks) == 1
    assert diagnostics["formula_fragment_cluster_count"] == 1
    assert diagnostics["formula_fragment_suppressed_block_count"] == 1
    assert diagnostics["formula_fragment_clusters"][0]["merged_block_ids"] == [
        "formula_a",
        "formula_b",
    ]
    assert len(normalized.formulas) == 2
    assert normalized.formulas[0].source_block_id == "formula_a"
    assert normalized.formulas[0].source_text_range == (0, len("@fs=@t"))
    assert normalized.formulas[1].source_block_id == "formula_a"
    assert normalized.formulas[1].source_text_range == (
        len("@fs=@t") + 1,
        len("@fs=@t") + 1 + len("f = 0"),
    )
    assert normalized.pages[0].blocks[0].source_text == "@fs=@t f = 0"


def test_formula_normalization_consumes_nonadjacent_fragments_in_equation_band() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[
                    DocumentBlock(
                        block_id="eq_main",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=330, y0=405, x1=560, y1=430),
                        column=1,
                        reading_order=0,
                        source_text="@fs=k2 + qs ms (E=k)",
                    ),
                    DocumentBlock(
                        block_id="frac_a",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=394, y0=429, x1=401, y1=452),
                        column=1,
                        reading_order=1,
                        source_text="f 0 s k2",
                    ),
                    DocumentBlock(
                        block_id="tiny_ref",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=430, y0=446, x1=436, y1=453),
                        column=1,
                        reading_order=2,
                        source_text="v_{n}",
                        text_for_translation="{{formula:Fvn}}",
                    ),
                    DocumentBlock(
                        block_id="prose_gap",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=315, y0=464, x1=559, y1=500),
                        column=1,
                        reading_order=3,
                        source_text="The velocity is a similarity invariant.",
                    ),
                    DocumentBlock(
                        block_id="frac_b",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=404, y0=429, x1=429, y1=452),
                        column=1,
                        reading_order=4,
                        source_text="f 0 n k - fs k2",
                    ),
                    DocumentBlock(
                        block_id="eq_tail",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=387, y0=423, x1=559, y1=447),
                        column=1,
                        reading_order=5,
                        source_text="× - d3vn gsn σsn(gsn, Ω) dΩ: (4)",
                    ),
                ],
            )
        ],
    )

    normalized = normalize_document_formulas(document)
    diagnostics = build_formula_diagnostics(normalized)
    blocks = normalized.pages[0].blocks

    assert [block.block_id for block in blocks] == ["eq_main", "prose_gap"]
    assert diagnostics["formula_fragment_cluster_count"] == 1
    assert set(diagnostics["formula_fragment_clusters"][0]["merged_block_ids"]) == {
        "eq_main",
        "frac_a",
        "tiny_ref",
        "frac_b",
        "eq_tail",
    }
    assert "f 0 s k2" not in [block.source_text for block in blocks]


def test_pdf_text_normalization_removes_control_glyphs() -> None:
    assert normalize_pdf_text("E \x01 B and cm\x032 with a ¼ b þ c") == "E × B and cm-2 with a = b + c"


def test_pdf_formula_normalization_repairs_corrupt_slash_glyphs_only_with_markers() -> None:
    from app.pipeline.formulas.normalization import latex_from_pdf_text

    corrupt_latex, corrupt_flags = latex_from_pdf_text("@fs=@t þ f 0 s=k2")
    clean_latex, clean_flags = latex_from_pdf_text("x = y + 1")

    assert r"\partial f_s / \partial t" in corrupt_latex
    assert "f'_s / k^2" in corrupt_latex
    assert "formula_slash_glyph_repaired" in corrupt_flags
    assert "formula_text_layer_corrupt" in corrupt_flags
    assert clean_latex == "x = y + 1"
    assert "formula_slash_glyph_repaired" not in clean_flags


def test_noise_text_blocks_are_not_normalized_as_translatable_formulas() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="noise",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=20, x1=60, y1=30),
                        reading_order=0,
                        source_text="\x01 \x03",
                    ),
                    DocumentBlock(
                        block_id="fragment",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=40, x1=60, y1=50),
                        reading_order=1,
                        source_text="vn",
                    ),
                ],
            )
        ],
    )

    normalized = normalize_document_formulas(document)

    assert all(not block.formulas for block in normalized.pages[0].blocks)


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
