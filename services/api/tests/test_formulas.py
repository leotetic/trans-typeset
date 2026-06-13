from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.pipeline.parser import parse_pdf
from app.pipeline.formulas import (
    DeterministicFormulaRecognizer,
    detect_formula_candidates,
    enrich_document_formulas,
)
import app.pipeline.formulas.validation as formula_validation
from app.pipeline.formulas.normalization import latex_from_pdf_text
from app.pipeline.formulas.recognizer import FormulaRecognitionError, _extract_json_object
from app.pipeline.formulas.validation import validate_formula_latex
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
    FormulaIR,
    FormulaRecognitionResult,
    OCRRecognitionResult,
    TextLineIR,
    TextSpanIR,
)
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT_AIP_FORMULA_PDF = REPO_ROOT / "1.pdf"


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


def test_legacy_formula_normalization_does_not_promote_sentence_formula_role_to_display() -> None:
    block = DocumentBlock(
        block_id="b_sentence_formula",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=90),
        reading_order=0,
        source_text="We solve E = mc^2 in the text and preserve it.",
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

    normalized_block = normalized.pages[0].blocks[0]
    assert normalized_block.formula_id is None
    assert len(normalized_block.formulas) == 1
    formula = normalized_block.formulas[0]
    assert formula.source_text == "E = mc^2"
    assert formula.kind == "inline"
    assert normalized_block.text_for_translation == (
        f"We solve {{{{formula:{formula.formula_id}}}}} in the text and preserve it."
    )


def test_formula_validation_handles_empty_stderr_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        formula_validation.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr=None),
    )

    assert formula_validation._katex_render_error(r"\int f_s\,d\Omega") == "katex_render_failed"


def test_detector_does_not_promote_prose_paragraphs_to_display_formulas() -> None:
    bad_samples = [
        "From the dispersion relation, we can obtain the phase velocity v ph = Re(ω)=kw = Te μe.",
        (
            "discharge scale. In conclusion, when pd remains constant and B=p increases, "
            "the breathing oscillations are intensified."
        ),
        (
            "neutrals, caused by variations in discharge parameters, play a dominant "
            "role in altering the ion energy loss mechanism."
        ),
    ]
    blocks = [
        DocumentBlock(
            block_id=f"b_prose_{index}",
            page_id="p1",
            role=BlockRole.PARAGRAPH,
            bbox=BoundingBox(x0=20, y0=55 + index * 30, x1=280, y1=80 + index * 30),
            reading_order=index,
            source_text=text,
        )
        for index, text in enumerate(bad_samples)
    ]
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=blocks,
            )
        ],
    )

    candidates = detect_formula_candidates(document)

    assert all(candidate.display_mode == "inline" for candidate in candidates)
    assert all(candidate.source_block_id is None for candidate in candidates)


def test_detector_promotes_legacy_display_cluster_before_inline_detection() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b_display",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
                        reading_order=0,
                        source_text="{{formula:Flegacy_a}} {{formula:Flegacy_b}} (4)",
                        text_for_translation="{{formula:Flegacy_a}} {{formula:Flegacy_b}} (4)",
                    )
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="Flegacy_a",
                page_id="p1",
                source_block_id="b_display",
                latex=r"\partial f_s / \partial t",
                source_text=r"\partial f_s / \partial t",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=["formula_display_cluster"],
            ),
            FormulaIR(
                formula_id="Flegacy_b",
                page_id="p1",
                source_block_id="b_display",
                latex=r"= \sum_n",
                source_text=r"= \sum_n",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=["formula_display_cluster"],
            ),
        ],
    )
    setattr(
        document,
        "_formula_fragment_cluster_diagnostics",
        {
            "formula_fragment_cluster_count": 1,
            "formula_fragment_suppressed_block_count": 1,
            "formula_fragment_clusters": [
                {
                    "cluster_id": "cluster_1",
                    "page_id": "p1",
                    "primary_block_id": "b_display",
                    "merged_block_ids": ["b_display"],
                    "formula_ids": ["Flegacy_a", "Flegacy_b"],
                    "display_mode": "display",
                    "combined_text": "{{formula:Flegacy_a}} {{formula:Flegacy_b}} (4)",
                }
            ],
        },
    )

    candidates = detect_formula_candidates(document)

    assert len(candidates) == 1
    assert candidates[0].display_mode == "display"
    assert candidates[0].source_block_id == "b_display"
    assert candidates[0].legacy_formula_ids == ("Flegacy_a", "Flegacy_b")
    assert "formula_equation_number_preserved" in candidates[0].quality_flags


