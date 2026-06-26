from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    FormulaIR,
    FormulaSourceKind,
    PageSize,
    PdfFormula,
    StyleSeed,
)
from pdf_translator_schema.models import DocumentBlock, TextLineIR, TextSpanIR

from .formulas.normalization import normalize_pdf_text
from .parser import classify_role

_AUXILIARY_TYPES = {
    "header",
    "footer",
    "page_header",
    "page_footer",
    "page_number",
    "aside_text",
    "page_aside_text",
}
_CAPTION_TYPES = {
    "image_caption",
    "table_caption",
    "chart_caption",
    "code_caption",
}
_FOOTNOTE_TYPES = {"image_footnote", "table_footnote", "chart_footnote", "page_footnote"}
_FORMULA_TYPES = {"equation", "interline_equation", "equation_interline"}
_INLINE_FORMULA_TYPES = {"inline_equation", "equation_inline"}
_VISUAL_TYPES = {"image", "chart"}
_TABLE_TYPES = {"table"}
_TEXT_TYPES = {"text", "title", "paragraph", "list", "index", "ref_text"}


class MinerUAdapterError(RuntimeError):
    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class MinerUParseResult:
    document: DocumentIR
    diagnostics: dict[str, Any]
    artifacts: dict[str, Any]


@dataclass(frozen=True)
class _SpanPayload:
    text: str
    span_type: str
    bbox: BoundingBox | None = None


@dataclass(frozen=True)
class _LinePayload:
    text: str
    bbox: BoundingBox | None
    spans: list[_SpanPayload]


