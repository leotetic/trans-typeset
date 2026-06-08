from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Any

from pdf_translator_schema import DocumentIR, TranslationLayoutPlan
from pdf_translator_schema.models import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentBlock,
    PageSize,
    RenderDefaults,
    StyleSeed,
)


_SCHEMA_RENDER_DEFAULTS = RenderDefaults()
_CONTINUATION_MARGIN_PT = 54.0


def _bbox_width(bbox: BoundingBox) -> float:
    return bbox.x1 - bbox.x0


def _bbox_height(bbox: BoundingBox) -> float:
    return bbox.y1 - bbox.y0


def _bbox_area(bbox: BoundingBox) -> float:
    return _bbox_width(bbox) * _bbox_height(bbox)


def _bbox_overlap_area(a: BoundingBox, b: BoundingBox) -> float:
    x_overlap = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    y_overlap = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    return x_overlap * y_overlap


def _glyph_width_factor(char: str) -> float:
    codepoint = ord(char)
    if char == "\t":
        return 2.0
    if char.isspace():
        return 0.34
    if (
        0x1100 <= codepoint <= 0x11FF
        or 0x2E80 <= codepoint <= 0xA4CF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0xFE30 <= codepoint <= 0xFE6F
        or 0xFF00 <= codepoint <= 0xFFEF
    ):
        return 1.0
    if char.isupper():
        return 0.64
    if char.isdigit():
        return 0.56
    if char.isascii():
        return 0.52
    return 0.72


def _estimated_line_count(text: str, width_pt: float, font_size_pt: float) -> int:
    if not text:
        return 0
    if width_pt <= 0 or font_size_pt <= 0:
        return 1

    lines = 0
    for hard_line in text.splitlines() or [text]:
        line_units = sum(_glyph_width_factor(char) for char in hard_line)
        line_width = line_units * font_size_pt
        lines += max(1, ceil(line_width / width_pt))
    return lines


def _text_overflows(
    text: str,
    bbox: BoundingBox,
    font_size_pt: float,
    line_height: float,
) -> bool:
    return _estimated_text_height(text, bbox, font_size_pt, line_height) > _bbox_height(bbox)


def _estimated_text_height(
    text: str,
    bbox: BoundingBox,
    font_size_pt: float,
    line_height: float,
) -> float:
    estimated_lines = _estimated_line_count(text, _bbox_width(bbox), font_size_pt)
    if estimated_lines == 0:
        return 0.0
    return estimated_lines * font_size_pt * line_height


def _line_capacity(bbox: BoundingBox, font_size_pt: float) -> int:
    if font_size_pt <= 0:
        return 1
    return max(1, int(_bbox_width(bbox) / font_size_pt))


def _line_count_capacity(bbox: BoundingBox, font_size_pt: float, line_height: float) -> int:
    if font_size_pt <= 0 or line_height <= 0:
        return 1
    return max(1, int(_bbox_height(bbox) / (font_size_pt * line_height)))


def _split_text_to_fit(
    text: str,
    bbox: BoundingBox,
    font_size_pt: float,
    line_height: float,
) -> tuple[str, str]:
    if not _text_overflows(text, bbox, font_size_pt, line_height):
        return text, ""

    max_lines = _line_count_capacity(bbox, font_size_pt, line_height)
    max_units = _line_capacity(bbox, font_size_pt)
    lines_used = 1
    units_used = 0.0
    split_index = 0

    for index, char in enumerate(text):
        if char == "\n":
            lines_used += 1
            units_used = 0.0
        else:
            char_units = _glyph_width_factor(char)
            if units_used > 0 and units_used + char_units > max_units:
                lines_used += 1
                units_used = 0.0
            units_used += char_units
        if lines_used > max_lines:
            break
        split_index = index + 1

    if split_index <= 0:
        split_index = 1

    whitespace_index = max(
        text.rfind(" ", 0, split_index),
        text.rfind("\n", 0, split_index),
        text.rfind("。", 0, split_index),
        text.rfind("，", 0, split_index),
        text.rfind(".", 0, split_index),
        text.rfind(",", 0, split_index),
    )
    if whitespace_index >= max(1, int(split_index * 0.6)):
        split_index = whitespace_index + 1

    return text[:split_index].rstrip(), text[split_index:].lstrip()


