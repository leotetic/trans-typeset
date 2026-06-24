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

export type InputKind = "text" | "image" | "pdf" | "docx";

export type InputSourceRole = "content" | "layout_reference";

export type OutputKind =
  | "translation"
  | "typeset_document"
  | "layout_reference"
  | "summary_layout";

export type WorkflowMode =
  | "translate_only"
  | "typeset_only"
  | "translate_and_typeset";

export type EditScopeMode = "all" | "pages" | "blocks";

export type OutputFormat = "html_preview" | "pdf" | "docx";

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

export type DocumentKind =
  | "course_paper"
  | "undergraduate_thesis"
  | "lab_report"
  | "proposal_report"
  | "book_report"
  | "social_practice_report"
  | "group_assignment"
  | "homework"
  | "generic_academic";

export type TemplateSource =
  | "school_template"
  | "course_requirement"
  | "user_specified"
  | "default_academic";

export type CitationStyle = "auto" | "gb_t_7714" | "apa" | "mla" | "ieee" | "chicago";

export type SectionKind =
  | "cover"
  | "title"
  | "abstract"
  | "keywords"
  | "toc"
  | "list_of_figures"
  | "list_of_tables"
  | "body"
  | "heading"
  | "figure"
  | "table"
  | "formula"
  | "references"
  | "appendix"
  | "acknowledgements"
  | "author_info"
  | "department"
  | "course_info"
  | "experiment_metadata"
  | "experiment_purpose"
  | "experiment_theory"
  | "experiment_steps"
  | "experiment_results"
  | "experiment_analysis"
  | "result_analysis"
  | "conclusion"
  | "unknown";

export type PaperSize = "a4" | "letter" | "source";
export type PageOrientation = "portrait" | "landscape";
export type NumberingStyle = "none" | "arabic" | "chinese" | "roman" | "parenthesized";

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
export type ColumnLayoutScope = "document" | "body";

export interface ColumnLayoutDefaults {
  column_count?: 1 | 2;
  column_gap_pt?: number;
  scope?: ColumnLayoutScope;
  balance_columns?: boolean;
}

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

export interface Formula {
  formula_id: string;
  placeholder: string;
  kind?: "inline" | "display";
  source_text?: string;
  latex?: string;
  bbox?: BoundingBox | null;
  confidence?: number | null;
  asset_id?: string | null;
  quality_flags?: string[];
}

export interface DocumentBlock {
  block_id: string;
  page_id: string;
  role?: BlockRole;
  bbox: BoundingBox;
  column?: number;
  reading_order: number;
  source_text?: string;
  text_for_translation?: string;
  formulas?: Formula[];
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
  accepted_provider?: string | null;
  accepted_confidence?: number | null;
  validator_status?: string | null;
  fallback_reason?: string | null;
  source_kind?: FormulaSourceKind;
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

export interface EditScope {
  mode?: EditScopeMode;
  page_numbers?: number[];
  block_ids?: string[];
}

export interface TaskIntent extends NoLayoutCoordinates {
  document_kind?: DocumentKind;
  audience?: string;
  language?: string;
  confidence?: number;
  evidence?: string[];
}

export interface OutputTarget extends NoLayoutCoordinates {
  format?: OutputFormat;
  required?: boolean;
  artifact_name?: string;
}

export interface TemplateProfile extends NoLayoutCoordinates {
  source?: TemplateSource;
  standard?: string;
  institution?: string;
  department?: string;
  template_asset_ids?: string[];
  fallback_used?: boolean;
}

export interface BibliographyPreference extends NoLayoutCoordinates {
  citation_style?: CitationStyle;
  default_reason?: string;
}

export interface AcademicRequirement extends NoLayoutCoordinates {
  requirement_id: string;
  label?: string;
  category?:
    | "structure"
    | "style"
    | "metadata"
    | "numbering"
    | "bibliography"
    | "length"
    | "tone"
    | "asset";
  required?: boolean;
  section_kinds?: SectionKind[];
  evidence?: string[];
  quality_flags?: string[];
}

export interface UserIntent extends NoLayoutCoordinates {
  schema_version?: "0.1" | "0.2";
  target_lang?: string;
  workflow_mode?: WorkflowMode;
  output_kind?: OutputKind;
  style_intent?: StyleIntent;
  typesetting_standard?: TypesettingStandard;
  instruction?: string;
  preserve_policy?: Array<
    "citations" | "formulas" | "tables" | "figures" | "reference_markers"
  >;
  reference_assets?: string[];
  constraints?: UserConstraints;
  column_layout?: ColumnLayoutDefaults;
  task_intent?: TaskIntent;
  output_targets?: OutputTarget[];
  template_profile?: TemplateProfile;
  bibliography_preference?: BibliographyPreference;
  requirements?: AcademicRequirement[];
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

export interface DocumentStructureCandidate extends NoLayoutCoordinates {
  section_id: string;
  kind?: SectionKind;
  title?: string;
  level?: number;
  source_block_ids?: string[];
  required?: boolean;
  confidence?: number;
  quality_flags?: string[];
}

export interface BlockSectionMapping extends NoLayoutCoordinates {
  source_block_id: string;
  section_id: string;
  section_kind?: SectionKind;
  confidence?: number;
  quality_flags?: string[];
}

export interface SemanticLayoutAnalysis extends NoLayoutCoordinates {
  schema_version?: "0.1" | "0.2";
  analysis_id: string;
  doc_id: string;
  target_lang?: string;
  block_signals?: SemanticBlockSignal[];
  asset_signals?: SemanticAssetSignal[];
  section_hints?: string[];
  structure_candidates?: DocumentStructureCandidate[];
  block_section_mappings?: BlockSectionMapping[];
  recognized_requirements?: AcademicRequirement[];
  missing_sections?: SectionKind[];
  uncertain_sections?: string[];
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

export interface DocumentProfile extends NoLayoutCoordinates {
  document_kind?: DocumentKind;
  target_lang?: string;
  style_intent?: StyleIntent;
  template_profile?: TemplateProfile;
  citation_style?: CitationStyle;
}

export interface DocumentStructureSection extends NoLayoutCoordinates {
  section_id: string;
  kind?: SectionKind;
  title?: string;
  level?: number;
  source_block_ids?: string[];
  required?: boolean;
  quality_flags?: string[];
}

export interface DocumentStructurePlan extends NoLayoutCoordinates {
  sections?: DocumentStructureSection[];
  missing_sections?: SectionKind[];
  uncertain_sections?: string[];
}

export interface HeaderFooterPlan extends NoLayoutCoordinates {
  header_text?: string;
  footer_text?: string;
  enabled?: boolean;
}

export interface PageNumberingPlan extends NoLayoutCoordinates {
  enabled?: boolean;
  style?: NumberingStyle;
  start_at?: number;
}

export interface PageSetup extends NoLayoutCoordinates {
  paper_size?: PaperSize;
  orientation?: PageOrientation;
  margin_top_pt?: number;
  margin_right_pt?: number;
  margin_bottom_pt?: number;
  margin_left_pt?: number;
  header_footer?: HeaderFooterPlan;
  page_numbering?: PageNumberingPlan;
  section_breaks?: string[];
  page_breaks?: string[];
}

export interface NamedStyle extends NoLayoutCoordinates {
  font_size_pt?: number;
  bold?: boolean;
  italic?: boolean;
  alignment?: TextAlignment;
  line_height?: number;
  first_line_indent_em?: number;
  space_before_pt?: number;
  space_after_pt?: number;
  font_stack?: string[] | null;
}

export interface StyleSystem extends NoLayoutCoordinates {
  named_styles?: Record<string, NamedStyle>;
}

export interface NumberingRule extends NoLayoutCoordinates {
  enabled?: boolean;
  style?: NumberingStyle;
  section_ids?: string[];
}

export interface TocGenerationPlan extends NoLayoutCoordinates {
  enabled?: boolean;
  max_level?: number;
  section_ids?: string[];
}

export interface NumberingPlan extends NoLayoutCoordinates {
  heading_numbering?: NumberingRule;
  figure_numbering?: NumberingRule;
  table_numbering?: NumberingRule;
  formula_numbering?: NumberingRule;
  reference_numbering?: NumberingRule;
  toc_generation?: TocGenerationPlan;
}

export interface BibliographyPlan extends NoLayoutCoordinates {
  citation_style?: CitationStyle;
  default_reason?: string;
  in_text_citation_policy?: string;
  bibliography_sorting?: string;
  hanging_indent?: boolean;
  section_ids?: string[];
}

export interface LayoutIntentPlan extends NoLayoutCoordinates {
  schema_version?: "0.1" | "0.2";
  plan_id: string;
  doc_id: string;
  target_lang?: string;
  workflow_mode?: WorkflowMode;
  output_kind?: OutputKind;
  style_intent?: StyleIntent;
  column_layout?: ColumnLayoutDefaults;
  document_profile?: DocumentProfile;
  structure_plan?: DocumentStructurePlan;
  page_setup?: PageSetup;
  style_system?: StyleSystem;
  numbering_plan?: NumberingPlan;
  bibliography_plan?: BibliographyPlan;
  requirements?: AcademicRequirement[];
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
  /** Per-role font family override; null/absent inherits the document font_stack. */
  font_stack?: string[] | null;
}

/** GB/T 7713.1 display formula numbering: sequential right-aligned "(n)". */
export type FormulaNumbering = "none" | "parenthesized";

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
  formula_numbering?: FormulaNumbering;
  column_layout?: ColumnLayoutDefaults;
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
  requires_translation?: boolean;
}

export interface ArticleBrief extends NoLayoutCoordinates {
  schema_version?: "0.1";
  title?: string;
  field?: string;
  background?: string;
  main_idea?: string;
  contribution?: string;
  key_terms?: Record<string, string>;
  quality_flags?: string[];
}

export interface TranslationChunk {
  chunk_id: string;
  target_lang?: string;
  source_blocks: SourceBlock[];
  context?: string;
  glossary?: Record<string, string>;
  article_brief?: ArticleBrief | null;
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
