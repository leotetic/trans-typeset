---
name: run-trans-typesetting-workflow
description: Run and verify this repository's local Trans Typesetting PDF workflow, including starting or reusing the FastAPI/Vite dev servers, driving the local workbench UI, submitting PDF translation/typesetting jobs, monitoring job status, and checking preview/download artifacts. Use when working in the trans-typesetting project and the user asks to demo, test, debug, or operate the local pipeline from PDF upload through DocumentIR, TranslationChunk arrays, TranslationLayoutPlan arrays, renderer output, preview, and download.
---

# Run Trans Typesetting Workflow

## Overview

Use this project-local skill only inside `/Users/leotetic/app/trans-typesetting`. It captures the demonstrated local workbench flow for translating and typesetting an English PDF into a previewable and downloadable PDF.

Prefer semantic actions over coordinate replay. Use the browser UI for user-visible workflow checks, and use direct API/status/artifact checks for stable verification.

## Guardrails

- Read `AGENTS.md` and respect the repository boundaries before changing files.
- Keep skills, notes, and project automation under this repository. Do not create or update global skills under `~/.codex/skills`.
- Do not commit or create source fixtures from user PDFs, uploaded documents, `data/`, generated previews, downloaded PDFs, `node_modules/`, or `.venv/`.
- Use the project virtualenv for Python commands: `.venv/bin/python -m ...`.
- Use root workspace Node commands such as `npm run dev:web`, `npm run typecheck:web`, and `npm run build:web`; do not install global Node packages.
- Treat the deterministic translator as a valid local end-to-end mode when no model key is configured.

## Workflow

1. Orient in the current repo.
   - Confirm the request is about the local Trans Typesetting workbench or pipeline.
   - Check whether the user wants visible UI operation, API-level validation, or both.
   - For UI work, prefer the in-app browser for localhost verification unless the user needs their existing Chrome tab/session.

2. Ensure local services are available.
   - Backend: `.venv/bin/python -m uvicorn app.main:app --reload --app-dir services/api`
   - Frontend: `npm run dev:web`
   - Default URLs: `http://127.0.0.1:8000` for the API and `http://127.0.0.1:5173` for the workbench.
   - Verify backend health with `GET /api/health` and runtime limits with `GET /api/config` when relevant.

3. Prepare the workflow inputs.
   - Use a user-provided or existing local PDF; do not add PDFs to source-controlled fixtures unless the user explicitly asks for a sanitized sample.
   - For the recorded full workflow, select `Translate + Typeset`.
   - Use target language `zh-CN` / Simplified Chinese unless the user asks otherwise.
   - Use style intent `academic` unless the user asks otherwise.
   - Include an instruction such as `按照 GB/T 7713.1 进行排版` only when the user requests GB/T-style layout or wants the recorded behavior repeated.
   - Treat the layout reference PDF as optional; when absent, the backend falls back to using the content PDF as the layout source.

4. Submit the job.
   - UI path: open `http://127.0.0.1:5173`, choose `Translate + Typeset`, upload the required `待翻译 PDF`, optionally upload `排版素材 PDF`, set target language/style/instruction, then run or re-run the job.
   - API path: submit `POST /api/documents` as multipart form data with `content_file`, optional `layout_file`, `target_lang`, `workflow_mode=translate_and_typeset`, `output_kind=typeset_document`, `style_intent=academic`, and optional `instruction`.
   - Record the returned `job_id` and `doc_id`.

5. Monitor status and diagnose failures.
   - Poll or inspect `GET /api/jobs/{job_id}` for `queued`, `parsing`, `translating`, `rendering`, `completed`, `failed`, or `canceled`.
   - Use `GET /api/jobs/{job_id}/events?limit=80` for a readable timeline and artifact/event context.
   - On failure, inspect available artifacts before guessing: `GET /api/documents/{doc_id}/artifacts`, then relevant artifacts such as `parser-diagnostics`, `translation-diagnostics`, `renderer-diagnostics`, or `pdf-export-diagnostics`.

6. Verify completion.
   - Confirm `GET` or `HEAD /api/documents/{doc_id}/preview` returns HTML.
   - Confirm `GET` or `HEAD /api/documents/{doc_id}/download` returns a PDF.
   - In the UI, verify the preview frame renders content and the download/open controls are enabled.
   - If checking GB/T formula behavior, inspect `renderer-diagnostics` for formula numbering and layout flags.

## Recorded UI Landmarks

The demonstrated workflow used the local Chrome workbench at `127.0.0.1:5173` with:

- Primary navigation button: `Translate + Typeset` / `Full workflow`.
- Required upload control: `待翻译 PDF`.
- Optional layout control: `排版素材 PDF`.
- Target language control: `目标语言`, set to `简体中文`.
- Typesetting instruction area: `自然语言要求`.
- Style control: `版式风格`, set to `Academic`.
- Status area showing the pipeline stages `排队中`, `解析`, `翻译`, `排版`, `完成`.
- Completion controls for preview/open/download and re-run.

Use these labels as accessibility targets when controlling the UI. Avoid relying on click coordinates.
