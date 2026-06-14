from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pdf_translator_schema import Asset, BoundingBox, DocumentIR, FormulaIR
from pdf_translator_schema.models import (
    BlockRole,
    DocumentBlock,
    DocumentPage,
    FormulaRecognitionResult,
    FormulaSourceKind,
)

from ..ocr import DeterministicOCRProvider, OCRService
from .detector import FormulaCandidate, detect_formula_candidates
from .deterministic import DeterministicFormulaRecognizer
from .normalization import contains_natural_language, normalize_pdf_text
from .validation import validate_formula_latex

_MATH_SIGNAL_PATTERN = re.compile(
    r"(?:[=≤≥∑∫√∞≈≠∂∇^_+\-*/]|\\(?:partial|nabla|frac|sum|int|sqrt|alpha|beta|gamma|delta|epsilon|theta|lambda|mu|nu|pi|rho|sigma|phi|omega|Delta|Omega|cdot|times)|"
    r"\b[A-Za-zα-ωΑ-Ω]\s*[+\-*/^_]\s*[A-Za-z0-9α-ωΑ-Ω])"
)
_VISUAL_MOCK_FLAGS = {
    "formula_recognition_mock",
    "visual_formula_not_recognized_without_model",
    "ocr_visual_candidate_limit_reached",
}
_TEXT_ESCALATION_FLAGS = {
    "formula_low_confidence",
    "formula_text_truncated",
    "formula_delimiter_repaired",
    "formula_text_layer_corrupt",
    "formula_pdf_ligature_corrupt",
    "formula_underbrace_artifact_repaired",
    "formula_slash_glyph_suspect",
    "formula_prime_glyph_suspect",
}
_DISPLAY_COMPLEXITY_FLAGS = {
    "formula_display_cluster",
    "formula_cluster_incomplete",
    "formula_delimiter_repaired",
}
_FORMULA_OCR_PROVIDER_DIAGNOSTICS_ATTR = "_formula_ocr_provider_diagnostics"
_DISPLAY_COMPLEXITY_PATTERN = re.compile(
    r"(?:\\(?:frac|sum|int|partial|nabla|sqrt|left|right)|[∂∇∫∑]|[(){}\[\]])"
)


class FormulaRecognizer(Protocol):
    async def recognize(self, candidate: FormulaCandidate) -> FormulaRecognitionResult:
        ...


class _RecognizerOCRAdapter:
    def __init__(self, recognizer: FormulaRecognizer, name: str) -> None:
        self.recognizer = recognizer
        self.name = name

    async def recognize_formula(
        self,
        candidate: FormulaCandidate,
        *,
        image_path: Path | None = None,
    ) -> Any:
        result = await self.recognizer.recognize(candidate)
        from pdf_translator_schema.models import OCRRecognitionResult

        return OCRRecognitionResult(
            text=result.latex,
            latex=result.latex,
            region_kind="formula",
            provider=self.name,
            confidence=result.confidence,
            quality_flags=result.quality_flags,
        )


@dataclass(frozen=True)
class FormulaEnrichmentResult:
    document: DocumentIR
    formulas: list[FormulaIR]
    diagnostics: dict
    recognition_records: list[dict]
    candidates: list[dict]
    ocr_records: list[dict]


