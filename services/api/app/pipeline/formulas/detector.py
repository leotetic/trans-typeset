from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from pdf_translator_schema import Asset, BlockRole, BoundingBox, DocumentIR
from pdf_translator_schema.models import (
    DocumentBlock,
    FormulaDisplayMode,
    FormulaSourceKind,
    TextSpanIR,
)

from .normalization import (
    alpha_word_tokens,
    contains_natural_language,
    is_noise_text,
    normalize_pdf_text,
)

_FORMULA_REF_PATTERN = re.compile(r"^\{\{formula:[A-Za-z0-9_.:-]+\}\}$")
_INLINE_FORMULA_REF_PATTERN = re.compile(r"\{\{formula:([A-Za-z0-9_.:-]+)\}\}")
_MATH_SIGNAL_PATTERN = re.compile(
    r"(?:[=≤≥∑∫√∞≈≠∂∇]|\\(?:partial|nabla|frac|sum|int|sqrt|alpha|beta|gamma|theta|lambda|mu|sigma)|"
    r"\b[A-Za-zα-ωΑ-Ω]\s*[+\-*/^_]\s*[A-Za-z0-9α-ωΑ-Ω])"
)
_EQUATION_NUMBER_SUFFIX = re.compile(r"(?:[,;:]\s*)?(\(\d+\))\s*$")
_EQUATION_NUMBER_WITH_SHORT_TAIL = re.compile(
    r"(?:[,;:]\s*)?(\(\d+\))(?P<tail>\s+[A-Za-z0-9α-ωΑ-Ω_{}^\\+\-*/.,\s]{1,24})$"
)
_FORMULA_CLUSTER_CACHE_ATTR = "_formula_fragment_cluster_diagnostics"
_DISPLAY_CLUSTER_MAX_TEXT_LEN = 240
_DISPLAY_CLUSTER_MAX_VERTICAL_GAP = 22.0
_DISPLAY_CLUSTER_MAX_HORIZONTAL_GAP = 96.0
_DISPLAY_CLUSTER_MIN_CENTER_ALIGNMENT_PT = 32.0


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
    legacy_formula_ids: tuple[str, ...] = ()
    parser_cluster_id: str | None = None


def detect_formula_candidates(document: DocumentIR) -> list[FormulaCandidate]:
    candidates: list[FormulaCandidate] = []
    seen_keys: set[tuple[str | None, str | None]] = set()
    promoted_formula_ids: set[str] = set()
    consumed_block_ids: set[str] = set()

    promoted_candidates, promoted_formula_ids, consumed_block_ids = _promote_existing_formula_candidates(
        document
    )
    for candidate in promoted_candidates:
        key = (candidate.source_block_id or candidate.anchor_block_id, candidate.candidate_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(candidate)

    for page in document.pages:
        assets_by_id = {asset.asset_id: asset for asset in page.assets}
        page_consumed_block_ids = {
            block_id
            for block_id in consumed_block_ids
            if any(page.page_id == block.page_id and block.block_id == block_id for block in page.blocks)
        }
        cluster_candidates, clustered_block_ids = _detect_display_cluster_candidates(
            page,
            skip_block_ids=page_consumed_block_ids,
        )
        for candidate in cluster_candidates:
            key = (candidate.source_block_id, candidate.candidate_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(candidate)
        page_consumed_block_ids.update(clustered_block_ids)
        for block in page.blocks:
            if block.block_id in page_consumed_block_ids:
                continue
            block_text = (block.text_for_translation or block.source_text).strip()
            if block.formula_id or _FORMULA_REF_PATTERN.fullmatch(block_text):
                continue
            if block.role == BlockRole.FORMULA and _looks_like_display_formula_text(
                block_text
            ):
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
                            source_text=block_text,
                            source_text_range=(0, len(block_text)),
                            span_ids=tuple(block.span_refs),
                            display_mode="display",
                        )
                    )
                continue

            for inline_candidate in _detect_inline_formula_candidates(
                block.model_copy(update={"source_text": block_text}, deep=True)
            ):
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

    return _sort_candidates_by_document_order(candidates, document)


