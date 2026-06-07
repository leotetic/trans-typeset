# Goal

本项目的最终目标是构建一个本地优先、脱离 Zotero、受 PDF2zh 启发但独立演进的英文论文 PDF 翻译与排版系统。用户上传英文文献 PDF，选择目标语言，例如中文，系统在本地后端完成解析、分块、翻译、schema 校验、排版渲染，并输出可预览、可下载、尽量保真且可诊断的译文 PDF。

## Product Target

第一屏就是可用工作台：上传 PDF、选择目标语言、查看任务进度、预览译文、下载结果。系统不依赖 Zotero 插件、不要求外部文献管理器，也不把用户文档默认上传到非必要服务。真实模型调用应兼容 OpenAI-style provider；没有模型密钥时，本地 deterministic translator 继续作为端到端开发和回归测试模式。

长期产品能力包括：

- 本地上传英文论文，输出指定语言的译文 PDF。
- 支持纯译文排版，后续扩展双语对照、术语表、模型配置和任务历史。
- 支持任务恢复、取消、重试、失败诊断和 artifact 查看。
- 对用户暴露可理解的翻译与排版状态，包括 chunk、保留 token、schema plan、质量 flags、溢出和缺失翻译。
- 优先做好数字版论文 PDF；扫描版/OCR、复杂图表和高度视觉保真作为后续阶段。

## Architecture Target

核心流水线固定为：

```text
PDF upload
  -> DocumentIR
  -> TranslationChunk[]
  -> LLM TranslationLayoutPlan[]
  -> schema validation and repair
  -> RenderDocument
  -> HTML preview
  -> PDF export
```

`DocumentIR` 是解析器和渲染器之间的事实来源，包含页面尺寸、block 坐标、阅读顺序、语义角色、样式种子和后续资产引用。`TranslationChunk` 是给模型的分块输入，包含一组论文 block、上下文、附近标题、保留 token、术语表、默认渲染约束。`TranslationLayoutPlan` 是模型唯一允许返回的 contract，表达每个 source block 的译文、inline items、语义角色、render intent 和质量标记。

关键边界：LLM 不负责绝对坐标、分页和最终版面。模型只返回大模型能稳定理解和生成的语义级排版计划；后端 renderer 基于 `DocumentIR + TranslationLayoutPlan + RenderDefaults` 做确定性排版。

## Schema Direction

当前 `TranslationLayoutPlan@0.1` 已经建立了正确边界：模型不得返回 `bbox`、`x`、`y`、`page` 等布局坐标字段。后续 schema 要继续沿这个方向变精细，而不是把坐标决策交给模型。

目标 schema 需要逐步覆盖：

- block 映射：每个 `source_block_id` 必须恰好对应一个译文块。
- 语义角色：title、abstract、heading、paragraph、caption、formula、table、figure、footnote、reference。
- inline items：citation、formula、reference marker、asset reference、术语、不可翻译 token。
- 保留策略：引用编号、作者年份引用、公式编号、图表编号、参考文献 marker 必须可校验。
- render intent：normal、compact、emphasis、preserve_asset，以及后续可能的 list、quote、continued、table_cell 等语义意图。
- 质量 flags：missing token、uncertain translation、role mismatch、overflow risk、needs human review。
- 默认值：目标语言、字体栈、行高、段距、对齐、溢出策略、资产保留策略必须有可运行默认值。

默认排版基线继续以 `RenderDefaults` 为准：

- `target_lang`: `zh-CN`
- `font_stack`: `Noto Sans CJK SC`, `Source Han Sans SC`, `Arial Unicode MS`, `sans-serif`
- `line_height`: `1.35`
- `paragraph_spacing_em`: `0.45`
- `overflow_policy.strategy`: `scale_then_expand_then_continue`
- `overflow_policy.min_font_scale`: `0.86`

任何 schema 变更都必须同步 Python models、TypeScript types、JSON Schema、文档和测试。

## Chunking And Translation

翻译输入不是整篇论文一次性塞给模型，而是按论文结构分块。分块器应从当前字符数切分逐步升级为 section-aware、page-aware、figure/table-aware 的 chunk builder。

每个 chunk 至少应包含：

- `chunk_id`
- `target_lang`
- `source_blocks[]`
- `source_blocks[].block_id`
- `source_blocks[].role`
- `source_blocks[].source_text`
- `source_blocks[].nearby_titles`
- `source_blocks[].preserve_tokens`
- `context`
- `glossary`
- `render_defaults`
- `constraints`

翻译器必须做到：

