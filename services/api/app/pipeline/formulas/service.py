from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


class FormulaRecognizer(Protocol):
    async def recognize(self, candidate: FormulaCandidate) -> FormulaRecognitionResult:
        ...


@dataclass(frozen=True)
class FormulaEnrichmentResult:
    document: DocumentIR
    formulas: list[FormulaIR]
    diagnostics: dict


async def enrich_document_formulas(
    document: DocumentIR,
    *,
    doc_id: str,
    asset_output_dir: Path,
    pdf_path: Path | None = None,
    recognizer: FormulaRecognizer | None = None,
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
                "quality_flags": [],
            },
        )

    recognizer = recognizer or DeterministicFormulaRecognizer()
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

    for candidate in candidates:
        try:
            result = await recognizer.recognize(candidate)
            formula = FormulaIR(
                formula_id=candidate.candidate_id,
                page_id=candidate.page_id,
                source_block_id=candidate.source_block_id,
                asset_id=candidate.asset_id,
                latex=result.latex,
                display_mode=result.display_mode,
                confidence=result.confidence,
                source_kind=candidate.source_kind,
                quality_flags=_unique(
                    [*candidate.quality_flags, *result.quality_flags]
                ),
            )
        except Exception as exc:
            formula = FormulaIR(
                formula_id=candidate.candidate_id,
                page_id=candidate.page_id,
                source_block_id=candidate.source_block_id,
                asset_id=candidate.asset_id,
                latex=candidate.source_text,
                display_mode="display",
                confidence=0.0,
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
            records.append({"formula_id": formula.formula_id, "status": "recognized"})
        formulas.append(formula)
        diagnostic_flags.extend(formula.quality_flags)

    enriched = _attach_formulas(working_document, formulas)
    diagnostics = {
        "kind": "formula_diagnostics",
        "candidate_count": len(candidates),
        "recognized_count": sum(1 for formula in formulas if formula.latex.strip()),
        "quality_flags": _unique(diagnostic_flags),
        "records": records,
        "source_counts": _source_counts(candidates),
    }
    return FormulaEnrichmentResult(
        document=enriched,
        formulas=formulas,
        diagnostics=diagnostics,
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
                    asset_id=candidate.candidate_id,
                    source_text=candidate.source_text,
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
            if formula.source_block_id
        }
        formula_by_asset = {
            formula.asset_id: formula
            for formula in page_formulas
            if formula.asset_id
        }
        blocks: list[DocumentBlock] = []
        for block in page.blocks:
            formula = formula_by_block.get(block.block_id)
            if formula is None:
                blocks.append(block)
                continue
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
            if not formula.asset_id or formula.asset_id in existing_assets:
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


def _source_counts(candidates: list[FormulaCandidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        key = candidate.source_kind.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _unique(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique
