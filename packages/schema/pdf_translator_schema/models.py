from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .defaults import DEFAULT_RENDER_DEFAULTS


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


FORBIDDEN_LAYOUT_KEYS = frozenset(
    {
        "bbox",
        "bounding_box",
        "x",
        "y",
        "x0",
        "y0",
        "x1",
        "y1",
        "width",
        "height",
        "page",
        "page_id",
        "page_index",
        "page_number",
        "top",
        "right",
        "bottom",
        "left",
    }
)


class NoLayoutCoordinatesModel(StrictBaseModel):
    @model_validator(mode="before")
    @classmethod
    def reject_layout_coordinates(cls, data: object) -> object:
        if isinstance(data, dict):
            forbidden = sorted(FORBIDDEN_LAYOUT_KEYS.intersection(data))
            if forbidden:
                raise ValueError(
                    "LLM layout plans must not include coordinate fields: "
                    + ", ".join(forbidden)
                )
        return data


class BlockRole(StrEnum):
    TITLE = "title"
    ABSTRACT = "abstract"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    CAPTION = "caption"
    FORMULA = "formula"
    TABLE = "table"
    FIGURE = "figure"
    FOOTNOTE = "footnote"
    REFERENCE = "reference"
    UNKNOWN = "unknown"


class BoundingBox(StrictBaseModel):
    x0: float = Field(ge=0)
    y0: float = Field(ge=0)
    x1: float = Field(gt=0)
    y1: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> BoundingBox:
        if self.x1 <= self.x0:
            raise ValueError("bbox.x1 must be greater than bbox.x0")
        if self.y1 <= self.y0:
            raise ValueError("bbox.y1 must be greater than bbox.y0")
        return self


class PageSize(StrictBaseModel):
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class StyleSeed(StrictBaseModel):
    font_size: float = Field(default=10.0, gt=0)
    font_name: str | None = None
    bold: bool = False
    italic: bool = False
    color: str = "#111111"


class Asset(StrictBaseModel):
    asset_id: str = Field(min_length=1)
    page_id: str = Field(min_length=1)
    kind: Literal["figure", "formula", "table", "image", "unknown"] = "unknown"
    bbox: BoundingBox
    path: str | None = None
    alt_text: str | None = None


class DocumentBlock(StrictBaseModel):
    block_id: str = Field(min_length=1)
    page_id: str = Field(min_length=1)
    role: BlockRole = BlockRole.UNKNOWN
    bbox: BoundingBox
    column: int = Field(default=0, ge=0)
    reading_order: int = Field(ge=0)
    source_text: str = ""
    span_refs: list[str] = Field(default_factory=list)
    style_seed: StyleSeed = Field(default_factory=StyleSeed)


class DocumentPage(StrictBaseModel):
    page_id: str = Field(min_length=1)
    size: PageSize
    blocks: list[DocumentBlock] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_page_refs(self) -> DocumentPage:
        reading_orders: set[int] = set()
        for block in self.blocks:
            if block.page_id != self.page_id:
                raise ValueError(f"block {block.block_id} points to another page")
            if block.reading_order in reading_orders:
                raise ValueError(
                    f"duplicate reading_order on page {self.page_id}: {block.reading_order}"
                )
            reading_orders.add(block.reading_order)
        for asset in self.assets:
            if asset.page_id != self.page_id:
                raise ValueError(f"asset {asset.asset_id} points to another page")
        return self


class DocumentIR(StrictBaseModel):
    doc_id: str = Field(min_length=1)
    pages: list[DocumentPage] = Field(min_length=1)

    def blocks_by_id(self) -> dict[str, DocumentBlock]:
        return {block.block_id: block for page in self.pages for block in page.blocks}

    @model_validator(mode="after")
    def validate_unique_ids(self) -> DocumentIR:
        page_ids: set[str] = set()
        block_ids: set[str] = set()
        asset_ids: set[str] = set()
        for page in self.pages:
            if page.page_id in page_ids:
                raise ValueError(f"duplicate page_id: {page.page_id}")
            page_ids.add(page.page_id)
            for block in page.blocks:
                if block.block_id in block_ids:
                    raise ValueError(f"duplicate block_id: {block.block_id}")
                block_ids.add(block.block_id)
            for asset in page.assets:
                if asset.asset_id in asset_ids:
                    raise ValueError(f"duplicate asset_id: {asset.asset_id}")
                asset_ids.add(asset.asset_id)
        return self


TextAlignment = Literal["left", "center", "right", "justify"]


class AlignmentDefaults(StrictBaseModel):
    title: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["title"]
    abstract: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["abstract"]
    heading: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["heading"]
    paragraph: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["paragraph"]
    caption: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["caption"]
    formula: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["formula"]
    table: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["table"]
    figure: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["figure"]
    footnote: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["footnote"]
    reference: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["reference"]
    unknown: TextAlignment = DEFAULT_RENDER_DEFAULTS["alignment"]["unknown"]


