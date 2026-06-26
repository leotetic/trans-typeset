# NON_GOALS.md

## Not In Scope Yet

- **User-stated**: Do not reintroduce Zotero dependency or make the app a Zotero plugin.
- **Observed from repo**: Do not turn the first screen into a marketing page; the first screen remains a usable local workbench.
- **Observed from repo**: Do not make public deployment, authentication, teams, role-based permissions, cloud sync, payments, subscriptions, or collaboration features unless the user explicitly asks and the control docs are updated.
- **Observed from repo**: Do not treat local JSON storage under `data/` as a production multi-user database.
- **Observed from repo**: Do not silently upload documents, images, formulas, OCR crops, or artifacts to remote services. Remote model calls require configured provider credentials and should be visible in config and diagnostics.
- **Observed from repo**: Do not require a model API key for the local deterministic path.
- **Observed from repo**: Do not allow LLM output to decide absolute coordinates, bboxes, page numbers, page sizes, or layout positions.
- **Observed from repo**: Do not store model keys, user PDFs, generated outputs, local runtime config, or real user artifacts in source code, tests, or committed fixtures.
- **Observed from repo**: Do not install Python or Node dependencies globally for this project.
- **Observed from repo**: Do not make scanned PDF OCR, complex table reconstruction, arbitrary vector-graphics fidelity, or perfect formula semantic reconstruction block the digital PDF MVP.
- **Observed from repo**: Do not expose raw JSON schema editing in the first workbench path; existing custom constraints are typed controls.
- **Observed from repo**: Do not change cross-module schema contracts in only one language. Python models, TypeScript types, exported JSON Schema, docs, and tests must stay aligned.
- **Assumption**: Do not add destructive data cleanup, database reset, or output-pruning flows without explicit user confirmation and tests using fake data.

## Risky Features To Revisit Later

- **TODO**: Full backup/restore UI for `data/`.
- **TODO**: Encrypted local secret storage for persisted API keys.
- **TODO**: Public deployment profile with auth, storage isolation, and consent boundaries.
- **TODO**: Full-page OCR for scanned PDFs as a robust default path.
- **TODO**: High-fidelity table structure reconstruction and vector graphic rasterization.
- **TODO**: DOCX export, despite `OutputFormat.DOCX` existing in schema as a target.