def test_detector_orders_legacy_display_formulas_by_source_block_position() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=500),
                blocks=[
                    DocumentBlock(
                        block_id="eq1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=40, y0=80, x1=260, y1=100),
                        reading_order=0,
                        source_text="{{formula:F1}}",
                        text_for_translation="{{formula:F1}}",
                        formula_id="F1",
                    ),
                    DocumentBlock(
                        block_id="eq2",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=40, y0=180, x1=260, y1=210),
                        reading_order=1,
                        source_text="{{formula:F2}}",
                        text_for_translation="{{formula:F2}}",
                    ),
                    DocumentBlock(
                        block_id="eq3",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=40, y0=280, x1=260, y1=310),
                        reading_order=2,
                        source_text="{{formula:F3}}",
                        text_for_translation="{{formula:F3}}",
                    ),
                    DocumentBlock(
                        block_id="eq4",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=40, y0=380, x1=260, y1=430),
                        reading_order=3,
                        source_text="{{formula:F4a}} {{formula:F4b}}",
                        text_for_translation="{{formula:F4a}} {{formula:F4b}}",
                    ),
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="F1",
                page_id="p1",
                source_block_id="eq1",
                latex=r"G(x_1,t_1)=G(x_k,t_k)",
                source_text=r"G(x_1,t_1)=G(x_k,t_k) (1)",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=["formula_display_cluster"],
            ),
            FormulaIR(
                formula_id="F2",
                page_id="p1",
                source_block_id="eq2",
                latex=r"\frac{\partial f_s}{\partial t}",
                source_text=r"\frac{\partial f_s}{\partial t}",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=["formula_display_cluster"],
            ),
            FormulaIR(
                formula_id="F3",
                page_id="p1",
                source_block_id="eq3",
                latex=r"\int f_s\,dv",
                source_text=r"\int f_s\,dv",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=["formula_display_cluster"],
            ),
            FormulaIR(
                formula_id="F4a",
                page_id="p1",
                source_block_id="eq4",
                latex=r"\partial f_s/k^2",
                source_text=r"\partial f_s/k^2",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=["formula_display_cluster"],
            ),
            FormulaIR(
                formula_id="F4b",
                page_id="p1",
                source_block_id="eq4",
                latex=r"f'_s/k^2",
                source_text=r"f'_s/k^2",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=["formula_display_cluster"],
            ),
        ],
    )
    setattr(
        document,
        "_formula_fragment_cluster_diagnostics",
        {
            "formula_fragment_cluster_count": 3,
            "formula_fragment_suppressed_block_count": 2,
            "formula_fragment_clusters": [
                {
                    "cluster_id": "cluster_2",
                    "page_id": "p1",
                    "primary_block_id": "eq2",
                    "merged_block_ids": ["eq2"],
                    "formula_ids": ["F2"],
                    "display_mode": "display",
                    "combined_text": "{{formula:F2}} (2)",
                },
                {
                    "cluster_id": "cluster_3",
                    "page_id": "p1",
                    "primary_block_id": "eq3",
                    "merged_block_ids": ["eq3"],
                    "formula_ids": ["F3"],
                    "display_mode": "display",
                    "combined_text": "{{formula:F3}} (3)",
                },
                {
                    "cluster_id": "cluster_4",
                    "page_id": "p1",
                    "primary_block_id": "eq4",
                    "merged_block_ids": ["eq4"],
                    "formula_ids": ["F4a", "F4b"],
                    "display_mode": "display",
                    "combined_text": "{{formula:F4a}} {{formula:F4b}} (4)",
                },
            ],
        },
    )

    display_candidates = [
        candidate
        for candidate in detect_formula_candidates(document)
        if candidate.display_mode == "display"
    ]

    assert [candidate.source_block_id for candidate in display_candidates] == [
        "eq1",
        "eq2",
        "eq3",
        "eq4",
    ]
    assert _equation_numbers_from_candidates(display_candidates) == ["1", "2", "3", "4"]