def _looks_like_display_formula_text(text: str) -> bool:
    if is_noise_text(text):
        return False
    stripped = normalize_pdf_text(text)
    if len(stripped) < 3 or len(stripped) > 260:
        return False
    if re.search(r"@\S+\.\S+|\b(?:doi|https?|fig|figure|table)\b", stripped, re.IGNORECASE):
        return False
    if not _MATH_SIGNAL_PATTERN.search(stripped):
        return False
    if _looks_like_prose(stripped):
        return False
    alpha_words = alpha_word_tokens(stripped)
    if any(
        word.lower()
        in {
            "where",
            "holds",
            "with",
            "when",
            "and",
            "for",
            "defined",
            "ratio",
            "pressure",
            "electron",
            "equation",
            "notably",
        }
        for word in alpha_words
    ):
        return False
    signal_count = len(_MATH_SIGNAL_PATTERN.findall(stripped))
    if len(alpha_words) > 4 and signal_count < 2:
        return False
    return len(alpha_words) <= 8


def _sort_candidates_by_document_order(
    candidates: list[FormulaCandidate],
    document: DocumentIR,
) -> list[FormulaCandidate]:
    page_order = {page.page_id: index for index, page in enumerate(document.pages)}
    block_order: dict[str, tuple[int, int, float, float]] = {}
    asset_order: dict[str, tuple[int, float, float, float]] = {}
    for page in document.pages:
        page_index = page_order[page.page_id]
        for block in page.blocks:
            block_order[block.block_id] = (
                page_index,
                block.reading_order,
                block.bbox.y0,
                block.bbox.x0,
            )
        for asset in page.assets:
            asset_order[asset.asset_id] = (
                page_index,
                asset.bbox.y0,
                asset.bbox.x0,
                asset.bbox.y1,
            )

    def sort_key(
        indexed_candidate: tuple[int, FormulaCandidate],
    ) -> tuple[int, float, float, float, int, int]:
        index, candidate = indexed_candidate
        block_id = candidate.source_block_id or candidate.anchor_block_id
        if block_id and block_id in block_order:
            page_index, reading_order, y0, x0 = block_order[block_id]
            source_start = candidate.source_text_range[0] if candidate.source_text_range else -1
            return (page_index, float(reading_order), y0, x0, source_start, index)
        if candidate.asset_id and candidate.asset_id in asset_order:
            page_index, y0, x0, y1 = asset_order[candidate.asset_id]
            return (page_index, y0, x0, y1, -1, index)
        source_start = candidate.source_text_range[0] if candidate.source_text_range else -1
        return (
            page_order.get(candidate.page_id, len(page_order)),
            candidate.bbox.y0,
            candidate.bbox.x0,
            candidate.bbox.y1,
            source_start,
            index,
        )

    return [candidate for _index, candidate in sorted(enumerate(candidates), key=sort_key)]


def _promote_existing_formula_candidates(
    document: DocumentIR,
) -> tuple[list[FormulaCandidate], set[str], set[str]]:
    candidates: list[FormulaCandidate] = []
    promoted_formula_ids: set[str] = set()
    consumed_block_ids: set[str] = set()
    blocks_by_id = {
        block.block_id: block
        for page in document.pages
        for block in page.blocks
    }
    formulas_by_id = document.formulas_by_id()
    cluster_diagnostics = getattr(document, _FORMULA_CLUSTER_CACHE_ATTR, {}) or {}
    formula_ids_in_clusters: set[str] = set()

    for cluster in cluster_diagnostics.get("formula_fragment_clusters", []):
        primary_block_id = cluster.get("primary_block_id")
        if not isinstance(primary_block_id, str):
            continue
        primary_block = blocks_by_id.get(primary_block_id)
        if primary_block is None:
            continue
        formula_ids = tuple(
            formula_id
            for formula_id in cluster.get("formula_ids", [])
            if isinstance(formula_id, str) and formula_id in formulas_by_id
        )
        if not formula_ids:
            continue
        formula_ids_in_clusters.update(formula_ids)
        raw_cluster_text = cluster.get("combined_text")
        cluster_text = (
            raw_cluster_text
            if isinstance(raw_cluster_text, str) and raw_cluster_text.strip()
            else primary_block.source_text
        )
        resolved_cluster_text = _resolve_formula_refs(cluster_text, formulas_by_id).strip()
        equation_number = _extract_equation_number(cluster_text)
        cluster_flags = [
            "formula_display_cluster",
            "legacy_formula_migrated",
            *[f"legacy_formula_replaced:{formula_id}" for formula_id in formula_ids],
        ]
        if equation_number is not None:
            cluster_flags.append("formula_equation_number_preserved")
        candidates.append(
            FormulaCandidate(
                candidate_id=_candidate_id(
                    primary_block.page_id,
                    "legacy_cluster",
                    primary_block.block_id,
                    primary_block.bbox,
                ),
                page_id=primary_block.page_id,
                bbox=primary_block.bbox,
                source_kind=FormulaSourceKind.TEXT_LAYER,
                source_block_id=primary_block.block_id,
                source_text=resolved_cluster_text,
                source_text_range=(0, len(resolved_cluster_text))
                if resolved_cluster_text
                else None,
                span_ids=tuple(primary_block.span_refs),
                display_mode="display",
                quality_flags=tuple(_unique_flags(cluster_flags)),
                legacy_formula_ids=formula_ids,
                parser_cluster_id=cluster.get("cluster_id")
                if isinstance(cluster.get("cluster_id"), str)
                else None,
            )
        )
        for block_id in cluster.get("merged_block_ids", []):
            if isinstance(block_id, str):
                consumed_block_ids.add(block_id)
        promoted_formula_ids.update(formula_ids)

    for formula in document.formulas:
        if formula.formula_id in promoted_formula_ids:
            continue
        block_id = formula.source_block_id or formula.anchor_block_id
        block = blocks_by_id.get(block_id or "")
        if block is None:
            continue
        source_text = formula.source_text.strip()
        if not source_text or is_noise_text(source_text):
            continue
        flags = [
            "legacy_formula_migrated",
            f"legacy_formula_replaced:{formula.formula_id}",
        ]
        if formula.display_mode == "display":
            flags.append("formula_display_cluster")
        candidate = FormulaCandidate(
            candidate_id=_candidate_id(
                formula.page_id,
                "legacy_formula",
                formula.formula_id,
                block.bbox,
            ),
            page_id=formula.page_id,
            bbox=block.bbox,
            source_kind=formula.source_kind,
            source_block_id=formula.source_block_id,
            anchor_block_id=formula.anchor_block_id,
            asset_id=formula.asset_id,
            source_text=source_text,
            source_text_range=formula.source_text_range,
            span_ids=tuple(formula.span_ids),
            display_mode=formula.display_mode,
            quality_flags=tuple(_unique_flags([*flags, *formula.quality_flags])),
            legacy_formula_ids=(formula.formula_id,),
        )
        candidates.append(candidate)
        consumed_block_ids.add(block.block_id)
        promoted_formula_ids.add(formula.formula_id)

    return candidates, promoted_formula_ids, consumed_block_ids


def _detect_display_cluster_candidates(
    page: Any,
    *,
    skip_block_ids: set[str],
) -> tuple[list[FormulaCandidate], set[str]]:
    ordered_blocks = sorted(page.blocks, key=lambda item: item.reading_order)
    consumed: set[str] = set()
    candidates: list[FormulaCandidate] = []
    index = 0
    while index < len(ordered_blocks):
        block = ordered_blocks[index]
        if block.block_id in skip_block_ids or block.block_id in consumed:
            index += 1
            continue
        if not _looks_like_display_cluster_fragment(block):
            index += 1
            continue
        cluster = [block]
        next_index = index + 1
        while next_index < len(ordered_blocks):
            candidate_block = ordered_blocks[next_index]
            if (
                candidate_block.block_id in skip_block_ids
                or candidate_block.block_id in consumed
                or not _looks_like_display_cluster_fragment(candidate_block)
                or not _display_cluster_members_align(cluster[-1], candidate_block)
            ):
                break
            cluster.append(candidate_block)
            next_index += 1
        if len(cluster) == 1 and cluster[0].role != BlockRole.FORMULA:
            index += 1
            continue
        combined_text = " ".join(
            block.source_text.strip()
            for block in cluster
            if block.source_text.strip()
        )
        if len(cluster) == 1 and not _looks_like_display_formula_text(combined_text):
            index += 1
            continue
        bbox = _bbox_union([member.bbox for member in cluster])
        source_block = cluster[0]
        flags = ["formula_display_cluster"]
        if _extract_equation_number(combined_text) is not None:
            flags.append("formula_equation_number_preserved")
        candidates.append(
            FormulaCandidate(
                candidate_id=_candidate_id(
                    source_block.page_id,
                    "display_cluster",
                    source_block.block_id,
                    bbox,
                ),
                page_id=source_block.page_id,
                bbox=bbox,
                source_kind=FormulaSourceKind.TEXT_LAYER,
                source_block_id=source_block.block_id,
                source_text=combined_text,
                source_text_range=(0, len(combined_text)) if combined_text else None,
                span_ids=tuple(
                    span_ref
                    for member in cluster
                    for span_ref in member.span_refs
                ),
                display_mode="display",
                quality_flags=tuple(flags),
            )
        )
        consumed.update(member.block_id for member in cluster)
        index = next_index
    return candidates, consumed


