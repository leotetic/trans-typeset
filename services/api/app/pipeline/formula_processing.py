from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from pdf_translator_schema import BlockRole, DocumentIR, Formula, FormulaIR
from pdf_translator_schema.models import DocumentBlock
from .formulas.normalization import (
    GREEK_TO_LATEX,
    SYMBOL_TO_LATEX,
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

_INLINE_PATTERNS = [
    re.compile(
        r"@[A-Za-z][A-Za-z0-9_]*\s*=\s*@?[A-Za-z][A-Za-z0-9_]*(?:\s*[+\-−*/·]\s*[A-Za-z0-9_@().,\[\]α-ωΑ-Ω∇]+)*"
    ),
    re.compile(
        r"[A-Za-zα-ωΑ-Ω]\[[A-Za-z0-9_]+\](?:\s*(?:=|¼)\s*[−-]?[A-Za-z0-9α-ωΑ-Ω.\[\]_]+)+"
    ),
    re.compile(
        r"\b[A-Za-zα-ωΑ-Ω][A-Za-z0-9_]*\s*=\s*[A-Za-z0-9α-ωΑ-Ω_()+\-−*/^.,\s]+"
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
    return document.model_copy(update={"pages": pages, "formulas": _merge_formulas(formulas)}, deep=True)


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
        latex = _text_to_latex(match.text)
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


def _detect_formula_matches(block: DocumentBlock) -> list[FormulaMatch]:
    text = normalize_pdf_text(block.source_text)
    if not text or is_noise_text(text):
        return []
    if block.role == BlockRole.FORMULA or _looks_like_display_formula(text):
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


def _text_to_latex(text: str) -> str:
    latex, _flags = latex_from_pdf_text(text)
    return latex


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