def test_aip_pdf_page_one_regression_when_fixture_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not ROOT_AIP_FORMULA_PDF.exists():
        pytest.skip(f"local AIP formula fixture is missing: {ROOT_AIP_FORMULA_PDF}")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))

    document = parse_pdf(ROOT_AIP_FORMULA_PDF, doc_id="doc_aip_page_one")
    display_candidates = [
        candidate
        for candidate in detect_formula_candidates(document)
        if candidate.display_mode == "display"
    ]
    page_one_blocks = document.pages[0].blocks
    block_texts = [block.source_text for block in page_one_blocks]

    assert len(display_candidates) == 4
    assert _equation_numbers_from_candidates(display_candidates) == ["1", "2", "3", "4"]
    assert not any(_is_leftover_formula_fragment(text) for text in block_texts)

    class Pix2TextFixtureProvider:
        name = "pix2text"

        async def recognize_formula(self, candidate, *, image_path=None):
            if "(2)" in candidate.source_text or "(4)" in candidate.source_text:
                latex = r"\frac{\partial f_s}{\partial t} + v \cdot \nabla_r f_s"
            else:
                latex = candidate.source_text
            return OCRRecognitionResult(
                text=latex,
                latex=latex,
                region_kind="formula",
                provider="pix2text",
                confidence=0.93,
                quality_flags=["pix2text_used"],
            )

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_aip_page_one",
            asset_output_dir=tmp_path / "assets",
            pdf_path=ROOT_AIP_FORMULA_PDF,
            recognizer=DeterministicFormulaRecognizer(),
            ocr_service=OCRService(
                providers=[Pix2TextFixtureProvider(), DeterministicOCRProvider()]
            ),
        )
    )
    display_formulas = [
        formula for formula in result.formulas if formula.display_mode == "display"
    ]

    assert len(display_formulas) == 4
    assert any(
        formula.ocr_provider == "pix2text"
        and r"\frac{\partial f_s}{\partial t}" in formula.latex
        for formula in display_formulas
    )


def _equation_numbers_from_candidates(candidates) -> list[str]:
    numbers: list[str] = []
    for candidate in candidates:
        match = re.search(r"\((\d+)\)\s*$", candidate.source_text)
        if match is not None:
            numbers.append(match.group(1))
            continue
        match = re.search(r"\((\d+)\)", candidate.source_text)
        if match is not None:
            numbers.append(match.group(1))
    return numbers