def _unique_flags(flags: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for flag in flags:
        if flag and flag not in seen:
            unique.append(flag)
            seen.add(flag)
    return unique


def _continuation_bbox(size: PageSize) -> BoundingBox:
    x0 = min(_CONTINUATION_MARGIN_PT, max(0.0, size.width / 8))
    y0 = min(_CONTINUATION_MARGIN_PT, max(0.0, size.height / 8))
    x1 = max(x0 + 1, size.width - x0)
    y1 = max(y0 + 1, size.height - y0)
    return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)


def _expand_bbox_to_fit(
    text: str,
    bbox: BoundingBox,
    page_size: PageSize,
    font_size_pt: float,
    line_height: float,
) -> BoundingBox | None:
    needed_height = _estimated_text_height(text, bbox, font_size_pt, line_height)
    if needed_height <= _bbox_height(bbox):
        return bbox
    page_bottom = max(bbox.y1, page_size.height - _CONTINUATION_MARGIN_PT)
    expanded_y1 = min(page_bottom, bbox.y0 + needed_height)
    if expanded_y1 > bbox.y1 and expanded_y1 - bbox.y0 >= needed_height:
        return BoundingBox(x0=bbox.x0, y0=bbox.y0, x1=bbox.x1, y1=expanded_y1)
    return None


@dataclass(frozen=True)
class RenderBlock:
    block_id: str
    role: BlockRole
    bbox: BoundingBox
    text: str
    style_seed: StyleSeed
    font_size_pt: float
    font_scale: float = 1.0
    render_intent: str = "normal"
    quality_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenderAsset:
    asset_id: str
    kind: str
    bbox: BoundingBox
    path: str | None = None
    alt_text: str | None = None
    quality_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenderPage:
    page_id: str
    size: PageSize
    blocks: list[RenderBlock]
    assets: list[RenderAsset] = field(default_factory=list)


