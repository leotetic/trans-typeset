from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pdf_renderer.katex as katex_helper
import pytest
import app.pipeline.formulas.validation as formula_validation
from app.pipeline.formulas import (
    DeterministicFormulaRecognizer,
    FormulaCandidate,
    detect_formula_candidates,
    enrich_document_formulas,
)
from app.pipeline.formulas.normalization import formula_corruption_flags, latex_from_pdf_text
from app.pipeline.formulas.recognizer import FormulaRecognitionError, _extract_json_object
from app.pipeline.formulas.service import (
    _formula_attachment_rejection_reason,
    _should_escalate_text_candidate,
    _should_attach_image_fallback_formula,
)
from app.pipeline.formulas.validation import validate_formula_latex
from app.pipeline.ocr import (
    DeterministicOCRProvider,
    MiniMaxVisionOCRProvider,
    OCRService,
    Pix2TextOCRProvider,
)
from app.pipeline.parser import parse_pdf
from app.pipeline.workflow import normalized_input_payload, validate_translation_plan_formula_refs
from pdf_renderer import RenderDocument, render_to_html
from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    InlineItem,
    PageSize,
    SourceBlock,
    TranslationBlockPlan,
    TranslationChunk,
    TranslationLayoutPlan,
)
from pdf_translator_schema.models import (
    DocumentBlock,
    FormulaIR,
    FormulaRecognitionResult,
    FormulaSourceKind,
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


@pytest.mark.parametrize(
    ("text", "font_name"),
    [
        ("x", "CMMI10"),
        ("α", "Times New Roman"),
        ("(cid:123)", "Times New Roman"),
    ],
)
def test_detector_preserves_single_math_glyph_runs(text: str, font_name: str) -> None:
    source_text = f"where {text} holds."
    prefix_len = len("where ")
    span = TextSpanIR(
        span_id="s_formula",
        page_id="p1",
        block_id="b_inline",
        line_id="l1",
        text=text,
        bbox=BoundingBox(x0=52, y0=60, x1=72, y1=72),
        font_name=font_name,
        font_size=10,
    )
    block = DocumentBlock(
        block_id="b_inline",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=130, y1=80),
        reading_order=0,
        source_text=source_text,
        lines=[
            TextLineIR(
                line_id="l1",
                page_id="p1",
                block_id="b_inline",
                text=source_text,
                bbox=BoundingBox(x0=20, y0=60, x1=125, y1=72),
                span_ids=["s_formula"],
            )
        ],
        spans=[span],
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
    assert candidates[0].source_text == text
    assert candidates[0].source_text_range == (prefix_len, prefix_len + len(text))
    assert "formula_source_preserved" in candidates[0].quality_flags


@pytest.mark.parametrize("text", ["γ", "−γ", "23"])
def test_detector_rejects_standalone_inline_formula_fragments(text: str) -> None:
    span = TextSpanIR(
        span_id="s_fragment",
        page_id="p1",
        block_id="b_fragment",
        line_id="l1",
        text=text,
        bbox=BoundingBox(x0=52, y0=60, x1=72, y1=72),
        font_name="CMMI10",
        font_size=10,
    )
    block = DocumentBlock(
        block_id="b_fragment",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=52, y0=55, x1=72, y1=80),
        reading_order=0,
        source_text=text,
        lines=[
            TextLineIR(
                line_id="l1",
                page_id="p1",
                block_id="b_fragment",
                text=text,
                bbox=BoundingBox(x0=52, y0=60, x1=72, y1=72),
                span_ids=[span.span_id],
            )
        ],
        spans=[span],
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


@pytest.mark.parametrize("text", ["define", "another example,", "field", "of kinetic", "decreasing:"])
def test_detector_rejects_prose_only_math_font_runs(text: str) -> None:
    source_text = f"We use {text} here."
    start = source_text.index(text)
    span = TextSpanIR(
        span_id="s_prose_math_font",
        page_id="p1",
        block_id="b_inline",
        line_id="l1",
        text=text,
        bbox=BoundingBox(x0=60, y0=60, x1=140, y1=72),
        font_name="CMMI10",
        font_size=10,
    )
    block = DocumentBlock(
        block_id="b_inline",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=220, y1=80),
        reading_order=0,
        source_text=source_text,
        lines=[
            TextLineIR(
                line_id="l1",
                page_id="p1",
                block_id="b_inline",
                text=source_text,
                bbox=BoundingBox(x0=20, y0=60, x1=210, y1=72),
                span_ids=[span.span_id],
            )
        ],
        spans=[span],
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

    assert candidates == []
    assert source_text[start : start + len(text)] == text


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
    assert candidates[0].bbox.x0 > block.bbox.x0
    assert candidates[0].bbox.x1 < block.bbox.x1
    assert "formula_estimated_bbox" in candidates[0].quality_flags


def test_legacy_inline_formula_without_span_ids_gets_estimated_bbox() -> None:
    source_text = "given by eta(t,x)=u(t,x)- R/R x is"
    block = DocumentBlock(
        block_id="b_legacy_inline",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=260, y1=80),
        reading_order=0,
        source_text=source_text,
        lines=[
            TextLineIR(
                line_id="l1",
                page_id="p1",
                block_id="b_legacy_inline",
                text=source_text,
                bbox=BoundingBox(x0=20, y0=60, x1=250, y1=72),
                span_ids=[],
            )
        ],
    )
    formula = FormulaIR(
        formula_id="legacy_inline",
        page_id="p1",
        anchor_block_id="b_legacy_inline",
        latex="\\dot{R} Rx",
        source_text="R/R x",
        source_text_range=(27, 32),
        display_mode="inline",
        source_kind=FormulaSourceKind.INLINE_TEXT,
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
        formulas=[formula],
    )

    candidates = detect_formula_candidates(document)

    assert len(candidates) == 1
    assert candidates[0].bbox.x0 > block.bbox.x0
    assert candidates[0].bbox.x1 < block.bbox.x1
    assert "formula_estimated_bbox" in candidates[0].quality_flags


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