def _is_leftover_formula_fragment(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    return bool(
        re.fullmatch(r"f\s*0\s*s(?:\s*k2)?", normalized)
        or re.fullmatch(r"f\s*0\s*n(?:\s*k\s*-\s*fs\s*k2)?", normalized)
        or re.fullmatch(r"v[_\\{]*n[\\}]*", normalized)
    )


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


def test_deterministic_recognizer_downgrades_corrupt_aip_text_layer() -> None:
    candidate = detect_formula_candidates(
        DocumentIR(
            doc_id="doc_1",
            pages=[
                DocumentPage(
                    page_id="p1",
                    size=PageSize(width=300, height=400),
                    blocks=[
                        DocumentBlock(
                            block_id="b_corrupt_formula",
                            page_id="p1",
                            role=BlockRole.FORMULA,
                            bbox=BoundingBox(x0=20, y0=55, x1=240, y1=88),
                            reading_order=0,
                            source_text="@fs=@t þ f 0 s=k2 (4)",
                        )
                    ],
                )
            ],
        )
    )[0]

    result = asyncio.run(DeterministicFormulaRecognizer().recognize(candidate))

    assert r"\partial f_s / \partial t" in result.latex
    assert "f'_s / k^2" in result.latex
    assert result.confidence < 0.65
    assert "formula_text_layer_corrupt" in result.quality_flags
    assert "formula_slash_glyph_repaired" in result.quality_flags
    assert "formula_prime_glyph_suspect" in result.quality_flags


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

    assert len(result.formulas) == 1
    assert result.document.formulas_by_id()[result.formulas[0].formula_id].latex == "E = mc^2"
    assert result.document.pages[0].blocks[0].source_text.startswith("{{formula:")
    assert result.document.pages[0].blocks[0].formula_id == result.formulas[0].formula_id
    assert result.diagnostics["candidate_count"] == 2
    assert result.diagnostics["accepted_count"] == 1
    assert result.diagnostics["rejected_count"] == 1
    assert "formula_visual_mock_rejected" in result.diagnostics["quality_flags"]
    assert "visual_formula_recognition_disabled" in result.diagnostics["quality_flags"]
    assert result.diagnostics["recognizer_type"] == "deterministic"


def test_formula_service_migrates_legacy_display_formula_cluster(tmp_path) -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b_cluster",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
                        reading_order=0,
                        source_text="{{formula:Flegacy}} (4)",
                        text_for_translation="{{formula:Flegacy}} (4)",
                    )
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="Flegacy",
                page_id="p1",
                source_block_id="b_cluster",
                latex=r"\partial f_s / \partial t",
                source_text=r"\partial f_s / \partial t",
                display_mode="display",
                source_kind="text_layer",
                quality_flags=["formula_display_cluster"],
            )
        ],
    )
    setattr(
        document,
        "_formula_fragment_cluster_diagnostics",
        {
            "formula_fragment_cluster_count": 1,
            "formula_fragment_suppressed_block_count": 0,
            "formula_fragment_clusters": [
                {
                    "cluster_id": "cluster_legacy",
                    "page_id": "p1",
                    "primary_block_id": "b_cluster",
                    "merged_block_ids": ["b_cluster"],
                    "formula_ids": ["Flegacy"],
                    "display_mode": "display",
                    "combined_text": "{{formula:Flegacy}} (4)",
                }
            ],
        },
    )

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
        )
    )

    assert len(result.formulas) == 1
    assert result.document.pages[0].blocks[0].source_text == (
        f"{{{{formula:{result.formulas[0].formula_id}}}}}"
    )
    assert result.document.pages[0].blocks[0].text_for_translation == (
        f"{{{{formula:{result.formulas[0].formula_id}}}}}"
    )
    assert result.diagnostics["parser_cluster_count"] == 1
    assert result.diagnostics["display_cluster_promoted_count"] == 1
    assert result.diagnostics["legacy_formula_migrated_count"] >= 1


def test_formula_service_prefers_visual_provider_for_complex_display_formula(tmp_path) -> None:
    (tmp_path / "formula_asset.png").write_bytes(b"fake-image")
    block = DocumentBlock(
        block_id="b_complex_display",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
        reading_order=0,
        source_text=r"\partial f_s / \partial t = \sum_n (4)",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[block],
                assets=[
                    Asset(
                        asset_id="formula_asset",
                        page_id="p1",
                        kind="formula",
                        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
                        path="/api/documents/doc_1/assets/formula_asset.png",
                    )
                ],
            )
        ],
    )

    class VisualOnlyProvider:
        name = "pix2text"

        async def recognize_formula(self, candidate, *, image_path=None):
            return OCRRecognitionResult(
                text=r"\frac{\partial f_s}{\partial t} = \sum_n",
                latex=r"\frac{\partial f_s}{\partial t} = \sum_n",
                region_kind="formula",
                provider="pix2text",
                confidence=0.91,
                quality_flags=["pix2text_used"],
            )

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
            ocr_service=OCRService(providers=[VisualOnlyProvider(), DeterministicOCRProvider()]),
        )
    )

    assert len(result.formulas) == 1
    assert result.formulas[0].ocr_provider == "pix2text"
    assert "formula_visual_escalated" in result.diagnostics["records"][0]["quality_flags"]
    assert result.diagnostics["records"][0]["status"] in {"recognized", "upgraded"}


