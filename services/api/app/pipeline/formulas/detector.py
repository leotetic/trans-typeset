from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from pdf_translator_schema import Asset, BlockRole, BoundingBox, DocumentIR
from pdf_translator_schema.models import FormulaSourceKind


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
    asset_id: str | None = None
    source_text: str = ""
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
            if block.role != BlockRole.FORMULA and not _looks_like_formula_text(block.source_text):
                continue
            key = (block.block_id, None)
            if key in seen_keys:
                continue
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
                )
            )

        for asset in page.assets:
            if asset.formula_id:
                continue
            source_kind = _asset_formula_source_kind(asset)
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
    return len(alpha_words) <= 12


def _asset_formula_source_kind(asset: Asset) -> FormulaSourceKind | None:
    if asset.kind == "formula":
        if asset.path:
            return FormulaSourceKind.IMAGE_CANDIDATE
        return FormulaSourceKind.VECTOR_CANDIDATE
    if asset.kind == "image" and _asset_aspect_looks_formula_like(asset):
        return FormulaSourceKind.IMAGE_CANDIDATE
    if asset.kind == "figure" and _asset_aspect_looks_formula_like(asset):
        return FormulaSourceKind.VECTOR_CANDIDATE
    return None


def _asset_aspect_looks_formula_like(asset: Asset) -> bool:
    width = asset.bbox.x1 - asset.bbox.x0
    height = asset.bbox.y1 - asset.bbox.y0
    if width <= 0 or height <= 0:
        return False
    area = width * height
    aspect = width / height
    return area >= 80 and 1.4 <= aspect <= 18 and height <= 130


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
                asset_id=best.asset_id,
                source_text=text_candidate.source_text,
                image_path=best.image_path,
                quality_flags=text_candidate.quality_flags,
            )
        )
    return updates


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
