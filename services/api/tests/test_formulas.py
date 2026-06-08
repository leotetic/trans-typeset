from __future__ import annotations

import asyncio

import pytest
from app.pipeline.formulas import (
    DeterministicFormulaRecognizer,
    detect_formula_candidates,
    enrich_document_formulas,
)
from app.pipeline.formulas.recognizer import FormulaRecognitionError, _extract_json_object
from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
)
from pdf_translator_schema.models import DocumentBlock, FormulaRecognitionResult
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
        "vector_candidate",
        "image_candidate",
    ]
    assert candidates[0].source_block_id == "b_formula"
    assert candidates[1].asset_id == "a_vector_formula"
    assert candidates[2].asset_id == "a_image_formula"


def test_deterministic_recognizer_preserves_text_and_marks_visual_mock() -> None:
    candidates = detect_formula_candidates(_document())
    recognizer = DeterministicFormulaRecognizer()

    text_result = asyncio.run(recognizer.recognize(candidates[0]))
    visual_result = asyncio.run(recognizer.recognize(candidates[1]))

    assert text_result.latex == "E = mc^2"
    assert text_result.confidence > 0.9
    assert "formula_recognition_mock" in visual_result.quality_flags
    assert "visual_formula_not_recognized_without_model" in visual_result.quality_flags


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

    assert len(result.formulas) == 3
    assert result.document.formulas_by_id()[result.formulas[0].formula_id].latex == "E = mc^2"
    assert result.document.pages[0].blocks[0].source_text.startswith("{{formula:")
    assert result.document.pages[0].blocks[0].formula_id == result.formulas[0].formula_id
    assert result.diagnostics["candidate_count"] == 3
    assert "formula_recognition_mock" in result.diagnostics["quality_flags"]
