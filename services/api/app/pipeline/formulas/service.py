from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from pdf_translator_schema import Asset, BoundingBox, DocumentIR, FormulaIR
from pdf_translator_schema.models import (
    BlockRole,
    DocumentBlock,
    DocumentPage,
    FormulaRecognitionResult,
    FormulaSourceKind,
)

from .detector import FormulaCandidate, detect_formula_candidates
from .deterministic import DeterministicFormulaRecognizer
from ..ocr import OCRService


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
                "quality_flags": _formula_diagnostic_base_flags(
                    visual_formula_recognition_enabled,
                    recognizer_type,
                ),
                "recognizer_type": recognizer_type,
                "visual_formula_recognition_enabled": visual_formula_recognition_enabled,
            },
            candidates=[],
            ocr_records=[],
        )

    recognizer = recognizer or DeterministicFormulaRecognizer()
    ocr_service = ocr_service or OCRService(
        providers=[_RecognizerOCRAdapter(recognizer, recognizer_type)],
        asset_base_path=asset_output_dir,
    )
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
    diagnostic_flags: list[str] = []

    total_candidates = len(candidates)
    for index, candidate in enumerate(candidates, start=1):
        if on_progress is not None:
            try:
                on_progress(index, total_candidates, candidate)
            except Exception:
                pass
        try:
            ocr_result = await ocr_service.recognize_formula(candidate)
            formula = FormulaIR(
                formula_id=candidate.candidate_id,
                page_id=candidate.page_id,
                source_block_id=candidate.source_block_id,
                anchor_block_id=candidate.anchor_block_id,
                asset_id=candidate.asset_id,
                latex=ocr_result.latex or ocr_result.text,
                source_text=candidate.source_text,
                source_text_range=candidate.source_text_range,
                span_ids=list(candidate.span_ids),
                display_mode=candidate.display_mode,
                confidence=ocr_result.confidence,
                ocr_provider=ocr_result.provider,
                ocr_confidence=ocr_result.confidence,
                source_kind=candidate.source_kind,
                quality_flags=_unique(
                    [*candidate.quality_flags, *ocr_result.quality_flags]
                ),
            )
        except Exception as exc:
            formula = FormulaIR(
                formula_id=candidate.candidate_id,
                page_id=candidate.page_id,
                source_block_id=candidate.source_block_id,
                anchor_block_id=candidate.anchor_block_id,
                asset_id=candidate.asset_id,
                latex=candidate.source_text,
                source_text=candidate.source_text,
                source_text_range=candidate.source_text_range,
                span_ids=list(candidate.span_ids),
                display_mode=candidate.display_mode,
                confidence=0.0,
                ocr_provider="failed",
                ocr_confidence=0.0,
                source_kind=candidate.source_kind,
                quality_flags=_unique(
                    [
                        *candidate.quality_flags,
                        "formula_recognition_failed",
                    ]
                ),
            )
            records.append(
                {
                    "formula_id": formula.formula_id,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            diagnostic_flags.append("formula_recognition_failed")
        else:
            records.append(
                {
                    "formula_id": formula.formula_id,
                    "status": "recognized",
                    "ocr_provider": formula.ocr_provider,
                    "confidence": formula.confidence,
                }
            )
        formulas.append(formula)
        diagnostic_flags.extend(formula.quality_flags)

    enriched = _attach_formulas(working_document, formulas)
    ocr_diagnostics = ocr_service.diagnostics()
    diagnostics = {
        "kind": "formula_diagnostics",
        "candidate_count": len(candidates),
        "recognized_count": sum(1 for formula in formulas if formula.latex.strip()),
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
        "source_counts": _source_counts(candidates),
        "recognizer_type": recognizer_type,
        "visual_formula_recognition_enabled": visual_formula_recognition_enabled,
        "ocr": ocr_diagnostics,
    }
    return FormulaEnrichmentResult(
        document=enriched,
        formulas=formulas,
        diagnostics=diagnostics,
        candidates=[_candidate_record(candidate) for candidate in candidates],
        ocr_records=ocr_diagnostics["records"],
    )


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
                clip = fitz.Rect(
                    candidate.bbox.x0,
                    candidate.bbox.y0,
                    candidate.bbox.x1,
                    candidate.bbox.y1,
                )
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
                pixmap.save(output_path)
            except Exception:
                updated.append(candidate)
                continue
            asset_path = f"/api/documents/{doc_id}/assets/{output_path.name}"
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
                    quality_flags=candidate.quality_flags,
                )
            )
        return updated
    finally:
        pdf.close()


def _attach_formulas(document: DocumentIR, formulas: list[FormulaIR]) -> DocumentIR:
    formulas_by_id = {formula.formula_id: formula for formula in formulas}
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
                            "formula_id": formula.formula_id,
                        },
                        deep=True,
                    )
                )
                continue
            rewritten = _rewrite_inline_formula_refs(block.source_text, inline_formulas)
            blocks.append(
                block.model_copy(update={"source_text": rewritten}, deep=True)
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
