# TEST_PLAN.md

## Automated Checks

- **Observed from repo**: Full Python test suite:

```bash
.venv/bin/python -m pytest
```

- **Observed from repo**: Compile Python packages and services:

```bash
.venv/bin/python -m compileall packages services
```

- **Observed from repo**: Schema-only tests:

```bash
.venv/bin/python -m pytest packages/schema/tests
```

- **Observed from repo**: Renderer tests:

```bash
.venv/bin/python -m pytest packages/renderer/tests
```

- **Observed from repo**: API/pipeline tests:

```bash
.venv/bin/python -m pytest services/api/tests
```

- **Observed from repo**: Frontend typecheck and build:

```bash
npm run typecheck:web
npm run build:web
```

- **Observed from repo**: Renderer visual-regression gate:

```bash
npm run visual-regression
```

- **Observed from repo**: Full acceptance script:

```bash
npm run acceptance
```

- **Observed from repo**: Optional first-four-pages PDF acceptance gate when a root `test.pdf` exists:

```bash
bash scripts/accept-test-pdf-first-four-pages.sh
```

## What Existing Tests Cover

- **Observed from repo**: Schema tests cover workflow defaults, user intent compatibility, no-coordinate model boundaries, duplicate ids, formula refs, edit scope, layout plan coverage, preserve tokens, and JSON Schema exports.
- **Observed from repo**: Renderer tests cover HTML escaping, source bbox constraints, role styles, overflow scaling, box expansion, continuation pages, continuous reflow, GB/T formula numbering, image assets, formula replay/fallbacks, diagnostics, and visual regression fixture behavior.
- **Observed from repo**: API tests cover health/config, provider config, runtime config persistence without key leakage in responses, upload validation, job history, cancel/retry/continue/retypeset, text/image/DOCX/batch queues, preview/download, artifact endpoints, scheduler behavior, resume, parser, chunker, translator, formula processing, MinerU adapter, and local pipeline smoke paths.

## Manual Checks

1. **Observed from repo**: Start backend:

```bash
.venv/bin/python -m uvicorn app.main:app --reload --app-dir services/api
```

2. **Observed from repo**: Start frontend:

```bash
npm run dev:web
```

3. **Observed from repo**: Open `http://127.0.0.1:5173` and confirm the workbench, not a marketing page, is the first screen.
4. **Observed from repo**: Confirm `http://127.0.0.1:8000/api/health` returns `{"status":"ok"}`.
5. **Observed from repo**: Submit a small digitally born English PDF with no model API key configured and verify deterministic translation completes.
6. **Observed from repo**: Confirm job progresses through queued/parsing/translating/rendering/completed or fails with a clear status message.
7. **Observed from repo**: Open preview, download PDF, and inspect `document-ir`, `translation-chunks`, `translation-plans`, `renderer-diagnostics`, and `pdf-export-diagnostics`.
8. **Observed from repo**: Test invalid upload cases: non-PDF for PDF endpoint, PDF extension with non-PDF bytes, unsupported language, and oversized file.
9. **Observed from repo**: Test cancel, retry, continuation, and re-typeset on disposable inputs.
10. **Observed from repo**: If testing DOCX, confirm LibreOffice/`soffice` is installed or verify the expected `docx-conversion` failure artifact.

## Data-Safety Checks

- **Observed from repo**: Automated tests must use temporary directories or fake fixtures, not real `data/`.
- **Observed from repo**: Do not commit `data/`, uploads, output PDFs, preview HTML, runtime config, model keys, `.venv/`, `node_modules/`, cache directories, or uploaded user PDFs.
- **Observed from repo**: Config API responses must report whether a key is configured without returning the key value.
- **Assumption**: Before manual testing with real PDFs, confirm they stay local unless a model provider is configured and the user consents to remote processing.
- **TODO**: Add or document an explicit backup/restore test for `data/` once a backup flow exists.

## Regression Risks To Watch

- **Observed from repo**: Schema changes can break Python/TypeScript/JSON Schema alignment.
- **Observed from repo**: Changing parser ids or reading order can invalidate chunking, cached plans, edit scopes, and renderer mappings.
- **Observed from repo**: Translator repair changes can accidentally allow coordinate-bearing model output or lose preserve tokens.
- **Observed from repo**: Renderer changes can produce text overlap, empty blocks, bbox overflow, missing images, raw formula placeholders, or fallback PDFs.
- **Observed from repo**: Frontend API type changes can desync from backend response models.
- **Observed from repo**: Runtime config changes can leak local API key values if response redaction is bypassed.

## Gaps

- **Unknown / needs user answer**: Whether broad text/image/DOCX flows should have the same release gate strength as the PDF-paper workflow.
- **TODO**: Add an explicit user-facing backup/restore verification plan when storage lifecycle is designed.
- **TODO**: Add browser-driven end-to-end UI automation if the project wants a stable UI release gate beyond typecheck/build and local workflow scripts.