def test_legacy_formula_cluster_replaces_already_kept_weak_fragment() -> None:
    weak_fragment = DocumentBlock(
        block_id="weak_fragment",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=80, y0=72, x1=102, y1=82),
        reading_order=0,
        source_text="vn",
    )
    inline_formula = DocumentBlock(
        block_id="inline_formula",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=96, y0=74, x1=240, y1=96),
        reading_order=1,
        source_text="{{formula:Finline}}",
        text_for_translation="{{formula:Finline}}",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[weak_fragment, inline_formula],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="Finline",
                page_id="p1",
                anchor_block_id="inline_formula",
                latex="x = y + 1",
                source_text="x = y + 1",
                display_mode="inline",
                source_kind="inline_text",
                quality_flags=["latex_heuristic"],
            )
        ],
    )

    normalized = __import__(
        "app.pipeline.formula_processing",
        fromlist=["normalize_document_formulas"],
    ).normalize_document_formulas(document)

    block_ids = [block.block_id for block in normalized.pages[0].blocks]
    assert len(block_ids) == len(set(block_ids))
    assert block_ids == ["weak_fragment"]
    assert normalized.pages[0].blocks[0].source_text == "vn {{formula:Finline}}"


@pytest.mark.parametrize(
    "latex",
    [
        r"d \ge 2",
        r"h \ge 1",
        r"\le 0",
        r"2 dx \le C",
        r"\infty",
    ],
)
def test_formula_validator_accepts_short_latex_relation_commands(
    latex: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(formula_validation, "_katex_render_error", lambda _latex: None)

    result = validate_formula_latex(latex)

    assert result.accepted


def test_inline_formula_detector_rejects_prose_and_address_fragments() -> None:
    detector = __import__(
        "app.pipeline.formulas.detector",
        fromlist=["_looks_like_inline_formula_text"],
    )

    assert not detector._looks_like_inline_formula_text("D-80333")
    assert not detector._looks_like_inline_formula_text("x-variable")
    assert not detector._looks_like_inline_formula_text(r"\infty and")
    assert detector._looks_like_inline_formula_text("d≥3")


def test_inline_formula_detector_uses_text_boundaries_for_false_positive_guards() -> None:
    spans = [
        TextSpanIR(
            span_id="s_math",
            page_id="p1",
            block_id="b_inline",
            line_id="l1",
            text="∞",
            bbox=BoundingBox(x0=20, y0=60, x1=28, y1=72),
            font_name="Cambria Math",
            font_size=10,
        ),
        TextSpanIR(
            span_id="s_tail",
            page_id="p1",
            block_id="b_inline",
            line_id="l1",
            text="and the limit holds.",
            bbox=BoundingBox(x0=28, y0=60, x1=130, y1=72),
            font_name="Times New Roman",
            font_size=10,
        ),
    ]
    block = DocumentBlock(
        block_id="b_inline",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=55, x1=140, y1=80),
        reading_order=0,
        source_text="∞and the limit holds.",
        lines=[
            TextLineIR(
                line_id="l1",
                page_id="p1",
                block_id="b_inline",
                text="∞and the limit holds.",
                bbox=BoundingBox(x0=20, y0=60, x1=130, y1=72),
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

    assert detect_formula_candidates(document) == []


def test_pdf_formula_normalization_repairs_common_vlasov_poisson_glyphs() -> None:
    latex, flags = latex_from_pdf_text("△U = ερ, ρ(t,x)= Z IR_d f(t,x,v)dv (1.1)")

    assert r"\Delta" in latex
    assert r"\epsilon" in latex
    assert r"\rho" in latex
    assert r"\int_{\mathbb{R}^{d}}" in latex
    assert r"\tag{1.1}" in latex
    assert "formula_pdf_math_degradation_repaired" in flags
    assert "formula_equation_number_preserved" in flags


def test_pdf_formula_normalization_repairs_compact_partial_derivatives() -> None:
    latex, flags = latex_from_pdf_text("∂tf + ∂xU - ∂ξW")

    assert r"\partial_{t} f" in latex
    assert r"\partial_{x} U" in latex
    assert r"\partial_{\xi} W" in latex
    assert "formula_compact_partial_repaired" in flags


def test_formula_validation_handles_empty_stderr_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    katex_helper.clear_katex_cache()
    monkeypatch.setattr(
        katex_helper.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr=None),
    )

    assert formula_validation._katex_render_error(r"\int f_s\,d\Omega") == "katex_render_failed"


def test_formula_ligature_marks_text_layer_corrupt() -> None:
    flags = formula_corruption_flags("J \\cdot E |ﬄﬄ{zﬄﬄ}")

    assert "formula_text_layer_corrupt" in flags
    assert "formula_pdf_ligature_corrupt" in flags


def test_underbrace_text_layer_artifact_is_removed_from_latex() -> None:
    latex, flags = latex_from_pdf_text("J · E |{z} 1 = A |ﬄﬄ{zﬄﬄ} 3")

    assert "\ufb04" not in latex
    assert "ffl" not in latex
    assert "|{" not in latex
    assert "formula_pdf_ligature_repaired" in flags
    assert "formula_underbrace_artifact_repaired" in flags


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
            formula_recognition_mode="visual_ocr",
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
    diagnostics = service.diagnostics()
    assert diagnostics["record_count"] == 1
    assert diagnostics["active_provider_order"] == ["empty", "deterministic"]


def test_ocr_service_reports_visual_candidate_limit(tmp_path: Path) -> None:
    png_path = tmp_path / "formula.png"
    png_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00"
        b"\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    candidate = replace(
        detect_formula_candidates(_document())[1],
        image_path=str(png_path),
    )
    service = OCRService(
        providers=[DeterministicOCRProvider()],
        max_visual_candidates=0,
    )

    result = asyncio.run(service.recognize_formula(candidate, prefer_visual=True))
    diagnostics = service.diagnostics()

    assert result.provider == "deterministic"
    assert "formula_visual_ocr_skipped_by_cap" in result.quality_flags
    assert diagnostics["records"][0]["visual_status"] == "skipped_by_cap"
    assert diagnostics["records"][0]["fallback_status"] == (
        "deterministic_fallback_after_visual_skip"
    )
    assert diagnostics["visual_skipped_by_cap_count"] == 1
    assert diagnostics["deterministic_fallback_after_visual_failure_count"] == 1
    assert diagnostics["visual_skipped_count"] == 1
    assert diagnostics["visual_failure_count"] == 1
    assert diagnostics["max_visual_candidates"] == 0


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


def test_minimax_provider_sends_adaptive_thinking_payload_and_data_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "formula.png"
    image_path.write_bytes(b"fake-png")
    requests: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"latex":"\\\\frac{\\\\partial f_s}{\\\\partial t}",'
                                '"display_mode":"display","confidence":0.92,'
                                '"quality_flags":["minimax_vision_used"]}'
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, endpoint, *, headers, json):
            requests.append({"endpoint": endpoint, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("app.pipeline.ocr.providers.httpx.AsyncClient", FakeClient)
    provider = MiniMaxVisionOCRProvider(
        api_key="secret-key",
        endpoint="https://api.minimaxi.com/v1/chat/completions",
        model="MiniMax-M3",
    )
    candidate = detect_formula_candidates(_document())[1]

    result = asyncio.run(provider.recognize_formula(candidate, image_path=image_path))

    assert result.provider == "minimax_vision"
    assert result.latex == r"\frac{\partial f_s}{\partial t}"
    assert result.confidence == pytest.approx(0.92)
    assert "minimax_vision_used" in result.quality_flags
    request = requests[0]
    assert request["headers"]["Authorization"] == "Bearer secret-key"
    body = request["json"]
    assert body["model"] == "MiniMax-M3"
    assert body["thinking"] == {"type": "adaptive"}
    content = body["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_minimax_provider_rejects_coordinate_bearing_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "formula.png"
    image_path.write_bytes(b"fake-png")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"latex":"x=y","display_mode":"display",'
                                '"confidence":0.9,"quality_flags":[],"bbox":[0,0,1,1]}'
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, endpoint, *, headers, json):
            return FakeResponse()

    monkeypatch.setattr("app.pipeline.ocr.providers.httpx.AsyncClient", FakeClient)
    provider = MiniMaxVisionOCRProvider(api_key="secret-key")
    candidate = detect_formula_candidates(_document())[1]

    with pytest.raises(FormulaRecognitionError):
        asyncio.run(provider.recognize_formula(candidate, image_path=image_path))


def test_ocr_service_falls_back_when_minimax_provider_fails(tmp_path: Path) -> None:
    image_path = tmp_path / "formula_asset.png"
    image_path.write_bytes(b"fake-image")
    candidate = detect_formula_candidates(_document())[1]
    candidate = replace(candidate, image_path=str(image_path))

    class FailingMiniMaxProvider:
        name = "minimax_vision"

        async def recognize_formula(self, candidate, *, image_path=None):
            raise RuntimeError("minimax unavailable")

    service = OCRService(
        providers=[FailingMiniMaxProvider(), DeterministicOCRProvider()],
        asset_base_path=tmp_path,
    )

    result = asyncio.run(service.recognize_formula(candidate, prefer_visual=True))

    assert result.provider == "deterministic"
    diagnostics = service.diagnostics()
    assert diagnostics["active_provider_order"] == ["minimax_vision", "deterministic"]
    assert diagnostics["records"][0]["attempts"][0]["provider"] == "minimax_vision"
    assert diagnostics["records"][0]["attempts"][0]["status"] == "failed"


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
    text_formula = next(formula for formula in result.formulas if formula.latex == "E = mc^2")
    assert result.document.formulas_by_id()[text_formula.formula_id].latex == "E = mc^2"
    assert result.document.pages[0].blocks[0].source_text.startswith("{{formula:")
    assert result.document.pages[0].blocks[0].formula_id == text_formula.formula_id
    assert result.diagnostics["candidate_count"] == 2
    assert result.diagnostics["accepted_count"] == 2
    assert result.diagnostics["rejected_count"] == 0
    assert any("formula_source_preserved" in formula.quality_flags for formula in result.formulas)
    assert "formula_visual_mock_rejected" not in result.diagnostics["quality_flags"]
    assert "visual_formula_recognition_disabled" in result.diagnostics["quality_flags"]
    assert result.diagnostics["recognizer_type"] == "deterministic"


def test_formula_service_creates_source_assets_for_inline_and_display_formulas(tmp_path) -> None:
    import fitz

    pdf_path = tmp_path / "formulas.pdf"
    pdf = fitz.open()
    page = pdf.new_page(width=300, height=220)
    page.insert_text((20, 60), "where E = mc^2 holds.")
    page.insert_text((80, 130), "a^2 + b^2 = c^2 (1)")
    pdf.save(pdf_path)
    pdf.close()

    inline_spans = [
        TextSpanIR(
            span_id="s_text",
            page_id="p1",
            block_id="b_inline",
            line_id="l_inline",
            text="where ",
            bbox=BoundingBox(x0=20, y0=50, x1=52, y1=64),
            font_name="Times New Roman",
            font_size=10,
        ),
        TextSpanIR(
            span_id="s_formula",
            page_id="p1",
            block_id="b_inline",
            line_id="l_inline",
            text="E = mc^2",
            bbox=BoundingBox(x0=52, y0=50, x1=96, y1=64),
            font_name="Cambria Math",
            font_size=10,
        ),
        TextSpanIR(
            span_id="s_tail",
            page_id="p1",
            block_id="b_inline",
            line_id="l_inline",
            text=" holds.",
            bbox=BoundingBox(x0=96, y0=50, x1=132, y1=64),
            font_name="Times New Roman",
            font_size=10,
        ),
    ]
    inline_block = DocumentBlock(
        block_id="b_inline",
        page_id="p1",
        role=BlockRole.PARAGRAPH,
        bbox=BoundingBox(x0=20, y0=48, x1=140, y1=68),
        reading_order=0,
        source_text="where E = mc^2 holds.",
        lines=[
            TextLineIR(
                line_id="l_inline",
                page_id="p1",
                block_id="b_inline",
                text="where E = mc^2 holds.",
                bbox=BoundingBox(x0=20, y0=50, x1=132, y1=64),
                span_ids=[span.span_id for span in inline_spans],
            )
        ],
        spans=inline_spans,
    )
    display_block = DocumentBlock(
        block_id="b_display",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=78, y0=116, x1=220, y1=138),
        reading_order=1,
        source_text="a^2 + b^2 = c^2 (1)",
    )
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=220),
                blocks=[inline_block, display_block],
            )
        ],
    )

    class FailingOCRProvider:
        name = "pix2text"

        async def recognize_formula(self, candidate, *, image_path=None):
            raise AssertionError("default primitive replay must not invoke OCR")

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            pdf_path=pdf_path,
            recognizer=DeterministicFormulaRecognizer(),
            ocr_service=OCRService(providers=[FailingOCRProvider()]),
        )
    )

    assert len(result.formulas) == 2
    assert all(formula.asset_id for formula in result.formulas)
    assert all(formula.pdf_formula is not None for formula in result.formulas)
    assert all("formula_pdf_primitive_primary" in formula.quality_flags for formula in result.formulas)
    assert all("formula_source_preserved" in formula.quality_flags for formula in result.formulas)
    assert all("formula_source_asset_primary" in formula.quality_flags for formula in result.formulas)
    assert result.diagnostics["formula_recognition_mode"] == "pdf_primitive_replay"
    assert result.diagnostics["primitive_formula_count"] == 2
    assert result.diagnostics["performance"]["visual_attempt_count"] == 0
    assert result.ocr_records == []
    assert len([asset for asset in result.document.pages[0].assets if asset.kind == "formula"]) == 2
    assert len(list(tmp_path.glob("*.svg"))) == 2
    assert not list(tmp_path.glob("*.png"))
    assert all("formula_source_asset_svg" in formula.quality_flags for formula in result.formulas)
    assert all(
        asset.path and asset.path.endswith(".svg")
        for asset in result.document.pages[0].assets
        if asset.kind == "formula"
    )
    for svg_path in tmp_path.glob("*.svg"):
        svg = svg_path.read_text(encoding="utf-8")
        assert "<path" in svg
        assert "<text" not in svg


