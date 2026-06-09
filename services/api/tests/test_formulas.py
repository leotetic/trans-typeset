from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.pipeline.formulas import (
    DeterministicFormulaRecognizer,
    detect_formula_candidates,
    enrich_document_formulas,
)
from app.pipeline.formulas.recognizer import FormulaRecognitionError, _extract_json_object
from app.pipeline.ocr import DeterministicOCRProvider, OCRService, Pix2TextOCRProvider
from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
)
from pdf_translator_schema.models import (
    DocumentBlock,
    FormulaRecognitionResult,
    OCRRecognitionResult,
    TextLineIR,
    TextSpanIR,
)
from pydantic import ValidationError


def _document() -> DocumentIR:
    formula_block = DocumentBlock(
        block_id="b_formula",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=30, y0=80, x1=220, y1=110),
        reading_order=0,
        source_text="E = mc^2",
    )
    vector_asset = Asset(
        asset_id="a_vector_formula",
        page_id="p1",
        kind="figure",
        bbox=BoundingBox(x0=40, y0=140, x1=260, y1=170),
        alt_text="vector placeholder",
    )
    image_asset = Asset(
        asset_id="a_image_formula",
        page_id="p1",
        kind="image",
        bbox=BoundingBox(x0=50, y0=200, x1=290, y1=230),
        path="/api/documents/doc_1/assets/a_image_formula.png",
    )
    return DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[formula_block],
                assets=[vector_asset, image_asset],
            )
        ],
    )


def test_detector_finds_text_vector_and_image_formula_candidates() -> None:
    candidates = detect_formula_candidates(_document())

    assert [candidate.source_kind.value for candidate in candidates] == [
        "text_layer",
        "image_candidate",
    ]
    assert candidates[0].source_block_id == "b_formula"
    assert candidates[1].asset_id == "a_image_formula"


def test_detector_finds_inline_formula_candidates_from_span_metadata() -> None:
    spans = [
        TextSpanIR(
            span_id="s_text",
            page_id="p1",
            block_id="b_inline",
            line_id="l1",
            text="where ",
            bbox=BoundingBox(x0=20, y0=60, x1=52, y1=72),
            font_name="Times New Roman",
            font_size=10,
        ),
        TextSpanIR(
            span_id="s_formula",
            page_id="p1",
            block_id="b_inline",
            line_id="l1",
            text="E = mc^2",
            bbox=BoundingBox(x0=52, y0=60, x1=92, y1=72),
            font_name="Cambria Math",
            font_size=10,
        ),
        TextSpanIR(
            span_id="s_tail",
            page_id="p1",
            block_id="b_inline",
            line_id="l1",
            text=" holds.",
            bbox=BoundingBox(x0=92, y0=60, x1=125, y1=72),
            font_name="Times New Roman",
            font_size=10,
        ),
    ]
    block = DocumentBlock(
        block_id="b_inline",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=130, y1=80),
        reading_order=0,
        source_text="where E = mc^2 holds.",
        lines=[
            TextLineIR(
                line_id="l1",
                page_id="p1",
                block_id="b_inline",
                text="where E = mc^2 holds.",
                bbox=BoundingBox(x0=20, y0=60, x1=125, y1=72),
                span_ids=[span.span_id for span in spans],
            )
        ],
        spans=spans,
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
    )

    candidates = detect_formula_candidates(document)

    assert len(candidates) == 1
    assert candidates[0].display_mode == "inline"
    assert candidates[0].anchor_block_id == "b_inline"
    assert candidates[0].source_text == "E = mc^2"
    assert candidates[0].source_text_range == (6, 14)


def test_detector_keeps_inline_formula_boundary_before_explanation() -> None:
    block = DocumentBlock(
        block_id="b_inline_boundary",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=260, y1=90),
        reading_order=0,
        source_text=(
            "The reduced magnetic field B=p, defined as the ratio of the "
            "magnetic field to pressure, triggers oscillations."
        ),
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
    )

    candidates = detect_formula_candidates(document)

    assert len(candidates) == 1
    assert candidates[0].display_mode == "inline"
    assert candidates[0].source_text == "B=p"