class OverflowPolicy(StrictBaseModel):
    strategy: Literal[
        "scale_then_expand_then_continue",
        "scale_then_continue",
        "continue_without_scaling",
    ] = DEFAULT_RENDER_DEFAULTS["overflow_policy"]["strategy"]
    min_font_scale: float = Field(
        default=DEFAULT_RENDER_DEFAULTS["overflow_policy"]["min_font_scale"],
        gt=0,
        le=1,
    )
    max_font_scale: float = Field(
        default=DEFAULT_RENDER_DEFAULTS["overflow_policy"]["max_font_scale"],
        ge=1,
    )
    allow_box_expansion: bool = DEFAULT_RENDER_DEFAULTS["overflow_policy"][
        "allow_box_expansion"
    ]
    allow_continuation_page: bool = DEFAULT_RENDER_DEFAULTS["overflow_policy"][
        "allow_continuation_page"
    ]

    @model_validator(mode="after")
    def validate_font_scale_range(self) -> OverflowPolicy:
        if self.min_font_scale > self.max_font_scale:
            raise ValueError("min_font_scale must be less than or equal to max_font_scale")
        return self


class PreservePolicy(StrictBaseModel):
    formulas: Literal["preserve"] = DEFAULT_RENDER_DEFAULTS["preserve_policy"]["formulas"]
    citations: Literal["preserve"] = DEFAULT_RENDER_DEFAULTS["preserve_policy"]["citations"]
    reference_markers: Literal["preserve"] = DEFAULT_RENDER_DEFAULTS["preserve_policy"][
        "reference_markers"
    ]
    figure_table_assets: Literal["preserve"] = DEFAULT_RENDER_DEFAULTS["preserve_policy"][
        "figure_table_assets"
    ]
    whitespace: Literal["allow_reflow", "preserve"] = DEFAULT_RENDER_DEFAULTS[
        "preserve_policy"
    ]["whitespace"]
    line_breaks: Literal["allow_reflow", "preserve"] = DEFAULT_RENDER_DEFAULTS[
        "preserve_policy"
    ]["line_breaks"]


class RenderDefaults(StrictBaseModel):
    target_lang: str = DEFAULT_RENDER_DEFAULTS["target_lang"]
    font_stack: list[str] = Field(
        default_factory=lambda: DEFAULT_RENDER_DEFAULTS["font_stack"].copy(),
        min_length=1,
    )
    line_height: float = Field(default=DEFAULT_RENDER_DEFAULTS["line_height"], gt=0)
    paragraph_spacing_em: float = Field(
        default=DEFAULT_RENDER_DEFAULTS["paragraph_spacing_em"], ge=0
    )
    alignment: AlignmentDefaults = Field(default_factory=AlignmentDefaults)
    overflow_policy: OverflowPolicy = Field(default_factory=OverflowPolicy)
    preserve_policy: PreservePolicy = Field(default_factory=PreservePolicy)


class SourceBlock(StrictBaseModel):
    block_id: str = Field(min_length=1)
    role: BlockRole
    source_text: str
    nearby_titles: list[str] = Field(default_factory=list)
    preserve_tokens: list[str] = Field(default_factory=list)


class TranslationConstraints(StrictBaseModel):
    max_output_ratio: float = Field(default=1.8, gt=0)
    require_all_blocks: bool = True
    preserve_tokens: bool = True


class TranslationChunk(StrictBaseModel):
    chunk_id: str = Field(min_length=1)
    target_lang: str = "zh-CN"
    source_blocks: list[SourceBlock] = Field(min_length=1)
    context: str = ""
    glossary: dict[str, str] = Field(default_factory=dict)
    render_defaults: RenderDefaults = Field(default_factory=RenderDefaults)
    constraints: TranslationConstraints = Field(default_factory=TranslationConstraints)

    def source_block_ids(self) -> set[str]:
        return {block.block_id for block in self.source_blocks}

    @model_validator(mode="after")
    def validate_unique_source_blocks(self) -> TranslationChunk:
        seen: set[str] = set()
        for block in self.source_blocks:
            if block.block_id in seen:
                raise ValueError(f"duplicate source block_id: {block.block_id}")
            seen.add(block.block_id)
        return self


class InlineItem(NoLayoutCoordinatesModel):
    kind: Literal["text", "citation", "formula", "reference_marker", "asset_ref"] = "text"
    text: str = ""
    source_token: str | None = None
    asset_id: str | None = None


class TranslationBlockPlan(NoLayoutCoordinatesModel):
    source_block_id: str = Field(min_length=1)
    translated_text: str
    inline_items: list[InlineItem] = Field(default_factory=list)
    role: BlockRole
    render_intent: Literal["normal", "compact", "emphasis", "preserve_asset"] = "normal"
    quality_flags: list[str] = Field(default_factory=list)


class TranslationLayoutPlan(NoLayoutCoordinatesModel):
    schema_version: Literal["0.1"] = "0.1"
    chunk_id: str = Field(min_length=1)
    target_lang: str = "zh-CN"
    blocks: list[TranslationBlockPlan] = Field(min_length=1)

    @field_validator("blocks")
    @classmethod
    def validate_no_duplicate_source_blocks(
        cls, blocks: list[TranslationBlockPlan]
    ) -> list[TranslationBlockPlan]:
        seen: set[str] = set()
        for block in blocks:
            if block.source_block_id in seen:
                raise ValueError(f"duplicate source_block_id: {block.source_block_id}")
            seen.add(block.source_block_id)
        return blocks