def test_formula_service_keeps_many_clean_text_formulas_on_fast_path(tmp_path) -> None:
    blocks = [
        DocumentBlock(
            block_id=f"b_formula_{index}",
            page_id="p1",
            role=BlockRole.PARAGRAPH,
            bbox=BoundingBox(x0=20, y0=20 + index * 2, x1=260, y1=30 + index * 2),
            reading_order=index,
            source_text=f"where x{index} = y{index} + 1 holds.",
        )
        for index in range(500)
    ]
    document = DocumentIR(
        doc_id="doc_many",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=1200),
                blocks=blocks,
            )
        ],
    )

    class FailingVisualProvider:
        name = "pix2text"

        async def recognize_formula(self, candidate, *, image_path=None):
            raise AssertionError("clean text-layer formulas should not use visual OCR")

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_many",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
            ocr_service=OCRService(
                providers=[FailingVisualProvider(), DeterministicOCRProvider()],
                max_visual_candidates=4,
            ),
            formula_recognition_concurrency=16,
            formula_visual_ocr_concurrency=2,
        )
    )

    assert len(result.formulas) == 500
    assert result.diagnostics["candidate_count"] == 500
    performance = result.diagnostics["performance"]
    assert performance["crop_count"] == 0
    assert performance["visual_attempt_count"] == 0
    assert not list(tmp_path.glob("*.png"))