def test_detector_rejects_author_email_as_formula() -> None:
    block = DocumentBlock(
        block_id="b_email",
        page_id="p1",
        role=BlockRole.FOOTNOTE,
        bbox=BoundingBox(x0=20, y0=350, x1=280, y1=370),
        reading_order=0,
        source_text="a)Authors to whom correspondence should be addressed: author@example.edu",
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
    )

    assert detect_formula_candidates(document) == []


def test_detector_rejects_operator_only_partial_fragments() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b_fragment",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=20, y0=55, x1=80, y1=70),
                        reading_order=0,
                        source_text="@=@",
                    )
                ],
            )
        ],
    )

    assert detect_formula_candidates(document) == []


def test_deterministic_recognizer_normalizes_pdf_formula_glyphs_and_flags_repairs() -> None:
    candidate = detect_formula_candidates(
        DocumentIR(
            doc_id="doc_1",
            pages=[
                DocumentPage(
                    page_id="p1",
                    size=PageSize(width=300, height=400),
                    blocks=[
                        DocumentBlock(
                            block_id="b_bad_formula",
                            page_id="p1",
                            role=BlockRole.PARAGRAPH,
                            bbox=BoundingBox(x0=20, y0=55, x1=150, y1=80),
                            reading_order=0,
                            source_text="νizμe=Te)1",
                        )
                    ],
                )
            ],
        )
    )[0]

    result = asyncio.run(DeterministicFormulaRecognizer().recognize(candidate))

    assert r"\nu" in result.latex
    assert r"\mu" in result.latex
    assert result.latex.count("(") == result.latex.count(")")
    assert result.confidence < 0.65
    assert "formula_low_confidence" in result.quality_flags


def test_deterministic_recognizer_preserves_text_and_marks_visual_mock() -> None:
    candidates = detect_formula_candidates(_document())
    recognizer = DeterministicFormulaRecognizer()

    text_result = asyncio.run(recognizer.recognize(candidates[0]))
    visual_result = asyncio.run(recognizer.recognize(candidates[1]))

    assert text_result.latex == "E = mc^2"
    assert text_result.confidence > 0.9
    assert "formula_recognition_mock" in visual_result.quality_flags
    assert "visual_formula_not_recognized_without_model" in visual_result.quality_flags


def test_ocr_service_falls_back_to_deterministic_provider() -> None:
    class EmptyProvider:
        name = "empty"

        async def recognize_formula(self, candidate, *, image_path=None):
            return OCRRecognitionResult(
                region_kind="formula",
                provider="empty",
                confidence=0,
                quality_flags=["empty_provider"],
            )

    candidate = detect_formula_candidates(_document())[0]
    service = OCRService(providers=[EmptyProvider(), DeterministicOCRProvider()])

    result = asyncio.run(service.recognize_formula(candidate))

    assert result.provider == "deterministic"
    assert result.latex == "E = mc^2"
    assert service.diagnostics()["record_count"] == 1


def test_ocr_service_times_out_slow_provider_and_falls_back_to_deterministic() -> None:
    class SlowProvider:
        name = "slow"

        async def recognize_formula(self, candidate, *, image_path=None):
            await asyncio.sleep(1)
            return OCRRecognitionResult(
                latex="late",
                region_kind="formula",
                provider="slow",
                confidence=0.99,
            )

    records: list[dict] = []
    candidate = detect_formula_candidates(_document())[0]
    service = OCRService(
        providers=[SlowProvider(), DeterministicOCRProvider()],
        provider_timeout_seconds=0.01,
        on_record=records.append,
    )

    result = asyncio.run(service.recognize_formula(candidate))

    assert result.provider == "deterministic"
    diagnostics = service.diagnostics()
    assert diagnostics["records"][0]["attempts"][0]["quality_flags"] == ["slow_timeout"]
    assert any(record.get("status") == "failed" for record in records)