def test_formula_service_prefers_visual_provider_for_corrupt_display_text_layer(tmp_path) -> None:
    (tmp_path / "formula_asset.png").write_bytes(b"fake-image")
    block = DocumentBlock(
        block_id="b_corrupt_display",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
        reading_order=0,
        source_text="@fs=@t þ f 0 s=k2 (4)",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[block],
                assets=[
                    Asset(
                        asset_id="formula_asset",
                        page_id="p1",
                        kind="formula",
                        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
                        path="/api/documents/doc_1/assets/formula_asset.png",
                    )
                ],
            )
        ],
    )

    class VisualProvider:
        name = "pix2text"

        async def recognize_formula(self, candidate, *, image_path=None):
            return OCRRecognitionResult(
                text=r"\frac{\partial f_s}{\partial t} + \frac{f'_s}{k^2}",
                latex=r"\frac{\partial f_s}{\partial t} + \frac{f'_s}{k^2}",
                region_kind="formula",
                provider="pix2text",
                confidence=0.93,
                quality_flags=["pix2text_used"],
            )

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
            ocr_service=OCRService(providers=[VisualProvider(), DeterministicOCRProvider()]),
        )
    )

    assert len(result.formulas) == 1
    assert result.formulas[0].ocr_provider == "pix2text"
    assert r"\frac{\partial f_s}{\partial t}" in result.formulas[0].latex
    assert "formula_visual_escalated" in result.diagnostics["records"][0]["quality_flags"]


def test_display_validation_rejects_corrupt_text_layer_even_when_katex_compiles() -> None:
    validation = validate_formula_latex(
        r"\partial fs=k2 @(kt)",
        source_text=r"\partial fs=k2 @(kt)",
        display_mode="display",
    )

    assert validation.accepted is False
    assert validation.status == "corrupt_text_layer"
    assert validation.fallback_reason == "formula_asset_image"
    assert "formula_corrupt_text_rejected" in validation.quality_flags


def test_inline_validation_keeps_corrupt_text_layer_low_confidence() -> None:
    validation = validate_formula_latex(
        r"\partial fs=k2 @(kt)",
        source_text=r"\partial fs=k2 @(kt)",
        display_mode="inline",
    )

    assert validation.accepted is True
    assert validation.status == "accepted"
    assert validation.fallback_reason is None
    assert "formula_low_confidence" in validation.quality_flags


def test_display_validation_accepts_clean_structured_visual_result_from_corrupt_source() -> None:
    validation = validate_formula_latex(
        r"\frac{\partial f_s}{\partial t} + \frac{f'_s}{k^2}",
        source_text="@fs=@t þ f 0 s=k2 (4)",
        display_mode="display",
    )

    assert validation.accepted is True
    assert validation.status == "accepted"
    assert validation.fallback_reason is None
    assert "formula_text_layer_corrupt" in validation.quality_flags


def test_formula_service_attaches_image_fallback_when_corrupt_display_ocr_fails(tmp_path) -> None:
    (tmp_path / "formula_asset.png").write_bytes(b"fake-image")
    block = DocumentBlock(
        block_id="b_corrupt_display",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
        reading_order=0,
        source_text=r"\partial fs=k2 @(kt)",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[block],
                assets=[
                    Asset(
                        asset_id="formula_asset",
                        page_id="p1",
                        kind="formula",
                        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
                        path="/api/documents/doc_1/assets/formula_asset.png",
                    )
                ],
            )
        ],
    )

    class EmptyVisualProvider:
        name = "pix2text"

        async def recognize_formula(self, candidate, *, image_path=None):
            return OCRRecognitionResult(
                text="",
                latex="",
                region_kind="formula",
                provider="pix2text",
                confidence=0.0,
                quality_flags=["pix2text_formula_ocr_empty"],
            )

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
            ocr_service=OCRService(providers=[EmptyVisualProvider(), DeterministicOCRProvider()]),
        )
    )

    assert len(result.formulas) == 1
    assert result.formulas[0].asset_id == "formula_asset"
    assert result.formulas[0].latex
    assert "formula_corrupt_text_rejected" in result.formulas[0].quality_flags
    assert result.document.pages[0].blocks[0].source_text == (
        f"{{{{formula:{result.formulas[0].formula_id}}}}}"
    )
    assert result.diagnostics["rejected_count"] == 0
    assert result.diagnostics["records"][0]["fallback_reason"] == "formula_asset_image"