def test_formula_service_parallelizes_slow_visual_candidates(tmp_path) -> None:
    assets = []
    for index in range(6):
        image_path = tmp_path / f"formula_{index}.png"
        image_path.write_bytes(f"fake-image-{index}".encode("ascii"))
        assets.append(
            Asset(
                asset_id=f"a_formula_{index}",
                page_id="p1",
                kind="image",
                bbox=BoundingBox(
                    x0=20,
                    y0=80 + index * 40,
                    x1=220,
                    y1=100 + index * 40,
                ),
                path=str(image_path),
            )
        )
    document = DocumentIR(
        doc_id="doc_parallel",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[],
                assets=assets,
            )
        ],
    )

    class SlowVisualProvider:
        name = "pix2text"

        async def recognize_formula(self, candidate, *, image_path=None):
            await asyncio.sleep(0.05)
            return OCRRecognitionResult(
                text="x = y + 1",
                latex="x = y + 1",
                region_kind="formula",
                provider="pix2text",
                confidence=0.95,
            )

    async def run_once(concurrency: int) -> tuple[float, object]:
        started = time.perf_counter()
        result = await enrich_document_formulas(
            document,
            doc_id=f"doc_parallel_{concurrency}",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
            ocr_service=OCRService(
                providers=[SlowVisualProvider(), DeterministicOCRProvider()],
                asset_base_path=tmp_path,
                max_visual_candidates=20,
            ),
            formula_recognition_concurrency=concurrency,
            formula_visual_ocr_concurrency=concurrency,
            formula_recognition_mode="visual_ocr",
        )
        return time.perf_counter() - started, result

    serial_elapsed, serial_result = asyncio.run(run_once(1))
    parallel_elapsed, parallel_result = asyncio.run(run_once(6))

    assert len(serial_result.formulas) == 6
    assert len(parallel_result.formulas) == 6
    assert parallel_elapsed < serial_elapsed * 0.75