def test_pix2text_init_failure_returns_unavailable_result(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Pix2TextOCRProvider(timeout_seconds=0.01)
    monkeypatch.setattr(
        provider,
        "_get_engine",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    candidate = detect_formula_candidates(_document())[1]

    result = asyncio.run(
        provider.recognize_formula(candidate, image_path=Path("formula.png"))
    )

    assert result.provider == "pix2text"
    assert result.confidence == 0
    assert "ocr_provider_unavailable" in result.quality_flags


def test_formula_recognition_result_rejects_model_coordinates() -> None:
    with pytest.raises(ValidationError):
        FormulaRecognitionResult.model_validate(
            {"latex": "x = y", "display_mode": "display", "bbox": [0, 0, 1, 1]}
        )


def test_formula_recognition_json_extraction_rejects_non_object() -> None:
    with pytest.raises(FormulaRecognitionError):
        _extract_json_object("[]")


def test_formula_service_updates_document_ir_and_diagnostics(tmp_path) -> None:
    result = asyncio.run(
        enrich_document_formulas(
            _document(),
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
        )
    )

    assert len(result.formulas) == 2
    assert result.document.formulas_by_id()[result.formulas[0].formula_id].latex == "E = mc^2"
    assert result.document.pages[0].blocks[0].source_text.startswith("{{formula:")
    assert result.document.pages[0].blocks[0].formula_id == result.formulas[0].formula_id
    assert result.diagnostics["candidate_count"] == 2
    assert "formula_recognition_mock" in result.diagnostics["quality_flags"]
    assert "visual_formula_recognition_disabled" in result.diagnostics["quality_flags"]
    assert result.diagnostics["recognizer_type"] == "deterministic"


def test_formula_service_rewrites_inline_formula_refs(tmp_path) -> None:
    block = DocumentBlock(
        block_id="b_inline",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=130, y1=80),
        reading_order=0,
        source_text="where E = mc^2 holds.",
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
    )

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            ocr_service=OCRService(providers=[DeterministicOCRProvider()]),
        )
    )

    assert result.formulas[0].display_mode == "inline"
    assert result.document.pages[0].blocks[0].role == BlockRole.PARAGRAPH
    assert "{{formula:" in result.document.pages[0].blocks[0].source_text
    assert "E = mc^2" not in result.document.pages[0].blocks[0].source_text


def test_legacy_formula_normalization_does_not_swallow_natural_language() -> None:
    block = DocumentBlock(
        block_id="b_legacy_boundary",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=100),
        reading_order=0,
        source_text=(
            "The reduced magnetic field B=p, defined as the ratio of the "
            "magnetic field to pressure, triggers oscillations."
        ),
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
    )

    normalized = __import__(
        "app.pipeline.formula_processing",
        fromlist=["normalize_document_formulas"],
    ).normalize_document_formulas(document)

    formulas = normalized.pages[0].blocks[0].formulas
    assert len(formulas) == 1
    assert formulas[0].source_text == "B=p"
    assert "defined as" in normalized.pages[0].blocks[0].text_for_translation


def test_legacy_formula_normalization_splits_latex_command_before_sentence() -> None:
    block = DocumentBlock(
        block_id="b_partial",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=100),
        reading_order=0,
        source_text=(
            r"\partial ne \partial x into the electron continuity equation "
            "gives the reduced form."
        ),
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
    )

    normalized = __import__(
        "app.pipeline.formula_processing",
        fromlist=["normalize_document_formulas"],
    ).normalize_document_formulas(document)

    formula = normalized.pages[0].blocks[0].formulas[0]
    assert formula.source_text == r"\partial ne \partial x"
    assert "into the electron continuity equation" in normalized.pages[0].blocks[0].text_for_translation


def test_detector_skips_vector_placeholder_and_header_banner_assets() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[],
                assets=[
                    Asset(
                        asset_id="vector_placeholder",
                        page_id="p1",
                        kind="figure",
                        bbox=BoundingBox(x0=40, y0=120, x1=240, y1=150),
                        alt_text="PDF vector drawing placeholder",
                    ),
                    Asset(
                        asset_id="journal_banner",
                        page_id="p1",
                        kind="image",
                        bbox=BoundingBox(x0=20, y0=10, x1=280, y1=45),
                        path="/api/documents/doc_1/assets/banner.png",
                        alt_text="journal header banner",
                    ),
                ],
            )
        ],
    )

    assert detect_formula_candidates(document) == []
