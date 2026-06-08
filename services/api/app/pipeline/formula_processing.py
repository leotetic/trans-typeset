from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from pdf_translator_schema import BlockRole, DocumentIR, Formula
from pdf_translator_schema.models import DocumentBlock

FORMULA_PLACEHOLDER_PATTERN = re.compile(r"@@FORMULA_[A-Za-z0-9_]+@@")

_GREEK_TO_LATEX = {
    "α": r"\alpha",
    "β": r"\beta",
    "γ": r"\gamma",
    "δ": r"\delta",
    "ε": r"\epsilon",
    "θ": r"\theta",
    "λ": r"\lambda",
    "μ": r"\mu",
    "ν": r"\nu",
    "ρ": r"\rho",
    "σ": r"\sigma",
    "τ": r"\tau",
    "φ": r"\phi",
    "ω": r"\omega",
    "Ω": r"\Omega",
    "Δ": r"\Delta",
    "∇": r"\nabla",
}

_SYMBOL_TO_LATEX = {
    "≤": r"\le",
    "≥": r"\ge",
    "∑": r"\sum",
    "∫": r"\int",
    "×": r"\times",
    "÷": r"\div",
    "·": r"\cdot",
    "−": "-",
    "¼": "=",
}

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
    pages = []
    for page in document.pages:
        normalized_blocks = []
        for block in page.blocks:
            normalized_blocks.append(_normalize_block_formulas(block))
        pages.append(page.model_copy(update={"blocks": normalized_blocks}, deep=True))
    return document.model_copy(update={"pages": pages}, deep=True)


def build_formula_diagnostics(document: DocumentIR) -> dict:
    formulas = [
        formula
        for page in document.pages
        for block in page.blocks
        for formula in block.formulas
    ]
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
    for page in document.pages:
        for block in page.blocks:
            text = block.text_for_translation or block.source_text
            known = {formula.placeholder for formula in block.formulas}
            for placeholder in FORMULA_PLACEHOLDER_PATTERN.findall(text):
                if placeholder not in known:
                    unresolved.append(
                        {
                            "block_id": block.block_id,
                            "placeholder": placeholder,
                        }
                    )
    return {
        "kind": "formula_diagnostics",
        "formula_count": len(formulas),
        "inline_count": sum(1 for formula in formulas if formula.kind == "inline"),
        "display_count": sum(1 for formula in formulas if formula.kind == "display"),
        "latex_success_count": sum(1 for formula in formulas if formula.latex.strip()),
        "fallback_count": fallback_count,
        "low_confidence_formula_ids": low_confidence,
        "unresolved_placeholders": unresolved,
        "quality_flag_counts": _formula_flag_counts(formulas),
        "ocr_provider": _formula_ocr_provider_status(),
    }


def formula_placeholders_for_block(block: DocumentBlock) -> list[str]:
    placeholders = [formula.placeholder for formula in block.formulas]
    text = block.text_for_translation or block.source_text
    for placeholder in FORMULA_PLACEHOLDER_PATTERN.findall(text):
        if placeholder not in placeholders:
            placeholders.append(placeholder)
    return placeholders


def is_formula_only_block(block: DocumentBlock) -> bool:
    text = (block.text_for_translation or block.source_text).strip()
    return (
        block.role == BlockRole.FORMULA
        and bool(block.formulas)
        and text in {formula.placeholder for formula in block.formulas}
    )


def _normalize_block_formulas(block: DocumentBlock) -> DocumentBlock:
    if block.formulas or FORMULA_PLACEHOLDER_PATTERN.search(block.source_text):
        return block
    matches = _detect_formula_matches(block)
    if not matches:
        return block

    formulas: list[Formula] = []
    text_for_translation = block.source_text
    offset = 0
    for index, match in enumerate(matches, start=1):
        formula_id = _formula_id(block.block_id, match.text, index)
        placeholder = f"@@FORMULA_{formula_id}@@"
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
        start = match.start + offset
        end = match.end + offset
        text_for_translation = (
            text_for_translation[:start] + placeholder + text_for_translation[end:]
        )
        offset += len(placeholder) - (match.end - match.start)

    return block.model_copy(
        update={
            "text_for_translation": re.sub(r"\s+", " ", text_for_translation).strip(),
            "formulas": formulas,
        },
        deep=True,
    )


def _detect_formula_matches(block: DocumentBlock) -> list[FormulaMatch]:
    text = block.source_text.strip()
    if not text:
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
    if len(text) > 260:
        return False
    if re.search(r"\s(?:is|are|was|were|law|shown|solve|scaled)\s", f" {text.lower()} "):
        return False
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    return _looks_like_formula(text) and _math_signal_count(text) >= 2


def _looks_like_formula(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 3:
        return False
    if re.search(r"\b(?:doi|http|https|Fig|Figure|Table|Section|Sec)\b", stripped):
        return False
    if re.fullmatch(r"\([A-Z][A-Za-z'’\-]+,?\s+\d{4}[a-z]?\)", stripped):
        return False
    return _math_signal_count(stripped) >= 1 and bool(
        re.search(r"[A-Za-zα-ωΑ-Ω@∇∫∑]", stripped)
    )


def _math_signal_count(text: str) -> int:
    signals = 0
    signals += len(re.findall(r"=|¼|≤|≥", text))
    signals += len(re.findall(r"[∇∫∑]|\\[A-Za-z]+", text))
    signals += len(re.findall(r"[@^_]", text))
    signals += len(re.findall(r"\[[A-Za-z0-9_]+\]", text))
    return signals


def _trim_formula_candidate(text: str) -> str:
    candidate = text.strip(" \t\n\r,;。；")
    candidate = re.sub(r"\s+", " ", candidate)
    sentence_mark = re.search(r"[。；;]\s*", candidate)
    if sentence_mark:
        candidate = candidate[: sentence_mark.start()].strip()
    if candidate.endswith(".") and not re.search(r"\d\.$", candidate):
        candidate = candidate[:-1].rstrip()
    return candidate


def _formula_id(block_id: str, text: str, index: int) -> str:
    digest = hashlib.sha1(f"{block_id}|{index}|{text}".encode()).hexdigest()[:12]
    return f"F{digest}"


def _text_to_latex(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return ""
    for symbol, replacement in {**_GREEK_TO_LATEX, **_SYMBOL_TO_LATEX}.items():
        normalized = normalized.replace(symbol, f" {replacement} ")
    normalized = re.sub(r"@([A-Za-z][A-Za-z0-9_]*)", r"\\partial \1", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = normalized.replace(" - ", " - ")
    return normalized


def _formula_flag_counts(formulas: list[Formula]) -> dict[str, int]:
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