def test_formula_service_rejects_prose_display_formula_without_rewriting_block(tmp_path) -> None:
    block = DocumentBlock(
        block_id="b_formula_like_prose",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=90),
        reading_order=0,
        source_text=(
            "discharge scale. In conclusion, when pd remains constant and B=p increases, "
            "the breathing oscillations are intensified."
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

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            ocr_service=OCRService(providers=[DeterministicOCRProvider()]),
        )
    )

    assert result.formulas == []
    assert result.document.pages[0].blocks[0].source_text == block.source_text
    assert result.document.pages[0].blocks[0].formula_id is None
    assert result.diagnostics["rejected_count"] == 0


def test_formula_service_keeps_deterministic_display_fallback_when_visual_ocr_is_prose(
    tmp_path,
) -> None:
    block = DocumentBlock(
        block_id="b_display",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=90),
        reading_order=0,
        source_text=r"\partial f_s / \partial t = \sum_n (4)",
    )
    (tmp_path / "formula_asset.png").write_bytes(b"fake-image")
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[block],
                assets=[
                    Asset(
                        asset_id="formula_asset",
                        page_id="p1",
                        kind="formula",
                        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=90),
                        path="/api/documents/doc_1/assets/formula_asset.png",
                    )
                ],
            )
        ],
    )

    class ProseOCRProvider:
        name = "prose"

        async def recognize_formula(self, candidate, *, image_path=None):
            return OCRRecognitionResult(
                text="In conclusion, this paragraph is not a mathematical formula.",
                latex="In conclusion, this paragraph is not a mathematical formula.",
                region_kind="formula",
                provider="prose",
                confidence=0.98,
            )

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            ocr_service=OCRService(providers=[ProseOCRProvider()]),
        )
    )

    assert len(result.formulas) == 1
    assert result.formulas[0].latex
    assert result.formulas[0].ocr_provider == "text_layer_normalizer"
    assert result.document.pages[0].blocks[0].source_text.startswith("{{formula:")
    assert result.diagnostics["rejected_count"] == 0


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
    assert (
        "into the electron continuity equation"
        in normalized.pages[0].blocks[0].text_for_translation
    )


def test_legacy_formula_normalization_extracts_mid_equation_number_with_short_tail() -> None:
    block = DocumentBlock(
        block_id="b_mid_number",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=95),
        reading_order=0,
        source_text=r"\int f_s d \Omega , (3) v_{n}",
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

    formula = normalized.formulas[0]
    assert r"\tag{3}" in formula.latex
    assert "(3)" not in formula.latex.replace(r"\tag{3}", "")
    assert "v_{n}" in formula.latex
    assert "formula_equation_number_preserved" in formula.quality_flags


def test_legacy_formula_normalization_extracts_mid_equation_number_with_prime_tail() -> None:
    block = DocumentBlock(
        block_id="b_mid_prime_number",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=95),
        reading_order=0,
        source_text=r"\int f_s d \Omega : (4) v'_{n}",
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

    formula = normalized.formulas[0]
    assert r"\tag{4}" in formula.latex
    assert "(4)" not in formula.latex.replace(r"\tag{4}", "")
    assert "v'_{n}" in formula.latex
    assert "formula_equation_number_preserved" in formula.quality_flags


def test_legacy_formula_normalization_recognizes_tex_command_display_formula() -> None:
    block = DocumentBlock(
        block_id="b_tex_display",
        page_id="p1",
        role=BlockRole.HEADING,
        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=95),
        reading_order=0,
        source_text=r"\frac{\alpha}{\beta + 1} = q_s , (4)",
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

    normalized_block = normalized.pages[0].blocks[0]
    formula = normalized.formulas[0]

    assert normalized_block.text_for_translation == f"{{{{formula:{formula.formula_id}}}}}"
    assert formula.display_mode == "display"
    assert formula.latex == r"\frac{\alpha}{\beta + 1} = q_s \tag{4}"
    assert "formula_equation_number_preserved" in formula.quality_flags