def parse_pdf_with_mineru(
    pdf_path: Path,
    doc_id: str,
    *,
    asset_output_dir: Path | None,
    output_dir: Path,
    backend: str = "pipeline",
    method: str = "auto",
    formula_enabled: bool = True,
    table_enabled: bool = True,
    timeout_seconds: int = 3600,
) -> MinerUParseResult:
    pdf_path = pdf_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    mineru_executable = _resolve_mineru_executable()
    invocation = {
        "mineru_executable": mineru_executable,
        "mineru_executable_exists": Path(mineru_executable).is_file()
        if Path(mineru_executable).is_absolute()
        else shutil.which(mineru_executable) is not None,
        "cwd": str(output_dir),
        "input_path": str(pdf_path),
        "input_path_exists": pdf_path.exists(),
        "output_path": str(output_dir),
        "output_path_exists": output_dir.exists(),
    }
    command = [
        mineru_executable,
        "-p",
        str(pdf_path),
        "-o",
        str(output_dir),
        "-b",
        backend,
        "-m",
        method,
        "-f",
        str(formula_enabled).lower(),
        "-t",
        str(table_enabled).lower(),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=output_dir,
            capture_output=True,
            text=True,
            timeout=max(1, timeout_seconds),
            check=False,
        )
    except FileNotFoundError as exc:
        raise MinerUAdapterError(
            "MinerU CLI was not found in the project environment",
            diagnostics={"invocation": invocation},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MinerUAdapterError(
            f"MinerU timed out after {timeout_seconds} seconds",
            diagnostics={"invocation": invocation, "timeout_seconds": timeout_seconds},
        ) from exc

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise MinerUAdapterError(
            f"MinerU exited with status {completed.returncode}: {stderr[:500]}",
            diagnostics={
                "invocation": invocation,
                "returncode": completed.returncode,
                "stdout_tail": (completed.stdout or "")[-2000:],
                "stderr_tail": (completed.stderr or "")[-2000:],
            },
        )

    middle_path = _find_output_file(output_dir, "_middle.json", "middle.json")
    if middle_path is None:
        raise MinerUAdapterError("MinerU completed without a middle.json output")
    content_list_path = _find_output_file(output_dir, "_content_list.json", "content_list.json")
    content_list_v2_path = _find_output_file(
        output_dir,
        "_content_list_v2.json",
        "content_list_v2.json",
    )
    middle = _read_json(middle_path)
    content_list = _read_json(content_list_path) if content_list_path is not None else None
    content_list_v2 = _read_json(content_list_v2_path) if content_list_v2_path is not None else None

    document, adapter_diagnostics = _document_from_mineru_outputs(
        doc_id=doc_id,
        pdf_path=pdf_path,
        output_root=output_dir,
        asset_output_dir=asset_output_dir,
        middle=middle,
        content_list=content_list,
        content_list_v2=content_list_v2,
    )
    diagnostics = {
        "kind": "mineru_diagnostics",
        "status": "completed",
        "backend": backend,
        "method": method,
        "formula_enabled": formula_enabled,
        "table_enabled": table_enabled,
        "invocation": invocation,
        "duration_ms": duration_ms,
        "returncode": completed.returncode,
        "stdout_tail": (completed.stdout or "")[-2000:],
        "stderr_tail": (completed.stderr or "")[-2000:],
        "middle_path": str(middle_path),
        "content_list_path": str(content_list_path) if content_list_path else None,
        "content_list_v2_path": str(content_list_v2_path) if content_list_v2_path else None,
        **adapter_diagnostics,
    }
    return MinerUParseResult(
        document=document,
        diagnostics=diagnostics,
        artifacts={
            "mineru-middle": middle,
            "mineru-content-list": content_list,
            "mineru-content-list-v2": content_list_v2,
        },
    )


def _resolve_mineru_executable() -> str:
    candidates = [Path(sys.executable).with_name("mineru")]
    for parent in [Path.cwd(), *Path(__file__).resolve().parents]:
        candidates.append(parent / ".venv" / "bin" / "mineru")
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return str(resolved)
    return shutil.which("mineru") or "mineru"


def _find_output_file(root: Path, suffix: str, exact_name: str) -> Path | None:
    candidates = [
        path
        for path in root.rglob("*.json")
        if path.name == exact_name or path.name.endswith(suffix)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _document_from_mineru_outputs(
    *,
    doc_id: str,
    pdf_path: Path,
    output_root: Path,
    asset_output_dir: Path | None,
    middle: Any,
    content_list: Any,
    content_list_v2: Any,
) -> tuple[DocumentIR, dict[str, Any]]:
    pdf_info = middle.get("pdf_info") if isinstance(middle, dict) else None
    if not isinstance(pdf_info, list):
        pdf_info = []
    fallback_page_sizes = _pdf_page_sizes(pdf_path)
    content_v2_pages = content_list_v2 if isinstance(content_list_v2, list) else []
    page_count = max(len(pdf_info), len(content_v2_pages), len(fallback_page_sizes))
    if page_count <= 0:
        raise MinerUAdapterError("MinerU output did not contain any pages")

    pages: list[DocumentPage] = []
    formulas: list[FormulaIR] = []
    seen_block_ids: set[str] = set()
    copied_asset_count = 0
    discarded_count = 0
    role_counts: dict[str, int] = {}
    asset_counts: dict[str, int] = {}

    for page_index in range(page_count):
        page_info = pdf_info[page_index] if page_index < len(pdf_info) else {}
        if not isinstance(page_info, dict):
            page_info = {}
        page_id = f"p{page_index + 1:04d}"
        width, height = _page_size(page_info, fallback_page_sizes, page_index)
        page_size = PageSize(width=width, height=height)
        items = _page_items(page_info, content_v2_pages, page_index)
        page_blocks: list[DocumentBlock] = []
        page_assets: list[Asset] = []
        seen_item_keys: set[str] = set()

        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = _item_type(item)
            item_key = _item_key(item)
            if item_key in seen_item_keys:
                continue
            seen_item_keys.add(item_key)
            bbox = _bbox_from_item(item, page_size)
            if bbox is None:
                continue
            if item_type in _AUXILIARY_TYPES:
                discarded_count += 1
                continue
            if item_type in _VISUAL_TYPES:
                asset, copied = _asset_from_item(
                    doc_id,
                    page_id,
                    item,
                    bbox,
                    output_root,
                    asset_output_dir,
                    kind="figure",
                )
                page_assets.append(asset)
                copied_asset_count += int(copied)
                asset_counts[asset.kind] = asset_counts.get(asset.kind, 0) + 1
                continue
            if item_type in _TABLE_TYPES:
                asset, copied = _asset_from_item(
                    doc_id,
                    page_id,
                    item,
                    bbox,
                    output_root,
                    asset_output_dir,
                    kind="table",
                )
                page_assets.append(asset)
                copied_asset_count += int(copied)
                asset_counts[asset.kind] = asset_counts.get(asset.kind, 0) + 1
                block_text = _table_text(item)
                if not block_text:
                    block_text = "Table"
                block = _document_block(
                    page_id=page_id,
                    item=item,
                    bbox=bbox,
                    source_text=block_text,
                    text_for_translation=block_text,
                    role=BlockRole.TABLE,
                    reading_order=len(page_blocks),
                    seen_block_ids=seen_block_ids,
                )
                page_blocks.append(block)
                role_counts[block.role.value] = role_counts.get(block.role.value, 0) + 1
                continue
            if item_type in _FORMULA_TYPES:
                block, formula, asset, copied = _display_formula_from_item(
                    doc_id,
                    page_id,
                    page_index,
                    item,
                    bbox,
                    output_root,
                    asset_output_dir,
                    reading_order=len(page_blocks),
                    seen_block_ids=seen_block_ids,
                )
                page_blocks.append(block)
                formulas.append(formula)
                if asset is not None:
                    page_assets.append(asset)
                    copied_asset_count += int(copied)
                    asset_counts[asset.kind] = asset_counts.get(asset.kind, 0) + 1
                role_counts[block.role.value] = role_counts.get(block.role.value, 0) + 1
                continue

            block, inline_formulas = _text_block_from_item(
                page_id=page_id,
                page_index=page_index,
                item=item,
                bbox=bbox,
                page_size=page_size,
                reading_order=len(page_blocks),
                seen_block_ids=seen_block_ids,
            )
            if block is None:
                continue
            page_blocks.append(block)
            formulas.extend(inline_formulas)
            role_counts[block.role.value] = role_counts.get(block.role.value, 0) + 1

        pages.append(DocumentPage(page_id=page_id, size=page_size, blocks=page_blocks, assets=page_assets))

    document = DocumentIR(
        doc_id=doc_id,
        pages=pages,
        formulas=formulas,
        extraction_backend="mineru",
        extraction_version=str(middle.get("_version_name") or "")
        if isinstance(middle, dict)
        else None,
        quality_flags=["mineru_extraction"],
    )
    return document, {
        "page_count": len(document.pages),
        "text_block_count": sum(len(page.blocks) for page in document.pages),
        "formula_count": len(document.formulas),
        "asset_count": sum(len(page.assets) for page in document.pages),
        "copied_asset_count": copied_asset_count,
        "discarded_block_count": discarded_count,
        "role_counts": role_counts,
        "asset_counts": asset_counts,
    }


def _pdf_page_sizes(pdf_path: Path) -> list[tuple[float, float]]:
    try:
        import fitz
    except Exception:
        return []
    try:
        with fitz.open(pdf_path) as document:
            return [(float(page.rect.width), float(page.rect.height)) for page in document]
    except Exception:
        return []


def _page_size(
    page_info: dict[str, Any],
    fallback_page_sizes: list[tuple[float, float]],
    page_index: int,
) -> tuple[float, float]:
    raw = page_info.get("page_size")
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            width = float(raw[0])
            height = float(raw[1])
            if width > 0 and height > 0:
                return width, height
        except (TypeError, ValueError):
            pass
    if page_index < len(fallback_page_sizes):
        return fallback_page_sizes[page_index]
    return 612.0, 792.0


def _page_items(
    page_info: dict[str, Any],
    content_v2_pages: list[Any],
    page_index: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in ("para_blocks", "interline_equations", "tables", "images"):
        value = page_info.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    if items:
        return _sort_page_items(items)
    if page_index < len(content_v2_pages) and isinstance(content_v2_pages[page_index], list):
        items.extend(item for item in content_v2_pages[page_index] if isinstance(item, dict))
    return _sort_page_items(items)


def _sort_page_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            int(item.get("index", 10**6)) if isinstance(item.get("index"), int) else 10**6,
            _bbox_sort_key(item),
        ),
    )


def _bbox_sort_key(item: dict[str, Any]) -> tuple[float, float]:
    bbox = item.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        try:
            return float(bbox[1]), float(bbox[0])
        except (TypeError, ValueError):
            pass
    return 0.0, 0.0


def _item_key(item: dict[str, Any]) -> str:
    return json.dumps(
        {
            "type": _item_type(item),
            "bbox": item.get("bbox"),
            "index": item.get("index"),
            "text": _item_text(item)[:80],
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _item_type(item: dict[str, Any]) -> str:
    value = item.get("type")
    return str(value or "").strip().lower()


def _bbox_from_item(item: dict[str, Any], page_size: PageSize) -> BoundingBox | None:
    bbox = item.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    except (TypeError, ValueError):
        return None
    max_value = max(abs(x0), abs(y0), abs(x1), abs(y1))
    if 0 <= max_value <= 1.0:
        x0, x1 = x0 * page_size.width, x1 * page_size.width
        y0, y1 = y0 * page_size.height, y1 * page_size.height
    elif (
        max_value <= 1000.0
        and (x1 > page_size.width + 1 or y1 > page_size.height + 1)
    ):
        x0, x1 = x0 / 1000.0 * page_size.width, x1 / 1000.0 * page_size.width
        y0, y1 = y0 / 1000.0 * page_size.height, y1 / 1000.0 * page_size.height
    x0 = min(max(0.0, x0), page_size.width)
    x1 = min(max(0.0, x1), page_size.width)
    y0 = min(max(0.0, y0), page_size.height)
    y1 = min(max(0.0, y1), page_size.height)
    if x1 <= x0 or y1 <= y0:
        return None
    return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)


def _asset_from_item(
    doc_id: str,
    page_id: str,
    item: dict[str, Any],
    bbox: BoundingBox,
    output_root: Path,
    asset_output_dir: Path | None,
    *,
    kind: str,
    formula_id: str | None = None,
) -> tuple[Asset, bool]:
    asset_id = _stable_id(page_id, kind, _bbox_key(bbox), _item_text(item))[:24]
    source_path = _resolve_output_path(output_root, str(item.get("img_path") or ""))
    asset_path: str | None = None
    copied = False
    if source_path is not None and asset_output_dir is not None:
        asset_output_dir.mkdir(parents=True, exist_ok=True)
        suffix = source_path.suffix if source_path.suffix else ".png"
        target = asset_output_dir / f"{asset_id}{suffix}"
        shutil.copyfile(source_path, target)
        asset_path = f"/api/documents/{doc_id}/assets/{target.name}"
        copied = True
    return (
        Asset(
            asset_id=asset_id,
            page_id=page_id,
            kind=kind,  # type: ignore[arg-type]
            bbox=bbox,
            path=asset_path,
            alt_text=_item_text(item) or f"MinerU {kind} asset",
            formula_id=formula_id,
        ),
        copied,
    )


def _resolve_output_path(output_root: Path, value: str) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    candidates = [candidate] if candidate.is_absolute() else [output_root / candidate]
    candidates.extend(output_root.rglob(candidate.name))
    for path in candidates:
        if path.is_file():
            return path
    return None


def _display_formula_from_item(
    doc_id: str,
    page_id: str,
    page_index: int,
    item: dict[str, Any],
    bbox: BoundingBox,
    output_root: Path,
    asset_output_dir: Path | None,
    *,
    reading_order: int,
    seen_block_ids: set[str],
) -> tuple[DocumentBlock, FormulaIR, Asset | None, bool]:
    latex = _strip_latex_delimiters(_item_text(item))
    formula_id = _stable_id(page_id, "formula", _bbox_key(bbox), latex)
    token = f"{{{{formula:{formula_id}}}}}"
    block = _document_block(
        page_id=page_id,
        item=item,
        bbox=bbox,
        source_text=token,
        text_for_translation=token,
        role=BlockRole.FORMULA,
        reading_order=reading_order,
        seen_block_ids=seen_block_ids,
        formula_id=formula_id,
    )
    asset: Asset | None = None
    copied = False
    asset_path = _resolve_output_path(output_root, str(item.get("img_path") or ""))
    asset_id: str | None = None
    if asset_path is not None:
        asset, copied = _asset_from_item(
            doc_id,
            page_id,
            item,
            bbox,
            output_root,
            asset_output_dir,
            kind="formula",
            formula_id=formula_id,
        )
        asset_id = asset.asset_id
    formula = FormulaIR(
        formula_id=formula_id,
        page_id=page_id,
        source_block_id=block.block_id,
        asset_id=asset_id,
        latex=latex,
        source_text=latex,
        display_mode="display",
        confidence=0.95,
        ocr_provider="mineru",
        source_kind=FormulaSourceKind.MINERU,
        pdf_formula=_source_clip_formula(page_id, page_index, bbox),
        quality_flags=_unique(
            [
                "mineru_formula",
                "formula_source_clip_replay",
                "formula_source_preserved",
                "formula_source_asset_primary" if asset_id is not None else "",
            ]
        ),
    )
    return block, formula, asset, copied


def _text_block_from_item(
    *,
    page_id: str,
    page_index: int,
    item: dict[str, Any],
    bbox: BoundingBox,
    page_size: PageSize,
    reading_order: int,
    seen_block_ids: set[str],
) -> tuple[DocumentBlock | None, list[FormulaIR]]:
    lines = _line_payloads(item, page_size)
    if not lines:
        text = _item_text(item)
        if not text.strip():
            return None, []
        lines = [_LinePayload(text=text, bbox=bbox, spans=[_SpanPayload(text=text, span_type="text", bbox=bbox)])]
    source_text = normalize_pdf_text(" ".join(line.text for line in lines))
    if not source_text:
        return None, []
    item_type = _item_type(item)
    role = _role_for_item(item, source_text, page_index, reading_order, bbox)
    block_id = _unique_block_id(
        _stable_id(page_id, "block", _bbox_key(bbox), source_text),
        seen_block_ids,
    )
    text_for_translation_parts: list[str] = []
    formulas: list[FormulaIR] = []
    rendered_lines: list[TextLineIR] = []
    rendered_spans: list[TextSpanIR] = []
    span_refs: list[str] = []
    source_cursor = 0
    for line_index, line in enumerate(lines):
        line_span_ids: list[str] = []
        line_text_parts: list[str] = []
        for span_index, span in enumerate(line.spans):
            span_text = normalize_pdf_text(span.text)
            if not span_text:
                continue
            span_bbox = span.bbox or line.bbox or bbox
            span_id = f"{page_id}:{block_id}:l{line_index}:s{span_index}"
            line_id = f"{page_id}:{block_id}:l{line_index}"
            rendered_spans.append(
                TextSpanIR(
                    span_id=span_id,
                    page_id=page_id,
                    block_id=block_id,
                    line_id=line_id,
                    text=span_text,
                    bbox=span_bbox,
                )
            )
            span_refs.append(span_id)
            line_span_ids.append(span_id)
            if span.span_type in _INLINE_FORMULA_TYPES:
                formula_id = _stable_id(page_id, "inline_formula", block_id, _bbox_key(span_bbox), span_text)
                token = f"{{{{formula:{formula_id}}}}}"
                text_for_translation_parts.append(token)
                formulas.append(
                    FormulaIR(
                        formula_id=formula_id,
                        page_id=page_id,
                        anchor_block_id=block_id,
                        latex=_strip_latex_delimiters(span_text),
                        source_text=span_text,
                        source_text_range=(source_cursor, source_cursor + len(span_text)),
                        span_ids=[span_id],
                        display_mode="inline",
                        confidence=0.95,
                        ocr_provider="mineru",
                        source_kind=FormulaSourceKind.MINERU,
                        pdf_formula=_source_clip_formula(page_id, page_index, span_bbox),
                        quality_flags=[
                            "mineru_inline_formula",
                            "formula_source_clip_replay",
                            "formula_source_preserved",
                        ],
                    )
                )
                line_text_parts.append(span_text)
            else:
                text_for_translation_parts.append(span_text)
                line_text_parts.append(span_text)
            source_cursor += len(span_text)
        line_text = normalize_pdf_text(" ".join(line_text_parts))
        if line_text and line.bbox is not None:
            rendered_lines.append(
                TextLineIR(
                    line_id=f"{page_id}:{block_id}:l{line_index}",
                    page_id=page_id,
                    block_id=block_id,
                    text=line_text,
                    bbox=line.bbox,
                    span_ids=line_span_ids,
                )
            )
    text_for_translation = normalize_pdf_text(" ".join(text_for_translation_parts)) or source_text
    block = DocumentBlock(
        block_id=block_id,
        page_id=page_id,
        role=role,
        bbox=bbox,
        column=0,
        reading_order=reading_order,
        source_text=source_text,
        text_for_translation=text_for_translation,
        span_refs=span_refs,
        lines=rendered_lines,
        spans=rendered_spans,
        style_seed=StyleSeed(
            font_size=14.0 if item_type == "title" else 10.0,
            bold=item_type == "title",
        ),
    )
    return block, formulas


def _document_block(
    *,
    page_id: str,
    item: dict[str, Any],
    bbox: BoundingBox,
    source_text: str,
    text_for_translation: str,
    role: BlockRole,
    reading_order: int,
    seen_block_ids: set[str],
    formula_id: str | None = None,
) -> DocumentBlock:
    block_id = _unique_block_id(
        _stable_id(page_id, "block", _bbox_key(bbox), source_text),
        seen_block_ids,
    )
    return DocumentBlock(
        block_id=block_id,
        page_id=page_id,
        role=role,
        bbox=bbox,
        column=0,
        reading_order=reading_order,
        source_text=source_text,
        text_for_translation=text_for_translation,
        style_seed=StyleSeed(
            font_size=14.0 if _item_type(item) == "title" else 10.0,
            bold=_item_type(item) == "title",
        ),
        formula_id=formula_id,
    )


def _role_for_item(
    item: dict[str, Any],
    text: str,
    page_index: int,
    reading_order: int,
    bbox: BoundingBox,
) -> BlockRole:
    item_type = _item_type(item)
    if item_type == "title":
        return BlockRole.TITLE if page_index == 0 and reading_order == 0 else BlockRole.HEADING
    if item_type in _CAPTION_TYPES:
        return BlockRole.CAPTION
    if item_type in _FOOTNOTE_TYPES:
        return BlockRole.FOOTNOTE
    if item_type == "ref_text":
        return BlockRole.REFERENCE
    return classify_role(
        text,
        page_index,
        reading_order,
        14.0 if item_type == "title" else 10.0,
        (bbox.x0, bbox.y0, bbox.x1, bbox.y1),
        None,
    )


def _line_payloads(item: dict[str, Any], page_size: PageSize) -> list[_LinePayload]:
    raw_lines = item.get("lines")
    if not isinstance(raw_lines, list):
        raw_lines = []
        for child in item.get("blocks", []) if isinstance(item.get("blocks"), list) else []:
            if isinstance(child, dict) and isinstance(child.get("lines"), list):
                raw_lines.extend(child["lines"])
    lines: list[_LinePayload] = []
    for raw_line in raw_lines:
        if not isinstance(raw_line, dict):
            continue
        spans: list[_SpanPayload] = []
        for raw_span in raw_line.get("spans", []) if isinstance(raw_line.get("spans"), list) else []:
            if not isinstance(raw_span, dict):
                continue
            text = _span_text(raw_span)
            if not text:
                continue
            spans.append(
                _SpanPayload(
                    text=text,
                    span_type=str(raw_span.get("type") or "text").lower(),
                    bbox=_bbox_from_item(raw_span, page_size),
                )
            )
        line_text = normalize_pdf_text(" ".join(span.text for span in spans))
        if line_text:
            lines.append(
                _LinePayload(
                    text=line_text,
                    bbox=_bbox_from_item(raw_line, page_size),
                    spans=spans,
                )
            )
    if lines:
        return lines
    text = _item_text(item)
    return (
        [_LinePayload(text=text, bbox=_bbox_from_item(item, page_size), spans=[_SpanPayload(text=text, span_type="text", bbox=_bbox_from_item(item, page_size))])]
        if text.strip()
        else []
    )


def _span_text(span: dict[str, Any]) -> str:
    value = span.get("content")
    if value is None:
        value = span.get("text")
    return normalize_pdf_text(str(value or ""))


def _item_text(item: dict[str, Any]) -> str:
    for key in ("text", "content", "table_body", "code_body"):
        value = item.get(key)
        if isinstance(value, str):
            return normalize_pdf_text(_strip_html(value))
    content = item.get("content")
    if isinstance(content, dict):
        return normalize_pdf_text(_content_dict_text(content))
    texts: list[str] = []
    for key in (
        "image_caption",
        "table_caption",
        "chart_caption",
        "image_footnote",
        "table_footnote",
        "chart_footnote",
        "list_items",
    ):
        value = item.get(key)
        if isinstance(value, list):
            texts.extend(str(part) for part in value if str(part).strip())
    if texts:
        return normalize_pdf_text(" ".join(texts))
    line_texts: list[str] = []
    raw_lines = item.get("lines")
    if isinstance(raw_lines, list):
        for raw_line in raw_lines:
            if not isinstance(raw_line, dict):
                continue
            for raw_span in raw_line.get("spans", []) if isinstance(raw_line.get("spans"), list) else []:
                if isinstance(raw_span, dict):
                    line_texts.append(_span_text(raw_span))
    return normalize_pdf_text(" ".join(line_texts))


def _content_dict_text(content: dict[str, Any]) -> str:
    texts: list[str] = []
    for value in content.values():
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, list):
            texts.append(_content_list_text(value))
    return " ".join(texts)


def _content_list_text(values: list[Any]) -> str:
    texts: list[str] = []
    for value in values:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            raw = value.get("content")
            if isinstance(raw, str):
                texts.append(raw)
            elif isinstance(raw, list):
                texts.append(_content_list_text(raw))
            children = value.get("children")
            if isinstance(children, list):
                texts.append(_content_list_text(children))
    return " ".join(texts)


def _table_text(item: dict[str, Any]) -> str:
    for key in ("table_body", "content"):
        value = item.get(key)
        if isinstance(value, str):
            return normalize_pdf_text(_strip_html(value))
    return _item_text(item)


def _strip_html(value: str) -> str:
    return normalize_pdf_text(re.sub(r"<[^>]+>", " ", value))


def _strip_latex_delimiters(value: str) -> str:
    stripped = value.strip()
    stripped = re.sub(r"^\$\$\s*", "", stripped)
    stripped = re.sub(r"\s*\$\$$", "", stripped)
    stripped = re.sub(r"^\$\s*", "", stripped)
    stripped = re.sub(r"\s*\$$", "", stripped)
    return stripped.strip()


def _source_clip_formula(page_id: str, page_index: int, bbox: BoundingBox) -> PdfFormula:
    return PdfFormula(
        replay_kind="source_clip",
        source_page_id=page_id,
        source_page_index=page_index,
        source_bbox=bbox,
        width_pt=max(1.0, bbox.x1 - bbox.x0),
        height_pt=max(1.0, bbox.y1 - bbox.y0),
        primitives=[],
        quality_flags=["formula_source_clip_replay"],
    )


def _bbox_key(bbox: BoundingBox) -> str:
    return ",".join(f"{value:.2f}" for value in (bbox.x0, bbox.y0, bbox.x1, bbox.y1))


def _stable_id(*parts: object) -> str:
    digest = hashlib.sha1("|".join(str(part) for part in parts).encode()).hexdigest()
    return f"mineru_{digest[:16]}"


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _unique_block_id(base_block_id: str, seen_block_ids: set[str]) -> str:
    candidate = base_block_id
    suffix = 2
    while candidate in seen_block_ids:
        candidate = f"{base_block_id}_{suffix}"
        suffix += 1
    seen_block_ids.add(candidate)
    return candidate
