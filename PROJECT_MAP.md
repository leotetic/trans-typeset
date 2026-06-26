# PROJECT_MAP.md

## High-Level Tree

```text
.
├── AGENTS.md
├── README.md
├── SPEC.md
├── NON_GOALS.md
├── DATA_MODEL.md
├── TEST_PLAN.md
├── DECISIONS.md
├── PROJECT_MAP.md
├── apps/web
├── services/api
├── packages/schema
├── packages/json-schema
├── packages/renderer
├── docs
├── scripts
└── data              # local runtime/user data, not source
```

## Important Files

- **Observed from repo**: `services/api/app/main.py` creates the FastAPI app, CORS, health endpoint, startup resume hook, and document routes.
- **Observed from repo**: `services/api/app/routes/documents.py` defines config, upload, workflow, job, preview, download, asset, and artifact endpoints.
- **Observed from repo**: `services/api/app/storage.py` defines local file storage for uploads, documents, jobs, outputs, assets, previews, PDFs, and runtime config.
- **Observed from repo**: `services/api/app/models.py` defines API response/request models such as `JobStatus`, `RuntimeConfig`, and `RetypesetJobRequest`.
- **Observed from repo**: `services/api/app/pipeline/orchestrator.py` coordinates parse/chunk/translate/render/export flows and job status.
- **Observed from repo**: `services/api/app/pipeline/parser.py` is the PyMuPDF parser and diagnostics fallback.
- **Observed from repo**: `services/api/app/pipeline/mineru_adapter.py` maps MinerU outputs into `DocumentIR`.
- **Observed from repo**: `services/api/app/pipeline/chunker.py` builds `TranslationChunk[]` and preserve tokens.
- **Observed from repo**: `services/api/app/pipeline/translator.py` implements deterministic and OpenAI-compatible translators.
- **Observed from repo**: `services/api/app/pipeline/workflow.py` builds user intent, workflow artifacts, semantic layout plans, and adapter documents.
- **Observed from repo**: `packages/schema/pdf_translator_schema/models.py` defines the Python contract models.
- **Observed from repo**: `packages/schema/pdf_translator_schema/validation.py` validates translation/layout/formula constraints.
- **Observed from repo**: `packages/schema/typescript/src/index.ts` mirrors schema types for the frontend.
- **Observed from repo**: `packages/schema/pdf_translator_schema/json_schema.py` exports JSON Schema files to `packages/json-schema`.
- **Observed from repo**: `packages/renderer/pdf_renderer/models.py` maps `DocumentIR + TranslationLayoutPlan + RenderDefaults` into renderable pages/items and diagnostics.
- **Observed from repo**: `packages/renderer/pdf_renderer/renderer.py` renders HTML, measures browser layout, exports PDF, inlines assets, and writes export diagnostics.
- **Observed from repo**: `apps/web/src/api.ts` is the typed frontend API client with JSON parsing, errors, timeouts, and retries.
- **Observed from repo**: `apps/web/src/main.tsx` is the local workbench UI.
- **Observed from repo**: `scripts/acceptance.sh` runs visual regression, Python tests, compileall, frontend typecheck, and frontend build.

## App Startup Flow

1. **Observed from repo**: Backend loads `.env` through `python-dotenv` when available.
2. **Observed from repo**: `services/api/app/config.py` builds process settings.
3. **Observed from repo**: `services/api/app/main.py` creates FastAPI, adds CORS, and includes document routes.
4. **Observed from repo**: Lifespan startup calls `resume_incomplete_jobs(storage)`.
5. **Observed from repo**: Frontend starts through `npm run dev:web`, using Vite and proxying `/api`.

## Request And Data Flow

```text
Frontend workbench
  -> apps/web/src/api.ts
  -> FastAPI route in services/api/app/routes/documents.py
  -> services/api/app/storage.py saves upload/status
  -> services/api/app/jobs.py schedules async work
  -> services/api/app/pipeline/orchestrator.py
  -> parser or adapter creates DocumentIR
  -> chunker creates TranslationChunk[]
  -> translator/source-preserving path creates TranslationLayoutPlan[]
  -> schema validation/repair
  -> renderer creates preview HTML, diagnostics, and PDF
  -> storage writes artifacts under data/outputs/{doc_id}
  -> frontend polls job/status/events and opens preview/download/artifacts
```

## Data Storage

- **Observed from repo**: `data/uploads/` stores uploaded source files.
- **Observed from repo**: `data/documents/` stores parsed `DocumentIR` JSON.
- **Observed from repo**: `data/jobs/` stores `JobStatus` JSON.
- **Observed from repo**: `data/outputs/{doc_id}/` stores preview, PDF, diagnostics, chunks, plans, workflow artifacts, and assets.
- **Observed from repo**: `data/config/runtime-config.json` stores persisted local runtime config and may contain API keys.

## Validation Locations

- **Observed from repo**: Upload validation lives in `services/api/app/routes/documents.py`.
- **Observed from repo**: Environment/runtime setting parsing lives in `services/api/app/config.py` and `services/api/app/runtime_config.py`.
- **Observed from repo**: Contract validation lives in `packages/schema/pdf_translator_schema/models.py` and `packages/schema/pdf_translator_schema/validation.py`.
- **Observed from repo**: Translator response repair/validation lives in `services/api/app/pipeline/translator.py`.
- **Observed from repo**: Renderer diagnostics and layout issue detection live in `packages/renderer/pdf_renderer/models.py` and `packages/renderer/pdf_renderer/renderer.py`.
- **Observed from repo**: Frontend API response validation lives in `apps/web/src/api.ts`.

## UI Locations

- **Observed from repo**: Main React app: `apps/web/src/main.tsx`.
- **Observed from repo**: Frontend API client: `apps/web/src/api.ts`.
- **Observed from repo**: Styling: `apps/web/src/styles.css`.
- **Observed from repo**: Vite config: `apps/web/vite.config.ts`.

## Test Locations

- **Observed from repo**: Schema tests: `packages/schema/tests`.
- **Observed from repo**: Renderer tests: `packages/renderer/tests`.
- **Observed from repo**: API/pipeline tests: `services/api/tests`.
- **Observed from repo**: Visual regression fixture: `packages/renderer/tests/fixtures/visual_regression.py`.
- **Observed from repo**: Acceptance scripts: `scripts/acceptance.sh`, `scripts/visual-regression.sh`, and `scripts/accept-test-pdf-first-four-pages.sh`.

## Discoverable Commands

```bash
.venv/bin/python -m uvicorn app.main:app --reload --app-dir services/api
npm run dev:web
.venv/bin/python -m pytest
.venv/bin/python -m compileall packages services
npm run visual-regression
npm run typecheck:web
npm run build:web
npm run acceptance
```

## First Five Files For A Beginner

1. `README.md` - setup, workflow overview, diagnostics, and known limitations.
2. `AGENTS.md` - project rules, invariants, boundaries, and required reading.
3. `services/api/app/routes/documents.py` - the public API surface.
4. `packages/schema/pdf_translator_schema/models.py` - the contract source of truth.
5. `apps/web/src/main.tsx` - the user-facing workbench.

## Known Map Issues

- **Known discrepancy**: `README.md` ends with an absolute worktree link using an older path. Current workspace path is `/Users/leotetic/app/trans-typesetting`.
- **TODO**: Keep this map updated whenever routes, storage paths, workflow steps, commands, or important files change.
