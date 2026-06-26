# DECISIONS.md

## Decisions

### Local-first standalone workbench

- **Status**: User-stated and observed.
- **Decision**: The product is a standalone local web workbench, not a Zotero plugin.
- **Evidence**: `AGENTS.md`, `goal.md`, `README.md`, and `apps/web`.
- **Implication**: Keep upload, status, preview, download, config, and diagnostics available from the first screen.

### FastAPI backend and React/Vite frontend

- **Status**: Observed.
- **Decision**: The backend is FastAPI under `services/api`; the frontend is React/Vite under `apps/web`.
- **Evidence**: `services/api/app/main.py`, `services/api/app/routes/documents.py`, `apps/web/package.json`, `apps/web/src/main.tsx`.
- **Implication**: Backend owns pipeline logic; frontend calls typed API client and renders the local workbench.

### Shared schema is the contract boundary

- **Status**: User-stated and observed.
- **Decision**: `packages/schema` is the source of truth for cross-module contracts, mirrored to TypeScript and JSON Schema.
- **Evidence**: `AGENTS.md`, `packages/schema/pdf_translator_schema/models.py`, `packages/schema/typescript/src/index.ts`, `packages/json-schema`.
- **Implication**: Contract changes must update Python, TypeScript, exported JSON Schema, docs, and tests together.

### LLMs cannot own coordinates

- **Status**: User-stated and observed.
- **Decision**: LLM-facing plans must not contain bbox, page, width/height, or coordinate fields.
- **Evidence**: `FORBIDDEN_LAYOUT_KEYS`, `NoLayoutCoordinatesModel`, `validate_layout_plan`, `AGENTS.md`, `docs/schema.md`.
- **Implication**: Renderer remains responsible for coordinates, pagination, overflow, assets, and diagnostics.

### Local file storage instead of a production database

- **Status**: Observed.
- **Decision**: User-visible app records are stored as local files under `data/`.
- **Evidence**: `services/api/app/storage.py`, `services/api/app/runtime_config.py`.
- **Implication**: Do not design multi-user, cloud, auth, or migration-heavy behavior without a new decision.

### Deterministic fallback remains mandatory

- **Status**: User-stated and observed.
- **Decision**: When no model key is configured, the deterministic translator and deterministic fallback paths must keep end-to-end verification possible.
- **Evidence**: `AGENTS.md`, `README.md`, `services/api/app/pipeline/translator.py`, tests.
- **Implication**: New pipeline stages must preserve a local no-key path.

### OpenAI-compatible model provider

- **Status**: Observed.
- **Decision**: Real translation uses an OpenAI-compatible chat completions endpoint; MiniMax-specific handling exists for compatible provider behavior.
- **Evidence**: `services/api/app/pipeline/translator.py`, `services/api/app/provider_config.py`, `.env.example`.
- **Implication**: Provider config must validate base URLs, avoid key leakage in responses, and surface request failures in job status.

### MinerU default extraction with PyMuPDF fallback

- **Status**: Observed.
- **Decision**: PDF extraction defaults to MinerU pipeline, with PyMuPDF fallback when needed.
- **Evidence**: `README.md`, `services/api/app/config.py`, `services/api/app/pipeline/mineru_adapter.py`, `services/api/app/pipeline/parser.py`, tests.
- **Implication**: Parser diagnostics should explain fallback and scanned-PDF limitations.

### HTML/CSS renderer with Playwright PDF export

- **Status**: Observed.
- **Decision**: Renderer produces HTML preview and uses Playwright Chromium for PDF export, with fallback PDF generation if browser export fails.
- **Evidence**: `packages/renderer/pdf_renderer/renderer.py`, `packages/renderer/pdf_renderer/templates/document.html.j2`.
- **Implication**: Renderer tests and diagnostics must cover overflow, assets, formulas, browser availability, and fallback behavior.

### In-process async job scheduler

- **Status**: Observed.
- **Decision**: Jobs are queued in-process with asyncio and tracked by job id.
- **Evidence**: `services/api/app/jobs.py`, `services/api/app/pipeline/resume.py`.
- **Implication**: This is suitable for local use, not a durable distributed queue.

### Runtime config is local and redacted in responses

- **Status**: Observed.
- **Decision**: Runtime provider/render/OCR settings persist locally, while API responses hide raw key values.
- **Evidence**: `services/api/app/runtime_config.py`, `services/api/app/routes/documents.py`, `services/api/tests/test_api.py`.
- **Implication**: Treat `data/config/runtime-config.json` as sensitive local data.

### Subagent/worktree ownership boundaries

- **Status**: User-stated and observed.
- **Decision**: Schema, backend, renderer, web, integration, and docs/coordinator work should be split by file ownership when parallelized.
- **Evidence**: `AGENTS.md`, `docs/worktree.md`.
- **Implication**: Cross-module schema changes should land before renderer/backend/web adaptations.

## Rejected Or Deferred

- **Observed**: Public multi-user deployment is not implemented.
- **Observed**: Cloud sync, payments, and auth are not implemented.
- **Observed**: Full-page OCR for scanned PDFs is not the default MVP path.
- **Observed**: Perfect table/vector/formula visual fidelity is deferred behind diagnostics and fallback behavior.
- **Observed**: Raw JSON schema editing is not exposed in the first workbench path.

## Decisions To Revisit

- **Needs confirmation**: Whether text, image, DOCX, batch, and re-typesetting workflows are permanent first-class scope or experimental extensions around the PDF-paper MVP.
- **Needs confirmation**: Whether local API keys should remain in JSON runtime config or move to encrypted/keychain storage.
- **Needs confirmation**: Whether a durable external queue or database is needed after local MVP.
- **Needs confirmation**: How to migrate existing `data/` artifacts across future schema versions.