def test_formula_service_reports_active_ocr_provider_order(tmp_path) -> None:
    class EmptyPix2TextProvider:
        name = "pix2text"

        async def recognize_formula(self, candidate, *, image_path=None):
            return OCRRecognitionResult(
                region_kind="formula",
                provider="pix2text",
                confidence=0,
                quality_flags=["empty_provider"],
            )

    result = asyncio.run(
        enrich_document_formulas(
            _document(),
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            ocr_service=OCRService(
                providers=[EmptyPix2TextProvider(), DeterministicOCRProvider()]
            ),
            formula_recognition_mode="visual_ocr",
        )
    )

    assert result.diagnostics["ocr"]["active_provider_order"] == [
        "pix2text",
        "deterministic",
    ]
    assert result.diagnostics["ocr_provider"]["active_provider_order"] == [
        "pix2text",
        "deterministic",
    ]
    assert result.diagnostics["ocr_provider"]["active_provider_order_includes_pix2text"] is True


def test_formula_service_accepts_minimax_latex_for_renderer(tmp_path) -> None:
    (tmp_path / "formula_asset.png").write_bytes(b"fake-image")
    block = DocumentBlock(
        block_id="b_minimax_display",
        page_id="p1",
        role=BlockRole.FORMULA,
        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
        reading_order=0,
        source_text=r"@fs=@t þ f 0 s=k2 (4)",
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

    class MiniMaxProvider:
        name = "minimax_vision"

        async def recognize_formula(self, candidate, *, image_path=None):
            return OCRRecognitionResult(
                text=r"\frac{\partial f_s}{\partial t} + \frac{f'_s}{k^2}",
                latex=r"\frac{\partial f_s}{\partial t} + \frac{f'_s}{k^2}",
                region_kind="formula",
                provider="minimax_vision",
                confidence=0.94,
                quality_flags=["minimax_vision_used"],
            )

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
            ocr_service=OCRService(providers=[MiniMaxProvider(), DeterministicOCRProvider()]),
            formula_recognition_mode="visual_ocr",
            visual_formula_recognition_enabled=True,
        )
    )

    assert len(result.formulas) == 1
    assert result.formulas[0].ocr_provider == "minimax_vision"
    assert r"\frac{\partial f_s}{\partial t}" in result.formulas[0].latex
    assert result.document.pages[0].blocks[0].source_text.startswith("{{formula:")
    html = render_to_html(RenderDocument.from_ir_and_plans(result.document, [], "zh-CN"))
    assert 'data-latex="\\frac{\\partial f_s}{\\partial t} + \\frac{f&#x27;_s}{k^2}"' in html
    assert "formula-ir" in html


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
            formula_recognition_mode="text_latex",
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


def test_formula_service_flags_retained_legacy_formula_after_promoted_cluster_rejection(
    tmp_path,
) -> None:
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
                        source_text="{{formula:Flegacy_bad}}",
                        text_for_translation="{{formula:Flegacy_bad}}",
                    )
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="Flegacy_bad",
                page_id="p1",
                source_block_id="b_cluster",
                latex="for (VP) or",
                source_text="for (VP) or",
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
            "formula_fragment_clusters": [
                {
                    "cluster_id": "cluster_bad",
                    "page_id": "p1",
                    "primary_block_id": "b_cluster",
                    "merged_block_ids": ["b_cluster"],
                    "formula_ids": ["Flegacy_bad"],
                    "display_mode": "display",
                    "combined_text": "{{formula:Flegacy_bad}}",
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
            formula_recognition_mode="text_latex",
        )
    )

    retained = result.document.formulas_by_id()["Flegacy_bad"]
    assert result.formulas == []
    assert "legacy_formula_retained_after_rejection" in retained.quality_flags
    assert result.diagnostics["legacy_retained_count"] == 1
    assert result.diagnostics["stale_formula_ref_count"] == 1
    assert result.diagnostics["unknown_formula_ref_count"] == 0
    assert "legacy_formula_retained_after_rejection" in result.diagnostics["quality_flags"]


def test_formula_diagnostics_do_not_mark_stable_existing_refs_stale(tmp_path: Path) -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b_formula",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
                        reading_order=0,
                        source_text="{{formula:f_existing}}",
                        text_for_translation="{{formula:f_existing}}",
                    )
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="f_existing",
                page_id="p1",
                source_block_id="b_formula",
                latex="E = mc^2",
                source_text="E = mc^2",
                display_mode="display",
                source_kind="text_layer",
            )
        ],
    )

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
        )
    )

    assert result.diagnostics["stale_formula_ref_count"] == 0
    assert result.diagnostics["legacy_retained_count"] == 0
    assert result.diagnostics["unknown_formula_ref_count"] == 0


def test_display_cluster_crop_asset_keeps_image_fallback_after_text_rejection() -> None:
    candidate = FormulaCandidate(
        candidate_id="p0005_display_cluster_deadbeef",
        page_id="p1",
        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
        source_kind=FormulaSourceKind.TEXT_LAYER,
        source_block_id="b_cluster",
        source_text="for (VP) or",
        display_mode="display",
        quality_flags=("formula_display_cluster",),
    )
    validator = validate_formula_latex(
        "for (VP) or",
        source_text=candidate.source_text,
        display_mode="display",
    )
    formula = FormulaIR(
        formula_id=candidate.candidate_id,
        page_id="p1",
        source_block_id="b_cluster",
        asset_id=candidate.candidate_id,
        latex="for (VP) or",
        source_text=candidate.source_text,
        display_mode="display",
        source_kind="text_layer",
        quality_flags=[
            "formula_display_cluster",
            *validator.quality_flags,
        ],
    )

    assert validator.status in {"prose_like", "not_math"}
    assert _should_attach_image_fallback_formula(
        candidate,
        validator,
        candidate_asset_id=candidate.candidate_id,
    )
    assert _formula_attachment_rejection_reason(candidate, formula, validator) is None


def test_translation_plan_formula_ref_diagnostics_rejects_unknown_refs_and_raw_tex() -> None:
    token = "{{formula:Fknown}}"
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=20, y0=55, x1=280, y1=90),
                        reading_order=0,
                        source_text=f"The relation {token} holds.",
                    )
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="Fknown",
                page_id="p1",
                anchor_block_id="b1",
                latex="d \\ge 3",
                source_text="d≥3",
                display_mode="inline",
                source_kind="inline_text",
            )
        ],
    )
    chunk = TranslationChunk(
        chunk_id="chunk_1",
        target_lang="zh-CN",
        source_blocks=[
            SourceBlock(
                block_id="b1",
                role=BlockRole.PARAGRAPH,
                source_text=f"The relation {token} holds.",
                preserve_tokens=[token],
            )
        ],
    )
    plan = TranslationLayoutPlan(
        chunk_id="chunk_1",
        target_lang="zh-CN",
        blocks=[
            TranslationBlockPlan(
                source_block_id="b1",
                translated_text=(
                    "关系 {{formula:Fknown}} 与 {{formula:Fmissing}} and $t\\ge 0$."
                ),
                inline_items=[
                    InlineItem(kind="formula", text=token, source_token=token),
                ],
                role=BlockRole.PARAGRAPH,
            )
        ],
    )

    diagnostics = validate_translation_plan_formula_refs(
        document=document,
        chunks=[chunk],
        plans=[plan],
    )

    assert diagnostics["status"] == "invalid"
    assert diagnostics["unknown_formula_ref_count"] == 1
    assert diagnostics["unknown_plan_formula_refs"][0]["formula_id"] == "Fmissing"
    assert diagnostics["raw_tex_detected_count"] == 1
    assert diagnostics["raw_tex_unrendered_count"] == 0
    assert "raw_tex_detected" in diagnostics["quality_flags"]


def test_normalized_input_reports_formula_asset_counts() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b_formula",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
                        reading_order=0,
                        source_text="{{formula:F1}}",
                        formula_id="F1",
                    )
                ],
                assets=[
                    Asset(
                        asset_id="a_formula",
                        page_id="p1",
                        kind="formula",
                        bbox=BoundingBox(x0=20, y0=80, x1=280, y1=120),
                        path="/api/documents/doc_1/assets/a_formula.png",
                        formula_id="F1",
                    )
                ],
            )
        ],
        formulas=[
            FormulaIR(
                formula_id="F1",
                page_id="p1",
                source_block_id="b_formula",
                asset_id="a_formula",
                latex="E = mc^2",
                source_text="E = mc^2",
                display_mode="display",
            )
        ],
    )

    payload = normalized_input_payload(input_sources=[], document=document)

    assert payload["document_asset_count"] == 1
    assert payload["formula_asset_count"] == 1
    assert payload["normalized_input_formula_asset_count"] == 1
    assert payload["document_formula_count"] == 1


def test_formula_service_keeps_clean_complex_display_formula_on_text_layer(tmp_path) -> None:
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

    class FailingVisualProvider:
        name = "pix2text"

        async def recognize_formula(self, candidate, *, image_path=None):
            raise AssertionError("clean display formula should stay on text-layer path")

    result = asyncio.run(
        enrich_document_formulas(
            document,
            doc_id="doc_1",
            asset_output_dir=tmp_path,
            recognizer=DeterministicFormulaRecognizer(),
            ocr_service=OCRService(providers=[FailingVisualProvider(), DeterministicOCRProvider()]),
            formula_recognition_mode="text_latex",
        )
    )

    assert len(result.formulas) == 1
    assert result.formulas[0].ocr_provider == "text_layer_normalizer"
    assert "formula_visual_escalated" not in result.diagnostics["records"][0]["quality_flags"]
    assert result.diagnostics["records"][0]["status"] == "recognized"


def test_formula_service_escalates_promoted_display_cluster_even_when_text_is_accepted() -> None:
    candidate = FormulaCandidate(
        candidate_id="p0016_formula_baacb8365adb",
        page_id="p0016",
        bbox=BoundingBox(x0=330, y0=577, x1=443, y1=609),
        source_kind=FormulaSourceKind.TEXT_LAYER,
        source_block_id="p0016_ba67e157c3022",
        source_text=r"|x|2 - 2 \dot{R} R| \eta |2F dxd \eta . \dot{R} R",
        display_mode="display",
        quality_flags=("formula_display_cluster", "legacy_formula_migrated"),
    )
    latex = r"|x|2 - 2 \dot{R} R| \eta |2F dxd \eta . \dot{R} R"
    validator = validate_formula_latex(
        latex,
        source_text=candidate.source_text,
        display_mode="display",
    )
    result = FormulaRecognitionResult(
        latex=latex,
        display_mode="display",
        confidence=0.92,
        accepted_provider="text_layer_normalizer",
        accepted_confidence=0.92,
        validator_status=validator.status,
        source_kind=FormulaSourceKind.TEXT_LAYER,
    )

    assert validator.accepted
    assert _should_escalate_text_candidate(candidate, result, validator)
    clean_candidate = replace(candidate, quality_flags=("formula_display_cluster",))
    assert not _should_escalate_text_candidate(clean_candidate, result, validator)


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
            formula_recognition_mode="visual_ocr",
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
            formula_recognition_mode="visual_ocr",
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
            formula_recognition_mode="text_latex",
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
            formula_recognition_mode="visual_ocr",
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
