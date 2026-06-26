from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    StyleSeed,
)
from pdf_translator_schema.models import DocumentBlock, TextLineIR, TextSpanIR

from .formula_processing import build_formula_diagnostics, normalize_document_formulas
from .formulas.normalization import (
    contains_natural_language,
    is_noise_text,
    normalize_pdf_text,
    normalize_pdf_text_fragment,
)

HEADER_FOOTER_BAND_RATIO = 0.08
MIN_TEXT_BLOCKS_FOR_DIGITAL_PDF = 1
TEXT_DICT_FLAGS = 0
_AIP_PARTIAL_SLASH_FONT_MARKERS = ("4C4E51",)
_AIP_SYMBOL_FONT_MARKERS = ("4C4E74",)
_SCRIPTABLE_PREVIOUS_CHAR = re.compile(r"[A-Za-z0-9α-ωΑ-Ω)\]\}']")
_MATH_GEOMETRY_MARKER = re.compile(r"(?:[∂@∇∫∑¼þ=/_^]|\b[fgqmn][sn]\b)")
_PUBLICATION_COPYRIGHT_PATTERN = re.compile(
    r"^©\s*(?:(?:the\s+)?author\(s\)|authors?|作者)\b.*(?:19|20)\d{2}\.?$",
    re.IGNORECASE,
)
_PUBLICATION_TIMESTAMP_PATTERN = re.compile(
    r"^\d{1,2}\s+[A-Z][a-z]+\s+(?:19|20)\d{2}\s+\d{2}:\d{2}:\d{2}$"
)
_STRICT_CAPTION_LABEL_PATTERN = re.compile(
    r"^\s*(?:"
    r"(?:fig\.|figure|table)\s*[A-Z]?\d+(?:[.\-][A-Za-z0-9]+)*\s*[.:：)]|"
    r"(?:图|表)\s*[A-Z]?\d+(?:[.\-][A-Za-z0-9]+)*\s*[.:：、)]"
    r")",
    re.IGNORECASE,
)
_CAPTIONABLE_ASSET_KINDS = {"image", "figure", "table"}
_CAPTION_CONTEXT_MAX_DISTANCE_PT = 90.0


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
    if _has_strict_caption_label(stripped):
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
    if has_math_operator and contains_natural_language(stripped):
        has_math_operator = False
    if has_math_operator and re.fullmatch(
        r"[A-Za-z0-9\s+\-*/=().,<>[\]{}\\≤≥∑∫α-ωΑ-Ω^_]+",
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


def _has_strict_caption_label(text: str) -> bool:
    return bool(_STRICT_CAPTION_LABEL_PATTERN.match(re.sub(r"\s+", " ", text).strip()))


def _bbox_vertical_distance(a: BoundingBox, b: BoundingBox) -> float:
    if a.y1 < b.y0:
        return b.y0 - a.y1
    if b.y1 < a.y0:
        return a.y0 - b.y1
    return 0.0


def _bbox_horizontal_overlap_ratio(a: BoundingBox, b: BoundingBox) -> float:
    overlap = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    width = max(1.0, min(a.x1 - a.x0, b.x1 - b.x0))
    return overlap / width


def _has_nearby_captionable_asset(block: DocumentBlock, assets: list[Asset]) -> bool:
    for asset in assets:
        if asset.kind not in _CAPTIONABLE_ASSET_KINDS:
            continue
        if (
            _bbox_vertical_distance(asset.bbox, block.bbox)
            <= _CAPTION_CONTEXT_MAX_DISTANCE_PT
            and _bbox_horizontal_overlap_ratio(asset.bbox, block.bbox) >= 0.35
        ):
            return True
    return False


def _refine_caption_roles(
    blocks: list[DocumentBlock],
    assets: list[Asset],
) -> list[DocumentBlock]:
    refined: list[DocumentBlock] = []
    for block in blocks:
        has_caption_label = _has_strict_caption_label(block.source_text)
        role = block.role
        if role == BlockRole.CAPTION and not has_caption_label:
            role = BlockRole.PARAGRAPH
        elif (
            role == BlockRole.PARAGRAPH
            and has_caption_label
            and _has_nearby_captionable_asset(block, assets)
            and (block.style_seed.font_size or 10.0) <= 10.5
        ):
            role = BlockRole.CAPTION
        if role != block.role:
            refined.append(block.model_copy(update={"role": role}, deep=True))
        else:
            refined.append(block)
    return refined


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


def _unique_block_id(
    base_block_id: str,
    source_text: str,
    bbox: tuple[float, float, float, float],
    seen_block_ids: set[str],
) -> str:
    if base_block_id not in seen_block_ids:
        seen_block_ids.add(base_block_id)
        return base_block_id

    normalized_text = re.sub(r"\s+", " ", source_text).strip()
    precise_bbox = ",".join(f"{coordinate:.3f}" for coordinate in bbox)
    suffix_seed = f"{base_block_id}|{precise_bbox}|{normalized_text}"
    suffix = hashlib.sha1(suffix_seed.encode()).hexdigest()[:8]
    candidate = f"{base_block_id}_d{suffix}"
    ordinal = 2
    while candidate in seen_block_ids:
        candidate = f"{base_block_id}_d{suffix}_{ordinal}"
        ordinal += 1
    seen_block_ids.add(candidate)
    return candidate


def _reading_sort_key(block: dict) -> tuple[int, float, float, float]:
    x0, y0, x1, _ = block["bbox"]
    # Prefer a deterministic top-to-bottom order for digitally born PDFs. The
    # coarse row bucket keeps minor extraction jitter from reshuffling lines.
    return (round(y0 / 8), round(x0, 1), round(y0, 1), round(x1 - x0, 1))


def _block_text(block: dict) -> str:
    return normalize_pdf_text(" ".join(_block_text_parts(block)))


def _is_publication_boilerplate_text(
    text: str,
    bbox: object | None = None,
    page_height: float = 0.0,
) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return False
    if _PUBLICATION_COPYRIGHT_PATTERN.match(normalized):
        return True

    bbox_tuple = _coerce_bbox_tuple(bbox)
    in_footer = False
    if bbox_tuple is not None and page_height > 0:
        x0, y0, x1, y1 = bbox_tuple
        footer_band = page_height * HEADER_FOOTER_BAND_RATIO
        in_footer = y1 >= page_height - footer_band or y0 >= page_height * 0.72
        if (
            x1 - x0 <= 14
            and y1 - y0 >= 40
            and _PUBLICATION_TIMESTAMP_PATTERN.match(normalized)
        ):
            return True

    lower = normalized.lower()
    if in_footer and "doi:" in lower:
        return True
    if in_footer and re.search(r"\bj\.\s+[a-z]", lower) and re.search(
        r"\b(?:19|20)\d{2}\b",
        lower,
    ):
        return True
    return False


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
        raw_text = _block_text(block)
        if _is_publication_boilerplate_text(raw_text, block.get("bbox"), height):
            continue
        text = raw_text.lower()
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


def _is_column_body_candidate(block: dict, page_width: float) -> bool:
    x0, y0, x1, y1 = block["bbox"]
    width = float(x1) - float(x0)
    height = float(y1) - float(y0)
    if width <= 0 or height <= 0:
        return False
    if width > page_width * 0.58:
        return False
    if width < 14 and height > 40:
        return False
    return True


def _order_text_blocks(text_blocks: list[dict], page_width: float) -> list[dict]:
    if len(text_blocks) < 3:
        return sorted(text_blocks, key=_reading_sort_key)

    column_blocks = [
        block for block in text_blocks if _is_column_body_candidate(block, page_width)
    ]
    left = [block for block in column_blocks if _assign_column(block, page_width) == 0]
    right = [block for block in column_blocks if _assign_column(block, page_width) == 1]
    if not left or not right:
        return sorted(text_blocks, key=_reading_sort_key)

    left_width = max(block["bbox"][2] for block in left) - min(block["bbox"][0] for block in left)
    right_width = max(block["bbox"][2] for block in right) - min(block["bbox"][0] for block in right)
    has_plausible_columns = left_width < page_width * 0.58 and right_width < page_width * 0.58
    if not has_plausible_columns:
        return sorted(text_blocks, key=_reading_sort_key)

    min_body_y = min(block["bbox"][1] for block in column_blocks)
    max_body_y = max(block["bbox"][3] for block in column_blocks)

    def column_order_key(block: dict) -> tuple[float, float, float, float]:
        x0, y0, _x1, y1 = block["bbox"]
        if not _is_column_body_candidate(block, page_width):
            if y1 < min_body_y:
                group = -1.0
            else:
                group = 2.0 if y0 > max_body_y else 1.5
            return (group, round(y0 / 8), round(y0, 1), round(x0, 1))
        return (
            float(_assign_column(block, page_width)),
            round(y0 / 8),
            round(y0, 1),
            round(x0, 1),
        )

    return sorted(text_blocks, key=column_order_key)


def _font_has_marker(font_name: str, markers: tuple[str, ...]) -> bool:
    return any(marker in font_name for marker in markers)


def _normalize_span_text(span: dict) -> str:
    text = str(span.get("text", ""))
    font_name = str(span.get("font", ""))
    if _font_has_marker(font_name, _AIP_PARTIAL_SLASH_FONT_MARKERS):
        text = text.replace("@", "∂").replace("=", "/")
    return normalize_pdf_text_fragment(text)


def _line_base_font_size(line: dict) -> float:
    sizes = [
        float(span.get("size"))
        for span in line.get("spans", [])
        if isinstance(span.get("size"), (int, float))
        and str(span.get("text", "")).strip()
    ]
    return max(sizes) if sizes else 10.0


def _line_main_center_y(line: dict, base_font_size: float) -> float | None:
    centers: list[float] = []
    for span in line.get("spans", []):
        size = span.get("size")
        bbox = _coerce_bbox_tuple(span.get("bbox"))
        if not isinstance(size, (int, float)) or bbox is None:
            continue
        if float(size) >= base_font_size * 0.9 and str(span.get("text", "")).strip():
            centers.append((bbox[1] + bbox[3]) / 2)
    if not centers:
        line_bbox = _coerce_bbox_tuple(line.get("bbox"))
        if line_bbox is None:
            return None
        return (line_bbox[1] + line_bbox[3]) / 2
    centers.sort()
    return centers[len(centers) // 2]


def _last_non_space(text: str) -> str:
    match = re.search(r"\S(?=\s*$)", text)
    return match.group(0) if match else ""


def _can_attach_script(text: str) -> bool:
    previous = _last_non_space(text)
    return bool(previous and _SCRIPTABLE_PREVIOUS_CHAR.fullmatch(previous))


def _is_prime_glyph(text: str, fonts: list[str]) -> bool:
    return text == "0" and any(
        _font_has_marker(font_name, _AIP_SYMBOL_FONT_MARKERS)
        for font_name in fonts
    )


def _script_suffix(kind: str, text: str) -> str:
    if text == "'":
        return "'"
    return f"_{text}" if kind == "sub" and text.startswith("{") else (
        f"^{text}" if kind == "super" and text.startswith("{") else f"{'^' if kind == 'super' else '_'}{{{text}}}"
    )


def _format_line_text(line: dict) -> str:
    base_size = _line_base_font_size(line)
    main_center_y = _line_main_center_y(line, base_size)
    tokens: list[dict[str, object]] = []
    for span in line.get("spans", []):
        text = _normalize_span_text(span)
        if not text:
            continue
        bbox = _coerce_bbox_tuple(span.get("bbox"))
        size = span.get("size")
        kind: str | None = None
        if (
            main_center_y is not None
            and bbox is not None
            and isinstance(size, (int, float))
            and float(size) <= base_size * 0.82
        ):
            center_y = (bbox[1] + bbox[3]) / 2
            threshold = max(0.9, base_size * 0.16)
            if center_y < main_center_y - threshold:
                kind = "super"
            elif center_y > main_center_y + threshold:
                kind = "sub"
        tokens.append(
            {
                "text": text,
                "kind": kind,
                "font": str(span.get("font", "")),
            }
        )

    output = ""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token["kind"]
        text = str(token["text"])
        if kind not in {"sub", "super"} or not text.strip() or not _can_attach_script(output):
            output += text
            index += 1
            continue

        group_texts = [text]
        group_fonts = [str(token["font"])]
        next_index = index + 1
        while next_index < len(tokens) and tokens[next_index]["kind"] == kind:
            group_texts.append(str(tokens[next_index]["text"]))
            group_fonts.append(str(tokens[next_index]["font"]))
            next_index += 1
        script_text = "".join(group_texts).strip()
        if not script_text:
            output += "".join(group_texts)
        else:
            if _is_prime_glyph(script_text, group_fonts):
                script_text = "'"
            output += _script_suffix(str(kind), script_text)
        index = next_index
    return output


def _block_has_stacked_math_geometry(block: dict) -> bool:
    lines = [
        line
        for line in block.get("lines", [])
        if "".join(_normalize_span_text(span) for span in line.get("spans", [])).strip()
    ]
    if len(lines) < 2:
        return False
    line_text = " ".join(
        _format_line_text(line)
        for line in lines
    )
    if not _MATH_GEOMETRY_MARKER.search(line_text):
        return False
    narrow_lines = 0
    for line in lines:
        bbox = _coerce_bbox_tuple(line.get("bbox"))
        if bbox is None:
            continue
        width = bbox[2] - bbox[0]
        text = _format_line_text(line).strip()
        if width <= 42 or len(text) <= 8:
            narrow_lines += 1
    return narrow_lines >= 1


def _repair_stacked_formula_text(text: str, block: dict) -> str:
    if not _block_has_stacked_math_geometry(block):
        return text
    repaired = text
    repaired = re.sub(
        r"(?:∂|@)\s*f(?:_?\{?s\}?)\s*(?:∂|@)\s*t\b",
        r"\\frac{∂f_s}{∂t}",
        repaired,
    )
    repaired = re.sub(
        r"\bq(?:_?\{?s\}?)\s+m(?:_?\{?s\}?)\b",
        r"\\frac{q_s}{m_s}",
        repaired,
    )
    repaired = re.sub(
        r"\bf'\s+s\s+k\^?\{?2\}?\b",
        r"\\frac{f'_s}{k^2}",
        repaired,
    )
    return repaired


def _block_text_parts(block: dict) -> list[str]:
    parts: list[str] = []
    for line in block.get("lines", []):
        line_text = _format_line_text(line)
        if line_text.strip():
            parts.append(line_text.strip())
    return parts


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


def _text_dict_flags_without_images() -> int:
    try:
        import fitz
    except Exception:
        return TEXT_DICT_FLAGS

    base_flags = int(getattr(fitz, "TEXTFLAGS_DICT", TEXT_DICT_FLAGS))
    preserve_images = int(getattr(fitz, "TEXT_PRESERVE_IMAGES", 0))
    return base_flags & ~preserve_images


def _extract_assets_from_page(
    doc_id: str,
    page_id: str,
    page: Any,
    asset_output_dir: Path | None,
) -> list[Asset]:
    image_infos = []
    try:
        image_infos = page.get_image_info(xrefs=True)
    except TypeError:
        try:
            image_infos = page.get_image_info()
        except Exception:
            image_infos = []
    except Exception:
        image_infos = []

    assets: list[Asset] = []
    seen_bboxes: set[tuple[float, float, float, float]] = set()
    for image_index, info in enumerate(image_infos, start=1):
        bbox = info.get("bbox") if isinstance(info, dict) else None
        if bbox is None:
            continue
        bbox_tuple = _coerce_bbox_tuple(bbox)
        if bbox_tuple is None:
            continue
        bbox_key = tuple(round(value, 1) for value in bbox_tuple)
        if bbox_key in seen_bboxes:
            continue
        seen_bboxes.add(bbox_key)
        asset_id = _stable_asset_id(page_id, image_index, bbox_tuple)
        asset_path: str | None = None
        if asset_output_dir is not None:
            image_bytes, extension = _extract_image_bytes(page.parent, info)
            if image_bytes is not None:
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


def _coerce_bbox_tuple(value: object) -> tuple[float, float, float, float] | None:
    try:
        if hasattr(value, "x0"):
            return (float(value.x0), float(value.y0), float(value.x1), float(value.y1))
        if isinstance(value, (list, tuple)) and len(value) >= 4:
            return (
                float(value[0]),
                float(value[1]),
                float(value[2]),
                float(value[3]),
            )
    except (TypeError, ValueError):
        return None
    return None


def _bbox_from_tuple(value: tuple[float, float, float, float]) -> BoundingBox:
    return BoundingBox(x0=value[0], y0=value[1], x1=value[2], y1=value[3])


def _valid_bbox_tuple(value: tuple[float, float, float, float] | None) -> bool:
    return value is not None and value[2] > value[0] and value[3] > value[1]


def _bbox_union(values: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    if not values:
        return None
    return (
        min(value[0] for value in values),
        min(value[1] for value in values),
        max(value[2] for value in values),
        max(value[3] for value in values),
    )


def _extract_image_bytes(document: Any, image_info: dict) -> tuple[bytes, str] | tuple[None, str]:
    xref = image_info.get("xref")
    if not isinstance(xref, int) or xref <= 0:
        return None, "png"
    try:
        extracted = document.extract_image(xref)
    except Exception:
        return None, "png"
    image_bytes = extracted.get("image") if isinstance(extracted, dict) else None
    extension = str(extracted.get("ext") if isinstance(extracted, dict) else "png").lower()
    if extension not in {"png", "jpg", "jpeg", "webp"}:
        extension = "png"
    if not isinstance(image_bytes, bytes):
        return None, extension
    return image_bytes, extension


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
    text_flags = _text_dict_flags_without_images()
    pages: list[DocumentPage] = []
    page_dicts: list[dict] = []
    seen_block_ids: set[str] = set()
    for page in document:
        page_dict = page.get_text("dict", flags=text_flags)
        page_dict["height"] = page.rect.height
        page_dict["width"] = page.rect.width
        page_dicts.append(page_dict)
    repeated_header_footer = _header_footer_keys(page_dicts)

    for page_index, page in enumerate(document):
        page_id = f"p{page_index + 1:04d}"
        page_dict = page_dicts[page_index]
        page_blocks: list[DocumentBlock] = []
        page_assets = _extract_assets_from_page(doc_id, page_id, page, asset_output_dir)
        page_assets.extend(_extract_vector_assets(page, page_id))

        text_blocks = _order_text_blocks(
            _filter_header_footer_blocks(page_dict, repeated_header_footer),
            page.rect.width,
        )

        for block_index, block in enumerate(text_blocks):
            text_parts = _block_text_parts(block)
            font_sizes: list[float] = []
            font_names: list[str] = []
            is_bold = False
            is_italic = False

            for line_index, line in enumerate(block.get("lines", [])):
                for span_index, span in enumerate(line.get("spans", [])):
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

            source_text = normalize_pdf_text(" ".join(text_parts))
            source_text = normalize_pdf_text(_repair_stacked_formula_text(source_text, block))
            if not source_text or is_noise_text(source_text):
                continue

            bbox = block["bbox"]
            bbox_tuple = tuple(float(value) for value in bbox)
            avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 10.0
            block_id = _unique_block_id(
                _stable_block_id(page_id, source_text, bbox_tuple),
                source_text,
                bbox_tuple,
                seen_block_ids,
            )
            lines: list[TextLineIR] = []
            spans: list[TextSpanIR] = []
            span_refs: list[str] = []
            for line_index, line in enumerate(block.get("lines", [])):
                line_text = ""
                line_span_ids: list[str] = []
                line_bboxes: list[tuple[float, float, float, float]] = []
                for span_index, span in enumerate(line.get("spans", [])):
                    text = _normalize_span_text(span)
                    if not text:
                        continue
                    span_bbox_tuple = _coerce_bbox_tuple(span.get("bbox"))
                    if not _valid_bbox_tuple(span_bbox_tuple):
                        continue
                    span_id = f"{page_id}:{block_id}:l{line_index}:s{span_index}"
                    line_id = f"{page_id}:{block_id}:l{line_index}"
                    size = span.get("size")
                    flags = span.get("flags")
                    color = span.get("color")
                    origin_value = span.get("origin")
                    origin: tuple[float, float] | None = None
                    if isinstance(origin_value, (list, tuple)) and len(origin_value) >= 2:
                        try:
                            origin = (float(origin_value[0]), float(origin_value[1]))
                        except (TypeError, ValueError):
                            origin = None
                    spans.append(
                        TextSpanIR(
                            span_id=span_id,
                            page_id=page_id,
                            block_id=block_id,
                            line_id=line_id,
                            text=text,
                            bbox=_bbox_from_tuple(span_bbox_tuple),
                            font_name=str(span.get("font", "")) or None,
                            font_size=float(size) if isinstance(size, (int, float)) else None,
                            flags=int(flags) if isinstance(flags, int) else None,
                            color=f"#{int(color):06x}" if isinstance(color, int) else None,
                            origin=origin,
                        )
                    )
                    span_refs.append(span_id)
                    line_span_ids.append(span_id)
                    line_bboxes.append(span_bbox_tuple)
                    line_text += text
                line_bbox_tuple = _coerce_bbox_tuple(line.get("bbox")) or _bbox_union(line_bboxes)
                line_text = _format_line_text(line)
                if line_text.strip() and _valid_bbox_tuple(line_bbox_tuple):
                    lines.append(
                        TextLineIR(
                            line_id=f"{page_id}:{block_id}:l{line_index}",
                            page_id=page_id,
                            block_id=block_id,
                            text=line_text.strip(),
                            bbox=_bbox_from_tuple(line_bbox_tuple),
                            span_ids=line_span_ids,
                        )
                    )
            page_blocks.append(
                DocumentBlock(
                    block_id=block_id,
                    page_id=page_id,
                    role=classify_role(
                        source_text,
                        page_index,
                        len(page_blocks),
                        avg_font_size,
                        bbox_tuple,
                        page.rect.height,
                    ),
                    bbox=BoundingBox(x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3]),
                    column=_assign_column(block, page.rect.width),
                    reading_order=len(page_blocks),
                    source_text=source_text,
                    span_refs=span_refs,
                    lines=lines,
                    spans=spans,
                    style_seed=StyleSeed(
                        font_size=avg_font_size,
                        font_name=font_names[0] if font_names else None,
                        bold=is_bold,
                        italic=is_italic,
                    ),
                )
            )

        page_blocks = _refine_caption_roles(page_blocks, page_assets)
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
    return normalize_document_formulas(DocumentIR(doc_id=doc_id, pages=pages))


def build_parser_diagnostics(document: DocumentIR) -> dict:
    formula_diagnostics = build_formula_diagnostics(document)
    role_counts: dict[str, int] = {}
    for page in document.pages:
        for block in page.blocks:
            role_counts[block.role.value] = role_counts.get(block.role.value, 0) + 1
    asset_counts: dict[str, int] = {}
    for page in document.pages:
        for asset in page.assets:
            asset_counts[asset.kind] = asset_counts.get(asset.kind, 0) + 1

    fallback_flags: list[str] = []
    fallback_flags.extend(document.quality_flags)
    if role_counts.get(BlockRole.TABLE.value, 0):
        fallback_flags.append("table_text_fallback")
    if role_counts.get(BlockRole.FORMULA.value, 0):
        if formula_diagnostics["formula_count"]:
            fallback_flags.append("formula_placeholder_normalized")
        else:
            fallback_flags.append("formula_text_fallback")
    if asset_counts.get("image", 0):
        fallback_flags.append("raster_image_assets_preserved")
    if asset_counts.get("figure", 0):
        fallback_flags.append("vector_asset_placeholder")
        fallback_flags.append("vector_assets_not_rasterized")

    return {
        "kind": "parser_diagnostics",
        "extraction_backend": document.extraction_backend,
        "extraction_version": document.extraction_version,
        "extraction_quality_flags": document.quality_flags,
        "fallback_used": "pymupdf_fallback_used" in document.quality_flags,
        "page_count": len(document.pages),
        "text_block_count": sum(len(page.blocks) for page in document.pages),
        "asset_count": sum(len(page.assets) for page in document.pages),
        "role_counts": role_counts,
        "asset_counts": asset_counts,
        "formula_diagnostics": formula_diagnostics,
        "formula_fragment_cluster_count": formula_diagnostics.get(
            "formula_fragment_cluster_count",
            0,
        ),
        "formula_fragment_suppressed_block_count": formula_diagnostics.get(
            "formula_fragment_suppressed_block_count",
            0,
        ),
        "formula_fragment_clusters": formula_diagnostics.get(
            "formula_fragment_clusters",
            [],
        ),
        "fallback_flags": fallback_flags,
        "unsupported_features": [
            {
                "kind": "vector_assets",
                "status": "placeholder_only",
                "message": "Vector graphics are carried as page-positioned placeholders; raster image assets and text-level formulas/tables are preserved as fallbacks.",
            }
        ],
    }
