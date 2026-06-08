from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from math import ceil
from typing import Any

from pdf_translator_schema import (
    DocumentIR,
    LayoutIntentPlan,
    LayoutMode,
    TranslationLayoutPlan,
)
from pdf_translator_schema.models import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentBlock,
    Formula,
    PageSize,
    RenderDefaults,
    RoleStyleDefaults,
    StyleSeed,
)


_SCHEMA_RENDER_DEFAULTS = RenderDefaults()
_CONTINUATION_MARGIN_PT = 54.0
_MIN_FINAL_FRAGMENT_CHARS = 12
_MIN_FINAL_FRAGMENT_LINES = 2
_LOW_UTILIZATION_THRESHOLD = 0.18
_FORMULA_PLACEHOLDER_PATTERN = re.compile(r"@@FORMULA_[A-Za-z0-9_]+@@")


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


def _normalized_text(text: str) -> str:
    return " ".join(text.split())


def _enum_value(value: object) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


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
    html: str
    style_seed: StyleSeed
    font_size_pt: float
    font_scale: float = 1.0
    render_intent: str = "normal"
    text_align: str | None = None
    font_weight: int | None = None
    font_style: str | None = None
    first_line_indent_em: float = 0.0
    line_height: float | None = None
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
    footer_text: str | None = None


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
    layout_mode: str = _enum_value(_SCHEMA_RENDER_DEFAULTS.layout_mode)
    layout_trace: dict[str, Any] = field(default_factory=dict)

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
        formula_rendered_count = 0
        unresolved_formula_placeholders: list[dict[str, str]] = []
        page_utilization: list[dict[str, Any]] = []
        low_utilization_pages: list[str] = []
        single_fragment_pages: list[str] = []
        for page in self.pages:
            page_flags: list[dict[str, Any]] = []
            asset_flags: list[dict[str, Any]] = []
            text_area = 0.0
            for block in page.blocks:
                block_count += 1
                text_area += _bbox_area(block.bbox)
                formula_rendered_count += block.html.count("data-formula-id=")
                for placeholder in _FORMULA_PLACEHOLDER_PATTERN.findall(block.html):
                    unresolved_formula_placeholders.append(
                        {
                            "page_id": page.page_id,
                            "block_id": block.block_id,
                            "placeholder": placeholder,
                        }
                    )
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
            page_area = page.size.width * page.size.height
            utilization = round(text_area / page_area, 4) if page_area > 0 else 0.0
            page_utilization.append(
                {
                    "page_id": page.page_id,
                    "text_area_ratio": utilization,
                    "block_count": len(page.blocks),
                    "asset_count": len(page.assets),
                }
            )
            if page.blocks and utilization < _LOW_UTILIZATION_THRESHOLD:
                low_utilization_pages.append(page.page_id)
            if len(page.blocks) == 1:
                text = page.blocks[0].text.strip()
                estimated_lines = _estimated_line_count(
                    text,
                    _bbox_width(page.blocks[0].bbox),
                    page.blocks[0].font_size_pt,
                )
                if (
                    len(text) < _MIN_FINAL_FRAGMENT_CHARS
                    or estimated_lines < _MIN_FINAL_FRAGMENT_LINES
                ):
                    single_fragment_pages.append(page.page_id)
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
            "layout_mode": self.layout_mode,
            "page_count": len(self.pages),
            "block_count": block_count,
            "quality_flag_counts": quality_flag_counts,
            "layout_issues": self.layout_issues(),
            "formula_rendered_count": formula_rendered_count,
            "unresolved_formula_placeholders": unresolved_formula_placeholders,
            "page_utilization": page_utilization,
            "low_utilization_pages": low_utilization_pages,
            "single_fragment_pages": single_fragment_pages,
            "suppressed_artifacts": self.layout_trace.get("suppressed_artifacts", []),
            "pages": pages,
        }

    @classmethod
    def from_ir_and_plans(
        cls,
        document: DocumentIR,
        plans: list[TranslationLayoutPlan],
        target_lang: str,
        render_defaults: RenderDefaults | None = None,
        layout_intent_plan: LayoutIntentPlan | None = None,
    ) -> RenderDocument:
        defaults = (
            render_defaults.model_copy(update={"target_lang": target_lang}, deep=True)
            if render_defaults is not None
            else RenderDefaults(target_lang=target_lang)
        )
        if defaults.layout_mode == LayoutMode.CONTINUOUS_REFLOW:
            return _from_ir_and_plans_continuous_reflow(
                document,
                plans,
                target_lang,
                defaults,
                layout_intent_plan,
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
        layout_intents = {
            block.source_block_id: block
            for block in (layout_intent_plan.blocks if layout_intent_plan else [])
        }
        asset_usages = {
            asset.asset_id: asset.usage
            for asset in (layout_intent_plan.assets if layout_intent_plan else [])
        }
        pages: list[RenderPage] = []
        for page in document.pages:
            render_blocks: list[RenderBlock] = []
            continuation_blocks: list[RenderBlock] = []
            for block in page.blocks:
                plan = translations.get(block.block_id)
                layout_intent = layout_intents.get(block.block_id)
                quality_flags: list[str]
                if plan is None:
                    text = block.source_text
                    render_intent = (
                        layout_intent.render_intent if layout_intent is not None else "normal"
                    )
                    quality_flags = ["missing_translation"]
                else:
                    text = plan.translated_text if plan.translated_text.strip() else block.source_text
                    render_intent = (
                        layout_intent.render_intent if layout_intent is not None else plan.render_intent
                    )
                    quality_flags = list(plan.quality_flags)
                    if not plan.translated_text.strip():
                        quality_flags.append("empty_translation")
                    if plan.role != block.role:
                        quality_flags.append("role_mismatch")
                if layout_intent is not None:
                    quality_flags.extend(layout_intent.quality_flags)
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

                html, formula_flags = _formula_html_for_text(text, block.formulas)
                quality_flags.extend(formula_flags)

                render_blocks.append(
                    RenderBlock(
                        block_id=block.block_id,
                        role=block.role,
                        bbox=render_bbox,
                        text=text,
                        html=html,
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
                    assets=_render_assets(page.assets, asset_usages),
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
            layout_mode=_enum_value(defaults.layout_mode),
            layout_trace=_build_source_bbox_trace(document, pages, defaults),
        )


def _render_assets(
    assets: list[Asset],
    asset_usages: dict[str, str] | None = None,
) -> list[RenderAsset]:
    render_assets: list[RenderAsset] = []
    for asset in assets:
        usage = (asset_usages or {}).get(asset.asset_id, "preserve")
        if usage == "ignore":
            continue
        quality_flags: list[str] = []
        if usage != "preserve":
            quality_flags.append(f"asset_usage_{usage}")
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


def _from_ir_and_plans_continuous_reflow(
    document: DocumentIR,
    plans: list[TranslationLayoutPlan],
    target_lang: str,
    defaults: RenderDefaults,
    layout_intent_plan: LayoutIntentPlan | None,
) -> RenderDocument:
    translations = {
        block.source_block_id: block
        for plan in plans
        for block in plan.blocks
    }
    layout_intents = {
        block.source_block_id: block
        for block in (layout_intent_plan.blocks if layout_intent_plan else [])
    }
    asset_usages = {
        asset.asset_id: asset.usage
        for asset in (layout_intent_plan.assets if layout_intent_plan else [])
    }
    page_layout = defaults.page_layout
    page_size = PageSize(width=page_layout.width_pt, height=page_layout.height_pt)
    content_x0 = page_layout.margin_left_pt
    content_y0 = page_layout.margin_top_pt
    content_width = page_layout.width_pt - page_layout.margin_left_pt - page_layout.margin_right_pt
    content_bottom = page_layout.height_pt - page_layout.margin_bottom_pt

    pages: list[RenderPage] = []
    current_blocks: list[RenderBlock] = []
    current_assets: list[RenderAsset] = []
    cursor_y = content_y0
    page_index = 1
    block_traces: list[dict[str, Any]] = []
    asset_traces: list[dict[str, Any]] = []
    suppressed_artifacts: list[dict[str, Any]] = []
    source_page_ids: set[str] = set()
    rendered_source_ids: set[str] = set()

    def finish_page() -> None:
        nonlocal current_blocks, current_assets, cursor_y, page_index
        pages.append(
            RenderPage(
                page_id=f"r{page_index:04d}",
                size=page_size,
                blocks=current_blocks,
                assets=current_assets,
                footer_text=str(page_index),
            )
        )
        page_index += 1
        current_blocks = []
        current_assets = []
        cursor_y = content_y0

    ordered_pages = sorted(document.pages, key=lambda item: item.page_id)
    ordered_items: list[tuple[str, DocumentBlock | Asset]] = []
    for page in ordered_pages:
        source_page_ids.add(page.page_id)
        page_items: list[tuple[float, float, float, str, DocumentBlock | Asset]] = [
            (
                float(block.reading_order),
                block.bbox.y0,
                block.bbox.x0,
                "block",
                block,
            )
            for block in page.blocks
        ]
        for asset in page.assets:
            usage = asset_usages.get(asset.asset_id, "preserve")
            if usage == "ignore":
                suppressed_artifacts.append(
                    {
                        "kind": "asset_ignored",
                        "asset_id": asset.asset_id,
                        "source_page_id": asset.page_id,
                    }
                )
            elif not asset.path:
                suppressed_artifacts.append(
                    {
                        "kind": "asset_without_path",
                        "asset_id": asset.asset_id,
                        "source_page_id": asset.page_id,
                    }
                )
            else:
                order = _asset_reading_order(page.blocks, asset)
                page_items.append((order, asset.bbox.y0, asset.bbox.x0, "asset", asset))
        ordered_items.extend(
            (kind, item)
            for _, _, _, kind, item in sorted(
                page_items,
                key=lambda entry: (entry[0], entry[1], entry[2], entry[3]),
            )
        )

    for kind, item in ordered_items:
        if kind == "asset":
            asset = item
            if not isinstance(asset, Asset):
                continue
            _, asset_height = _reflow_asset_dimensions(asset, page_size, content_width)
            if (
                (current_blocks or current_assets)
                and cursor_y + 8.0 + asset_height > content_bottom
            ):
                finish_page()
            cursor_y = _append_reflow_asset(
                asset=asset,
                page_size=page_size,
                content_x0=content_x0,
                content_y0=content_y0,
                content_width=content_width,
                content_bottom=content_bottom,
                cursor_y=cursor_y,
                current_assets=current_assets,
                current_page_id=lambda: f"r{page_index:04d}",
                asset_traces=asset_traces,
            )
            continue

        block = item
        if not isinstance(block, DocumentBlock):
            continue
        source_page_ids.add(block.page_id)
        if _should_suppress_reflow_block(block):
            suppressed_artifacts.append(
                {
                    "kind": "source_block_suppressed",
                    "source_block_id": block.block_id,
                    "source_page_id": block.page_id,
                    "reason": "running_header_footer_or_pdf_artifact",
                }
            )
            continue

        plan = translations.get(block.block_id)
        layout_intent = layout_intents.get(block.block_id)
        text, render_intent, flags = _translated_text_for_block(block, plan, layout_intent)
        if not text.strip():
            suppressed_artifacts.append(
                {
                    "kind": "empty_text_block_suppressed",
                    "source_block_id": block.block_id,
                    "source_page_id": block.page_id,
                }
            )
            continue

        style = _style_for_role(defaults, block.role)
        if render_intent == "compact":
            style = style.model_copy(
                update={
                    "font_size_pt": max(9.0, style.font_size_pt * 0.9),
                    "line_height": min(style.line_height, 1.35),
                }
            )
            flags.append("compact_reflow")
        if layout_intent is not None:
            flags.extend(layout_intent.quality_flags)
        html, formula_flags = _formula_html_for_text(text, block.formulas)
        flags.extend(formula_flags)

        if block.role == BlockRole.REFERENCE and current_blocks:
            finish_page()

        estimated_height = _estimated_reflow_height(text, content_width, style)
        keep_with_next = block.role in {BlockRole.TITLE, BlockRole.HEADING}
        if keep_with_next and current_blocks and cursor_y + estimated_height + 36 > content_bottom:
            finish_page()

        first_fragment_height = content_bottom - cursor_y - style.space_before_pt
        if (
            current_blocks
            and first_fragment_height
            < style.font_size_pt * style.line_height * _MIN_FINAL_FRAGMENT_LINES
        ):
            finish_page()
            first_fragment_height = content_bottom - cursor_y - style.space_before_pt
        fragments = _split_reflow_text(
            text,
            content_width,
            content_bottom - content_y0,
            style,
            first_max_height_pt=first_fragment_height if current_blocks else None,
        )
        fragment_count = len(fragments)
        for fragment_index, fragment in enumerate(fragments, start=1):
            fragment_height = _estimated_reflow_height(fragment, content_width, style)
            if current_blocks and cursor_y + style.space_before_pt + fragment_height > content_bottom:
                finish_page()

            cursor_y += style.space_before_pt
            if cursor_y + fragment_height > content_bottom and current_blocks:
                finish_page()
                cursor_y += style.space_before_pt

            bbox = BoundingBox(
                x0=content_x0,
                y0=cursor_y,
                x1=content_x0 + content_width,
                y1=min(content_bottom, cursor_y + fragment_height),
            )
            block_flags = list(flags)
            if fragment_count > 1:
                block_flags.append("reflow_split")
                if fragment_index > 1:
                    block_flags.append("reflow_continued")
            render_block_id = (
                block.block_id
                if fragment_count == 1
                else f"{block.block_id}__reflow_{fragment_index:02d}"
            )
            current_blocks.append(
                RenderBlock(
                    block_id=render_block_id,
                    role=block.role,
                    bbox=bbox,
                    text=fragment,
                    html=_formula_html_for_text(fragment, block.formulas)[0],
                    style_seed=block.style_seed,
                    font_size_pt=style.font_size_pt,
                    font_scale=style.font_size_pt / block.style_seed.font_size
                    if block.style_seed.font_size
                    else 1.0,
                    render_intent=render_intent,
                    text_align=style.alignment,
                    font_weight=700 if style.bold else 400,
                    font_style="italic" if style.italic else "normal",
                    first_line_indent_em=style.first_line_indent_em
                    if fragment_index == 1
                    else 0.0,
                    line_height=style.line_height,
                    quality_flags=_unique_flags(block_flags),
                )
            )
            rendered_source_ids.add(block.block_id)
            block_traces.append(
                {
                    "source_block_id": block.block_id,
                    "render_block_id": render_block_id,
                    "source_page_id": block.page_id,
                    "output_page_id": f"r{page_index:04d}",
                    "role": block.role.value,
                    "translated_chars": len(fragment),
                    "estimated_lines": _estimated_line_count(
                        fragment,
                        content_width,
                        style.font_size_pt,
                    ),
                    "bbox": bbox.model_dump(),
                    "fragment_index": fragment_index,
                    "fragment_count": fragment_count,
                    "quality_flags": _unique_flags(block_flags),
                }
            )
            cursor_y = bbox.y1 + style.space_after_pt

    if current_blocks or current_assets or not pages:
        finish_page()

    trace = _build_reflow_trace(
        document=document,
        pages=pages,
        defaults=defaults,
        source_page_ids=source_page_ids,
        rendered_source_ids=rendered_source_ids,
        block_traces=block_traces,
        asset_traces=asset_traces,
        suppressed_artifacts=suppressed_artifacts,
    )
    return RenderDocument(
        doc_id=document.doc_id,
        target_lang=target_lang,
        pages=pages,
        font_stack=defaults.font_stack,
        line_height=defaults.line_height,
        paragraph_spacing_em=defaults.paragraph_spacing_em,
        layout_mode=_enum_value(defaults.layout_mode),
        layout_trace=trace,
    )


def _translated_text_for_block(
    block: DocumentBlock,
    plan: Any,
    layout_intent: Any,
) -> tuple[str, str, list[str]]:
    quality_flags: list[str]
    if plan is None:
        text = block.source_text
        render_intent = layout_intent.render_intent if layout_intent is not None else "normal"
        quality_flags = ["missing_translation"]
    else:
        text = plan.translated_text if plan.translated_text.strip() else block.source_text
        render_intent = layout_intent.render_intent if layout_intent is not None else plan.render_intent
        quality_flags = list(plan.quality_flags)
        if not plan.translated_text.strip():
            quality_flags.append("empty_translation")
        if plan.role != block.role:
            quality_flags.append("role_mismatch")
    return text, render_intent, quality_flags


def _formula_html_for_text(text: str, formulas: list[Formula]) -> tuple[str, list[str]]:
    if not formulas:
        return escape(text), (
            ["unresolved_formula_placeholder"]
            if _FORMULA_PLACEHOLDER_PATTERN.search(text)
            else []
        )
    formulas_by_placeholder = {formula.placeholder: formula for formula in formulas}
    flags: list[str] = []
    parts: list[str] = []
    cursor = 0
    for match in _FORMULA_PLACEHOLDER_PATTERN.finditer(text):
        parts.append(escape(text[cursor : match.start()]))
        placeholder = match.group(0)
        formula = formulas_by_placeholder.get(placeholder)
        if formula is None:
            parts.append(escape(placeholder))
            flags.append("unresolved_formula_placeholder")
        else:
            parts.append(_formula_span(formula))
            flags.append("formula_placeholder_resolved")
            if not formula.latex.strip():
                flags.append("formula_render_fallback")
        cursor = match.end()
    parts.append(escape(text[cursor:]))
    return "".join(parts), _unique_flags(flags)


def _formula_span(formula: Formula) -> str:
    display = "true" if formula.kind == "display" else "false"
    css_kind = "display" if formula.kind == "display" else "inline"
    latex = formula.latex.strip() or formula.source_text
    fallback = formula.source_text or formula.placeholder
    return (
        f'<span class="formula formula-{css_kind}" '
        f'data-formula-id="{escape(formula.formula_id, quote=True)}" '
        f'data-display="{display}" '
        f'data-latex="{escape(latex, quote=True)}">'
        f'{escape(fallback)}</span>'
    )


def _style_for_role(defaults: RenderDefaults, role: BlockRole) -> RoleStyleDefaults:
    return getattr(defaults.role_styles, role.value, defaults.role_styles.unknown)


def _asset_reading_order(blocks: list[DocumentBlock], asset: Asset) -> float:
    before = [
        block.reading_order
        for block in blocks
        if block.bbox.y0 <= asset.bbox.y0
    ]
    if before:
        return max(before) + 0.5
    return -0.5


def _append_reflow_asset(
    *,
    asset: Asset,
    page_size: PageSize,
    content_x0: float,
    content_y0: float,
    content_width: float,
    content_bottom: float,
    cursor_y: float,
    current_assets: list[RenderAsset],
    current_page_id: Any,
    asset_traces: list[dict[str, Any]],
) -> float:
    width, height = _reflow_asset_dimensions(asset, page_size, content_width)
    space_before = 8.0
    space_after = 8.0
    cursor_y += space_before
    x0 = content_x0 + max(0.0, (content_width - width) / 2)
    bbox = BoundingBox(
        x0=x0,
        y0=cursor_y,
        x1=x0 + width,
        y1=min(content_bottom, cursor_y + height),
    )
    current_assets.append(
        RenderAsset(
            asset_id=asset.asset_id,
            kind=asset.kind,
            bbox=bbox,
            path=asset.path,
            alt_text=asset.alt_text,
            quality_flags=["reflow_asset"],
        )
    )
    asset_traces.append(
        {
            "asset_id": asset.asset_id,
            "source_page_id": asset.page_id,
            "output_page_id": current_page_id(),
            "kind": asset.kind,
            "bbox": bbox.model_dump(),
            "quality_flags": ["reflow_asset"],
        }
    )
    return bbox.y1 + space_after


def _reflow_asset_dimensions(
    asset: Asset,
    page_size: PageSize,
    content_width: float,
) -> tuple[float, float]:
    source_width = max(1.0, _bbox_width(asset.bbox))
    source_height = max(1.0, _bbox_height(asset.bbox))
    max_height = max(72.0, page_size.height * 0.42)
    scale = min(1.0, content_width / source_width, max_height / source_height)
    return max(24.0, source_width * scale), max(24.0, source_height * scale)


def _estimated_reflow_height(
    text: str,
    width_pt: float,
    style: RoleStyleDefaults,
) -> float:
    text_width = max(1.0, width_pt - style.first_line_indent_em * style.font_size_pt)
    lines = _estimated_line_count(text, text_width, style.font_size_pt)
    return max(style.font_size_pt * style.line_height, lines * style.font_size_pt * style.line_height)


def _split_reflow_text(
    text: str,
    width_pt: float,
    max_height_pt: float,
    style: RoleStyleDefaults,
    *,
    first_max_height_pt: float | None = None,
) -> list[str]:
    text = _normalized_text(text)
    if not text:
        return []
    max_lines = max(1, int(max_height_pt / (style.font_size_pt * style.line_height)))
    max_chars = max(1, _estimated_chars_for_lines(width_pt, style.font_size_pt, max_lines))
    if len(text) <= max_chars:
        return [text]

    fragments: list[str] = []
    remaining = text
    while remaining:
        height_for_fragment = first_max_height_pt if not fragments and first_max_height_pt else max_height_pt
        lines_for_fragment = max(
            1,
            int(height_for_fragment / (style.font_size_pt * style.line_height)),
        )
        chars_for_fragment = max(
            1,
            _estimated_chars_for_lines(width_pt, style.font_size_pt, lines_for_fragment),
        )
        chars_for_fragment = min(
            chars_for_fragment,
            _max_reflow_chars_to_fit(remaining, width_pt, style, height_for_fragment),
        )
        if len(remaining) <= chars_for_fragment:
            fragments.append(remaining)
            break
        split_index = _best_reflow_split_index(remaining, chars_for_fragment)
        fragment = remaining[:split_index].strip()
        rest = remaining[split_index:].strip()
        if rest and (
            len(rest) < _MIN_FINAL_FRAGMENT_CHARS
            or _estimated_line_count(rest, width_pt, style.font_size_pt)
            < _MIN_FINAL_FRAGMENT_LINES
        ):
            rebalance_at = _best_reflow_split_index(fragment, max(1, int(len(fragment) * 0.75)))
            rest = f"{fragment[rebalance_at:].strip()} {rest}".strip()
            fragment = fragment[:rebalance_at].strip()
        if not fragment:
            fragment = remaining[:chars_for_fragment].strip()
            rest = remaining[chars_for_fragment:].strip()
        fragments.append(fragment)
        remaining = rest
    return [fragment for fragment in fragments if fragment]


def _estimated_chars_for_lines(width_pt: float, font_size_pt: float, lines: int) -> int:
    return max(1, int((width_pt / max(font_size_pt, 1.0)) * lines * 1.75))


def _max_reflow_chars_to_fit(
    text: str,
    width_pt: float,
    style: RoleStyleDefaults,
    height_pt: float,
) -> int:
    low = 1
    high = max(1, len(text))
    best = 1
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].strip()
        if _estimated_reflow_height(candidate, width_pt, style) <= height_pt:
            best = max(1, mid)
            low = mid + 1
        else:
            high = mid - 1
    return best


def _best_reflow_split_index(text: str, max_chars: int) -> int:
    max_chars = min(max_chars, len(text))
    candidates = [
        text.rfind(marker, 0, max_chars)
        for marker in ("。", "！", "？", ". ", "! ", "? ", "；", "; ", "，", ", ", " ")
    ]
    split_index = max(candidates)
    if split_index >= max(1, int(max_chars * 0.55)):
        char = text[split_index]
        return split_index + (0 if char.isspace() else 1)
    return max_chars


def _should_suppress_reflow_block(block: DocumentBlock) -> bool:
    text = block.source_text.strip()
    if not text:
        return True
    width = _bbox_width(block.bbox)
    height = _bbox_height(block.bbox)
    if width <= 8 and height >= 40 and re.search(r"\d{4}", text):
        return True
    if block.role == BlockRole.FOOTNOTE and re.search(r"\bdoi:\s*\S+", text.lower()):
        return True
    if text.lower().startswith(("view online", "export citation", "crossmark")):
        return True
    return False


def _page_utilization(pages: list[RenderPage]) -> list[dict[str, Any]]:
    utilization: list[dict[str, Any]] = []
    for page in pages:
        area = page.size.width * page.size.height
        text_area = sum(_bbox_area(block.bbox) for block in page.blocks)
        utilization.append(
            {
                "page_id": page.page_id,
                "text_area_ratio": round(text_area / area, 4) if area > 0 else 0.0,
                "block_count": len(page.blocks),
                "asset_count": len(page.assets),
            }
        )
    return utilization


def _build_reflow_trace(
    *,
    document: DocumentIR,
    pages: list[RenderPage],
    defaults: RenderDefaults,
    source_page_ids: set[str],
    rendered_source_ids: set[str],
    block_traces: list[dict[str, Any]],
    asset_traces: list[dict[str, Any]],
    suppressed_artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    source_block_count = sum(len(page.blocks) for page in document.pages)
    return {
        "kind": "layout_trace",
        "layout_mode": _enum_value(defaults.layout_mode),
        "standard": "gb_t_7713_1_2025",
        "render_defaults": defaults.model_dump(mode="json"),
        "source": {
            "page_count": len(source_page_ids),
            "block_count": source_block_count,
        },
        "output": {
            "page_count": len(pages),
            "block_count": sum(len(page.blocks) for page in pages),
        },
        "rendered_source_block_count": len(rendered_source_ids),
        "suppressed_artifacts": suppressed_artifacts,
        "page_utilization": _page_utilization(pages),
        "blocks": block_traces,
        "assets": asset_traces,
    }


def _build_source_bbox_trace(
    document: DocumentIR,
    pages: list[RenderPage],
    defaults: RenderDefaults,
) -> dict[str, Any]:
    return {
        "kind": "layout_trace",
        "layout_mode": _enum_value(defaults.layout_mode),
        "standard": "none",
        "render_defaults": defaults.model_dump(mode="json"),
        "source": {
            "page_count": len(document.pages),
            "block_count": sum(len(page.blocks) for page in document.pages),
        },
        "output": {
            "page_count": len(pages),
            "block_count": sum(len(page.blocks) for page in pages),
        },
        "suppressed_artifacts": [],
        "page_utilization": _page_utilization(pages),
        "blocks": [
            {
                "source_block_id": block.block_id.split("__cont_", 1)[0],
                "render_block_id": block.block_id,
                "output_page_id": page.page_id,
                "role": block.role.value,
                "translated_chars": len(block.text),
                "estimated_lines": _estimated_line_count(
                    block.text,
                    _bbox_width(block.bbox),
                    block.font_size_pt,
                ),
                "bbox": block.bbox.model_dump(),
                "quality_flags": block.quality_flags,
            }
            for page in pages
            for block in page.blocks
        ],
    }


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
                html=_formula_html_for_text(visible_text, source_block.formulas)[0],
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
