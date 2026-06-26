# AGENTS.md

本仓库是一个本地优先的 PDF 文献翻译与排版系统。协作目标见 `goal.md`。所有 agent 都应围绕同一条流水线工作：

```text
PDF upload -> DocumentIR -> TranslationChunk[] -> TranslationLayoutPlan[] -> renderer -> preview/download
```

## 环境与依赖

- 在项目中如需安装 Python 依赖，必须先创建并激活项目本地虚拟环境，然后在虚拟环境中安装依赖。
- 不要将依赖直接安装到系统 Python、全局 Node 环境或其他全局运行时中。
- Python 命令优先使用 `.venv/bin/python -m ...`，例如 `.venv/bin/python -m pytest`。
- 初始化 Python 环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/python -m pip install -r services/api/requirements.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
```

- Node 依赖只允许安装到当前仓库工作区，使用项目根目录的 `npm install`。不要使用 `npm install -g` 或改动全局 Node 环境。
- 前端开发依赖通过根工作区管理，常用命令是 `npm run dev:web`、`npm run typecheck:web`、`npm run build:web`。
- 未配置 `OPENAI_API_KEY` 时，后端会使用 deterministic translator，这是本地端到端验证的默认模式。

## 仓库边界

- `packages/schema`: 跨模块 contract 源头，包含 Python Pydantic models、默认值、校验逻辑和 JSON Schema 导出。
- `packages/schema/typescript`: 前端消费的 TypeScript schema/types。修改 Python schema 时必须同步这里。
- `packages/json-schema`: 由 schema models 导出的 JSON Schema。schema 变化时需要重新生成并纳入审查。
- `services/api`: FastAPI 后端、上传、任务状态、文件存储、PDF 解析、分块、翻译编排。
- `packages/renderer`: HTML/CSS renderer、PDF 导出、渲染模型和模板。
- `apps/web`: React/Vite 本地工作台。前端不承载后端 pipeline 逻辑。
- `docs`: 架构、schema、worktree 和协作说明。

不要修改或提交生成物和本地产物：`.venv/`、`node_modules/`、`data/`、`.worktrees/`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`*.pyc`、`*.egg-info/`、`apps/web/dist/`、`*.tsbuildinfo`、上传 PDF、输出 HTML/PDF。

## Product Invariants

- 项目要脱离 Zotero，作为独立本地 Web 工作台运行。
- 第一屏必须是可用的工具界面：上传 PDF、选择目标语言、任务状态、预览、下载。不要把前端改成营销页。
- 系统优先处理数字版英文论文 PDF。扫描版 OCR、复杂图表保真和双语模式是后续增强，不要让它们阻塞文本型论文 MVP。
- 本地 deterministic translator 必须一直可用，便于没有模型密钥时做端到端测试。
- 用户文档、artifact 和模型密钥不得写入源码或测试 fixture，除非是明确脱敏的最小样例。

## Schema Contract

- `DocumentIR` 是解析器和 renderer 的事实来源，可以包含页面尺寸、block bbox、阅读顺序、样式种子和资产引用。
- `TranslationChunk` 是给模型的输入，包含 source blocks、nearby titles、preserve tokens、context、glossary、render defaults 和 constraints。
- `TranslationLayoutPlan@0.1` 是模型唯一允许返回的 contract。
- LLM 输出不得包含 `bbox`、`bounding_box`、`x`、`y`、`x0`、`y0`、`x1`、`y1`、`width`、`height`、`page`、`page_id`、`page_index`、`page_number`、`top`、`right`、`bottom`、`left` 等坐标或页面定位字段。
- LLM plan 必须覆盖 chunk 中所有 source block，除非 constraints 明确允许缺失。
- LLM plan 必须保留 `preserve_tokens`，包括 citation、formula、reference marker、figure/table token。
- 坐标、分页、溢出、字号缩放、扩盒和续页是 renderer 职责，不是 LLM 职责。
- schema 变更必须同步：
  - `packages/schema/pdf_translator_schema/models.py`
  - `packages/schema/pdf_translator_schema/defaults.py` 或 validation 相关文件
  - `packages/schema/typescript/src/index.ts`
  - `packages/json-schema/*.schema.json`
  - `docs/schema.md`
  - 对应测试

## Renderer Rules

- renderer 只消费公开 contract：`DocumentIR + TranslationLayoutPlan + RenderDefaults`。
- 原始页面尺寸、block bbox 和阅读顺序来自 `DocumentIR`，不能由模型覆盖。
- 缺失译文时可以回退 source text，但必须输出质量 flag。
- source block 的 role 是渲染 class 的基准；如果 plan role 不一致，应标记 mismatch，而不是静默改变源语义。
- overflow policy 应逐步实现为确定性策略：缩放、扩盒、续页和诊断 flag。
- 涉及渲染行为的改动要增加 HTML/PDF 或模型层测试，必要时补视觉回归 fixture。

## Backend Rules

- API 层负责输入校验、任务状态和 artifact 输出；pipeline 层负责 parse/chunk/translate/render orchestration。
- 上传接口必须校验 PDF 类型、文件头、大小和目标语言。
- parser 输出必须是合法 `DocumentIR`，block id 要稳定，阅读顺序要可测试。
- chunker 必须提取 preserve tokens，并尽量保留标题和局部上下文。
- translator 必须返回经过 `validate_layout_plan` 校验的 `TranslationLayoutPlan`。
- 真实模型调用失败、schema 校验失败、render 失败都要落到 job status，让前端能展示明确错误。
- 后续引入队列、取消、重试或并发时，不要破坏现有 deterministic 本地路径。

## Frontend Rules

- `apps/web` 是本地工作台，不放后端 pipeline 逻辑。
- API 调用应通过 typed client 统一处理 JSON、错误、超时和重试。
- 前端限制，例如语言和文件大小，后续应从后端 config API 获取；硬编码只能作为临时 fallback。
- 任务状态要覆盖 queued、parsing、translating、rendering、completed、failed。
- 预览/下载失败要有可恢复 UI，不要只让 iframe 空白。
- 修改 UI 时要检查桌面和移动布局，避免文本溢出、按钮挤压和预览区遮挡。

## Subagent 并行开发

并行开发必须先声明写入范围。不同 subagent 不要同时修改同一组文件。

- Schema Agent：负责 `packages/schema/**`、`packages/schema/typescript/**`、`packages/json-schema/**`、`docs/schema.md`。
- Backend Pipeline Agent：负责 `services/api/**` 和 `services/api/tests/**`。
- Renderer Agent：负责 `packages/renderer/**` 和 `packages/renderer/tests/**`。
- Web Agent：负责 `apps/web/**`。
- Integration Agent：负责跨模块 fixture、端到端测试、样例 artifact 和验收脚本。
- Docs/Coordinator Agent：负责 `README.md`、`AGENTS.md`、`goal.md`、`docs/worktree.md` 和任务拆分文档。

推荐合并顺序：

1. schema contract
2. renderer adaptation
3. backend pipeline adaptation
4. web integration
5. integration tests and docs

跨模块 contract 变更要先由 Schema Agent 落地版本、默认值、校验和测试，再让 Backend、Renderer、Web 并行适配。根配置、lockfile、README、AGENTS 和 goal 文档由协调者统一修改，避免冲突。

## 验证命令

- schema 改动：

```bash
.venv/bin/python -m pytest packages/schema/tests
```

- renderer 改动：

```bash
.venv/bin/python -m pytest packages/renderer/tests
```

- API/pipeline 改动：

```bash
.venv/bin/python -m pytest services/api/tests
```

- 跨模块 Python contract 改动：

```bash
.venv/bin/python -m pytest
.venv/bin/python -m compileall packages services
```

- 前端改动：

```bash
npm run typecheck:web
npm run build:web
```

- 端到端本地运行：

```bash
.venv/bin/python -m uvicorn app.main:app --reload --app-dir services/api
npm run dev:web
```

## 完成标准

- 功能路径能从用户入口跑通，而不是只完成局部函数。
- schema 输入输出有测试覆盖，错误输入会被拒绝并给出可理解错误。
- Python 和 TypeScript contract 同步。
- 涉及 renderer 的改动有溢出、缺失译文或角色不匹配等失败场景覆盖。
- 涉及前端的改动通过 typecheck，必要时通过 build。
- 文档更新说明新增能力、限制、验证命令和 subagent 影响范围。

## Project-control document map

- `SPEC.md` - Defines what the application must do, including user flows, requirements, and acceptance criteria.
- `NON_GOALS.md` - Defines what the application must not do yet, so Codex does not add risky or unnecessary features.
- `DATA_MODEL.md` - Defines stored data, entities, fields, relationships, validation rules, uniqueness rules, migration rules, and backup/restore expectations.
- `TEST_PLAN.md` - Defines how to verify the application, including manual checks, automated tests, data-safety tests, and regression tests.
- `DECISIONS.md` - Records important technical and product decisions, rejected options, assumptions, and decisions to revisit later.
- `README.md` - Gives a beginner-friendly entry point for humans using the repository.
- `PROJECT_MAP.md` - Explains the actual repository structure, important files, and how a request or command flows through the app.

## Required reading before work

Before modifying source code, Codex must read:

1. `AGENTS.md`
2. `SPEC.md`
3. `NON_GOALS.md`
4. `DATA_MODEL.md`
5. `TEST_PLAN.md`
6. `DECISIONS.md`
7. `PROJECT_MAP.md`, if present
8. The specific source files relevant to the task

## When to update each document

- Update `SPEC.md` when user-visible behavior, user flows, or requirements change.
- Update `NON_GOALS.md` when a feature is intentionally postponed or rejected.
- Update `DATA_MODEL.md` when entities, fields, validation, schema, storage, import/export, backup, or migration behavior changes.
- Update `TEST_PLAN.md` when new behavior needs verification or when test commands/checklists change.
- Update `DECISIONS.md` when an architecture, dependency, database, framework, or safety decision is made.
- Update `PROJECT_MAP.md` when important files, directories, commands, or request/data flow change.
- Update `README.md` when setup, usage, or project status changes.

## Conflict handling

If documents conflict with source code:

1. Treat source code as evidence of current behavior.
2. Treat `SPEC.md` and `DATA_MODEL.md` as intended behavior only if they are consistent and recent.
3. Do not silently rewrite code or docs to hide the conflict.
4. Record the conflict under "Open questions" or "Known discrepancies".
5. Ask the user which version should become the source of truth.

If the latest user instruction conflicts with existing documents, ask whether the documents should be updated.

## Work discipline

Before editing files, Codex must restate the requested change in plain English, list assumptions, list expected files to change, give a verification plan, and mention whether project-control documents need updates.

During implementation, Codex must make the smallest change that satisfies the task, avoid unrelated redesigns, avoid framework replacement unless explicitly requested, add or update tests when behavior changes, never use the real database for automated tests, and never silently delete or overwrite user data.

Before finishing, Codex must run relevant tests if available, report exact commands run and results, report files changed, report what was verified, report remaining risks, mention any behavior not verified, and mention whether any control documents were updated or should be updated.
