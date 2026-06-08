export type BlockRole =
  | "title"
  | "abstract"
  | "heading"
  | "paragraph"
  | "caption"
  | "formula"
  | "table"
  | "figure"
  | "footnote"
  | "reference"
  | "unknown";

export interface BoundingBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface PageSize {
  width: number;
  height: number;
}

export interface StyleSeed {
  font_size?: number;
  font_name?: string | null;
  bold?: boolean;
  italic?: boolean;
  color?: string;
}

export interface DocumentBlock {
  block_id: string;
  page_id: string;
  role?: BlockRole;
  bbox: BoundingBox;
  column?: number;
  reading_order: number;
  source_text?: string;
  span_refs?: string[];
  style_seed?: StyleSeed;
}

export interface Asset {
  asset_id: string;
  page_id: string;
  kind?: "figure" | "formula" | "table" | "image" | "unknown";
  bbox: BoundingBox;
  path?: string | null;
  alt_text?: string | null;
}

export interface DocumentPage {
  page_id: string;
  size: PageSize;
  blocks?: DocumentBlock[];
  assets?: Asset[];
}

export interface DocumentIR {
  doc_id: string;
  pages: DocumentPage[];
}

export type TextAlignment = "left" | "center" | "right" | "justify";

export interface AlignmentDefaults {
  title?: TextAlignment;
  abstract?: TextAlignment;
  heading?: TextAlignment;
  paragraph?: TextAlignment;
  caption?: TextAlignment;
  formula?: TextAlignment;
  table?: TextAlignment;
  figure?: TextAlignment;
  footnote?: TextAlignment;
  reference?: TextAlignment;
  unknown?: TextAlignment;
}

export interface OverflowPolicy {
  strategy?:
    | "scale_then_expand_then_continue"
    | "scale_then_continue"
    | "continue_without_scaling";
  min_font_scale?: number;
  max_font_scale?: number;
  allow_box_expansion?: boolean;
  allow_continuation_page?: boolean;
}

export interface PreservePolicy {
  formulas?: "preserve";
  citations?: "preserve";
  reference_markers?: "preserve";
  figure_table_assets?: "preserve";
  whitespace?: "allow_reflow" | "preserve";
  line_breaks?: "allow_reflow" | "preserve";
}

export interface RenderDefaults {
  target_lang?: string;
  font_stack?: string[];
  line_height?: number;
  paragraph_spacing_em?: number;
  alignment?: AlignmentDefaults;
  overflow_policy?: OverflowPolicy;
  preserve_policy?: PreservePolicy;
}

export interface SourceBlock {
  block_id: string;
  role: BlockRole;
  source_text: string;
  nearby_titles?: string[];
  preserve_tokens?: string[];
}

export interface TranslationChunk {
  chunk_id: string;
  target_lang?: string;
  source_blocks: SourceBlock[];
  context?: string;
  glossary?: Record<string, string>;
  render_defaults?: RenderDefaults;
  constraints?: {
    max_output_ratio?: number;
    require_all_blocks?: boolean;
    preserve_tokens?: boolean;
  };
}

export interface NoLayoutCoordinates {
  bbox?: never;
  bounding_box?: never;
  x?: never;
  y?: never;
  x0?: never;
  y0?: never;
  x1?: never;
  y1?: never;
  width?: never;
  height?: never;
  page?: never;
  page_id?: never;
  page_index?: never;
  page_number?: never;
  top?: never;
  right?: never;
  bottom?: never;
  left?: never;
}

export interface InlineItem extends NoLayoutCoordinates {
  kind?: "text" | "citation" | "formula" | "reference_marker" | "asset_ref";
  text?: string;
  source_token?: string | null;
  asset_id?: string | null;
}

export interface TranslationBlockPlan extends NoLayoutCoordinates {
  source_block_id: string;
  translated_text: string;
  inline_items?: InlineItem[];
  role: BlockRole;
  render_intent?: "normal" | "compact" | "emphasis" | "preserve_asset";
  quality_flags?: string[];
}

export interface TranslationLayoutPlan extends NoLayoutCoordinates {
  schema_version?: "0.1";
  chunk_id: string;
  target_lang?: string;
  blocks: TranslationBlockPlan[];
}

export interface ChunkProgress {
  chunk_id: string;
  index: number;
  total: number;
  status: string;
  progress: number;
  message: string;
  quality_flags?: string[];
  error?: string | null;
}

export interface JobStatus {
  job_id: string;
  doc_id?: string | null;
  filename: string;
  target_lang?: string | null;
  status:
    | "queued"
    | "parsing"
    | "translating"
    | "rendering"
    | "completed"
    | "failed"
    | "canceled";
  progress: number;
  message: string;
  error?: string | null;
  chunks?: ChunkProgress[];
}