- 覆盖 chunk 内所有 source block。
- 不丢失 preserve tokens。
- 返回严格 JSON object。
- 失败时提供可诊断错误，并支持后续 repair/retry。
- 真实模型和 deterministic mock 共享同一校验路径。

## Renderer Direction

renderer 是排版权威，不是模型输出的被动展示层。它必须能根据 `DocumentIR` 的页面、bbox、阅读顺序和样式种子，再结合模型返回的 semantic plan，生成稳定 HTML 和 PDF。

renderer 长期职责包括：

- 保留原页面尺寸和 block 位置约束。
- 根据默认值和角色应用字体、行高、对齐、段距。
- 消费 inline items，正确处理 citation、formula、reference marker 和 asset reference。
- 执行 overflow policy：缩放、扩盒、续页、质量标记。
- 保留或重建图片、表格、公式和图表标题关系。
- 对缺失译文、角色不一致、溢出、资产缺失输出质量 flags。
- 提供 preview HTML、PDF export 和调试 artifact。

当前 renderer 已能基于原始 bbox 做 HTML/CSS 绝对定位和 Playwright PDF 导出；下一阶段重点是让默认 overflow policy 真正落地，并让资产、inline items 和续页成为一等能力。

## Phases

### Phase 1: Text Paper MVP

目标是稳定处理数字版英文论文的文本层：

- PDF 上传、语言选择、任务状态、预览、下载闭环可用。
- PyMuPDF 解析基础文本 block。
- 字符数分块和 preserve token 提取可用。
- deterministic translator 和 OpenAI-compatible translator 共享 schema 校验。
- renderer 输出纯译文 HTML/PDF。
- schema、API、renderer、前端有基本测试门禁。

### Phase 2: Contract And Diagnostics

目标是让系统可审计、可调试、可并行开发：

- 增强 `TranslationLayoutPlan` 的 inline items、quality flags 和 render intent。
- 增加 artifact/debug endpoints，暴露 DocumentIR、chunks、plans、renderer diagnostics。
- 前端增加 schema inspector、任务历史、刷新恢复和模型配置。
- 后端增加 chunk 级 retry、repair、并发控制和更细进度。
- renderer 实现真实 overflow policy 和 continuation page。

### Phase 3: Layout Fidelity

目标是接近论文排版保真：

- 提取和保留图片、表格、公式、矢量或栅格资产。
- 改进多栏阅读顺序、页眉页脚过滤、脚注和参考文献处理。
- 引入视觉回归样例，检测溢出、重叠、丢块和错误分页。
- 支持术语表、专有名词一致性和跨 chunk 上下文。

### Phase 4: Robust Local Product

目标是成为可长期使用的本地文献翻译工具：

- 任务队列持久化，支持恢复、取消、批量处理。
- 本地任务历史和 artifact 管理。
- 可配置 provider、model、base URL、API key、目标语言和渲染默认值。
- 扫描版 OCR 和复杂 PDF fallback 策略。
- 发布前端、后端、schema、renderer 的自动验收流程。

## Parallel Subagent Plan

并行开发应按 contract 边界拆分，避免多个 agent 修改同一片文件。

- Schema Agent：负责 `packages/schema/**`、`packages/schema/typescript/**`、`packages/json-schema/**`、`docs/schema.md`。先定义 contract，再推动其他模块适配。
- Backend Pipeline Agent：负责 `services/api/**`，重点是 parser、chunker、translator、orchestrator、storage、API routes 和测试。
- Renderer Agent：负责 `packages/renderer/**`，重点是 overflow、inline items、assets、continuation page、HTML/PDF 输出和渲染测试。
- Web Agent：负责 `apps/web/**`，重点是上传工作台、任务状态、预览、schema inspector、设置和前端 QA。
- Integration Agent：负责跨模块 fixture、端到端测试、样例 PDF、artifact 验收和回归门禁。
- Docs/Coordinator Agent：负责 `README.md`、`AGENTS.md`、`goal.md`、协作计划和任务拆分，不直接改业务代码。

推荐合并顺序是 schema first，然后 renderer/backend 适配，最后 web 和 integration 收口。任何 breaking contract change 都必须先落 schema version、迁移说明和测试，再让其他 agent 并行升级。

## Definition Of Done

一个功能只有在以下条件满足时才算完成：

- 用户路径可运行，而不是只有局部函数通过。
- schema contract 被验证，错误输入有明确失败信息。
- 相关 Python/TypeScript 类型同步。
- 相关测试覆盖正常路径和至少一个失败路径。
- 生成物、缓存、上传文件、输出 PDF 不进入提交范围。
- 文档说明了新增能力、限制和验证命令。
