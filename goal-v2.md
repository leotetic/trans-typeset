# Goal v2: Smart Typesetting Workflow

本阶段目标是在现有 PDF 文献翻译与排版 MVP 之上，初步实现“智能排版”的底层架构。v2 不应推翻当前流水线，而是把它升级为一个本地优先、可审计、可分阶段交付的 workflow-based agent loop：系统能够接受文字、图片和 PDF 文件，先读取和理解输入内容、素材结构与用户排版意图，再生成语义级排版计划，最后由 deterministic renderer 完成坐标、分页、溢出和导出。

核心原则：agent 负责理解、规划、校验和修复；renderer 负责落版。模型不得直接控制绝对坐标、页面编号或 bbox。

## Product Direction

v2 的产品定位是本地智能排版工作台，而不是营销页、Zotero 插件或一次性通用文档生成器。第一屏仍然必须是可用工具界面，用户可以提交输入、描述目标、查看任务状态、预览结果并下载 artifact。

第一版支持三类输入：

- `Text input`: 用户粘贴或上传纯文本，用于生成结构化排版文档，也可作为 PDF 翻译的补充说明或排版指令。
- `Image input`: 用户上传截图、扫描图片、参考版式图或含文字图片。第一版通过 OCR/视觉摘要适配器提取文字、粗略结构、资产和版式意图；不承诺复杂图表重建或高保真视觉复刻。
- `PDF input`: 继续沿用当前数字版论文 PDF 解析能力，把 PDF 转换为 `DocumentIR`，再进入 v2 workflow。扫描版 PDF 可以通过 image adapter 或后续 OCR fallback 逐步接入。

v2 输出包括：

- 可预览 HTML。
- 可下载 PDF。
- workflow artifact，例如 normalized input、intent、plans、diagnostics、repair history。
- 用户可理解的失败原因，例如 OCR 不可用、输入为空、计划缺块、保留 token 丢失、renderer overflow 或资产缺失。

## Core Pipeline

v2 目标流水线为：

```text
Text / Image / PDF input
  -> InputAdapter
  -> DocumentIR / AssetIR / UserIntent
  -> WorkflowRun
  -> AgentLoop(read -> analyze -> plan -> validate -> repair -> render -> evaluate)
  -> LayoutIntentPlan / TranslationLayoutPlan
  -> renderer
  -> preview / PDF / diagnostics
```

### InputAdapter

`InputAdapter` 是 v2 的统一入口，负责把不同输入归一化为后续 workflow 可消费的结构。

- Text adapter: 将纯文本切成 section、paragraph、list、heading 等 block，生成无页面坐标或虚拟页面的 `DocumentIR`。
- Image adapter: 将图片保存为 asset，生成 `ImageIR`/`AssetIR`，并通过 OCR 或视觉摘要提取文字、阅读顺序、版式线索和不确定性 flags。
- PDF adapter: 复用现有 parser，输出包含页面尺寸、block bbox、阅读顺序、角色和 asset 的 `DocumentIR`。

所有 adapter 必须输出稳定 id、明确输入类型、可诊断 flags 和原始 artifact 引用。adapter 不负责最终排版美化。

### UserIntent

`UserIntent` 表达用户对结果的语义要求，来自显式表单、自然语言说明和默认配置。第一版至少覆盖：

- `target_lang`
- `output_kind`: translation、typeset_document、layout_reference、summary_layout
- `style_intent`: academic、report、handout、slide_like、plain
- `preserve_policy`: citations、formulas、tables、figures、reference markers
- `reference_assets`: 用户提供的图片或 PDF 作为内容素材或版式参考
- `constraints`: 页宽、目标字号、是否允许续页、是否保留图片

如果用户没有填写，系统使用可运行默认值，并在 workflow artifact 中记录默认来源。

### WorkflowRun

`WorkflowRun` 是一次智能排版任务的状态容器。它应记录：

- 输入文件和 normalized input。
- 当前 step、进度、错误和可恢复状态。
- agent 每轮的输入、输出、校验结果和修复记录。
- renderer diagnostics 和最终 artifact。

后端任务状态要从当前单向 pipeline 扩展为 workflow-aware 状态，但不能破坏已有 queued、parsing、translating、rendering、completed、failed、canceled 语义。可以在 artifact 中先记录更细 step，再逐步暴露到 API/UI。

## Agent Loop Boundaries

v2 内置的小型 workflow-based agent loop 应是可测试状态机，不是不可控的自由代理。

允许 agent 做的事情：

