from __future__ import annotations

import re

from pdf_translator_schema import FormulaRecognitionResult
from pdf_translator_schema.models import FormulaSourceKind

from .detector import FormulaCandidate


class DeterministicFormulaRecognizer:
    async def recognize(self, candidate: FormulaCandidate) -> FormulaRecognitionResult:
        latex = _text_to_latex(candidate.source_text)
        quality_flags = list(candidate.quality_flags)
        confidence = 0.92
        if not latex:
            latex = rf"\text{{{candidate.candidate_id}}}"
            confidence = 0.35
            quality_flags.append("formula_recognition_mock")
        if candidate.source_kind != FormulaSourceKind.TEXT_LAYER:
            quality_flags.append("visual_formula_not_recognized_without_model")
            if "formula_recognition_mock" not in quality_flags:
                quality_flags.append("formula_recognition_mock")

        return FormulaRecognitionResult(
            latex=latex,
            display_mode="display",
            confidence=confidence,
            quality_flags=_unique(quality_flags),
        )


def _text_to_latex(text: str) -> str:
    stripped = re.sub(r"\s+", " ", text.strip())
    if not stripped:
        return ""
    replacements = {
        "≤": r"\le",
        "≥": r"\ge",
        "≠": r"\ne",
        "≈": r"\approx",
        "∑": r"\sum",
        "∫": r"\int",
        "√": r"\sqrt{}",
        "∞": r"\infty",
        "α": r"\alpha",
        "β": r"\beta",
        "γ": r"\gamma",
        "θ": r"\theta",
        "λ": r"\lambda",
        "μ": r"\mu",
        "σ": r"\sigma",
    }
    for source, target in replacements.items():
        stripped = stripped.replace(source, target)
    return stripped


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique
