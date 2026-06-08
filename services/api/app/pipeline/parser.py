from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    StyleSeed,
)
from pdf_translator_schema.models import DocumentBlock

HEADER_FOOTER_BAND_RATIO = 0.08
MIN_TEXT_BLOCKS_FOR_DIGITAL_PDF = 1


class UnsupportedPdfError(ValueError):
    def __init__(self, message: str, diagnostics: dict) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def classify_role(
    text: str,
    page_index: int,
    block_index: int,
    font_size: float,
    bbox: tuple[float, float, float, float] | None = None,
    page_height: float | None = None,
) -> BlockRole:
    stripped = text.strip()
    lower = stripped.lower()
    normalized = re.sub(r"\s+", " ", stripped)
    if page_index == 0 and block_index == 0:
        return BlockRole.TITLE
    if re.fullmatch(r"abstract\.?", lower) or lower.startswith("abstract "):
        return BlockRole.ABSTRACT
    if _looks_like_table_text(stripped):
        return BlockRole.TABLE
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
    if bbox is not None and page_height is not None and page_height > 0:
        _, y0, _, _ = bbox
        if y0 > page_height * 0.78 and font_size <= 8.5 and len(stripped) < 360:
            return BlockRole.FOOTNOTE
    if re.match(r"^\(?\d+(?:\.\d+)*\)?\s+[A-Z]", normalized) and len(normalized) < 120:
        return BlockRole.HEADING
    if re.match(r"^\d+\.\s+\S+", stripped) and re.search(r"\b(19|20)\d{2}[a-z]?\b", stripped):
        return BlockRole.REFERENCE
    if len(stripped) < 90 and font_size >= 12:
        return BlockRole.HEADING
    return BlockRole.PARAGRAPH


def _looks_like_table_text(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    normalized = re.sub(r"\s+", " ", text.strip())
    if normalized.lower().startswith("table ") and len(lines) <= 1:
        return len(re.split(r"\s{2,}|\t", text.strip())) >= 4
    if len(lines) >= 2:
        separated_rows = sum(
            1 for line in lines if "\t" in line or len(re.split(r"\s{2,}", line)) >= 3
        )
        return separated_rows >= 2
    return len(re.split(r"\s{2,}", text.strip())) >= 4


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


def _block_text(block: dict) -> str:
    parts: list[str] = []
    for line in block.get("lines", []):
        line_text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
        if line_text.strip():
            parts.append(line_text.strip())
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _header_footer_keys(page_dicts: list[dict]) -> set[str]:
    counts: dict[str, int] = {}
    page_count = len(page_dicts)
    if page_count < 2:
        return set()
    for page_dict in page_dicts:
        height = float(page_dict.get("height", 0) or 0)
        if height <= 0:
            continue
        band = height * HEADER_FOOTER_BAND_RATIO
        seen_on_page: set[str] = set()
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0 or "bbox" not in block:
                continue
            _, y0, _, y1 = block["bbox"]
            if y0 > band and y1 < height - band:
                continue
            text = _block_text(block).lower()
            if not text:
                continue
            seen_on_page.add(text)
        for text in seen_on_page:
            counts[text] = counts.get(text, 0) + 1
    return {text for text, count in counts.items() if count >= 2}


def _filter_header_footer_blocks(page_dict: dict, repeated_keys: set[str]) -> list[dict]:
    filtered: list[dict] = []
    height = float(page_dict.get("height", 0) or 0)
    band = height * HEADER_FOOTER_BAND_RATIO if height > 0 else 0
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0 or not block.get("lines"):
            continue
        text = _block_text(block).lower()
        if text in repeated_keys and "bbox" in block:
            _, y0, _, y1 = block["bbox"]
            if y0 <= band or y1 >= height - band:
                continue
        filtered.append(block)
    return filtered


def _assign_column(block: dict, page_width: float) -> int:
    x0, _, x1, _ = block["bbox"]
    center = (float(x0) + float(x1)) / 2
    return 0 if center < page_width / 2 else 1


def _order_text_blocks(text_blocks: list[dict], page_width: float) -> list[dict]:
    if len(text_blocks) < 3:
        return sorted(text_blocks, key=_reading_sort_key)

    left = [block for block in text_blocks if _assign_column(block, page_width) == 0]
    right = [block for block in text_blocks if _assign_column(block, page_width) == 1]
    if not left or not right:
        return sorted(text_blocks, key=_reading_sort_key)

    left_width = max(block["bbox"][2] for block in left) - min(block["bbox"][0] for block in left)
    right_width = max(block["bbox"][2] for block in right) - min(block["bbox"][0] for block in right)
    has_plausible_columns = left_width < page_width * 0.58 and right_width < page_width * 0.58
    if not has_plausible_columns:
        return sorted(text_blocks, key=_reading_sort_key)

    return sorted(
        text_blocks,
        key=lambda block: (
            _assign_column(block, page_width),
            round(block["bbox"][1] / 8),
            round(block["bbox"][1], 1),
            round(block["bbox"][0], 1),
        ),
    )


def _stable_asset_id(
    page_id: str,
    image_index: int,
    bbox: tuple[float, float, float, float],
) -> str:
    normalized_bbox = ",".join(f"{coordinate:.1f}" for coordinate in bbox)
    digest = hashlib.sha1(f"{page_id}|{image_index}|{normalized_bbox}".encode()).hexdigest()
    return f"{page_id}_a{digest[:12]}"


def _stable_vector_asset_id(
    page_id: str,
    drawing_index: int,
    bbox: tuple[float, float, float, float],
) -> str:
    normalized_bbox = ",".join(f"{coordinate:.1f}" for coordinate in bbox)
    digest = hashlib.sha1(f"{page_id}|vector|{drawing_index}|{normalized_bbox}".encode()).hexdigest()
    return f"{page_id}_v{digest[:12]}"


def _extract_assets(
    doc_id: str,
    page_id: str,
    page_dict: dict,
    asset_output_dir: Path | None,
) -> list[Asset]:
    assets: list[Asset] = []
    for image_index, block in enumerate(page_dict.get("blocks", []), start=1):
        if block.get("type") != 1 or "bbox" not in block:
            continue
        bbox_tuple = tuple(float(value) for value in block["bbox"])
        asset_id = _stable_asset_id(page_id, image_index, bbox_tuple)
        extension = str(block.get("ext") or "png").lower()
        if extension not in {"png", "jpg", "jpeg", "webp"}:
            extension = "png"
        image_bytes = block.get("image")
        asset_path: str | None = None
        if asset_output_dir is not None and isinstance(image_bytes, bytes):
            asset_output_dir.mkdir(parents=True, exist_ok=True)
            output_path = asset_output_dir / f"{asset_id}.{extension}"
            output_path.write_bytes(image_bytes)
            asset_path = f"/api/documents/{doc_id}/assets/{output_path.name}"
        assets.append(
            Asset(
                asset_id=asset_id,
                page_id=page_id,
                kind="image",
                bbox=BoundingBox(
                    x0=bbox_tuple[0],
                    y0=bbox_tuple[1],
                    x1=bbox_tuple[2],
                    y1=bbox_tuple[3],
                ),
                path=asset_path,
                alt_text="Extracted PDF image asset",
            )
        )
    return assets


def _extract_vector_assets(page: object, page_id: str) -> list[Asset]:
    try:
        drawings = page.get_drawings()
    except Exception:
        return []

    assets: list[Asset] = []
    for drawing_index, drawing in enumerate(drawings, start=1):
        rect = drawing.get("rect") if isinstance(drawing, dict) else None
        if rect is None:
            continue
        bbox_tuple = (
            float(rect.x0),
            float(rect.y0),
            float(rect.x1),
            float(rect.y1),
        )
        if (bbox_tuple[2] - bbox_tuple[0]) * (bbox_tuple[3] - bbox_tuple[1]) < 36:
            continue
        asset_id = _stable_vector_asset_id(page_id, drawing_index, bbox_tuple)
        assets.append(
            Asset(
                asset_id=asset_id,
                page_id=page_id,
                kind="figure",
                bbox=BoundingBox(
                    x0=bbox_tuple[0],
                    y0=bbox_tuple[1],
                    x1=bbox_tuple[2],
                    y1=bbox_tuple[3],
                ),
                alt_text="PDF vector drawing placeholder",
            )
        )
    return assets


def parse_pdf(
    pdf_path: Path,
    doc_id: str,
    asset_output_dir: Path | None = None,
) -> DocumentIR:
    import fitz

    document = fitz.open(pdf_path)
    pages: list[DocumentPage] = []
    page_dicts: list[dict] = []
    for page in document:
        page_dict = page.get_text("dict")
        page_dict["height"] = page.rect.height
        page_dict["width"] = page.rect.width
        page_dicts.append(page_dict)
    repeated_header_footer = _header_footer_keys(page_dicts)

    for page_index, page in enumerate(document):
        page_id = f"p{page_index + 1:04d}"
        page_dict = page_dicts[page_index]
        page_blocks: list[DocumentBlock] = []
        page_assets = _extract_assets(doc_id, page_id, page_dict, asset_output_dir)
        page_assets.extend(_extract_vector_assets(page, page_id))

        text_blocks = _order_text_blocks(
            _filter_header_footer_blocks(page_dict, repeated_header_footer),
            page.rect.width,
        )

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
                    role=classify_role(
                        source_text,
                        page_index,
                        len(page_blocks),
                        avg_font_size,
                        tuple(float(value) for value in bbox),
                        page.rect.height,
                    ),
                    bbox=BoundingBox(x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3]),
                    column=_assign_column(block, page.rect.width),
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
                assets=page_assets,
            )
        )

    if not pages:
        raise ValueError("PDF has no pages")
    text_block_count = sum(len(page.blocks) for page in pages)
    asset_count = sum(len(page.assets) for page in pages)
    if text_block_count < MIN_TEXT_BLOCKS_FOR_DIGITAL_PDF:
        reason = "ocr_required" if asset_count > 0 else "no_text_layer"
        raise UnsupportedPdfError(
            "Scanned or image-only PDF requires OCR, which is not implemented yet",
            {
                "kind": "unsupported_scanned_pdf",
                "reason": reason,
                "page_count": len(pages),
                "text_block_count": text_block_count,
                "asset_count": asset_count,
                "recoverable": True,
                "next_step": "Use a digitally born PDF or run OCR before uploading.",
            },
        )
    return DocumentIR(doc_id=doc_id, pages=pages)


def build_parser_diagnostics(document: DocumentIR) -> dict:
    role_counts: dict[str, int] = {}
    for page in document.pages:
        for block in page.blocks:
            role_counts[block.role.value] = role_counts.get(block.role.value, 0) + 1
    asset_counts: dict[str, int] = {}
    for page in document.pages:
        for asset in page.assets:
            asset_counts[asset.kind] = asset_counts.get(asset.kind, 0) + 1

    fallback_flags: list[str] = []
    if role_counts.get(BlockRole.TABLE.value, 0):
        fallback_flags.append("table_text_fallback")
    if role_counts.get(BlockRole.FORMULA.value, 0):
        fallback_flags.append("formula_text_fallback")
    if asset_counts.get("image", 0):
        fallback_flags.append("raster_image_assets_preserved")
    if asset_counts.get("figure", 0):
        fallback_flags.append("vector_asset_placeholder")
        fallback_flags.append("vector_assets_not_rasterized")

    return {
        "kind": "parser_diagnostics",
        "page_count": len(document.pages),
        "text_block_count": sum(len(page.blocks) for page in document.pages),
        "asset_count": sum(len(page.assets) for page in document.pages),
        "role_counts": role_counts,
        "asset_counts": asset_counts,
        "fallback_flags": fallback_flags,
        "unsupported_features": [
            {
                "kind": "vector_assets",
                "status": "placeholder_only",
                "message": "Vector graphics are carried as page-positioned placeholders; raster image assets and text-level formulas/tables are preserved as fallbacks.",
            }
        ],
    }