def _looks_like_display_cluster_fragment(block: DocumentBlock) -> bool:
    text = (block.text_for_translation or block.source_text).strip()
    if not text or len(text) > _DISPLAY_CLUSTER_MAX_TEXT_LEN:
        return False
    if block.role == BlockRole.FORMULA:
        return True
    if is_noise_text(text):
        return False
    normalized = normalize_pdf_text(text)
    if len(normalized) < 2:
        return False
    if contains_natural_language(normalized):
        return False
    if re.search(r"@\S+\.\S+|\b(?:doi|https?|figure|table)\b", normalized, re.IGNORECASE):
        return False
    math_signals = len(_MATH_SIGNAL_PATTERN.findall(normalized))
    if math_signals >= 2:
        return True
    return any(marker in normalized for marker in ("=", "∂", "∇", "∫", "∑"))


def _display_cluster_members_align(left: DocumentBlock, right: DocumentBlock) -> bool:
    if right.reading_order != left.reading_order + 1:
        return False
    vertical_gap = right.bbox.y0 - left.bbox.y1
    if vertical_gap > _DISPLAY_CLUSTER_MAX_VERTICAL_GAP:
        return False
    horizontal_gap = max(0.0, max(left.bbox.x0, right.bbox.x0) - min(left.bbox.x1, right.bbox.x1))
    if horizontal_gap > _DISPLAY_CLUSTER_MAX_HORIZONTAL_GAP:
        return False
    left_center = (left.bbox.x0 + left.bbox.x1) / 2
    right_center = (right.bbox.x0 + right.bbox.x1) / 2
    return abs(left_center - right_center) <= _DISPLAY_CLUSTER_MIN_CENTER_ALIGNMENT_PT


def _extract_equation_number(text: str) -> str | None:
    normalized = normalize_pdf_text(text)
    match = _EQUATION_NUMBER_SUFFIX.search(normalized)
    if match is not None:
        return match.group(1)
    match = _EQUATION_NUMBER_WITH_SHORT_TAIL.search(normalized)
    if match is None:
        return None
    tail = match.group("tail").strip()
    if re.search(r"\b(?:and|as|for|from|is|represents?|the|where|with)\b", tail, re.IGNORECASE):
        return None
    if len(alpha_word_tokens(tail)) > 1:
        return None
    return match.group(1)


def _resolve_formula_refs(
    text: str,
    formulas_by_id: dict[str, Any],
) -> str:
    def replace_formula_ref(match: re.Match[str]) -> str:
        formula_id = match.group(1)
        formula = formulas_by_id.get(formula_id)
        if formula is None:
            return match.group(0)
        return str(formula.source_text or formula.latex or match.group(0))

    return _INLINE_FORMULA_REF_PATTERN.sub(replace_formula_ref, text)


def _looks_like_prose(text: str) -> bool:
    stripped = normalize_pdf_text(text)
    if contains_natural_language(stripped):
        return True
    if re.search(r"[,.;]\s+[A-Za-z]{3,}", stripped):
        return True
    words = alpha_word_tokens(stripped)
    return len(words) >= 6


def _asset_formula_source_kind(
    asset: Asset,
    *,
    page_height: float | None = None,
) -> FormulaSourceKind | None:
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
        if (
            candidate.page_id == page_id
            and candidate.asset_id
            and candidate.asset_id in assets_by_id
        )
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
            elif _span_continues_dangling_script(current, span):
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


def _span_continues_dangling_script(
    current: list[TextSpanIR],
    span: TextSpanIR,
) -> bool:
    if not current:
        return False
    current_text = "".join(item.text for item in current).rstrip()
    if not current_text or current_text[-1] not in {"_", "^"}:
        return False
    text = span.text.strip()
    if not text:
        return False
    if re.fullmatch(r"\{?[A-Za-z0-9α-ωΑ-Ω+\-–−]{1,12}\}?[)\]]*", text):
        return True
    if text.startswith("{") and "}" in text[:16]:
        return True
    return False


def _looks_like_inline_formula_text(text: str) -> bool:
    if is_noise_text(text):
        return False
    stripped = normalize_pdf_text(text)
    if len(stripped) < 2 or len(stripped) > 48:
        return False
    if re.search(r"@\S+\.\S+|\b(?:doi|https?)\b", stripped, re.IGNORECASE):
        return False
    if contains_natural_language(stripped):
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


def _unique_flags(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique
