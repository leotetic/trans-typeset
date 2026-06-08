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

export type InputKind = "text" | "image" | "pdf";

export type InputSourceRole = "content" | "layout_reference";

export type OutputKind =
  | "translation"
  | "typeset_document"
  | "layout_reference"
  | "summary_layout";

export type StyleIntent = "academic" | "report" | "handout" | "slide_like" | "plain";

export type WorkflowStatus = "queued" | "running" | "completed" | "failed" | "canceled";

export type WorkflowStepStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "skipped"
  | "repaired";

export type WorkflowStepName =
  | "read_input"
  | "analyze_intent"
  | "semantic_recognize"
  | "build_plan"
  | "validate_plan"
  | "translate"
  | "render"
  | "evaluate_render"
  | "repair"
  | "export_pdf"
  | "complete"
  | "fail";

export type TypesettingStandard = "none" | "gb_t_7713_1_2025";

export type LayoutMode = "source_bbox" | "continuous_reflow";

export type FormulaSourceKind =
  | "text_layer"
  | "inline_text"
  | "vector_candidate"
  | "image_candidate"
  | "ocr"
  | "mock"
  | "unknown";

export type FormulaDisplayMode = "inline" | "display";
export type OCRRegionKind = "formula" | "text" | "page";

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

export interface TextSpanIR {
  span_id: string;
  page_id: string;
  block_id: string;
  line_id: string;
  text?: string;
  bbox: BoundingBox;
  font_name?: string | null;
  font_size?: number | null;
  flags?: number | null;
  color?: string | null;
  origin?: [number, number] | null;
}

export interface TextLineIR {
  line_id: string;
  page_id: string;
  block_id: string;
  text?: string;
  bbox: BoundingBox;
  span_ids?: string[];
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
  lines?: TextLineIR[];
  spans?: TextSpanIR[];
  style_seed?: StyleSeed;
  formula_id?: string | null;
}

export interface Asset {
  asset_id: string;
  page_id: string;
  kind?: "figure" | "formula" | "table" | "image" | "unknown";
  bbox: BoundingBox;
  path?: string | null;
  alt_text?: string | null;
  formula_id?: string | null;
}

export interface FormulaIR {
  formula_id: string;
  page_id: string;
  source_block_id?: string | null;
  anchor_block_id?: string | null;
  asset_id?: string | null;
  latex?: string;
  source_text?: string;
  source_text_range?: [number, number] | null;
  span_ids?: string[];
  display_mode?: FormulaDisplayMode;
  confidence?: number;
  ocr_provider?: string | null;
  ocr_confidence?: number | null;
  source_kind?: FormulaSourceKind;
  quality_flags?: string[];
}

export interface FormulaRecognitionResult extends NoLayoutCoordinates {
  latex: string;
  display_mode?: FormulaDisplayMode;
  confidence?: number;
  quality_flags?: string[];
}

export interface OCRRecognitionResult extends NoLayoutCoordinates {
  text?: string;
  latex?: string;
  region_kind?: OCRRegionKind;
  confidence?: number;
  language?: string | null;
  provider?: string;
  quality_flags?: string[];
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
  formulas?: FormulaIR[];
}

export interface InputSource {
  source_id: string;
  input_type: InputKind;
  source_role?: InputSourceRole;
  filename?: string | null;
  mime_type?: string | null;
  size_bytes?: number;
  sha256?: string | null;
  artifact_path?: string | null;
  quality_flags?: string[];
}

export interface AssetIR {
  asset_id: string;
  source_id: string;
  kind?: "image" | "pdf_asset" | "reference" | "ocr_text" | "unknown";
  mime_type?: string | null;
  path?: string | null;
  ocr_text?: string;
  alt_text?: string | null;
  source_block_ids?: string[];
  confidence?: number | null;
  quality_flags?: string[];
}

export interface UserConstraints {
  page_width_pt?: number;
  page_height_pt?: number;
  target_font_size_pt?: number;
  allow_continuation?: boolean;
  preserve_images?: boolean;
}

export interface UserIntent {
  target_lang?: string;
  output_kind?: OutputKind;
  style_intent?: StyleIntent;
  typesetting_standard?: TypesettingStandard;
  instruction?: string;
  preserve_policy?: Array<
    "citations" | "formulas" | "tables" | "figures" | "reference_markers"
  >;
  reference_assets?: string[];
  constraints?: UserConstraints;
}

export interface WorkflowStep {
  step_id: string;
  name: WorkflowStepName;
  status: WorkflowStepStatus;
  progress?: number;
  attempt?: number;
  message?: string;
  input_artifacts?: string[];
  output_artifacts?: string[];
  diagnostics?: Record<string, unknown>;
  error?: string | null;
}

export interface WorkflowRun {
  workflow_id: string;
  job_id?: string | null;
  doc_id: string;
  status?: WorkflowStatus;
  current_step?: WorkflowStepName;
  progress?: number;
  input_sources?: InputSource[];
  user_intent?: UserIntent;
  steps?: WorkflowStep[];
  artifacts?: Record<string, string>;
  diagnostics?: Record<string, unknown>;
  error?: string | null;
}

export interface SemanticBlockSignal extends NoLayoutCoordinates {
  source_block_id: string;
  role_candidates?: BlockRole[];
  section_hint?: string | null;
  confidence?: number;
  quality_flags?: string[];
}

export interface SemanticAssetSignal extends NoLayoutCoordinates {
  asset_id: string;
  usage_hint?:
    | "preserve"
    | "inline_reference"
    | "background_reference"
    | "ignore"
    | "unknown";
  text_hint?: string;
  confidence?: number;
  quality_flags?: string[];
}

export interface SemanticLayoutAnalysis extends NoLayoutCoordinates {
  schema_version?: "0.1";
  analysis_id: string;
  doc_id: string;
  target_lang?: string;
  block_signals?: SemanticBlockSignal[];
  asset_signals?: SemanticAssetSignal[];
  section_hints?: string[];
  confidence?: number;
  quality_flags?: string[];
}

export interface LayoutIntentBlock extends NoLayoutCoordinates {
  source_block_id: string;
  role: BlockRole;
  priority?: number;
  render_intent?:
    | "normal"
    | "compact"
    | "emphasis"
    | "preserve_asset"
    | "callout"
    | "reference_layout";
  asset_refs?: string[];
  quality_flags?: string[];
}

export interface LayoutIntentAsset extends NoLayoutCoordinates {
  asset_id: string;
  usage?: "preserve" | "inline_reference" | "background_reference" | "ignore";
  quality_flags?: string[];
}

export interface LayoutIntentPlan extends NoLayoutCoordinates {
  schema_version?: "0.1";
  plan_id: string;
  doc_id: string;
  target_lang?: string;
  output_kind?: OutputKind;
  style_intent?: StyleIntent;
  blocks?: LayoutIntentBlock[];
  assets?: LayoutIntentAsset[];
  quality_flags?: string[];
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

export interface PageLayoutDefaults {
  width_pt?: number;
  height_pt?: number;
  margin_top_pt?: number;
  margin_right_pt?: number;
  margin_bottom_pt?: number;
  margin_left_pt?: number;
}

export interface RoleStyleDefaults {
  font_size_pt: number;
  bold?: boolean;
  italic?: boolean;
  alignment?: TextAlignment;
  line_height?: number;
  first_line_indent_em?: number;
  space_before_pt?: number;
  space_after_pt?: number;
}

export interface RoleStyles {
  title?: RoleStyleDefaults;
  abstract?: RoleStyleDefaults;
  heading?: RoleStyleDefaults;
  paragraph?: RoleStyleDefaults;
  caption?: RoleStyleDefaults;
  formula?: RoleStyleDefaults;
  table?: RoleStyleDefaults;
  figure?: RoleStyleDefaults;
  footnote?: RoleStyleDefaults;
  reference?: RoleStyleDefaults;
  unknown?: RoleStyleDefaults;
}

export interface RenderDefaults {
  target_lang?: string;
  font_stack?: string[];
  line_height?: number;
  paragraph_spacing_em?: number;
  layout_mode?: LayoutMode;
  page_layout?: PageLayoutDefaults;
  role_styles?: RoleStyles;
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
