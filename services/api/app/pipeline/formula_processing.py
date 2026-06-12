from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from pdf_translator_schema import BlockRole, BoundingBox, DocumentIR, Formula, FormulaIR
from pdf_translator_schema.models import DocumentBlock
from .formulas.normalization import (
    GREEK_TO_LATEX,
    SYMBOL_TO_LATEX,
    alpha_word_tokens,
    contains_natural_language,
    is_noise_text,
    latex_from_pdf_text,
    normalize_pdf_text,
    truncate_raw_at_language_boundary,
)
from .formulas.validation import (
    extract_formula_refs,
    formula_ref,
    legacy_formula_placeholder,
)

FORMULA_PLACEHOLDER_PATTERN = re.compile(r"@@FORMULA_[A-Za-z0-9_]+@@")
FORMULA_REF_PATTERN = re.compile(r"\{\{formula:[A-Za-z0-9_.:-]+\}\}")

_GREEK_TO_LATEX = GREEK_TO_LATEX
_SYMBOL_TO_LATEX = SYMBOL_TO_LATEX
_FORMULA_CLUSTER_CACHE_ATTR = "_formula_fragment_cluster_diagnostics"
_CLUSTER_MERGED_FROM_PREFIX = "formula_cluster_merged_from:"
_CLUSTER_PRIMARY_FLAG = "formula_fragment_cluster_primary"
_CLUSTER_SUPPRESSED_FLAG = "formula_fragment_cluster_suppressed"
_FORMULA_CLUSTER_ALLOWED_CONNECTORS = {
    "=",
    "+",
    "-",
    "−",
    "×",
    "·",
    ",",
    ".",
    ":",
    ";",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
}
_FORMULA_CLUSTER_MAX_TEXT_LEN = 96
_FORMULA_CLUSTER_MAX_VERTICAL_GAP = 18.0
_FORMULA_CLUSTER_MAX_BBOX_HEIGHT = 72.0
_FORMULA_CLUSTER_MIN_BBOX_HEIGHT = 7.0
_FORMULA_CLUSTER_MAX_NATURAL_WORDS = 3
_FORMULA_CLUSTER_MAX_HORIZONTAL_GAP = 72.0
_FORMULA_BAND_VERTICAL_TOLERANCE = 10.0
_FORMULA_BAND_MAX_WEAK_TEXT_LEN = 64

_INLINE_PATTERNS = [
    re.compile(
        r"@[A-Za-z][A-Za-z0-9_]*\s*=\s*@?[A-Za-z][A-Za-z0-9_]*(?:\s*[+\-−*/·]\s*[A-Za-z0-9_@().,\[\]α-ωΑ-Ω∇]+)*"
    ),
    re.compile(
        r"[A-Za-zα-ωΑ-Ω]\[[A-Za-z0-9_]+\](?:\s*(?:=|¼)\s*[−-]?[A-Za-z0-9α-ωΑ-Ω.\[\]_]+)+"
    ),
    re.compile(
        r"\b[A-Za-zα-ωΑ-Ω][A-Za-z0-9_]*\s*=\s*"
        r"(?:\([^)]{1,32}\)|[A-Za-z0-9α-ωΑ-Ω_{}+\-−*/^.,\[\]]+)"
        r"(?:\s*[+\-−*/=]\s*(?:\([^)]{1,32}\)|[A-Za-z0-9α-ωΑ-Ω_{}+\-−*/^.,\[\]]+)){0,6}"
    ),
    re.compile(
        r"(?:\\[A-Za-z]+|∫|∑|∇)[A-Za-z0-9α-ωΑ-Ω_{}()[\]\s+\-−*/=.,\\]+"
    ),
]


@dataclass(frozen=True)
class FormulaMatch:
    start: int
    end: int
    text: str
    kind: Literal["inline", "display"]


@dataclass(frozen=True)
class FormulaFragmentCluster:
    cluster_id: str
    page_id: str
    primary_block_id: str
    merged_block_ids: tuple[str, ...]
    formula_ids: tuple[str, ...]
    combined_text: str
    display_mode: Literal["display", "inline"]


def normalize_document_formulas(document: DocumentIR) -> DocumentIR:
    formulas: list[FormulaIR] = list(document.formulas)
    pages = []
    for page in document.pages:
        normalized_blocks = []
        for block in page.blocks:
            normalized_block, block_formulas = _normalize_block_formulas(block)
            normalized_blocks.append(normalized_block)
            formulas.extend(block_formulas)
        pages.append(page.model_copy(update={"blocks": normalized_blocks}, deep=True))
    normalized_document = document.model_copy(
        update={"pages": pages, "formulas": _merge_formulas(formulas)},
        deep=True,
    )
    normalized_document, clusters = _merge_formula_fragment_clusters(normalized_document)
    normalized_document = DocumentIR.model_validate(
        normalized_document.model_dump(mode="json")
    )
    setattr(
        normalized_document,
        _FORMULA_CLUSTER_CACHE_ATTR,
        {
            "formula_fragment_cluster_count": len(clusters),
            "formula_fragment_suppressed_block_count": sum(
                max(0, len(cluster.merged_block_ids) - 1) for cluster in clusters
            ),
            "formula_fragment_clusters": [
                {
                    "cluster_id": cluster.cluster_id,
                    "page_id": cluster.page_id,
                    "primary_block_id": cluster.primary_block_id,
                    "merged_block_ids": list(cluster.merged_block_ids),
                    "formula_ids": list(cluster.formula_ids),
                    "display_mode": cluster.display_mode,
                    "combined_text": cluster.combined_text,
                }
                for cluster in clusters
            ],
        },
    )
    return normalized_document


def build_formula_diagnostics(document: DocumentIR) -> dict:
    formulas = list(document.formulas)
    fallback_count = sum(
        1
        for formula in formulas
        if any(
            flag
            in {
                "formula_ocr_unavailable",
                "formula_text_fallback",
                "latex_heuristic",
                "formula_low_confidence",
            }
            for flag in formula.quality_flags
        )
    )
    low_confidence = [
        formula.formula_id
        for formula in formulas
        if formula.confidence is not None and formula.confidence < 0.65
    ]
    unresolved: list[dict[str, str]] = []
    known_refs = {formula_ref(formula.formula_id) for formula in formulas}
    for page in document.pages:
        for block in page.blocks:
            text = block.text_for_translation or block.source_text
            for ref in extract_formula_refs(text):
                if ref not in known_refs:
                    unresolved.append(
                        {
                            "block_id": block.block_id,
                            "placeholder": ref,
                        }
                    )
    cluster_diagnostics = _formula_cluster_diagnostics(document)
    return {
        "kind": "formula_diagnostics",
        "formula_count": len(formulas),
        "inline_count": sum(1 for formula in formulas if formula.display_mode == "inline"),
        "display_count": sum(1 for formula in formulas if formula.display_mode == "display"),
        "latex_success_count": sum(1 for formula in formulas if formula.latex.strip()),
        "fallback_count": fallback_count,
        "low_confidence_formula_ids": low_confidence,
        "unresolved_placeholders": unresolved,
        "quality_flag_counts": _formula_flag_counts(formulas),
        "ocr_provider": _formula_ocr_provider_status(),
        **cluster_diagnostics,
    }


def formula_placeholders_for_block(block: DocumentBlock) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    if block.formula_id:
        ref = formula_ref(block.formula_id)
        refs.append(ref)
        seen.add(ref)
    text = block.text_for_translation or block.source_text
    for ref in extract_formula_refs(text):
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


def is_formula_only_block(block: DocumentBlock) -> bool:
    text = (block.text_for_translation or block.source_text).strip()
    return block.role == BlockRole.FORMULA and (
        (
            bool(block.formula_id)
            and text == formula_ref(block.formula_id)
        )
        or FORMULA_REF_PATTERN.fullmatch(text) is not None
    )


def is_formula_like_text(text: str) -> bool:
    stripped = normalize_pdf_text(text).strip()
    if not stripped or len(stripped) > _FORMULA_CLUSTER_MAX_TEXT_LEN:
        return False
    if contains_natural_language(stripped):
        words = alpha_word_tokens(stripped)
        if len(words) > _FORMULA_CLUSTER_MAX_NATURAL_WORDS:
            return False
    refs_removed = FORMULA_REF_PATTERN.sub(" ", stripped)
    refs_removed = re.sub(r"\s+", " ", refs_removed).strip()
    if not refs_removed:
        return True
    alpha_words = alpha_word_tokens(refs_removed)
    if len(alpha_words) > _FORMULA_CLUSTER_MAX_NATURAL_WORDS:
        return False
    if _looks_like_formula(refs_removed):
        return True
    compact = refs_removed.replace(" ", "")
    if compact and all(
        char.isalnum() or char in _FORMULA_CLUSTER_ALLOWED_CONNECTORS
        for char in compact
    ):
        return any(marker in compact for marker in "=+-−×·()[]{}")
    return False