async def enrich_document_formulas(
    document: DocumentIR,
    *,
    doc_id: str,
    asset_output_dir: Path,
    pdf_path: Path | None = None,
    recognizer: FormulaRecognizer | None = None,
    ocr_service: OCRService | None = None,
    recognizer_type: str = "deterministic",
    visual_formula_recognition_enabled: bool = False,
    on_progress: Callable[[int, int, FormulaCandidate], None] | None = None,
) -> FormulaEnrichmentResult:
    candidates = detect_formula_candidates(document)
    if not candidates:
        return FormulaEnrichmentResult(
            document=document,
            formulas=[],
            diagnostics={
                "kind": "formula_diagnostics",
                "candidate_count": 0,
                "recognized_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "quality_flags": _formula_diagnostic_base_flags(
                    visual_formula_recognition_enabled,
                    recognizer_type,
                ),
                "recognizer_type": recognizer_type,
                "visual_formula_recognition_enabled": visual_formula_recognition_enabled,
            },
            recognition_records=[],
            candidates=[],
            ocr_records=[],
        )

    recognizer = recognizer or DeterministicFormulaRecognizer()
    prefer_text_recognizer = recognizer is not None
    ocr_service = ocr_service or OCRService(
        providers=[DeterministicOCRProvider()],
        asset_base_path=asset_output_dir,
    )
    if getattr(ocr_service, "asset_base_path", None) is None:
        ocr_service.asset_base_path = asset_output_dir
    working_document = document
    candidates = _ensure_formula_candidate_assets(
        working_document,
        candidates,
        doc_id=doc_id,
        asset_output_dir=asset_output_dir,
        pdf_path=pdf_path,
    )
    formulas: list[FormulaIR] = []
    records: list[dict] = []
    rejected_records: list[dict] = []
    diagnostic_flags: list[str] = []
    display_cluster_promoted_count = 0
    legacy_formula_migrated_count = 0

    total_candidates = len(candidates)
    for index, candidate in enumerate(candidates, start=1):
        if on_progress is not None:
            try:
                on_progress(index, total_candidates, candidate)
            except Exception:
                pass
        try:
            ocr_result, validator, recognition_record = await _recognize_candidate(
                candidate,
                recognizer=recognizer,
                ocr_service=ocr_service,
                prefer_text_recognizer=prefer_text_recognizer,
            )
            records.append(recognition_record)
            validator = validate_formula_latex(
                ocr_result.latex or ocr_result.text,
                source_text=candidate.source_text,
                display_mode=candidate.display_mode,
            )
            formula = FormulaIR(
                formula_id=candidate.candidate_id,
                page_id=candidate.page_id,
                source_block_id=candidate.source_block_id,
                anchor_block_id=candidate.anchor_block_id,
                asset_id=candidate.asset_id,
                latex=(ocr_result.latex or ocr_result.text)
                if validator.accepted or _should_attach_image_fallback_formula(candidate, validator)
                else "",
                source_text=candidate.source_text,
                source_text_range=candidate.source_text_range,
                span_ids=list(candidate.span_ids),
                display_mode=candidate.display_mode,
                confidence=ocr_result.confidence,
                ocr_provider=ocr_result.provider,
                ocr_confidence=ocr_result.confidence,
                source_kind=candidate.source_kind,
                quality_flags=_unique(
                    [*candidate.quality_flags, *ocr_result.quality_flags, *validator.quality_flags]
                ),
            )
            if candidate.parser_cluster_id:
                display_cluster_promoted_count += 1
            if candidate.legacy_formula_ids:
                legacy_formula_migrated_count += len(candidate.legacy_formula_ids)
        except Exception as exc:
            records.append(
                {
                    "formula_id": candidate.candidate_id,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            diagnostic_flags.append("formula_recognition_failed")
            continue
        else:
            rejection_reason = _formula_attachment_rejection_reason(candidate, formula)
            if rejection_reason is not None:
                rejection_flags = _unique([*formula.quality_flags, rejection_reason])
                rejected_record = {
                    **recognition_record,
                    "status": "rejected",
                    "reason": rejection_reason,
                    "formula_id": formula.formula_id,
                    "latex": formula.latex or (ocr_result.latex or ocr_result.text),
                    "accepted_provider": formula.ocr_provider if formula.latex else None,
                    "accepted_confidence": formula.confidence if formula.latex else None,
                    "validator_status": validator.status,
                    "fallback_reason": validator.fallback_reason,
                    "quality_flags": rejection_flags,
                }
                records[-1] = rejected_record
                rejected_records.append(rejected_record)
                diagnostic_flags.extend(rejection_flags)
                continue
            recognition_record.update(
                {
                    "formula_id": formula.formula_id,
                    "status": "recognized",
                    "latex": formula.latex,
                    "accepted_provider": formula.ocr_provider,
                    "accepted_confidence": formula.confidence,
                    "validator_status": validator.status,
                    "fallback_reason": validator.fallback_reason,
                    "source_kind": formula.source_kind.value,
                }
            )
            records[-1] = recognition_record
            formulas.append(formula)
            diagnostic_flags.extend(formula.quality_flags)

    enriched = _attach_formulas(working_document, formulas)
    ocr_diagnostics = ocr_service.diagnostics()
    active_provider_order = [
        str(item) for item in ocr_diagnostics.get("active_provider_order", [])
    ]
    diagnostics = {
        "kind": "formula_diagnostics",
        "parser_cluster_count": _parser_cluster_count(document),
        "enrichment_candidate_count": len(candidates),
        "display_cluster_promoted_count": display_cluster_promoted_count,
        "legacy_formula_migrated_count": legacy_formula_migrated_count,
        "candidate_count": len(candidates),
        "recognized_count": sum(1 for formula in formulas if formula.latex.strip()),
        "accepted_count": len(formulas),
        "rejected_count": len(rejected_records),
        "quality_flags": _unique(
            [
                *_formula_diagnostic_base_flags(
                    visual_formula_recognition_enabled,
                    recognizer_type,
                ),
                *diagnostic_flags,
            ]
        ),
        "records": records,
        "rejected_records": rejected_records,
        "source_counts": _source_counts(candidates),
        "recognizer_type": recognizer_type,
        "visual_formula_recognition_enabled": visual_formula_recognition_enabled,
        "ocr_provider": _ocr_provider_status(active_provider_order),
        "ocr": ocr_diagnostics,
    }
    setattr(
        enriched,
        _FORMULA_OCR_PROVIDER_DIAGNOSTICS_ATTR,
        {"active_provider_order": active_provider_order},
    )
    return FormulaEnrichmentResult(
        document=enriched,
        formulas=formulas,
        diagnostics=diagnostics,
        recognition_records=records,
        candidates=[_candidate_record(candidate) for candidate in candidates],
        ocr_records=ocr_diagnostics["records"],
    )


async def _recognize_candidate(
    candidate: FormulaCandidate,
    *,
    recognizer: FormulaRecognizer,
    ocr_service: OCRService,
    prefer_text_recognizer: bool,
) -> tuple[Any, Any, dict[str, Any]]:
    if prefer_text_recognizer and candidate.source_kind in {
        FormulaSourceKind.TEXT_LAYER,
        FormulaSourceKind.INLINE_TEXT,
    }:
        text_result = await recognizer.recognize(candidate)
        text_validator = validate_formula_latex(
            text_result.latex,
            source_text=candidate.source_text,
            display_mode=candidate.display_mode,
        )
        if _should_escalate_text_candidate(candidate, text_result, text_validator):
            visual_result = await ocr_service.recognize_formula(
                candidate,
                prefer_visual=_prefer_visual_formula_provider(candidate),
            )
            visual_validator = validate_formula_latex(
                visual_result.latex or visual_result.text,
                source_text=candidate.source_text,
                display_mode=candidate.display_mode,
            )
            if visual_validator.accepted and not (
                set(visual_result.quality_flags) & _VISUAL_MOCK_FLAGS
            ):
                return (
                    visual_result,
                    visual_validator,
                    {
                        "formula_id": candidate.candidate_id,
                        "status": "upgraded",
                        "accepted_provider": visual_result.provider,
                        "accepted_confidence": visual_result.confidence,
                        "validator_status": visual_validator.status,
                        "fallback_reason": visual_validator.fallback_reason,
                        "source_kind": candidate.source_kind.value,
                        "quality_flags": _unique(
                            [
                                *text_result.quality_flags,
                                *visual_result.quality_flags,
                                "formula_visual_escalated",
                            ]
                        ),
                    },
                )
        return (
            _recognition_result_from_formula_result(text_result),
            text_validator,
            {
                "formula_id": candidate.candidate_id,
                "status": "recognized" if text_validator.accepted else "rejected",
                "accepted_provider": text_result.accepted_provider,
                "accepted_confidence": text_result.accepted_confidence,
                "validator_status": text_result.validator_status,
                "fallback_reason": text_result.fallback_reason,
                "source_kind": candidate.source_kind.value,
                "quality_flags": list(text_result.quality_flags),
            },
        )
    ocr_result = await ocr_service.recognize_formula(
        candidate,
        prefer_visual=_prefer_visual_formula_provider(candidate),
    )
    validator = validate_formula_latex(
        ocr_result.latex or ocr_result.text,
        source_text=candidate.source_text,
        display_mode=candidate.display_mode,
    )
    return (
        ocr_result,
        validator,
        {
            "formula_id": candidate.candidate_id,
            "status": "recognized" if validator.accepted else "rejected",
            "accepted_provider": ocr_result.provider if validator.accepted else None,
            "accepted_confidence": ocr_result.confidence if validator.accepted else None,
            "validator_status": validator.status,
            "fallback_reason": validator.fallback_reason,
            "source_kind": candidate.source_kind.value,
            "quality_flags": list(ocr_result.quality_flags),
        },
    )


def _recognition_result_from_formula_result(result: FormulaRecognitionResult) -> Any:
    from pdf_translator_schema.models import OCRRecognitionResult

    return OCRRecognitionResult(
        text=result.latex,
        latex=result.latex,
        region_kind="formula",
        provider=result.accepted_provider or "text_layer_normalizer",
        confidence=result.confidence,
        quality_flags=list(result.quality_flags),
    )


def _should_escalate_text_candidate(
    candidate: FormulaCandidate,
    result: FormulaRecognitionResult,
    validator: Any,
) -> bool:
    if not candidate.image_path:
        return False
    if _prefer_visual_formula_provider(candidate):
        return True
    if candidate.display_mode == "display" and set(result.quality_flags) & _TEXT_ESCALATION_FLAGS:
        return True
    if validator.accepted and result.confidence >= 0.65 and not (
        set(result.quality_flags) & _TEXT_ESCALATION_FLAGS
    ):
        return False
    if validator.status in {"prose_like", "katex_error", "not_math"}:
        return True
    if set(result.quality_flags) & _TEXT_ESCALATION_FLAGS:
        return True
    return result.confidence < 0.65


def _prefer_visual_formula_provider(candidate: FormulaCandidate) -> bool:
    if candidate.display_mode != "display":
        return False
    candidate_flags = set(candidate.quality_flags)
    if candidate.image_path and candidate_flags & _TEXT_ESCALATION_FLAGS:
        return True
    if candidate.source_kind in {
        FormulaSourceKind.IMAGE_CANDIDATE,
        FormulaSourceKind.VECTOR_CANDIDATE,
    }:
        return True
    flags = set(candidate.quality_flags)
    if flags.intersection(_DISPLAY_COMPLEXITY_FLAGS):
        return True
    normalized = normalize_pdf_text(candidate.source_text)
    if len(re.findall(r"[A-Za-z]{3,}", normalized)) <= 2 and _DISPLAY_COMPLEXITY_PATTERN.search(
        normalized
    ):
        return True
    return normalized.count("=") >= 2


def _formula_attachment_rejection_reason(
    candidate: FormulaCandidate,
    formula: FormulaIR,
) -> str | None:
    flags = set(formula.quality_flags)
    if "formula_prose_like" in flags or "formula_not_math" in flags:
        return "formula_not_math_rejected"
    if flags.intersection(_VISUAL_MOCK_FLAGS):
        return "formula_visual_mock_rejected"
    validator = validate_formula_latex(
        formula.latex,
        source_text=formula.source_text,
        display_mode=formula.display_mode,
    )
    if not validator.accepted:
        if _formula_uses_image_fallback(formula, validator):
            return None
        return (
            "formula_not_math_rejected"
            if validator.status in {"prose_like", "not_math"}
            else "formula_visual_mock_rejected"
            if flags.intersection(_VISUAL_MOCK_FLAGS)
            else "formula_validator_rejected"
        )
    if formula.display_mode != "display":
        return None
    if not _display_latex_has_math_signal(formula.latex):
        return "formula_not_math_rejected"
    if _display_latex_looks_like_prose(formula.latex):
        return "formula_prose_rejected"
    if (
        candidate.source_kind
        in {FormulaSourceKind.IMAGE_CANDIDATE, FormulaSourceKind.VECTOR_CANDIDATE}
        and (formula.ocr_provider or "").lower() == "deterministic"
    ):
        return "formula_visual_mock_rejected"
    return None


def _should_attach_image_fallback_formula(
    candidate: FormulaCandidate,
    validator: Any,
) -> bool:
    return (
        candidate.display_mode == "display"
        and bool(candidate.asset_id or candidate.image_path)
        and validator.fallback_reason == "formula_asset_image"
    )


def _formula_uses_image_fallback(formula: FormulaIR, validator: Any) -> bool:
    return (
        formula.display_mode == "display"
        and bool(formula.asset_id)
        and validator.fallback_reason == "formula_asset_image"
    )


def _display_latex_has_math_signal(text: str) -> bool:
    normalized = normalize_pdf_text(text)
    if not normalized:
        return False
    if _MATH_SIGNAL_PATTERN.search(normalized):
        return True
    return bool(
        re.search(
            r"\\(?:begin|end|left|right|overline|underline|hat|bar|vec|mathbf|mathrm|mathcal|operatorname)\b",
            normalized,
        )
    )


def _display_latex_looks_like_prose(text: str) -> bool:
    normalized = normalize_pdf_text(text)
    if not normalized:
        return False
    words = re.findall(r"[A-Za-z]{3,}", normalized)
    math_signals = len(_MATH_SIGNAL_PATTERN.findall(normalized))
    if contains_natural_language(normalized) and len(words) >= 2:
        return True
    if re.search(r"[,.;]\s+[A-Za-z]{3,}", normalized) and len(words) >= 4:
        return True
    if len(normalized) > 80 and len(words) >= 6 and math_signals < 3:
        return True
    if len(words) >= 10 and math_signals <= max(1, len(words) // 8):
        return True
    return False


def _ensure_formula_candidate_assets(
    document: DocumentIR,
    candidates: list[FormulaCandidate],
    *,
    doc_id: str,
    asset_output_dir: Path,
    pdf_path: Path | None,
) -> list[FormulaCandidate]:
    if pdf_path is None:
        return candidates
    try:
        import fitz
    except Exception:
        return candidates

    asset_output_dir.mkdir(parents=True, exist_ok=True)
    try:
        pdf = fitz.open(pdf_path)
    except Exception:
        return candidates

    try:
        page_indexes = {page.page_id: index for index, page in enumerate(document.pages)}
        updated: list[FormulaCandidate] = []
        for candidate in candidates:
            if candidate.image_path:
                updated.append(candidate)
                continue
            if candidate.display_mode == "inline":
                updated.append(candidate)
                continue
            page_index = page_indexes.get(candidate.page_id)
            if page_index is None or page_index >= len(pdf):
                updated.append(candidate)
                continue
            output_path = asset_output_dir / f"{candidate.candidate_id}.png"
            try:
                page = pdf[page_index]
                clip = _padded_formula_crop_rect(
                    fitz,
                    candidate,
                    page_width=page.rect.width,
                    page_height=page.rect.height,
                )
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
                pixmap.save(output_path)
            except Exception:
                updated.append(candidate)
                continue
            asset_path = f"/api/documents/{doc_id}/assets/{output_path.name}"
            quality_flags = list(candidate.quality_flags)
            if clip.y0 < candidate.bbox.y0 or clip.y1 > candidate.bbox.y1:
                quality_flags.append("formula_crop_padded")
            if _equation_number_present(candidate.source_text):
                quality_flags.append("formula_equation_number_preserved")
            updated.append(
                FormulaCandidate(
                    candidate_id=candidate.candidate_id,
                    page_id=candidate.page_id,
                    bbox=candidate.bbox,
                    source_kind=candidate.source_kind,
                    source_block_id=candidate.source_block_id,
                    anchor_block_id=candidate.anchor_block_id,
                    asset_id=candidate.candidate_id,
                    source_text=candidate.source_text,
                    source_text_range=candidate.source_text_range,
                    span_ids=candidate.span_ids,
                    display_mode=candidate.display_mode,
                    image_path=asset_path,
                    quality_flags=tuple(_unique(quality_flags)),
                    legacy_formula_ids=candidate.legacy_formula_ids,
                    parser_cluster_id=candidate.parser_cluster_id,
                )
            )
        return updated
    finally:
        pdf.close()


def _attach_formulas(document: DocumentIR, formulas: list[FormulaIR]) -> DocumentIR:
    formulas_by_id = {formula.formula_id: formula for formula in formulas}
    legacy_formula_ids = {
        flag.split(":", 1)[1]
        for formula in formulas
        for flag in formula.quality_flags
        if flag.startswith("legacy_formula_replaced:")
    }
    existing_assets = {
        asset.asset_id
        for page in document.pages
        for asset in page.assets
    }
    pages: list[DocumentPage] = []
    for page in document.pages:
        page_formulas = [
            formula for formula in formulas if formula.page_id == page.page_id
        ]
        formula_by_block = {
            formula.source_block_id: formula
            for formula in page_formulas
            if formula.source_block_id and formula.display_mode == "display"
        }
        inline_formulas_by_block: dict[str, list[FormulaIR]] = {}
        for formula in page_formulas:
            if formula.display_mode != "inline" or not formula.anchor_block_id:
                continue
            inline_formulas_by_block.setdefault(formula.anchor_block_id, []).append(formula)
        formula_by_asset = {
            formula.asset_id: formula
            for formula in page_formulas
            if formula.asset_id
        }
        blocks: list[DocumentBlock] = []
        for block in page.blocks:
            formula = formula_by_block.get(block.block_id)
            inline_formulas = inline_formulas_by_block.get(block.block_id, [])
            if formula is None and not inline_formulas:
                blocks.append(block)
                continue
            if formula is not None:
                blocks.append(
                    block.model_copy(
                        update={
                            "role": BlockRole.FORMULA,
                            "source_text": f"{{{{formula:{formula.formula_id}}}}}",
                            "text_for_translation": f"{{{{formula:{formula.formula_id}}}}}",
                            "formula_id": formula.formula_id,
                            "formulas": [],
                        },
                        deep=True,
                    )
                )
                continue
            rewritten = _rewrite_inline_formula_refs(block.source_text, inline_formulas)
            blocks.append(
                block.model_copy(
                    update={
                        "source_text": rewritten,
                        "text_for_translation": rewritten,
                    },
                    deep=True,
                )
            )

        assets: list[Asset] = []
        for asset in page.assets:
            formula = formula_by_asset.get(asset.asset_id)
            if formula is None:
                assets.append(asset)
                continue
            assets.append(
                asset.model_copy(
                    update={
                        "kind": "formula",
                        "formula_id": formula.formula_id,
                        "alt_text": formula.latex or asset.alt_text,
                    },
                    deep=True,
                )
            )
        for formula in page_formulas:
            if (
                not formula.asset_id
                or formula.asset_id in existing_assets
                or formula.display_mode == "inline"
            ):
                continue
            assets.append(
                Asset(
                    asset_id=formula.asset_id,
                    page_id=page.page_id,
                    kind="formula",
                    bbox=_bbox_for_formula(page, formula),
                    path=f"/api/documents/{document.doc_id}/assets/{formula.asset_id}.png",
                    alt_text=formula.latex,
                    formula_id=formula.formula_id,
                )
            )
            existing_assets.add(formula.asset_id)
        pages.append(page.model_copy(update={"blocks": blocks, "assets": assets}, deep=True))

    merged_formulas = {
        formula.formula_id: formula for formula in document.formulas
        if formula.formula_id not in legacy_formula_ids
    }
    merged_formulas.update(formulas_by_id)
    updated = document.model_copy(
        update={
            "pages": pages,
            "formulas": list(merged_formulas.values()),
        },
        deep=True,
    )
    return DocumentIR.model_validate(updated.model_dump(mode="json"))


def _bbox_for_formula(page: DocumentPage, formula: FormulaIR) -> BoundingBox:
    if formula.source_block_id:
        for block in page.blocks:
            if block.block_id == formula.source_block_id:
                return block.bbox
    if formula.asset_id:
        for asset in page.assets:
            if asset.asset_id == formula.asset_id:
                return asset.bbox
    return BoundingBox(x0=0, y0=0, x1=1, y1=1)


def _rewrite_inline_formula_refs(source_text: str, formulas: list[FormulaIR]) -> str:
    rewritten = source_text
    for formula in sorted(
        formulas,
        key=lambda item: item.source_text_range[0] if item.source_text_range else -1,
        reverse=True,
    ):
        token = f"{{{{formula:{formula.formula_id}}}}}"
        if formula.source_text_range is None:
            if formula.source_text and formula.source_text in rewritten:
                rewritten = rewritten.replace(formula.source_text, token, 1)
            continue
        start, end = formula.source_text_range
        if start < 0 or end < start or start > len(rewritten):
            continue
        rewritten = rewritten[:start] + token + rewritten[end:]
    return rewritten


def _source_counts(candidates: list[FormulaCandidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        key = candidate.source_kind.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _candidate_record(candidate: FormulaCandidate) -> dict:
    return {
        "candidate_id": candidate.candidate_id,
        "page_id": candidate.page_id,
        "source_kind": candidate.source_kind.value,
        "source_block_id": candidate.source_block_id,
        "anchor_block_id": candidate.anchor_block_id,
        "asset_id": candidate.asset_id,
        "source_text": candidate.source_text,
        "source_text_range": list(candidate.source_text_range)
        if candidate.source_text_range
        else None,
        "span_ids": list(candidate.span_ids),
        "display_mode": candidate.display_mode,
        "image_path": candidate.image_path,
        "quality_flags": list(candidate.quality_flags),
    }


def _unique(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def _formula_diagnostic_base_flags(
    visual_formula_recognition_enabled: bool,
    recognizer_type: str,
) -> list[str]:
    flags: list[str] = []
    if not visual_formula_recognition_enabled:
        flags.append("visual_formula_recognition_disabled")
    if recognizer_type == "deterministic":
        flags.append("formula_recognition_deterministic")
    return flags


def _ocr_provider_status(active_provider_order: list[str]) -> dict:
    try:
        import pix2text  # noqa: F401
    except Exception as exc:
        status = {"name": "pix2text", "status": "unavailable", "error": str(exc)[:160]}
    else:
        status = {"name": "pix2text", "status": "available"}
    status["active_provider_order"] = list(active_provider_order)
    status["active_provider_order_includes_pix2text"] = "pix2text" in active_provider_order
    return status


def _parser_cluster_count(document: DocumentIR) -> int:
    diagnostics = getattr(document, "_formula_fragment_cluster_diagnostics", None)
    if not isinstance(diagnostics, dict):
        return 0
    count = diagnostics.get("formula_fragment_cluster_count", 0)
    return int(count) if isinstance(count, int) else 0


def _padded_formula_crop_rect(
    fitz_module: Any,
    candidate: FormulaCandidate,
    *,
    page_width: float,
    page_height: float,
) -> Any:
    width = max(1.0, candidate.bbox.x1 - candidate.bbox.x0)
    height = max(1.0, candidate.bbox.y1 - candidate.bbox.y0)
    pad_x = min(max(width * 0.06, 8.0), 28.0)
    pad_y = min(max(height * 0.22, 10.0), 36.0)
    right_pad = pad_x
    if _equation_number_present(candidate.source_text):
        right_pad = max(right_pad, width * 0.18, 22.0)
    return fitz_module.Rect(
        max(0.0, candidate.bbox.x0 - pad_x),
        max(0.0, candidate.bbox.y0 - pad_y),
        min(page_width, candidate.bbox.x1 + right_pad),
        min(page_height, candidate.bbox.y1 + pad_y),
    )


def _equation_number_present(text: str) -> bool:
    normalized = normalize_pdf_text(text)
    if re.search(r"\(\d+\)\s*$", normalized):
        return True
    match = re.search(
        r"\(\d+\)(?P<tail>\s+[A-Za-z0-9α-ωΑ-Ω_{}^\\+\-*/.,\s]{1,24})$",
        normalized,
    )
    if match is None:
        return False
    tail = match.group("tail").strip()
    if re.search(r"\b(?:and|as|for|from|is|represents?|the|where|with)\b", tail, re.IGNORECASE):
        return False
    return len(re.findall(r"[A-Za-z]{3,}", tail)) <= 1
