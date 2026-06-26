from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Iterable

from pdf_translator_schema import BoundingBox, DocumentIR
from pdf_translator_schema.models import PdfFormula, PdfFormulaPrimitive, TextSpanIR

from .detector import FormulaCandidate


def build_pdf_formula_for_candidate(
    document: DocumentIR,
    candidate: FormulaCandidate,
    *,
    pdf_path: Path | None = None,
) -> PdfFormula | None:
    """Capture source PDF formula drawing/text primitives in formula-local coordinates."""
    width = candidate.bbox.x1 - candidate.bbox.x0
    height = candidate.bbox.y1 - candidate.bbox.y0
    if width <= 0 or height <= 0:
        return None

    page_index = _page_index_for_candidate(document, candidate)
    primitives: list[PdfFormulaPrimitive] = []
    quality_flags: list[str] = []
    if pdf_path is not None:
        primitives, quality_flags = _pdfminer_primitives(
            pdf_path,
            candidate,
            page_index=page_index,
        )
    if not primitives:
        primitives, quality_flags = _document_span_primitives(document, candidate)
    if not primitives and candidate.source_text.strip():
        primitives = [
            PdfFormulaPrimitive(
                primitive_id="g0",
                kind="glyph",
                text=candidate.source_text.strip(),
                font_name=None,
                font_size_pt=max(1.0, min(14.0, height * 0.72)),
                bbox=BoundingBox(
                    x0=0,
                    y0=0,
                    x1=max(1.0, width),
                    y1=max(1.0, height),
                ),
                origin=(0.0, max(1.0, height * 0.78)),
                quality_flags=["formula_text_primitive_fallback"],
            )
        ]
        quality_flags = ["formula_text_primitive_fallback"]
    if not primitives:
        return None

    return PdfFormula(
        source_page_id=candidate.page_id,
        source_page_index=page_index,
        source_bbox=candidate.bbox,
        width_pt=width,
        height_pt=height,
        baseline_offset_pt=height * (0.78 if candidate.display_mode == "inline" else 0.5),
        primitives=primitives,
        quality_flags=_unique(
            [
                "formula_source_form_clip",
                *quality_flags,
            ]
        ),
    )


def _pdfminer_primitives(
    pdf_path: Path,
    candidate: FormulaCandidate,
    *,
    page_index: int | None,
) -> tuple[list[PdfFormulaPrimitive], list[str]]:
    if page_index is None:
        return [], []
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTChar, LTContainer, LTLine, LTPage
    except Exception:
        return [], []

    try:
        pages: Iterable[object] = extract_pages(str(pdf_path), page_numbers=[page_index])
        layout_page = next(iter(pages), None)
    except Exception:
        return [], []
    if layout_page is None or not isinstance(layout_page, LTPage):
        return [], []

    primitives: list[PdfFormulaPrimitive] = []
    source_bbox = candidate.bbox
    for item in _walk_pdfminer_layout(layout_page, LTContainer):
        try:
            if isinstance(item, LTChar):
                bbox = _pdfminer_bbox_to_top_left(item.bbox, page_height=layout_page.height)
                if not _bbox_intersects_source(bbox, source_bbox):
                    continue
                local_bbox = _local_bbox(bbox, source_bbox)
                if local_bbox is None:
                    continue
                primitive_id = f"g{len(primitives)}"
                text = item.get_text()
                if not text:
                    continue
                origin = getattr(item, "matrix", None)
                local_origin = None
                if isinstance(origin, tuple) and len(origin) >= 6:
                    local_origin = (
                        float(origin[4]) - source_bbox.x0,
                        float(layout_page.height - origin[5]) - source_bbox.y0,
                    )
                primitives.append(
                    PdfFormulaPrimitive(
                        primitive_id=primitive_id,
                        kind="glyph",
                        text=text,
                        font_name=getattr(item, "fontname", None),
                        font_size_pt=max(1.0, float(getattr(item, "size", 0) or 1.0)),
                        bbox=local_bbox,
                        origin=local_origin,
                        color="#111111",
                        quality_flags=["formula_pdfminer_char"],
                    )
                )
            elif isinstance(item, LTLine):
                bbox = _pdfminer_bbox_to_top_left(item.bbox, page_height=layout_page.height)
                if not _bbox_intersects_source(bbox, source_bbox):
                    continue
                local_bbox = _local_bbox(bbox, source_bbox)
                points = [
                    (float(x) - source_bbox.x0, float(layout_page.height - y) - source_bbox.y0)
                    for x, y in getattr(item, "pts", [])
                ]
                if local_bbox is None and len(points) < 2:
                    continue
                primitives.append(
                    PdfFormulaPrimitive(
                        primitive_id=f"l{len(primitives)}",
                        kind="line",
                        bbox=local_bbox,
                        points=points[:2],
                        stroke_width_pt=max(0.1, float(getattr(item, "linewidth", 0.5) or 0.5)),
                        color="#111111",
                        quality_flags=["formula_pdfminer_line"],
                    )
                )
        except Exception:
            continue
    if not primitives:
        return [], []
    return primitives, ["formula_pdfminer_primitives"]


