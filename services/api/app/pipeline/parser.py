from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pdf_translator_schema import (
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    StyleSeed,
)
from pdf_translator_schema.models import DocumentBlock


def classify_role(text: str, page_index: int, block_index: int, font_size: float) -> BlockRole:
    stripped = text.strip()
    lower = stripped.lower()
    normalized = re.sub(r"\s+", " ", stripped)
    if page_index == 0 and block_index == 0:
        return BlockRole.TITLE
    if re.fullmatch(r"abstract\.?", lower) or lower.startswith("abstract "):
        return BlockRole.ABSTRACT
    if lower.startswith(("fig.", "figure ", "table ")):
        return BlockRole.CAPTION
    if re.fullmatch(r"(references|bibliography|works cited)\.?", lower) or re.match(
        r"^\[\d+\]",
        stripped,
    ):
        return BlockRole.REFERENCE
    has_math_operator = (
        "=" in stripped
        or re.search(r"[≤≥∑∫]", stripped)
        or re.search(r"\b\d+\s*[+\-*/]\s*\d+\b", stripped)
        or re.search(r"\b[A-Za-zα-ωΑ-Ω]\s*[+\-*/]\s*[A-Za-z0-9α-ωΑ-Ω]\b", stripped)
    )
    if has_math_operator and re.fullmatch(
        r"[A-Za-z0-9\s+\-*/=().,<>≤≥∑∫α-ωΑ-Ω^_]+",
        stripped,
    ):
        return BlockRole.FORMULA
    if re.match(r"^\(?\d+(?:\.\d+)*\)?\s+[A-Z]", normalized) and len(normalized) < 120:
        return BlockRole.HEADING
    if re.match(r"^\d+\.\s+\S+", stripped) and re.search(r"\b(19|20)\d{2}[a-z]?\b", stripped):
        return BlockRole.REFERENCE
    if len(stripped) < 90 and font_size >= 12:
        return BlockRole.HEADING
    return BlockRole.PARAGRAPH


def _stable_block_id(
    page_id: str,
    source_text: str,
    bbox: tuple[float, float, float, float],
) -> str:
    normalized_text = re.sub(r"\s+", " ", source_text).strip()
    normalized_bbox = ",".join(f"{coordinate:.1f}" for coordinate in bbox)
    stable_key = f"{page_id}|{normalized_bbox}|{normalized_text}"
    digest = hashlib.sha1(stable_key.encode()).hexdigest()
    return f"{page_id}_b{digest[:12]}"


def _reading_sort_key(block: dict) -> tuple[int, float, float, float]:
    x0, y0, x1, _ = block["bbox"]
    # Prefer a deterministic top-to-bottom order for digitally born PDFs. The
    # coarse row bucket keeps minor extraction jitter from reshuffling lines.
    return (round(y0 / 8), round(x0, 1), round(y0, 1), round(x1 - x0, 1))


def parse_pdf(pdf_path: Path, doc_id: str) -> DocumentIR:
    import fitz

    document = fitz.open(pdf_path)
    pages: list[DocumentPage] = []

    for page_index, page in enumerate(document):
        page_id = f"p{page_index + 1:04d}"
        page_dict = page.get_text("dict")
        page_blocks: list[DocumentBlock] = []

        text_blocks = [
            block
            for block in page_dict.get("blocks", [])
            if block.get("type") == 0 and block.get("lines")
        ]
        text_blocks.sort(key=_reading_sort_key)

        for block_index, block in enumerate(text_blocks):
            text_parts: list[str] = []
            span_refs: list[str] = []
            font_sizes: list[float] = []
            font_names: list[str] = []
            is_bold = False
            is_italic = False

            for line_index, line in enumerate(block.get("lines", [])):
                line_text = ""
                for span_index, span in enumerate(line.get("spans", [])):
                    text = span.get("text", "")
                    line_text += text
                    span_refs.append(f"{page_id}:b{block_index}:l{line_index}:s{span_index}")
                    size = span.get("size")
                    if isinstance(size, (int, float)):
                        font_sizes.append(float(size))
                    font = str(span.get("font", ""))
                    if font:
                        font_names.append(font)
                        is_bold = is_bold or "bold" in font.lower()
                        is_italic = (
                            is_italic
                            or "italic" in font.lower()
                            or "oblique" in font.lower()
                        )
                if line_text.strip():
                    text_parts.append(line_text.strip())

            source_text = " ".join(text_parts).strip()
            if not source_text:
                continue

            bbox = block["bbox"]
            avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 10.0
            block_id = _stable_block_id(page_id, source_text, tuple(float(value) for value in bbox))
            page_blocks.append(
                DocumentBlock(
                    block_id=block_id,
                    page_id=page_id,
                    role=classify_role(source_text, page_index, len(page_blocks), avg_font_size),
                    bbox=BoundingBox(x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3]),
                    column=0 if bbox[0] < page.rect.width / 2 else 1,
                    reading_order=len(page_blocks),
                    source_text=source_text,
                    span_refs=span_refs,
                    style_seed=StyleSeed(
                        font_size=avg_font_size,
                        font_name=font_names[0] if font_names else None,
                        bold=is_bold,
                        italic=is_italic,
                    ),
                )
            )

        pages.append(
            DocumentPage(
                page_id=page_id,
                size=PageSize(width=page.rect.width, height=page.rect.height),
                blocks=page_blocks,
                assets=[],
            )
        )

    if not pages:
        raise ValueError("PDF has no pages")
    return DocumentIR(doc_id=doc_id, pages=pages)
