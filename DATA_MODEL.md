# DATA_MODEL.md

## Storage Overview

- **Observed from repo**: Primary app storage is local filesystem storage rooted at `STORAGE_DIR`, defaulting to `data/`.
- **Observed from repo**: `services/api/app/storage.py` creates `uploads/`, `documents/`, `jobs/`, `outputs/`, and `config/` under the storage root.
- **Observed from repo**: There are no app database migrations in the main storage layer. JSON files are the persisted application records.
- **Observed from repo**: LangGraph checkpoint SQLite dependencies exist for agent workflow internals, but the user-visible document/job/config storage in this repo is file-based.

## Stored Locations

- **Observed from repo**: Uploads are saved as `data/uploads/{doc_id}.content.{ext}`, optional `data/uploads/{doc_id}.layout.{ext}`, or `data/uploads/{doc_id}.{ext}` for non-PDF workflow uploads.
- **Observed from repo**: Parsed documents are saved as `data/documents/{doc_id}.json`.
- **Observed from repo**: Job status records are saved as `data/jobs/{job_id}.json`.
- **Observed from repo**: Per-document outputs are saved under `data/outputs/{doc_id}/`.
- **Observed from repo**: Runtime config is saved as `data/config/runtime-config.json`.
- **Observed from repo**: Preview HTML is saved as `data/outputs/{doc_id}/preview.html`.
- **Observed from repo**: Exported PDF is saved as `data/outputs/{doc_id}/translated.pdf`.
- **Observed from repo**: Extracted assets are saved under `data/outputs/{doc_id}/assets/`.

## Core Entities

### JobStatus

- **Observed from repo**: Defined in `services/api/app/models.py`.
- **Fields**: `job_id`, `doc_id`, `filename`, `target_lang`, `status`, `progress`, `message`, `error`, `chunks`.
- **Validation**: `progress` is 0 to 1; chunk progress index/total are positive; status is one of `queued`, `parsing`, `translating`, `rendering`, `completed`, `failed`, `canceled`.
- **Relationships**: A job normally references one document by `doc_id`; retry/continue may reuse the same `doc_id`; re-typeset creates a derived `doc_id`.

### RuntimeConfig

- **Observed from repo**: Defined in `services/api/app/models.py` and loaded/merged in `services/api/app/runtime_config.py`.
- **Fields**: target language defaults, provider/base URL/model/key presence, concurrency, retry settings, OCR settings, extraction backend, MinerU settings, formula recognition mode, and `RenderDefaults`.
- **Validation**: Update schema bounds concurrency, retry counts, OCR confidence/timeouts, MinerU timeout, formula concurrency, and enum-like settings.
- **Secret handling**: API key values may be persisted locally but are not returned by config responses.

### InputSource

- **Observed from repo**: Defined in `packages/schema/pdf_translator_schema/models.py`.
- **Fields**: `source_id`, `input_type`, `source_role`, `filename`, `mime_type`, `size_bytes`, `sha256`, `artifact_path`, `quality_flags`.
- **Relationships**: `WorkflowRun.input_sources[]` records content and optional layout-reference inputs.

### UserIntent

- **Observed from repo**: Schema version `0.2` model in `packages/schema/pdf_translator_schema/models.py`.
- **Fields**: `target_lang`, `workflow_mode`, `output_kind`, `style_intent`, `typesetting_standard`, `instruction`, preserve policy, reference assets, constraints, column layout, task intent, output targets, template profile, bibliography preference, requirements.
- **Validation**: Duplicate `output_targets` are rejected.

### WorkflowRun And WorkflowStep

- **Observed from repo**: Records workflow id, job id, doc id, status, current step, progress, input sources, user intent, steps, artifacts, diagnostics, and error.
- **Observed from repo**: Workflow steps include read input, analyze intent, semantic recognize, build plan, validate plan, translate, render, evaluate render, repair, export PDF, complete, and fail.

### DocumentIR

- **Observed from repo**: The parser/renderer source-of-truth schema is `DocumentIR`.
- **Fields**: `doc_id`, `pages`, formula metadata, extraction backend/version, and quality flags.
- **Relationships**: `DocumentIR.pages[].blocks[]` provide source blocks for chunking and rendering; `DocumentIR.pages[].assets[]` provide image/formula/table/figure assets; `DocumentIR.formulas[]` anchors formula refs.
- **Validation**: Tests cover duplicate block ids, duplicate page/asset/reading-order ids, invalid bbox, and formula ref consistency.

### DocumentPage

- **Fields**: `page_id`, `size`, `blocks`, `assets`.
- **Relationships**: Blocks and assets reference `page_id`.

### DocumentBlock

- **Fields**: `block_id`, `page_id`, `role`, `bbox`, `column`, `reading_order`, `source_text`, `text_for_translation`, formulas, span refs, lines, spans, style seed, optional formula id.
- **Relationships**: `block_id` is used by `TranslationChunk.source_blocks[]`, `TranslationLayoutPlan.blocks[]`, layout-intent blocks, edit scopes, diagnostics, and renderer mapping.
- **Validation**: The parser creates stable block ids from page, bbox, and normalized text; schema/tests reject duplicate ids.

### Asset And AssetIR

- **Observed from repo**: `Asset` lives inside `DocumentIR`; `AssetIR` records normalized workflow assets.
- **Fields**: asset id, kind, page/source ids, bbox/path/alt text, OCR text, source block ids, confidence, and quality flags.
- **Relationships**: Renderer uses `DocumentIR.pages[].assets[]` and API asset endpoints to preserve images/formulas/tables/figures.

### FormulaIR And FormulaRecognitionResult

- **Observed from repo**: Formula metadata records formula id, page/block/asset anchors, source text range, span ids, display mode, confidence, OCR provider, source kind, PDF formula replay, and quality flags.
- **Validation**: Helpers validate unknown formula refs, stale legacy formula ids, anchor/source consistency, and forbid coordinates in OCR/model recognition outputs.

### ArticleBrief

- **Observed from repo**: Built before translation and passed into chunks.
- **Fields**: title, field, background, main idea, contribution, key terms, and quality flags.
- **Behavior**: Deterministic fallback brief is used when no model key is configured.

### TranslationChunk

- **Fields**: `chunk_id`, `target_lang`, `source_blocks`, context, glossary, article brief, render defaults, and constraints.
- **Relationships**: Sent to translator; validated against `TranslationLayoutPlan`.
- **Validation**: Duplicate source block ids are rejected; preserve tokens are validated.

### SourceBlock

- **Fields**: `block_id`, role, source text, nearby titles, preserve tokens, and `requires_translation`.
- **Behavior**: Formula-only and formula-like blocks can be marked as not requiring translation.

### TranslationLayoutPlan

- **Fields**: `schema_version`, `chunk_id`, `target_lang`, and `blocks[]`.
- **Validation**: Must match chunk id and target language, cover required source blocks, preserve required tokens, use valid formula refs, and reject forbidden layout coordinate keys.
- **Boundary**: It is the only translator output contract and must not carry page positioning.

### TranslationBlockPlan And InlineItem

- **Fields**: `source_block_id`, translated text, inline items, role, render intent, and quality flags.
- **Validation**: Formula preserve tokens must appear in `translated_text`; formula inline items must use formula kind.

### LayoutIntentPlan And SemanticLayoutAnalysis

- **Observed from repo**: Agent/debug contracts for semantic layout planning.
- **Fields**: semantic block/asset signals, document profile, structure plan, page setup, style system, numbering, bibliography, requirements, and quality flags.
- **Boundary**: These models inherit no-coordinate validation and do not replace renderer-owned coordinates.

### EditScope

- **Fields**: `mode`, `page_numbers`, `block_ids`.
- **Validation**: Page scope requires 1-based positive unique page numbers; block scope requires nonblank unique block ids; fields cannot be mixed across modes.

## Uniqueness And Id Rules

- **Observed from repo**: `doc_id` and `job_id` are generated with UUID hex prefixes `doc_` and `job_`.
- **Observed from repo**: Parser block ids are stable hashes of page id, normalized bbox, and normalized text, with collision disambiguation.
- **Observed from repo**: Schema/tests reject duplicate document block ids, duplicate source block ids, duplicate output targets, duplicate edit-scope page numbers, and duplicate edit-scope block ids.

## Migration And Backup Expectations

- **Observed from repo**: JSON schema exports are versioned with `x-schema-version`, currently `0.2`; `TranslationLayoutPlan` remains schema version `0.1`.
- **Observed from repo**: Schema changes must update Python models, TypeScript types, JSON Schema exports, docs, and tests.
- **Assumption**: Because storage is local JSON and uploads/artifacts can contain user documents and API keys, backup/restore should copy the whole `data/` directory only with user consent.
- **TODO**: Define an explicit migration policy for older `data/` artifacts when schema versions change.
- **TODO**: Define whether `data/config/runtime-config.json` should be encrypted or moved to OS keychain storage.

## Data Safety Rules

- **Observed from repo**: `.gitignore` excludes generated/local artifacts such as `.venv/`, `node_modules/`, `.pytest_cache/`, and project local outputs.
- **Observed from repo**: AGENTS rules forbid committing generated artifacts, uploaded PDFs, output HTML/PDF, model keys, and user documents.
- **Observed from repo**: Tests use temporary storage paths and monkeypatching rather than real `data/`.
- **Assumption**: Manual testing should use fake or disposable PDFs unless the user explicitly provides real documents for local-only verification.