def _walk_pdfminer_layout(item: object, container_type: type) -> Iterable[object]:
    yield item
    if isinstance(item, container_type):
        for child in item:
            yield from _walk_pdfminer_layout(child, container_type)


def _document_span_primitives(
    document: DocumentIR,
    candidate: FormulaCandidate,
) -> tuple[list[PdfFormulaPrimitive], list[str]]:
    spans = _candidate_spans(document, candidate)
    primitives: list[PdfFormulaPrimitive] = []
    source_bbox = candidate.bbox
    for index, span in enumerate(spans):
        local_bbox = _local_bbox(span.bbox, source_bbox)
        if local_bbox is None:
            continue
        primitives.append(
            PdfFormulaPrimitive(
                primitive_id=f"g{index}",
                kind="glyph",
                text=span.text,
                font_name=span.font_name,
                font_size_pt=max(1.0, float(span.font_size or (span.bbox.y1 - span.bbox.y0))),
                bbox=local_bbox,
                origin=(
                    span.origin[0] - source_bbox.x0,
                    span.origin[1] - source_bbox.y0,
                )
                if span.origin is not None
                else None,
                color=span.color,
                quality_flags=["formula_pymupdf_span"],
            )
        )
    if primitives:
        return primitives, ["formula_pymupdf_span_primitives"]
    return [], []


def _candidate_spans(document: DocumentIR, candidate: FormulaCandidate) -> list[TextSpanIR]:
    wanted = set(candidate.span_ids)
    page = next((page for page in document.pages if page.page_id == candidate.page_id), None)
    if page is None:
        return []
    all_spans = list(itertools.chain.from_iterable(block.spans for block in page.blocks))
    if wanted:
        return [span for span in all_spans if span.span_id in wanted]
    source_text = candidate.source_text.strip()
    if not source_text:
        return []
    return [
        span
        for span in all_spans
        if _bbox_intersects_source(span.bbox, candidate.bbox)
        and (span.text.strip() in source_text or source_text in span.text.strip())
    ]


def _page_index_for_candidate(document: DocumentIR, candidate: FormulaCandidate) -> int | None:
    for index, page in enumerate(document.pages):
        if page.page_id == candidate.page_id:
            return index
    match = re.search(r"(\d+)$", candidate.page_id)
    if match is None:
        return None
    try:
        return max(0, int(match.group(1)) - 1)
    except ValueError:
        return None


def _pdfminer_bbox_to_top_left(
    bbox: tuple[float, float, float, float],
    *,
    page_height: float,
) -> BoundingBox:
    x0, y0, x1, y1 = bbox
    return BoundingBox(x0=float(x0), y0=float(page_height - y1), x1=float(x1), y1=float(page_height - y0))


def _bbox_intersects_source(bbox: BoundingBox, source: BoundingBox) -> bool:
    center_x = (bbox.x0 + bbox.x1) / 2
    center_y = (bbox.y0 + bbox.y1) / 2
    if source.x0 <= center_x <= source.x1 and source.y0 <= center_y <= source.y1:
        return True
    overlap_x = max(0.0, min(bbox.x1, source.x1) - max(bbox.x0, source.x0))
    overlap_y = max(0.0, min(bbox.y1, source.y1) - max(bbox.y0, source.y0))
    return overlap_x > 0 and overlap_y > 0


def _local_bbox(bbox: BoundingBox, source: BoundingBox) -> BoundingBox | None:
    width = source.x1 - source.x0
    height = source.y1 - source.y0
    x0 = max(0.0, bbox.x0 - source.x0)
    y0 = max(0.0, bbox.y0 - source.y0)
    x1 = min(width, bbox.x1 - source.x0)
    y1 = min(height, bbox.y1 - source.y0)
    if x1 <= x0 or y1 <= y0:
        return None
    return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)


def _unique(flags: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_flags: list[str] = []
    for flag in flags:
        if not flag or flag in seen:
            continue
        seen.add(flag)
        unique_flags.append(flag)
    return unique_flags
