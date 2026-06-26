# SPEC.md

## Purpose

- **Observed from repo**: Trans Typesetting is a local-first literature translation and typesetting workbench. It accepts document inputs, creates a structured `DocumentIR`, builds `TranslationChunk[]`, validates `TranslationLayoutPlan[]`, renders an HTML preview, and exports a translated/typeset PDF.
- **Observed from repo**: The core product path is `PDF upload -> DocumentIR -> TranslationChunk[] -> TranslationLayoutPlan[] -> renderer -> preview/download`, documented in `AGENTS.md`, `goal.md`, `README.md`, and implemented across `services/api`, `packages/schema`, `packages/renderer`, and `apps/web`.
- **Inferred from repo**: The primary target user is a local single user translating or re-typesetting academic papers, especially digitally born English PDFs.

## Current User Flows

### Local Setup And Startup

- **Observed from repo**: Python dependencies are installed into a project-local `.venv`; Node dependencies are installed through the root npm workspace.
- **Observed from repo**: Backend startup command is `.venv/bin/python -m uvicorn app.main:app --reload --app-dir services/api`.
- **Observed from repo**: Frontend startup command is `npm run dev:web`.
- **Observed from repo**: The Vite frontend proxies `/api` to `VITE_API_PROXY_TARGET`, defaulting to `http://127.0.0.1:8000`.

### Translate Or Typeset A PDF

- **Observed from repo**: The frontend workbench lets the user select workflow sections for translate-only, typeset-only, translate-and-typeset, and developer diagnostics in `apps/web/src/main.tsx`.
- **Observed from repo**: PDF submission calls `POST /api/documents` with a required content PDF, optional layout-reference PDF, target language, workflow mode, style intent, natural-language instruction, and optional render constraints.
- **Observed from repo**: The API validates PDF filename, MIME type, header bytes, target language, and upload size before queuing work.
- **Observed from repo**: A missing layout-reference PDF is treated as a fallback to the content PDF and marked in workflow artifacts.

### Other Input Modes

- **Observed from repo**: Text, image, DOCX, and batch PDF workflows exist through `/api/workflows/text`, `/api/workflows/image`, `/api/workflows/docx`, and `/api/documents/batch`.
- **Observed from repo**: DOCX input requires headless LibreOffice/`soffice` or `LIBREOFFICE_BIN`; when unavailable, the job fails with a `docx-conversion` artifact.
- **Observed from repo**: Image input can use OCR providers or deterministic fallback and stores the input image as a document asset.
- **Known discrepancy**: `goal.md` and the project invariants keep digital PDF papers as the MVP priority, while current code and README document broader text/image/DOCX workflows. Treat PDF papers as the core path, and treat the extra inputs as current product surface that still needs careful validation.

### Job Lifecycle

- **Observed from repo**: Job states include `queued`, `parsing`, `translating`, `rendering`, `completed`, `failed`, and `canceled`.
- **Observed from repo**: The API exposes job status, job history, job events, cancel, retry, continuation, and re-typesetting endpoints.
- **Observed from repo**: On backend startup, incomplete queued/parsing/translating/rendering jobs are resumed when enough upload or artifact state exists.
- **Observed from repo**: Job execution uses an in-process async scheduler with configurable translation concurrency.

### Preview, Download, And Artifacts

- **Observed from repo**: Completed jobs expose preview HTML at `/api/documents/{doc_id}/preview` and translated PDF at `/api/documents/{doc_id}/download`.
- **Observed from repo**: Artifacts include `document-ir`, `translation-chunks`, `translation-plans`, `renderer-diagnostics`, `pdf-export-diagnostics`, `workflow-run`, `semantic-analysis`, `layout-intent-plan`, parser/MinerU/OCR/formula diagnostics, and related debug JSON.
- **Observed from repo**: The frontend schema/artifact inspector can fetch and display artifact payloads.

### Runtime Configuration

- **Observed from repo**: `GET /api/config` returns effective runtime settings without exposing API key values.
- **Observed from repo**: `PUT /api/config` persists local runtime configuration under `data/config/runtime-config.json`.
- **Observed from repo**: Runtime config includes provider/base URL/model/key presence, target language, translation concurrency, OCR settings, MinerU settings, formula recognition mode, and render defaults.

## Functional Requirements

- **Observed from repo**: The deterministic translator must remain available when no model key is configured, so local end-to-end validation can run without remote model access.
- **Observed from repo**: Real model translation uses an OpenAI-compatible chat completions contract and validates or repairs `TranslationLayoutPlan` output.
- **Observed from repo**: LLM output must not contain coordinates or page-positioning fields; schema and validation reject those fields.
- **Observed from repo**: The renderer owns bbox, page sizing, overflow, continuation pages, role styling, formula replay, image asset preservation, and diagnostics.
- **Observed from repo**: Parser output must be a valid `DocumentIR` with stable block ids and testable reading order.
- **Observed from repo**: Chunking must preserve citations, formulas, reference markers, figure/table tokens, nearby titles, render defaults, article brief, and context.
- **Observed from repo**: Failed parse, translation, schema validation, render, export, and missing converter conditions must land in job status or artifacts so the frontend can show recoverable errors.

## Acceptance Criteria

- **Observed from repo**: A digital PDF can be uploaded, queued, parsed, chunked, translated with deterministic fallback, rendered to preview HTML, and exported to a downloadable PDF.
- **Observed from repo**: Upload errors reject non-PDF content, invalid PDF headers, unsupported target languages, and oversized files.
- **Observed from repo**: The frontend shows backend offline state, task progress, job history, preview/download readiness, artifact inspector state, and recoverable errors.
- **Observed from repo**: Schema tests cover invalid bbox, duplicate ids, forbidden layout coordinates, preserve tokens, formula refs, workflow defaults, and JSON Schema export.
- **Observed from repo**: Renderer tests cover missing translations, role mismatches, overflow scaling, continuation pages, continuous reflow, image assets, formula handling, HTML escaping, and layout diagnostics.
- **Observed from repo**: API tests cover health/config, upload validation, queueing, job history, cancel/retry/continue/retypeset, preview/download, artifact endpoints, and pipeline failures.

## Known Limitations

- **Observed from repo**: Scanned or image-only PDFs can still fail as `unsupported_scanned_pdf`; full-page OCR is not the default MVP path.
- **Observed from repo**: Complex tables, arbitrary vector graphics, and perfect visual fidelity are not complete; current behavior exposes fallbacks and diagnostics.
- **Observed from repo**: PDF export depends on Playwright Chromium, with fallback PDF generation when browser export is unavailable.
- **Observed from repo**: Runtime config and task history are local JSON files, not a multi-user database.
- **Observed from repo**: API keys persisted through runtime config are stored locally in `data/config/runtime-config.json`; responses hide the key value, but the file itself is not described as encrypted.

## Open Questions

- **Unknown / needs user answer**: Should text, image, DOCX, batch, and re-typesetting flows remain first-class product scope, or should docs label them experimental around the PDF-paper MVP?
- **Unknown / needs user answer**: What backup/restore policy should users follow for `data/` when it contains uploads, outputs, runtime config, and possible local API keys?
- **TODO**: Decide whether README's final worktree link should be updated from the older absolute path to this workspace path.