def test_legacy_formula_normalization_truncates_dehyphenated_prose_boundary() -> None:
    block = DocumentBlock(
        block_id="b_represents_boundary",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=100),
        reading_order=0,
        source_text=(
            "s = (e, i) rep- resents either electrons or ions, q_{s} "
            "and m_{s} respectively."
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

    normalized_block = normalized.pages[0].blocks[0]
    assert len(normalized_block.formulas) == 1
    assert normalized_block.formulas[0].source_text == "s = (e, i)"
    assert "rep- resents either electrons" in normalized_block.text_for_translation
    assert "q_}" not in normalized_block.text_for_translation


def test_span_inline_detection_absorbs_trailing_subscript_span() -> None:
    spans = [
        TextSpanIR(
            span_id="s_formula",
            page_id="p1",
            block_id="b_inline",
            line_id="l1",
            text=r"\alpha [w] = \alpha [E_",
            bbox=BoundingBox(x0=20, y0=60, x1=128, y1=72),
            font_name="Cambria Math",
            font_size=10,
        ),
        TextSpanIR(
            span_id="s_sub",
            page_id="p1",
            block_id="b_inline",
            line_id="l1",
            text="{e}]",
            bbox=BoundingBox(x0=128, y0=62, x1=146, y1=72),
            font_name="Times New Roman",
            font_size=7,
        ),
        TextSpanIR(
            span_id="s_text",
            page_id="p1",
            block_id="b_inline",
            line_id="l1",
            text=" holds.",
            bbox=BoundingBox(x0=146, y0=60, x1=180, y1=72),
            font_name="Times New Roman",
            font_size=10,
        ),
    ]
    block = DocumentBlock(
        block_id="b_inline",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=220, y1=80),
        reading_order=0,
        source_text=r"\alpha [w] = \alpha [E_{e}] holds.",
        lines=[
            TextLineIR(
                line_id="l1",
                page_id="p1",
                block_id="b_inline",
                text=r"\alpha [w] = \alpha [E_{e}] holds.",
                bbox=BoundingBox(x0=20, y0=60, x1=180, y1=72),
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
    assert candidates[0].source_text == r"\alpha [w] = \alpha [E_{e}]"
    assert candidates[0].span_ids == ("s_formula", "s_sub")


def test_detector_keeps_inline_formula_script_group_together_for_regex_candidates() -> None:
    block = DocumentBlock(
        block_id="b_inline_script",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=220, y1=90),
        reading_order=0,
        source_text=r"Measure d^{3}v and continue.",
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
    assert candidates[0].source_text == r"d^{3}v"
    assert candidates[0].source_text_range == (8, 14)


def test_span_inline_detection_absorbs_trailing_superscript_span() -> None:
    spans = [
        TextSpanIR(
            span_id="s_formula",
            page_id="p1",
            block_id="b_inline_sup",
            line_id="l1",
            text="d^",
            bbox=BoundingBox(x0=20, y0=60, x1=34, y1=72),
            font_name="Cambria Math",
            font_size=10,
        ),
        TextSpanIR(
            span_id="s_sup",
            page_id="p1",
            block_id="b_inline_sup",
            line_id="l1",
            text="{3}v",
            bbox=BoundingBox(x0=34, y0=58, x1=52, y1=72),
            font_name="Times New Roman",
            font_size=7,
        ),
        TextSpanIR(
            span_id="s_text",
            page_id="p1",
            block_id="b_inline_sup",
            line_id="l1",
            text=" remains.",
            bbox=BoundingBox(x0=52, y0=60, x1=102, y1=72),
            font_name="Times New Roman",
            font_size=10,
        ),
    ]
    block = DocumentBlock(
        block_id="b_inline_sup",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=220, y1=80),
        reading_order=0,
        source_text=r"d^{3}v remains.",
        lines=[
            TextLineIR(
                line_id="l1",
                page_id="p1",
                block_id="b_inline_sup",
                text=r"d^{3}v remains.",
                bbox=BoundingBox(x0=20, y0=60, x1=102, y1=72),
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
    assert candidates[0].source_text == r"d^{3}v"
    assert candidates[0].span_ids == ("s_formula", "s_sup")


def test_latex_normalization_trims_unrepairable_trailing_script_marker() -> None:
    latex, flags = latex_from_pdf_text("E_")

    assert latex == "E"
    assert "E_]" not in latex
    assert "formula_dangling_script_trimmed" in flags


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
