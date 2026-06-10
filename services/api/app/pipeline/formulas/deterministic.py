from __future__ import annotations

import re

from pdf_translator_schema import FormulaRecognitionResult
from pdf_translator_schema.models import FormulaSourceKind

from .detector import FormulaCandidate
from .normalization import latex_from_pdf_text
from .validation import validate_formula_latex


class DeterministicFormulaRecognizer:
    async def recognize(self, candidate: FormulaCandidate) -> FormulaRecognitionResult:
        latex, normalization_flags = latex_from_pdf_text(candidate.source_text)
        quality_flags = list(candidate.quality_flags)
        confidence = 0.92
        if not latex:
            latex = rf"\text{{{candidate.candidate_id}}}"
            confidence = 0.35
            quality_flags.append("formula_recognition_mock")
        if any(
            flag in normalization_flags
            for flag in {
                "formula_delimiter_repaired",
                "formula_low_confidence",
                "formula_text_truncated",
            }
        ):
            confidence = min(confidence, 0.58)
            quality_flags.append("formula_low_confidence")
        quality_flags.extend(normalization_flags)
        if candidate.source_kind != FormulaSourceKind.TEXT_LAYER:
            quality_flags.append("visual_formula_not_recognized_without_model")
            if "formula_recognition_mock" not in quality_flags:
                quality_flags.append("formula_recognition_mock")
        validation = validate_formula_latex(latex, source_text=candidate.source_text)
        quality_flags.extend(validation.quality_flags)

        return FormulaRecognitionResult(
            latex=latex,
            display_mode=candidate.display_mode,
            confidence=confidence,
            accepted_provider="text_layer_normalizer"
            if candidate.source_kind in {FormulaSourceKind.TEXT_LAYER, FormulaSourceKind.INLINE_TEXT}
            else None,
            accepted_confidence=confidence if validation.accepted else None,
            validator_status=validation.status,
            fallback_reason=validation.fallback_reason,
            source_kind=candidate.source_kind,
            quality_flags=_unique(quality_flags),
        )


def _text_to_latex(text: str) -> str:
    latex, _flags = latex_from_pdf_text(text)
    return latex


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique
