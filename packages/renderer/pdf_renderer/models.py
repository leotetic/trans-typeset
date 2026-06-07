from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil

from pdf_translator_schema import DocumentIR, TranslationLayoutPlan
from pdf_translator_schema.models import (
    BlockRole,
    BoundingBox,
    PageSize,
    RenderDefaults,
    StyleSeed,
)


_SCHEMA_RENDER_DEFAULTS = RenderDefaults()
_MIN_FONT_SCALE = float(_SCHEMA_RENDER_DEFAULTS.overflow_policy.min_font_scale)
_COMPACT_FONT_SCALE = max(_MIN_FONT_SCALE, 0.92)


def _bbox_width(bbox: BoundingBox) -> float:
    return bbox.x1 - bbox.x0


def _bbox_height(bbox: BoundingBox) -> float:
    return bbox.y1 - bbox.y0


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
    estimated_lines = _estimated_line_count(text, _bbox_width(bbox), font_size_pt)
    if estimated_lines == 0:
        return False
    estimated_height = estimated_lines * font_size_pt * line_height
    return estimated_height > _bbox_height(bbox)


def _unique_flags(flags: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for flag in flags:
        if flag and flag not in seen:
            unique.append(flag)
            seen.add(flag)
    return unique


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
class RenderPage:
    page_id: str
    size: PageSize
    blocks: list[RenderBlock]


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

    @classmethod
    def from_ir_and_plans(
        cls,
        document: DocumentIR,
        plans: list[TranslationLayoutPlan],
        target_lang: str,
    ) -> RenderDocument:
        translations = {
            block.source_block_id: block
            for plan in plans
            for block in plan.blocks
        }
        pages: list[RenderPage] = []
        for page in document.pages:
            render_blocks: list[RenderBlock] = []
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
                    font_scale = _COMPACT_FONT_SCALE
                font_size_pt = block.style_seed.font_size * font_scale

                if _text_overflows(text, block.bbox, font_size_pt, cls.line_height):
                    if render_intent == "compact" and font_scale > _MIN_FONT_SCALE:
                        font_scale = _MIN_FONT_SCALE
                        font_size_pt = block.style_seed.font_size * font_scale
                        quality_flags.append("font_scaled")
                    if _text_overflows(text, block.bbox, font_size_pt, cls.line_height):
                        quality_flags.append("overflow_clipped")

                render_blocks.append(
                    RenderBlock(
                        block_id=block.block_id,
                        role=block.role,
                        bbox=block.bbox,
                        text=text,
                        style_seed=block.style_seed,
                        font_size_pt=font_size_pt,
                        font_scale=font_scale,
                        render_intent=render_intent,
                        quality_flags=_unique_flags(quality_flags),
                    )
                )
            pages.append(RenderPage(page_id=page.page_id, size=page.size, blocks=render_blocks))
        return cls(doc_id=document.doc_id, target_lang=target_lang, pages=pages)
