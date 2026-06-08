from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from pdf_translator_schema import Asset, BlockRole, BoundingBox, DocumentIR
from pdf_translator_schema.models import DocumentBlock, FormulaDisplayMode, FormulaSourceKind, TextSpanIR


_FORMULA_REF_PATTERN = re.compile(r"^\{\{formula:[A-Za-z0-9_.:-]+\}\}$")
_MATH_SIGNAL_PATTERN = re.compile(
    r"(?:[=≤≥∑∫√∞≈≠]|\\(?:frac|sum|int|sqrt|alpha|beta|gamma|theta|lambda|mu|sigma)|"
    r"\b[A-Za-zα-ωΑ-Ω]\s*[+\-*/^_]\s*[A-Za-z0-9α-ωΑ-Ω])"
)


@dataclass(frozen=True)
class FormulaCandidate:
    candidate_id: str
    page_id: str
    bbox: BoundingBox
    source_kind: FormulaSourceKind
    source_block_id: str | None = None
    anchor_block_id: str | None = None
    asset_id: str | None = None
    source_text: str = ""
    source_text_range: tuple[int, int] | None = None
    span_ids: tuple[str, ...] = ()
    display_mode: FormulaDisplayMode = "display"
    image_path: str | None = None
    quality_flags: tuple[str, ...] = ()


def detect_formula_candidates(document: DocumentIR) -> list[FormulaCandidate]:
    candidates: list[FormulaCandidate] = []
    seen_keys: set[tuple[str | None, str | None]] = set()

    for page in document.pages:
        assets_by_id = {asset.asset_id: asset for asset in page.assets}
        for block in page.blocks:
            if block.formula_id or _FORMULA_REF_PATTERN.fullmatch(block.source_text.strip()):
                continue
            if block.role == BlockRole.FORMULA or _looks_like_formula_text(block.source_text):
                key = (block.block_id, None)
                if key not in seen_keys:
                    seen_keys.add(key)
                    candidates.append(
                        FormulaCandidate(
                            candidate_id=_candidate_id(
                                page.page_id,
                                "block",
                                block.block_id,
                                block.bbox,
                            ),
                            page_id=page.page_id,
                            bbox=block.bbox,
                            source_kind=FormulaSourceKind.TEXT_LAYER,
                            source_block_id=block.block_id,
                            source_text=block.source_text,
                            source_text_range=(0, len(block.source_text)),
                            span_ids=tuple(block.span_refs),
                            display_mode="display",
                        )
                    )
                continue

            for inline_candidate in _detect_inline_formula_candidates(block):
                key = (inline_candidate.anchor_block_id, inline_candidate.candidate_id)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                candidates.append(inline_candidate)

        for asset in page.assets:
            if asset.formula_id:
                continue
            source_kind = _asset_formula_source_kind(asset, page_height=page.size.height)
            if source_kind is None:
                continue
            key = (None, asset.asset_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(
                FormulaCandidate(
                    candidate_id=_candidate_id(
                        page.page_id,
                        "asset",
                        asset.asset_id,
                        asset.bbox,
                    ),
                    page_id=page.page_id,
                    bbox=asset.bbox,
                    source_kind=source_kind,
                    asset_id=asset.asset_id,
                    display_mode="display",
                    image_path=asset.path,
                    quality_flags=tuple(_asset_candidate_flags(asset, source_kind)),
                )
            )

        matched_asset_ids: set[str] = set()
        for candidate in _match_assets_to_formula_blocks(
            page.page_id,
            candidates,
            assets_by_id,
        ):
            if candidate.asset_id:
                matched_asset_ids.add(candidate.asset_id)
            yield_candidate = candidate
            candidate_index = next(
                (
                    index
                    for index, existing in enumerate(candidates)
                    if existing.candidate_id == yield_candidate.candidate_id
                ),
                None,
            )
            if candidate_index is not None:
                candidates[candidate_index] = yield_candidate
        if matched_asset_ids:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.source_kind == FormulaSourceKind.TEXT_LAYER
                or candidate.asset_id not in matched_asset_ids
            ]

    return candidates


def _looks_like_formula_text(text: str) -> bool:
    stripped = re.sub(r"\s+", " ", text.strip())
    if len(stripped) < 3 or len(stripped) > 260:
        return False
    if not _MATH_SIGNAL_PATTERN.search(stripped):
        return False
    alpha_words = re.findall(r"[A-Za-z]{3,}", stripped)
    if any(word.lower() in {"where", "holds", "with", "when", "and", "for"} for word in alpha_words):
        return False
    return len(alpha_words) <= 12


def _asset_formula_source_kind(asset: Asset, *, page_height: float | None = None) -> FormulaSourceKind | None:
    if asset.kind == "formula":
        if asset.path:
            return FormulaSourceKind.IMAGE_CANDIDATE
        return None
    if asset.kind == "image" and asset.path and _asset_aspect_looks_formula_like(
        asset,
        page_height=page_height,
    ):
        return FormulaSourceKind.IMAGE_CANDIDATE
    if asset.kind == "figure" and asset.path and _asset_aspect_looks_formula_like(
        asset,
        page_height=page_height,
    ):
        return FormulaSourceKind.VECTOR_CANDIDATE
    return None


def _asset_aspect_looks_formula_like(asset: Asset, *, page_height: float | None = None) -> bool:
    width = asset.bbox.x1 - asset.bbox.x0
    height = asset.bbox.y1 - asset.bbox.y0
    if width <= 0 or height <= 0:
        return False
    area = width * height
    aspect = width / height
    if area < 180 or area > 80_000:
        return False
    if not (1.4 <= aspect <= 16 and height <= 110):
        return False
    if page_height is not None and page_height > 0:
        top_band = page_height * 0.12
        bottom_band = page_height * 0.08
        if asset.bbox.y0 <= top_band and (height <= 70 or aspect >= 4.0):
            return False
        if asset.bbox.y1 >= page_height - bottom_band and height <= 70:
            return False
    alt_text = (asset.alt_text or "").lower()
    if any(
        marker in alt_text
        for marker in (
            "banner",
            "header",
            "logo",
            "decorative",
            "placeholder",
        )
    ):
        return False
    return True


def _asset_candidate_flags(asset: Asset, source_kind: FormulaSourceKind) -> list[str]:
    flags: list[str] = []
    if source_kind == FormulaSourceKind.VECTOR_CANDIDATE:
        flags.append("formula_vector_candidate")
    if source_kind == FormulaSourceKind.IMAGE_CANDIDATE:
        flags.append("formula_image_candidate")
    if not asset.path:
        flags.append("formula_candidate_without_image")
    return flags


def _match_assets_to_formula_blocks(
    page_id: str,
    candidates: list[FormulaCandidate],
    assets_by_id: dict[str, Asset],
) -> list[FormulaCandidate]:
    updates: list[FormulaCandidate] = []
    text_candidates = [
        candidate
        for candidate in candidates
        if candidate.page_id == page_id and candidate.source_kind == FormulaSourceKind.TEXT_LAYER
    ]
    asset_candidates = [
        candidate
        for candidate in candidates
        if candidate.page_id == page_id and candidate.asset_id and candidate.asset_id in assets_by_id
    ]
    for text_candidate in text_candidates:
        best: FormulaCandidate | None = None
        best_overlap = 0.0
        for asset_candidate in asset_candidates:
            overlap = _bbox_overlap_ratio(text_candidate.bbox, asset_candidate.bbox)
            if overlap > best_overlap:
                best = asset_candidate
                best_overlap = overlap
        if best is None or best_overlap < 0.6:
            continue
        updates.append(
            FormulaCandidate(
                candidate_id=text_candidate.candidate_id,
                page_id=text_candidate.page_id,
                bbox=text_candidate.bbox,
                source_kind=text_candidate.source_kind,
                source_block_id=text_candidate.source_block_id,
                anchor_block_id=text_candidate.anchor_block_id,
                asset_id=best.asset_id,
                source_text=text_candidate.source_text,
                source_text_range=text_candidate.source_text_range,
                span_ids=text_candidate.span_ids,
                display_mode=text_candidate.display_mode,
                image_path=best.image_path,
                quality_flags=text_candidate.quality_flags,
            )
        )
    return updates


def _detect_inline_formula_candidates(block: DocumentBlock) -> list[FormulaCandidate]:
    if block.role not in {BlockRole.PARAGRAPH, BlockRole.ABSTRACT, BlockRole.FOOTNOTE}:
        return []
    if not block.spans:
        return _regex_inline_formula_candidates(block)

    candidates: list[FormulaCandidate] = []
    source_text_cursor = 0
    for line in block.lines:
        line_spans = [span for span in block.spans if span.span_id in set(line.span_ids)]
        runs: list[list[TextSpanIR]] = []
        current: list[TextSpanIR] = []
        for span in line_spans:
            if _span_looks_math(span):
                current.append(span)
            else:
                if _span_run_looks_formula(current):
                    runs.append(current)
                current = []
        if _span_run_looks_formula(current):
            runs.append(current)

        line_offset = block.source_text.find(line.text, source_text_cursor)
        if line_offset < 0:
            line_offset = block.source_text.find(line.text)
        if line_offset < 0:
            line_offset = source_text_cursor
        for run in runs:
            text = "".join(span.text for span in run).strip()
            if not _looks_like_inline_formula_text(text):
                continue
            run_offset = block.source_text.find(text, line_offset)
            if run_offset < 0:
                run_offset = block.source_text.find(text)
            if run_offset < 0:
                continue
            bbox = _bbox_union([span.bbox for span in run])
            candidate = FormulaCandidate(
                candidate_id=_candidate_id(
                    block.page_id,
                    "inline",
                    f"{block.block_id}:{run_offset}:{text}",
                    bbox,
                ),
                page_id=block.page_id,
                bbox=bbox,
                source_kind=FormulaSourceKind.INLINE_TEXT,
                anchor_block_id=block.block_id,
                source_text=text,
                source_text_range=(run_offset, run_offset + len(text)),
                span_ids=tuple(span.span_id for span in run),
                display_mode="inline",
                quality_flags=("formula_inline_candidate",),
            )
            candidates.append(candidate)
        source_text_cursor = max(source_text_cursor, line_offset + len(line.text))
    if candidates:
        return _dedupe_inline_candidates(candidates)
    return _regex_inline_formula_candidates(block)


def _regex_inline_formula_candidates(block: DocumentBlock) -> list[FormulaCandidate]:
    candidates: list[FormulaCandidate] = []
    pattern = re.compile(
        r"(?<![A-Za-z])(?:[A-Za-zα-ωΑ-Ω][A-Za-z0-9α-ωΑ-Ω]*\s*(?:=|≈|≤|≥|≠)\s*"
        r"[A-Za-z0-9α-ωΑ-Ω+\-*/^_().]+|"
        r"[A-Za-zα-ωΑ-Ω]\s*(?:\^|_)\s*\{?[A-Za-z0-9+\-]+\}?)(?![A-Za-z])"
    )
    for match in pattern.finditer(block.source_text):
        text = match.group(0).strip()
        if not _looks_like_inline_formula_text(text):
            continue
        candidates.append(
            FormulaCandidate(
                candidate_id=_candidate_id(
                    block.page_id,
                    "inline_text",
                    f"{block.block_id}:{match.start()}:{text}",
                    block.bbox,
                ),
                page_id=block.page_id,
                bbox=block.bbox,
                source_kind=FormulaSourceKind.INLINE_TEXT,
                anchor_block_id=block.block_id,
                source_text=text,
                source_text_range=(match.start(), match.end()),
                display_mode="inline",
                quality_flags=("formula_inline_text_only",),
            )
        )
    return _dedupe_inline_candidates(candidates)


def _span_looks_math(span: TextSpanIR) -> bool:
    text = span.text.strip()
    if not text:
        return False
    font = (span.font_name or "").lower()
    if any(marker in font for marker in ("math", "symbol", "stix", "cmr", "cmsy", "cmmi")):
        return True
    if _MATH_SIGNAL_PATTERN.search(text):
        return True
    return False


def _span_run_looks_formula(spans: list[TextSpanIR]) -> bool:
    if not spans:
        return False
    text = "".join(span.text for span in spans).strip()
    return _looks_like_inline_formula_text(text)


def _looks_like_inline_formula_text(text: str) -> bool:
    stripped = re.sub(r"\s+", " ", text.strip())
    if len(stripped) < 2 or len(stripped) > 120:
        return False
    if re.fullmatch(r"[A-Za-z]{2,}", stripped):
        return False
    if _MATH_SIGNAL_PATTERN.search(stripped):
        return True
    return bool(re.search(r"[A-Za-zα-ωΑ-Ω][\^_][A-Za-z0-9{]", stripped))


def _dedupe_inline_candidates(candidates: list[FormulaCandidate]) -> list[FormulaCandidate]:
    result: list[FormulaCandidate] = []
    seen_ranges: set[tuple[str | None, tuple[int, int] | None]] = set()
    for candidate in candidates:
        key = (candidate.anchor_block_id, candidate.source_text_range)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        result.append(candidate)
    return result


def _bbox_union(boxes: list[BoundingBox]) -> BoundingBox:
    return BoundingBox(
        x0=min(box.x0 for box in boxes),
        y0=min(box.y0 for box in boxes),
        x1=max(box.x1 for box in boxes),
        y1=max(box.y1 for box in boxes),
    )


def _bbox_overlap_ratio(a: BoundingBox, b: BoundingBox) -> float:
    x_overlap = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    y_overlap = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    intersection = x_overlap * y_overlap
    a_area = max(0.0, (a.x1 - a.x0) * (a.y1 - a.y0))
    b_area = max(0.0, (b.x1 - b.x0) * (b.y1 - b.y0))
    denominator = min(a_area, b_area)
    return intersection / denominator if denominator else 0.0


def _candidate_id(
    page_id: str,
    source: str,
    source_id: str,
    bbox: BoundingBox,
) -> str:
    normalized_bbox = ",".join(
        f"{coordinate:.1f}" for coordinate in (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
    )
    digest = hashlib.sha1(
        f"{page_id}|{source}|{source_id}|{normalized_bbox}".encode()
    ).hexdigest()
    return f"{page_id}_formula_{digest[:12]}"
