from __future__ import annotations

from pathlib import Path
from typing import Any

from pdf_translator_schema import (
    Asset,
    AssetIR,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    StyleSeed,
    UserIntent,
)
from pdf_translator_schema.models import DocumentBlock

from ..storage import Storage
from .image_ocr import extract_image_text

_PAGE_MARGIN = 54.0


async def build_scanned_pdf_document(
    *,
    pdf_path: Path,
    doc_id: str,
    storage: Storage,
    filename: str,
    intent: UserIntent,
    runtime_config: dict[str, Any],
) -> tuple[DocumentIR, list[AssetIR], dict[str, Any]]:
    try:
        import fitz
    except Exception as exc:
        document = _fallback_document(doc_id, filename, str(exc))
        return document, [], {
            "kind": "scanned_pdf_ocr",
            "status": "fallback",
            "provider": "deterministic",
            "quality_flags": ["pdf_page_render_unavailable", "deterministic_ocr_mock"],
            "error": str(exc),
        }

    pages: list[DocumentPage] = []
    asset_ir: list[AssetIR] = []
    quality_flags: list[str] = []
    try:
        pdf = fitz.open(pdf_path)
    except Exception as exc:
        document = _fallback_document(doc_id, filename, str(exc))
        return document, [], {
            "kind": "scanned_pdf_ocr",
            "status": "fallback",
            "provider": "deterministic",
            "page_count": len(document.pages),
            "text_block_count": sum(len(page.blocks) for page in document.pages),
            "quality_flags": ["pdf_page_render_failed", "deterministic_ocr_mock"],
            "error": str(exc),
        }
    try:
        for page_index, page in enumerate(pdf, start=1):
            page_id = f"p{page_index}"
            asset_id = f"{doc_id}_scan_page_{page_index:04d}"
            image_path = storage.asset_dir(doc_id) / f"{asset_id}.png"
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            pixmap.save(str(image_path))
            ocr = await extract_image_text(
                image_path=image_path,
                filename=f"{filename} page {page_index}",
                mime_type="image/png",
                runtime_config=runtime_config,
            )
            quality_flags.extend(ocr.quality_flags)
            blocks: list[DocumentBlock] = []
            y = _PAGE_MARGIN
            text_width = max(float(page.rect.width) - (_PAGE_MARGIN * 2), 120.0)
            for block_index, ocr_block in enumerate(ocr.blocks):
                text = ocr_block.text.strip()
                if not text:
                    continue
                block_height = min(max(36.0, len(text) / max(text_width / 7.0, 1) * 14.0), 140.0)
                blocks.append(
                    DocumentBlock(
                        block_id=f"{doc_id}_ocr_p{page_index:04d}_b{block_index + 1:04d}",
                        page_id=page_id,
                        role=_block_role(ocr_block.role),
                        bbox=BoundingBox(
                            x0=_PAGE_MARGIN,
                            y0=y,
                            x1=float(page.rect.width) - _PAGE_MARGIN,
                            y1=min(y + block_height, float(page.rect.height) - _PAGE_MARGIN),
                        ),
                        reading_order=block_index,
                        source_text=text,
                        style_seed=StyleSeed(font_size=11),
                    )
                )
                y += block_height + 12.0
            if not blocks:
                blocks.append(
                    DocumentBlock(
                        block_id=f"{doc_id}_ocr_p{page_index:04d}_fallback",
                        page_id=page_id,
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(
                            x0=_PAGE_MARGIN,
                            y0=_PAGE_MARGIN,
                            x1=float(page.rect.width) - _PAGE_MARGIN,
                            y1=_PAGE_MARGIN + 48,
                        ),
                        reading_order=0,
                        source_text=(
                            f"Deterministic OCR fallback for {filename} page {page_index}. "
                            "No configured vision OCR text was extracted."
                        ),
                        style_seed=StyleSeed(font_size=11),
                    )
                )
            asset_url = f"/api/documents/{doc_id}/assets/{image_path.name}"
            pages.append(
                DocumentPage(
                    page_id=page_id,
                    size=PageSize(width=float(page.rect.width), height=float(page.rect.height)),
                    blocks=blocks,
                    assets=[
                        Asset(
                            asset_id=asset_id,
                            page_id=page_id,
                            kind="image",
                            bbox=BoundingBox(
                                x0=0,
                                y0=0,
                                x1=float(page.rect.width),
                                y1=float(page.rect.height),
                            ),
                            path=asset_url,
                            alt_text=f"Scanned PDF page {page_index}",
                        )
                    ],
                )
            )
            asset_ir.append(
                AssetIR(
                    asset_id=asset_id,
                    source_id="content_source",
                    kind="image",
                    mime_type="image/png",
                    path=asset_url,
                    ocr_text="\n\n".join(block.source_text for block in blocks),
                    alt_text=f"Scanned PDF page {page_index}",
                    source_block_ids=[block.block_id for block in blocks],
                    confidence=0.7 if ocr.provider != "deterministic" else 0.35,
                    quality_flags=ocr.quality_flags,
                )
            )
    finally:
        pdf.close()

    unique_flags = _unique(quality_flags or ["deterministic_ocr_mock", "ocr_uncertain"])
    return (
        DocumentIR(doc_id=doc_id, pages=pages),
        asset_ir,
        {
            "kind": "scanned_pdf_ocr",
            "status": "completed",
            "page_count": len(pages),
            "asset_count": len(asset_ir),
            "text_block_count": sum(len(page.blocks) for page in pages),
            "quality_flags": unique_flags,
        },
    )


def _fallback_document(doc_id: str, filename: str, reason: str) -> DocumentIR:
    return DocumentIR(
        doc_id=doc_id,
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=612, height=792),
                blocks=[
                    DocumentBlock(
                        block_id=f"{doc_id}_ocr_fallback_0001",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=54, y0=54, x1=558, y1=120),
                        reading_order=0,
                        source_text=(
                            f"Deterministic OCR fallback for {filename}. "
                            f"Page rendering was unavailable: {reason}"
                        ),
                        style_seed=StyleSeed(font_size=11),
                    )
                ],
            )
        ],
    )


def _block_role(role: str) -> BlockRole:
    try:
        return BlockRole(role)
    except ValueError:
        return BlockRole.PARAGRAPH


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result