- 读取 normalized text、image OCR/summary、PDF `DocumentIR`、用户意图、glossary、render defaults 和 renderer diagnostics。
- 生成 `LayoutIntentPlan`，描述章节结构、block 映射、角色、重要性、排版意图、资产关系和质量 flags。
- 调用现有 translator 生成或修复 `TranslationLayoutPlan`。
- 判断计划是否覆盖所有 block、是否保留 tokens、是否违反 schema、是否需要 repair。
- 根据 renderer diagnostics 做有限轮次修复，例如压缩意图、拆分段落、标记续页、降低排版密度。

禁止 agent 做的事情：

- 在 LLM 输出中写入 `bbox`、`bounding_box`、`x`、`y`、`x0`、`y0`、`x1`、`y1`、`width`、`height`、`page`、`page_id`、`page_index`、`page_number`、`top`、`right`、`bottom`、`left` 等绝对布局字段。
- 直接写用户文件、源码文件、密钥或未脱敏 fixture。
- 绕过 schema validation 或 renderer diagnostics。
- 在失败时静默回退为空白预览。

建议第一版 loop 固定为最多 2 到 3 轮：

```text
read_input
  -> analyze_intent
  -> build_plan
  -> validate_plan
  -> render
  -> evaluate_render
  -> optional_repair
```

每一轮都要可落 artifact，便于 Codex、测试和用户审计。

## Contract Direction

v2 需要在现有 `DocumentIR`、`TranslationChunk`、`TranslationLayoutPlan@0.1` 基础上新增或扩展 contract，但不能把绝对布局决策交给模型。

优先引入的 contract：

- `InputSource`: 描述 text/image/pdf 输入、文件名、MIME、大小、hash 和本地 artifact path。
- `AssetIR`: 描述图片、PDF 提取资产、OCR 文本、alt text、来源和不确定性 flags。
- `UserIntent`: 描述目标语言、输出类型、风格意图、保留规则和用户约束。
- `WorkflowRun`: 描述 workflow 状态、step、attempt、artifact refs、错误和诊断。
- `LayoutIntentPlan`: agent 输出的语义级排版计划，不含坐标字段。

`LayoutIntentPlan` 第一版只表达：

- block 或 section 的映射关系。
- semantic role 和 priority。
- render intent，例如 normal、compact、emphasis、preserve_asset、callout、reference_layout。
- asset usage，例如 preserve、inline_reference、background_reference、ignore。
- quality flags，例如 ocr_uncertain、intent_ambiguous、missing_source_block、overflow_risk、needs_human_review。

schema 变化必须同步 Python models、TypeScript types、JSON Schema、docs/schema.md 和测试。任何 breaking change 都要先由 Schema Agent 落地，再让 backend、renderer、web 适配。

## Renderer Direction

renderer 仍是排版权威。v2 不能让模型返回最终页面坐标，而是让 renderer 根据以下输入确定性落版：

- `DocumentIR` 的页面尺寸、block bbox、阅读顺序、style seed 和 assets。
- `LayoutIntentPlan` 的语义意图。
- `TranslationLayoutPlan` 的译文和 inline items。
- `RenderDefaults` 和用户允许的 constraints。
- renderer 自己的 overflow policy 和 diagnostics。

renderer v2 需要逐步支持：

- 无原始页面坐标的 text input 排版。
- 有参考图片但无可靠 bbox 的 image input 排版。
- PDF 原始 bbox 保留和智能续页。
- 根据 agent plan 应用 compact、emphasis、callout、preserve_asset 等语义意图。
- 将 overflow、重叠、空块、资产缺失、角色不一致写入 diagnostics。

如果缺失译文或计划失败，renderer 可以回退 source text，但必须输出质量 flag。

## Backend Direction

后端从当前单向 orchestrator 升级为 workflow orchestrator，但应分阶段进行，避免一次性重写。

第一阶段保留现有 PDF 路径，并新增 workflow artifact：

- normalized input artifact。
- user intent artifact。
- workflow steps artifact。
- agent plan artifact。
- validation and repair artifact。

第二阶段新增 text adapter，使用户不上传 PDF 也能生成可预览排版结果。

第三阶段新增 image adapter，先支持保存图片 asset、OCR/视觉摘要结果和可诊断 fallback。没有 OCR 依赖时，应允许 deterministic image summary mock，以便本地端到端测试。

第四阶段把 PDF adapter、translator、renderer 统一纳入 workflow run。真实模型失败、schema 校验失败、OCR 失败、render 失败都要落到 job status 和 artifact。

## Frontend Direction

前端仍是本地工作台，不能承载后端 pipeline 逻辑。

v2 前端应逐步支持：

- 输入模式切换：Text、Image、PDF。
- 用户意图输入：目标语言、输出类型、风格意图、补充说明。
- 任务状态展示：adapter、analysis、planning、translation、rendering、evaluation、repair、completed、failed。
- artifact inspector：normalized input、intent、plans、diagnostics、repair history。
- 预览/下载失败的可恢复 UI。

第一屏应保持工具界面，不做营销 hero。桌面和移动布局都要避免上传区、状态区、预览区互相遮挡。

## Phases

### Phase 1: Contract And Workflow Skeleton

目标是建立 v2 的最小可审计骨架。

- 定义 `InputSource`、`UserIntent`、`WorkflowRun`、`WorkflowStep`、`LayoutIntentPlan` 的 schema 草案。
- 在后端保存 workflow artifact，但暂时可以让 PDF 路径继续走现有 pipeline。
- deterministic agent loop 可用：不调用模型，也能产出合法 plan 和 diagnostics。
- 测试覆盖 schema validation、禁止坐标字段、artifact 写入和失败状态。

### Phase 2: Text Input Smart Typesetting

目标是让非 PDF 的纯文本输入跑通完整 workflow。

- Text adapter 将文本归一化为 `DocumentIR` 或 text-specific IR。
- agent 根据文本结构和 `UserIntent` 生成 `LayoutIntentPlan`。
- renderer 支持无原始 PDF bbox 的基础排版。
- 前端支持粘贴文本、填写意图、预览和下载。

### Phase 3: Image Input Adapter

目标是让图片成为可读、可诊断的输入。

- Image adapter 保存原图资产并输出 `AssetIR`。
- 支持 OCR/视觉摘要适配器；未配置 OCR 时使用 deterministic mock。
- 从图片中提取文字、粗略结构、阅读顺序和不确定性 flags。
- renderer 能保留图片资产，并根据摘要生成基础排版结果。
- 扫描版 PDF 可在后续复用 image adapter，但不是本阶段阻塞项。

### Phase 4: PDF Workflow Integration

目标是把现有 PDF 翻译排版路径纳入 v2 workflow。

- PDF adapter 复用当前 parser，继续输出 `DocumentIR`。
- chunker、translator、layout plan validation 和 renderer 都成为 workflow steps。
- chunk-level repair、preserve token repair、renderer overflow diagnostics 写入 workflow artifact。
- 前端 inspector 能按 step 查看输入、输出和错误。

### Phase 5: Render Evaluation And Repair Loop

目标是让“智能排版”具备可控的反馈修复能力。

- renderer 输出结构化 evaluation summary。
- agent 根据 overflow、重叠、空块、资产缺失等 diagnostics 做有限轮 repair。
- repair 只能修改语义 plan、压缩意图、分块策略或质量 flags，不能写坐标。
- 每次 repair 都保存 attempt、原因、前后 diff 和最终是否接受。

## Parallel Subagent Plan

推荐按以下边界拆分，避免多个 agent 同时写同一批文件：

- Schema Agent: `packages/schema/**`、`packages/schema/typescript/**`、`packages/json-schema/**`、`docs/schema.md`。
- Backend Workflow Agent: `services/api/**`，负责 adapters、workflow orchestrator、agent loop、artifact 和 API。
- Renderer Agent: `packages/renderer/**`，负责无 bbox 排版、semantic intent、diagnostics 和 repair 支持。
- Web Agent: `apps/web/**`，负责输入模式、intent UI、状态展示、preview 和 inspector。
- Integration Agent: 负责跨模块 fixture、端到端测试、acceptance 命令和本地样例。
- Docs/Coordinator Agent: 负责 `goal-v2.md`、`README.md`、`AGENTS.md`、`docs/worktree.md` 和任务拆分。

合并顺序应为 schema first，然后 backend skeleton，再 renderer adaptation，再 web integration，最后 integration tests 和 docs 收口。

## Definition Of Done

v2 功能只有满足以下条件才算完成：

- 用户路径从输入到预览/下载可运行。
- 每个 workflow step 都有 artifact 或诊断输出。
- schema 拒绝模型返回坐标字段。
- deterministic 本地路径在没有模型密钥、没有 OCR provider 时仍可端到端验证。
- 真实模型/OCR/provider 失败会落到 job status，并给出可理解错误。
- renderer diagnostics 能暴露溢出、重叠、空块、资产缺失和缺失译文。
- Python schema、TypeScript types、JSON Schema、文档和测试同步。
- 前端 typecheck/build 通过，涉及 renderer 的改动有模型层或视觉回归测试。
- 生成物、缓存、上传文件、输出 PDF、密钥和用户文档不进入提交范围。

## Non-Goals

v2 第一版不做以下事项：

- 不做 Zotero 插件。
- 不做营销页或纯展示站点。
- 不承诺扫描版 PDF 全量 OCR 产品化。
- 不承诺复杂图表、公式、矢量图和任意参考图片的高保真复刻。
- 不让 LLM 直接输出绝对定位坐标或页面布局字段。
- 不把用户文档、模型密钥、OCR 结果或上传文件写入源码和测试 fixture，除非是明确脱敏的最小样例。