def is_formula_like_block(block: DocumentBlock) -> bool:
    text = (block.text_for_translation or block.source_text).strip()
    if not text:
        return False
    if block.role == BlockRole.FORMULA:
        return True
    if FORMULA_REF_PATTERN.fullmatch(text):
        return True
    return is_formula_like_text(text)


def _normalize_block_formulas(block: DocumentBlock) -> tuple[DocumentBlock, list[FormulaIR]]:
    if block.formula_id or FORMULA_REF_PATTERN.search(block.source_text):
        return block, []
    matches = _detect_formula_matches(block)
    if not matches:
        return block, []

    formulas: list[Formula] = []
    formula_irs: list[FormulaIR] = []
    text_for_translation = block.source_text
    offset = 0
    for index, match in enumerate(matches, start=1):
        formula_id = _formula_id(block.block_id, match.text, index)
        placeholder = legacy_formula_placeholder(formula_id)
        ref = formula_ref(formula_id)
        quality_flags = ["formula_text_fallback", "latex_heuristic"]
        latex, normalization_flags = _text_to_latex(match.text)
        quality_flags.extend(normalization_flags)
        confidence = 0.72 if latex else 0.45
        if confidence < 0.65:
            quality_flags.append("formula_low_confidence")
        if _formula_ocr_provider_status()["status"] != "available":
            quality_flags.append("formula_ocr_unavailable")
        formulas.append(
            Formula(
                formula_id=formula_id,
                placeholder=placeholder,
                kind=match.kind,
                source_text=match.text,
                latex=latex,
                bbox=block.bbox if match.kind == "display" else None,
                confidence=confidence,
                quality_flags=_unique(quality_flags),
            )
        )
        formula_irs.append(
            FormulaIR(
                formula_id=formula_id,
                page_id=block.page_id,
                source_block_id=block.block_id if match.kind == "display" else None,
                anchor_block_id=block.block_id if match.kind == "inline" else None,
                latex=latex,
                source_text=match.text,
                source_text_range=(match.start, match.end),
                display_mode="display" if match.kind == "display" else "inline",
                confidence=confidence,
                ocr_provider="text_layer_normalizer",
                ocr_confidence=confidence,
                source_kind="text_layer" if match.kind == "display" else "inline_text",
                quality_flags=_unique(quality_flags),
            )
        )
        start = match.start + offset
        end = match.end + offset
        text_for_translation = (
            text_for_translation[:start] + ref + text_for_translation[end:]
        )
        offset += len(ref) - (match.end - match.start)

    block_update = {
        "text_for_translation": re.sub(r"\s+", " ", text_for_translation).strip(),
        "formulas": formulas,
    }
    if len(formula_irs) == 1 and formula_irs[0].display_mode == "display":
        block_update["formula_id"] = formula_irs[0].formula_id
    return (
        block.model_copy(
            update=block_update,
            deep=True,
        ),
        formula_irs,
    )


def _merge_formula_fragment_clusters(
    document: DocumentIR,
) -> tuple[DocumentIR, list[FormulaFragmentCluster]]:
    formulas_by_id = document.formulas_by_id()
    pages = []
    clusters: list[FormulaFragmentCluster] = []
    formula_updates: dict[str, FormulaIR] = {}
    for page in document.pages:
        ordered_blocks = sorted(page.blocks, key=lambda item: item.reading_order)
        keep_blocks: list[DocumentBlock] = []
        consumed_block_ids: set[str] = set()
        index = 0
        while index < len(ordered_blocks):
            block = ordered_blocks[index]
            if block.block_id in consumed_block_ids:
                index += 1
                continue
            if not _eligible_formula_cluster_block(block, formulas_by_id):
                keep_blocks.append(block)
                index += 1
                continue
            cluster_members = _collect_formula_band_members(
                block,
                ordered_blocks,
                formulas_by_id,
                consumed_block_ids,
            )
            if len(cluster_members) == 1:
                keep_blocks.append(block)
                index += 1
                continue
            merged_block, cluster = _merge_formula_cluster_members(
                cluster_members,
                formulas_by_id,
            )
            keep_blocks.append(merged_block)
            clusters.append(cluster)
            consumed_block_ids.update(member.block_id for member in cluster_members)
            formula_updates.update(
                _rewrite_cluster_formula_refs(
                    formulas_by_id,
                    cluster_members,
                    merged_block.block_id,
                )
            )
            index += 1
        reindexed_blocks = [
            item.model_copy(update={"reading_order": reading_order}, deep=True)
            for reading_order, item in enumerate(keep_blocks)
        ]
        pages.append(page.model_copy(update={"blocks": reindexed_blocks}, deep=True))
    formulas = [
        formula_updates.get(formula.formula_id, formula)
        for formula in document.formulas
    ]
    return document.model_copy(update={"pages": pages, "formulas": formulas}, deep=True), clusters


def _collect_formula_band_members(
    seed: DocumentBlock,
    ordered_blocks: list[DocumentBlock],
    formulas_by_id: dict[str, FormulaIR],
    consumed_block_ids: set[str],
) -> list[DocumentBlock]:
    band_y0 = seed.bbox.y0
    band_y1 = seed.bbox.y1
    members = [seed]
    member_ids = {seed.block_id}
    changed = True
    while changed:
        changed = False
        for candidate in ordered_blocks:
            if candidate.block_id in member_ids or candidate.block_id in consumed_block_ids:
                continue
            if not _can_join_formula_band(seed, candidate, formulas_by_id):
                continue
            if not _block_intersects_vertical_band(
                candidate,
                band_y0,
                band_y1,
                tolerance=_FORMULA_BAND_VERTICAL_TOLERANCE,
            ):
                continue
            members.append(candidate)
            member_ids.add(candidate.block_id)
            band_y0 = min(band_y0, candidate.bbox.y0)
            band_y1 = max(band_y1, candidate.bbox.y1)
            changed = True
    return sorted(
        members,
        key=lambda item: (
            round(item.bbox.y0 / 4),
            round(item.bbox.x0, 1),
            item.reading_order,
        ),
    )


def _can_join_formula_band(
    seed: DocumentBlock,
    candidate: DocumentBlock,
    formulas_by_id: dict[str, FormulaIR],
) -> bool:
    if candidate.page_id != seed.page_id:
        return False
    if candidate.column != seed.column and not _blocks_have_horizontal_relation(seed, candidate):
        return False
    if _eligible_formula_cluster_block(candidate, formulas_by_id):
        return True
    return _looks_like_formula_band_fragment(candidate)


def _block_intersects_vertical_band(
    block: DocumentBlock,
    y0: float,
    y1: float,
    *,
    tolerance: float,
) -> bool:
    return block.bbox.y1 >= y0 - tolerance and block.bbox.y0 <= y1 + tolerance


def _blocks_have_horizontal_relation(left: DocumentBlock, right: DocumentBlock) -> bool:
    horizontal_overlap = min(left.bbox.x1, right.bbox.x1) - max(left.bbox.x0, right.bbox.x0)
    if horizontal_overlap >= -4.0:
        return True
    horizontal_gap = max(0.0, max(left.bbox.x0, right.bbox.x0) - min(left.bbox.x1, right.bbox.x1))
    return horizontal_gap <= _FORMULA_CLUSTER_MAX_HORIZONTAL_GAP


def _looks_like_formula_band_fragment(block: DocumentBlock) -> bool:
    if block.role in {BlockRole.TITLE, BlockRole.HEADING, BlockRole.FOOTNOTE, BlockRole.REFERENCE}:
        return False
    raw_text = (block.text_for_translation or block.source_text).strip()
    if FORMULA_REF_PATTERN.fullmatch(raw_text):
        raw_text = block.source_text
    text = normalize_pdf_text(raw_text)
    if not text or len(text) > _FORMULA_BAND_MAX_WEAK_TEXT_LEN:
        return False
    if contains_natural_language(text):
        return False
    height = block.bbox.y1 - block.bbox.y0
    compact = re.sub(r"\s+", "", text)
    is_tiny_script_fragment = bool(
        height >= 4.0
        and len(compact) <= 8
        and re.fullmatch(r"[A-Za-z0-9_{}'^]+", compact)
        and re.search(r"[0-9_{}'^]|[A-Za-z]{2,}", compact)
    )
    if (
        not is_tiny_script_fragment
        and (
            height < _FORMULA_CLUSTER_MIN_BBOX_HEIGHT
            or height > _FORMULA_CLUSTER_MAX_BBOX_HEIGHT
        )
    ):
        return False
    if re.search(r"[∂@∇∫∑=+\-−×·/^_'\d]", text):
        return True
    return bool(
        re.fullmatch(r"[A-Za-z]{1,4}(?:[A-Za-z0-9]{0,3})", compact)
        and len(compact) <= 6
    )


def _eligible_formula_cluster_block(
    block: DocumentBlock,
    formulas_by_id: dict[str, FormulaIR],
) -> bool:
    if block.role in {BlockRole.TITLE, BlockRole.HEADING, BlockRole.FOOTNOTE, BlockRole.REFERENCE}:
        return False
    text = (block.text_for_translation or block.source_text).strip()
    if not text:
        return False
    height = block.bbox.y1 - block.bbox.y0
    if height < _FORMULA_CLUSTER_MIN_BBOX_HEIGHT or height > _FORMULA_CLUSTER_MAX_BBOX_HEIGHT:
        return False
    if len(text) > _FORMULA_CLUSTER_MAX_TEXT_LEN:
        return False
    if block.formula_id and formulas_by_id.get(block.formula_id) is not None:
        return True
    if FORMULA_REF_PATTERN.fullmatch(text):
        return True
    return is_formula_like_text(text)


def _blocks_can_share_formula_cluster(left: DocumentBlock, right: DocumentBlock) -> bool:
    if right.reading_order != left.reading_order + 1:
        return False
    vertical_gap = right.bbox.y0 - left.bbox.y1
    if vertical_gap > _FORMULA_CLUSTER_MAX_VERTICAL_GAP:
        return False
    horizontal_overlap = min(left.bbox.x1, right.bbox.x1) - max(left.bbox.x0, right.bbox.x0)
    horizontal_gap = max(0.0, max(left.bbox.x0, right.bbox.x0) - min(left.bbox.x1, right.bbox.x1))
    same_band = horizontal_overlap >= -4.0 or horizontal_gap <= _FORMULA_CLUSTER_MAX_HORIZONTAL_GAP
    if not same_band:
        return False
    left_text = (left.text_for_translation or left.source_text).strip()
    right_text = (right.text_for_translation or right.source_text).strip()
    if _looks_like_explicit_sentence_boundary(left_text) or _looks_like_explicit_sentence_boundary(right_text):
        return False
    return True


def _merge_formula_cluster_members(
    blocks: list[DocumentBlock],
    formulas_by_id: dict[str, FormulaIR],
) -> tuple[DocumentBlock, FormulaFragmentCluster]:
    primary = _select_formula_cluster_primary(blocks, formulas_by_id)
    merged_ids = [block.block_id for block in blocks]
    combined_source_text, _ = _combine_cluster_source_text(blocks)
    combined_text = " ".join(
        (block.text_for_translation or block.source_text).strip()
        for block in blocks
        if (block.text_for_translation or block.source_text).strip()
    )
    combined_text = re.sub(r"\s+", " ", combined_text).strip()
    cluster_formula_ids = [
        formula.formula_id
        for formula in formulas_by_id.values()
        if formula.page_id == primary.page_id
        and (
            formula.source_block_id in merged_ids
            or formula.anchor_block_id in merged_ids
        )
    ]
    merged_bbox = BoundingBox(
        x0=min(block.bbox.x0 for block in blocks),
        y0=min(block.bbox.y0 for block in blocks),
        x1=max(block.bbox.x1 for block in blocks),
        y1=max(block.bbox.y1 for block in blocks),
    )
    merged_formulas: list[Formula] = []
    for block in blocks:
        merged_formulas.extend(block.formulas)
    block_update: dict[str, object] = {
        "bbox": merged_bbox,
        "source_text": combined_source_text,
        "text_for_translation": combined_text,
        "formulas": merged_formulas,
    }
    display_formula_ids = [
        formula_id
        for formula_id in cluster_formula_ids
        if formulas_by_id[formula_id].display_mode == "display"
    ]
    if len(display_formula_ids) == 1 and len(cluster_formula_ids) == 1:
        block_update["formula_id"] = display_formula_ids[0]
    else:
        block_update["formula_id"] = None
    merged_block = primary.model_copy(update=block_update, deep=True)
    cluster_id = _formula_id(primary.block_id, combined_text or primary.block_id, len(blocks))
    cluster = FormulaFragmentCluster(
        cluster_id=cluster_id,
        page_id=primary.page_id,
        primary_block_id=primary.block_id,
        merged_block_ids=tuple(merged_ids),
        formula_ids=tuple(cluster_formula_ids),
        combined_text=combined_text,
        display_mode="display" if display_formula_ids else "inline",
    )
    return merged_block, cluster


def _select_formula_cluster_primary(
    blocks: list[DocumentBlock],
    formulas_by_id: dict[str, FormulaIR],
) -> DocumentBlock:
    def score(block: DocumentBlock) -> tuple[int, int, float, int]:
        text = normalize_pdf_text(block.text_for_translation or block.source_text)
        block_formula_ids = [
            formula.formula_id
            for formula in formulas_by_id.values()
            if formula.source_block_id == block.block_id or formula.anchor_block_id == block.block_id
        ]
        has_display_formula = any(
            formulas_by_id[formula_id].display_mode == "display"
            for formula_id in block_formula_ids
        )
        width = block.bbox.x1 - block.bbox.x0
        math_signals = _math_signal_count(text)
        return (
            1 if has_display_formula or block.role == BlockRole.FORMULA else 0,
            -block.reading_order,
            width,
            math_signals,
        )

    return max(blocks, key=score)


def _combine_cluster_source_text(blocks: list[DocumentBlock]) -> tuple[str, dict[str, int]]:
    combined_parts: list[str] = []
    offsets: dict[str, int] = {}
    cursor = 0
    for block in blocks:
        text = block.source_text.strip()
        if not text:
            continue
        if combined_parts:
            cursor += 1
        offsets[block.block_id] = cursor
        combined_parts.append(text)
        cursor += len(text)
    return " ".join(combined_parts), offsets


def _rewrite_cluster_formula_refs(
    formulas_by_id: dict[str, FormulaIR],
    blocks: list[DocumentBlock],
    primary_block_id: str,
) -> dict[str, FormulaIR]:
    merged_block_ids = {block.block_id for block in blocks}
    page_id = blocks[0].page_id
    _, offsets = _combine_cluster_source_text(blocks)
    updates: dict[str, FormulaIR] = {}
    for formula in formulas_by_id.values():
        if formula.page_id != page_id:
            continue
        source_block_id = formula.source_block_id
        anchor_block_id = formula.anchor_block_id
        range_offset: int | None = None
        if source_block_id in merged_block_ids:
            range_offset = offsets.get(source_block_id, 0)
            source_block_id = primary_block_id
        if anchor_block_id in merged_block_ids:
            range_offset = offsets.get(anchor_block_id, range_offset or 0)
            anchor_block_id = primary_block_id
        if (
            source_block_id == formula.source_block_id
            and anchor_block_id == formula.anchor_block_id
        ):
            continue
        updates[formula.formula_id] = formula.model_copy(
            update={
                "source_block_id": source_block_id,
                "anchor_block_id": anchor_block_id,
                "source_text_range": _offset_formula_source_text_range(
                    formula.source_text_range,
                    range_offset,
                ),
            },
            deep=True,
        )
    return updates


def _offset_formula_source_text_range(
    source_text_range: tuple[int, int] | None,
    offset: int | None,
) -> tuple[int, int] | None:
    if source_text_range is None or offset is None or offset <= 0:
        return source_text_range
    start, end = source_text_range
    return (start + offset, end + offset)


def _looks_like_explicit_sentence_boundary(text: str) -> bool:
    stripped = normalize_pdf_text(text).strip()
    if not stripped:
        return False
    if re.search(r"[。！？!?]$", stripped):
        return True
    if re.search(r"\.\s*$", stripped):
        words = alpha_word_tokens(stripped)
        return len(words) >= 4
    return False


def _formula_cluster_diagnostics(document: DocumentIR) -> dict:
    diagnostics = getattr(document, _FORMULA_CLUSTER_CACHE_ATTR, None)
    if isinstance(diagnostics, dict):
        return diagnostics
    return {
        "formula_fragment_cluster_count": 0,
        "formula_fragment_suppressed_block_count": 0,
        "formula_fragment_clusters": [],
    }


def _detect_formula_matches(block: DocumentBlock) -> list[FormulaMatch]:
    text = normalize_pdf_text(block.source_text)
    if not text or is_noise_text(text):
        return []
    if block.role == BlockRole.FORMULA and not contains_natural_language(text):
        return [FormulaMatch(0, len(block.source_text), text, "display")]
    if _looks_like_display_formula(text):
        return [FormulaMatch(0, len(block.source_text), text, "display")]

    matches: list[FormulaMatch] = []
    for pattern in _INLINE_PATTERNS:
        for raw_match in pattern.finditer(block.source_text):
            candidate = _trim_formula_candidate(raw_match.group(0))
            if not candidate or not _looks_like_formula(candidate):
                continue
            start = raw_match.start() + raw_match.group(0).find(candidate)
            end = start + len(candidate)
            if any(start < current.end and end > current.start for current in matches):
                continue
            matches.append(FormulaMatch(start, end, candidate, "inline"))
    return sorted(matches, key=lambda item: item.start)


def _looks_like_display_formula(text: str) -> bool:
    text = normalize_pdf_text(text)
    if is_noise_text(text):
        return False
    if len(text) > 260:
        return False
    if re.search(r"@\S+\.\S+|\b(?:doi|http|https|Fig|Figure|Table|Section|Sec)\b", text):
        return False
    if contains_natural_language(text):
        return False
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    min_signals = 1 if len(text) <= 24 else 2
    return _looks_like_formula(text) and _math_signal_count(text) >= min_signals


def _looks_like_formula(text: str) -> bool:
    stripped = normalize_pdf_text(text)
    if len(stripped) < 3:
        return False
    if is_noise_text(stripped):
        return False
    if re.search(r"@\S+\.\S+|\b(?:doi|http|https|Fig|Figure|Table|Section|Sec)\b", stripped):
        return False
    if re.fullmatch(r"\([A-Z][A-Za-z'’\-]+,?\s+\d{4}[a-z]?\)", stripped):
        return False
    if len(alpha_word_tokens(stripped, min_len=4)) >= 3:
        return False
    if contains_natural_language(stripped) and _math_signal_count(stripped) < 2:
        return False
    return _math_signal_count(stripped) >= 1 and bool(
        re.search(r"[A-Za-zα-ωΑ-Ω@∇∫∑]", stripped)
    )


def _math_signal_count(text: str) -> int:
    text = normalize_pdf_text(text)
    signals = 0
    signals += len(re.findall(r"=|≤|≥", text))
    signals += len(re.findall(r"[∂∇∫∑]|\\[A-Za-z]+", text))
    signals += len(re.findall(r"[@^_]", text))
    signals += len(re.findall(r"\[[A-Za-z0-9_]+\]", text))
    return signals


def _trim_formula_candidate(text: str) -> str:
    candidate, _truncated = truncate_raw_at_language_boundary(text)
    return candidate


def _formula_id(block_id: str, text: str, index: int) -> str:
    digest = hashlib.sha1(f"{block_id}|{index}|{text}".encode()).hexdigest()[:12]
    return f"F{digest}"


def _text_to_latex(text: str) -> tuple[str, list[str]]:
    return latex_from_pdf_text(text)


def _formula_flag_counts(formulas: list[FormulaIR]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for formula in formulas:
        for flag in formula.quality_flags:
            counts[flag] = counts.get(flag, 0) + 1
    return counts


def _formula_ocr_provider_status() -> dict[str, str]:
    try:
        import pix2text  # noqa: F401
    except Exception as exc:
        return {"name": "pix2text", "status": "unavailable", "error": str(exc)[:160]}
    return {"name": "pix2text", "status": "available"}


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _merge_formulas(formulas: list[FormulaIR]) -> list[FormulaIR]:
    merged: dict[str, FormulaIR] = {}
    for formula in formulas:
        merged[formula.formula_id] = formula
    return list(merged.values())