@dataclass(frozen=True)
class RenderDocument:
    doc_id: str
    target_lang: str
    pages: list[RenderPage]
    font_stack: list[str] = field(
        default_factory=lambda: _SCHEMA_RENDER_DEFAULTS.font_stack.copy()
    )
    line_height: float = _SCHEMA_RENDER_DEFAULTS.line_height
    paragraph_spacing_em: float = _SCHEMA_RENDER_DEFAULTS.paragraph_spacing_em

    def layout_issues(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for page in self.pages:
            positioned_items: list[tuple[str, str, BoundingBox]] = []
            for block in page.blocks:
                if block.bbox.x1 > page.size.width or block.bbox.y1 > page.size.height:
                    issues.append(
                        {
                            "kind": "bbox_outside_page",
                            "page_id": page.page_id,
                            "item_id": block.block_id,
                            "item_type": "block",
                        }
                    )
                if not block.text.strip():
                    issues.append(
                        {
                            "kind": "empty_render_block",
                            "page_id": page.page_id,
                            "item_id": block.block_id,
                            "item_type": "block",
                        }
                    )
                positioned_items.append(("block", block.block_id, block.bbox))
            for asset in page.assets:
                if asset.bbox.x1 > page.size.width or asset.bbox.y1 > page.size.height:
                    issues.append(
                        {
                            "kind": "bbox_outside_page",
                            "page_id": page.page_id,
                            "item_id": asset.asset_id,
                            "item_type": "asset",
                        }
                    )
                positioned_items.append(("asset", asset.asset_id, asset.bbox))

            for index, item in enumerate(positioned_items):
                item_type, item_id, item_bbox = item
                item_area = _bbox_area(item_bbox)
                if item_area <= 0:
                    continue
                for other_type, other_id, other_bbox in positioned_items[index + 1 :]:
                    overlap_area = _bbox_overlap_area(item_bbox, other_bbox)
                    if overlap_area <= 0:
                        continue
                    overlap_ratio = overlap_area / min(item_area, _bbox_area(other_bbox))
                    if overlap_ratio >= 0.12:
                        issues.append(
                            {
                                "kind": "overlap",
                                "page_id": page.page_id,
                                "item_id": item_id,
                                "item_type": item_type,
                                "other_id": other_id,
                                "other_type": other_type,
                                "overlap_ratio": round(overlap_ratio, 4),
                            }
                        )
        return issues

    def diagnostics(self) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        quality_flag_counts: dict[str, int] = {}
        block_count = 0
        for page in self.pages:
            page_flags: list[dict[str, Any]] = []
            asset_flags: list[dict[str, Any]] = []
            for block in page.blocks:
                block_count += 1
                for flag in block.quality_flags:
                    quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
                if block.quality_flags:
                    page_flags.append(
                        {
                            "block_id": block.block_id,
                            "role": block.role.value,
                            "render_intent": block.render_intent,
                            "font_scale": block.font_scale,
                            "quality_flags": block.quality_flags,
                        }
                    )
            for asset in page.assets:
                for flag in asset.quality_flags:
                    quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
                if asset.quality_flags:
                    asset_flags.append(
                        {
                            "asset_id": asset.asset_id,
                            "kind": asset.kind,
                            "quality_flags": asset.quality_flags,
                        }
                    )
            pages.append(
                {
                    "page_id": page.page_id,
                    "block_count": len(page.blocks),
                    "asset_count": len(page.assets),
                    "flagged_blocks": page_flags,
                    "flagged_assets": asset_flags,
                }
            )
        return {
            "doc_id": self.doc_id,
            "target_lang": self.target_lang,
            "page_count": len(self.pages),
            "block_count": block_count,
            "quality_flag_counts": quality_flag_counts,
            "layout_issues": self.layout_issues(),
            "pages": pages,
        }

    @classmethod
    def from_ir_and_plans(
        cls,
        document: DocumentIR,
        plans: list[TranslationLayoutPlan],
        target_lang: str,
        render_defaults: RenderDefaults | None = None,
    ) -> RenderDocument:
        defaults = (
            render_defaults.model_copy(update={"target_lang": target_lang}, deep=True)
            if render_defaults is not None
            else RenderDefaults(target_lang=target_lang)
        )
        min_font_scale = float(defaults.overflow_policy.min_font_scale)
        compact_font_scale = max(min_font_scale, 0.92)
        line_height = defaults.line_height
        overflow_policy = defaults.overflow_policy
        translations = {
            block.source_block_id: block
            for plan in plans
            for block in plan.blocks
        }
        pages: list[RenderPage] = []
        for page in document.pages:
            render_blocks: list[RenderBlock] = []
            continuation_blocks: list[RenderBlock] = []
            for block in page.blocks:
                plan = translations.get(block.block_id)
                quality_flags: list[str]
                if plan is None:
                    text = block.source_text
                    render_intent = "normal"
                    quality_flags = ["missing_translation"]
                else:
                    text = plan.translated_text if plan.translated_text.strip() else block.source_text
                    render_intent = plan.render_intent
                    quality_flags = list(plan.quality_flags)
                    if not plan.translated_text.strip():
                        quality_flags.append("empty_translation")
                    if plan.role != block.role:
                        quality_flags.append("role_mismatch")

                font_scale = 1.0
                if render_intent == "compact":
                    font_scale = compact_font_scale
                font_size_pt = block.style_seed.font_size * font_scale

                render_bbox = block.bbox

                if _text_overflows(text, render_bbox, font_size_pt, line_height):
                    if font_scale > min_font_scale and overflow_policy.strategy != "continue_without_scaling":
                        font_scale = min_font_scale
                        font_size_pt = block.style_seed.font_size * font_scale
                        quality_flags.append("font_scaled")
                    if _text_overflows(text, render_bbox, font_size_pt, line_height):
                        expanded_bbox = (
                            _expand_bbox_to_fit(
                                text,
                                render_bbox,
                                page.size,
                                font_size_pt,
                                line_height,
                            )
                            if overflow_policy.allow_box_expansion
                            and overflow_policy.strategy == "scale_then_expand_then_continue"
                            else None
                        )
                        if expanded_bbox is not None:
                            render_bbox = expanded_bbox
                            quality_flags.append("box_expanded")
                    if _text_overflows(text, render_bbox, font_size_pt, line_height):
                        if overflow_policy.allow_continuation_page:
                            visible_text, overflow_text = _split_text_to_fit(
                                text,
                                render_bbox,
                                font_size_pt,
                                line_height,
                            )
                            if overflow_text:
                                text = visible_text
                                quality_flags.append("continued_on_next_page")
                                continuation_blocks.extend(
                                    _make_continuation_blocks(
                                        block,
                                        overflow_text,
                                        page.size,
                                        font_size_pt,
                                        font_scale,
                                        render_intent,
                                        line_height,
                                    )
                                )
                            else:
                                quality_flags.append("overflow_clipped")
                        else:
                            quality_flags.append("overflow_clipped")

                render_blocks.append(
                    RenderBlock(
                        block_id=block.block_id,
                        role=block.role,
                        bbox=render_bbox,
                        text=text,
                        style_seed=block.style_seed,
                        font_size_pt=font_size_pt,
                        font_scale=font_scale,
                        render_intent=render_intent,
                        quality_flags=_unique_flags(quality_flags),
                    )
                )
            pages.append(
                RenderPage(
                    page_id=page.page_id,
                    size=page.size,
                    blocks=render_blocks,
                    assets=_render_assets(page.assets),
                )
            )
            for index, continuation_block in enumerate(continuation_blocks, start=1):
                pages.append(
                    RenderPage(
                        page_id=f"{page.page_id}_cont_{index:02d}",
                        size=page.size,
                        blocks=[continuation_block],
                    )
                )
        return cls(
            doc_id=document.doc_id,
            target_lang=target_lang,
            pages=pages,
            font_stack=defaults.font_stack,
            line_height=defaults.line_height,
            paragraph_spacing_em=defaults.paragraph_spacing_em,
        )


def _render_assets(assets: list[Asset]) -> list[RenderAsset]:
    render_assets: list[RenderAsset] = []
    for asset in assets:
        quality_flags: list[str] = []
        if not asset.path:
            quality_flags.append("asset_missing_path")
        render_assets.append(
            RenderAsset(
                asset_id=asset.asset_id,
                kind=asset.kind,
                bbox=asset.bbox,
                path=asset.path,
                alt_text=asset.alt_text,
                quality_flags=quality_flags,
            )
        )
    return render_assets


def _make_continuation_blocks(
    source_block: DocumentBlock,
    text: str,
    page_size: PageSize,
    font_size_pt: float,
    font_scale: float,
    render_intent: str,
    line_height: float,
) -> list[RenderBlock]:
    bbox = _continuation_bbox(page_size)
    blocks: list[RenderBlock] = []
    remaining = text
    index = 1
    while remaining:
        visible_text, overflow_text = _split_text_to_fit(
            remaining,
            bbox,
            font_size_pt,
            line_height,
        )
        if not visible_text:
            visible_text = remaining
            overflow_text = ""
        flags = ["continuation_page", "continued_from_overflow"]
        if overflow_text:
            flags.append("continued_on_next_page")
        blocks.append(
            RenderBlock(
                block_id=f"{source_block.block_id}__cont_{index:02d}",
                role=source_block.role,
                bbox=bbox,
                text=visible_text,
                style_seed=source_block.style_seed,
                font_size_pt=font_size_pt,
                font_scale=font_scale,
                render_intent=render_intent,
                quality_flags=flags,
            )
        )
        remaining = overflow_text
        index += 1
    return blocks
