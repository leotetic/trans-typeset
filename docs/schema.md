# Schema v0.1

`TranslationLayoutPlan@0.1` 是给大模型看的唯一输出 contract。它刻意停留在文本语义层，不允许返回绝对坐标。

## LLM 输入

后端发送 `TranslationChunk`，每个 block 包含：

- `block_id`: renderer 用来把译文放回原始 `DocumentIR` block。
- `role`: 标题、摘要、正文、图注、公式、参考文献等语义角色。
- `source_text`: 原文。
- `nearby_titles`: 局部上下文。
- `preserve_tokens`: 必须保留的 citation、公式、reference marker、figure/table token。
- `context`: 文档标题、附近标题、当前 chunk 摘要和前一个 chunk 尾部，用于跨 chunk 连贯性。
- `glossary`: 术语表，translator prompt 要求按该表保持术语一致。

## LLM 输出

模型必须返回：

- `schema_version: "0.1"`
- `chunk_id`
- `target_lang`
- `blocks[]`

每个 `blocks[]` 元素只允许包含：

- `source_block_id`
- `translated_text`
- `inline_items`
- `role`
- `render_intent`
- `quality_flags`

renderer 负责坐标、分页、溢出、字号缩放和 continuation page。模型不得返回 `bbox`、`x`、`y`、`page` 等布局坐标字段；Pydantic models 使用 `extra="forbid"` 拒绝这些字段。

后端真实模型路径使用同一套 `validate_layout_plan`。如果模型输出可机械修复，后端会移除坐标字段、补齐缺失 block、补齐 `preserve_tokens` 对应的 inline item，并在 block `quality_flags` 写入 `repaired_layout_plan`、`missing_block_repaired` 或 `preserve_token_repaired`。不可解析 JSON 或请求失败会按 chunk 重试，最终失败会进入 job status。

## 默认排版值

- `target_lang`: `zh-CN`
- `font_stack`: `Noto Sans CJK SC`, `Source Han Sans SC`, `Arial Unicode MS`, `sans-serif`
- `line_height`: `1.35`
- `paragraph_spacing_em`: `0.45`
- `overflow_policy.strategy`: `scale_then_expand_then_continue`
- `overflow_policy.min_font_scale`: `0.86`

当前 renderer 对该策略的实现顺序是缩放、扩盒、续页。扩盒只在原页面可用空间内进行；仍放不下时生成 continuation page，并输出 `font_scaled`、`box_expanded`、`continued_on_next_page`、`continuation_page` 等诊断 flag。

`RenderDefaults` 也可以通过本地运行配置持久化。`GET /api/config` 返回当前有效默认值，`PUT /api/config` 可更新字体栈、行高、段距和 `overflow_policy.min_font_scale` 等字段。后端会把同一份 `RenderDefaults` 写入 `TranslationChunk.render_defaults` 并传给 renderer；LLM 仍只能消费这些语义级约束，不能返回坐标或页面定位字段。

## 调试 Artifact

任务完成后，后端会保存并通过 debug endpoint 暴露以下 JSON artifact：

- `document-ir`: parser 输出的 `DocumentIR`，是 bbox、页面尺寸和阅读顺序的事实来源。
- `translation-chunks`: chunker 发送给 translator 的 `TranslationChunk[]`，包含 `preserve_tokens`、附近标题、默认渲染值和约束。
- `translation-plans`: translator 通过 `validate_layout_plan` 后的 `TranslationLayoutPlan[]`。
- `translation-progress`: chunk 级翻译进度、失败信息和 repair/quality flag 汇总。
- `parser-diagnostics`: parser 阶段的页数、文本块、资产、角色计数、复杂 PDF fallback flags，扫描版失败时也会写入该 artifact。
- `renderer-diagnostics`: renderer 根据 `DocumentIR + TranslationLayoutPlan` 生成的质量诊断，包括缺失译文、角色不一致和溢出 flags。

这些 artifact 用于前端 schema inspector 和本地问题定位，不包含上传 PDF 本体或模型密钥。

## 资产保留

`DocumentIR.pages[].assets[]` 记录 parser 提取的 PDF 图片资产。每个 asset 包含稳定 `asset_id`、`page_id`、`kind`、`bbox`、可选 `path` 和 `alt_text`。`path` 指向本地 API asset endpoint，例如 `/api/documents/{doc_id}/assets/{asset_id}.png`。

Renderer 仍以 `DocumentIR` 为坐标事实来源，在原页面 bbox 位置渲染图片。缺失 asset path 时不会静默丢弃，而是输出 `asset_missing_path` 质量 flag 和占位元素。

表格样文本和公式文本当前作为 `DocumentBlock` 保留，角色分别标记为 `table` 和 `formula`，renderer 会使用专用 table/formula/footnote CSS 规则。复杂表格结构、公式矢量图和 PDF vector graphics 仍不是完整结构化资产；较大的 vector drawing 会以 `figure` placeholder asset 保留 bbox，`parser-diagnostics.fallback_flags[]` 会暴露 `table_text_fallback`、`formula_text_fallback`、`vector_asset_placeholder`、`vector_assets_not_rasterized` 等状态，便于前端和测试识别当前保真边界。

`renderer-diagnostics.layout_issues[]` 是轻量视觉回归信号，记录同页 item overlap、bbox 越页和空 block 等风险。它不是像素级截图 diff，但可作为端到端验收前的 deterministic 门禁。

`packages/renderer/tests/fixtures/visual_regression.py` 是当前命名视觉回归样例。它构造一个最小 `DocumentIR + TranslationLayoutPlan`，并通过 `scripts/visual-regression.sh` 验证 overflow continuation、重叠、丢块风险、错误分页风险、结构化角色和资产占位的 renderer diagnostics/HTML token。该门禁不写入 HTML/PDF 生成物，避免把本地 artifact 纳入提交范围。

## 阅读顺序和角色

Parser 会在 `DocumentBlock.reading_order` 中保存用于 chunker 和 renderer 的阅读顺序。当前实现对常见双栏论文按栏分组排序，并过滤跨页重复页眉页脚候选。角色识别包含 `title`、`abstract`、`heading`、`paragraph`、`caption`、`formula`、`table`、`footnote` 和 `reference` 的基础规则。

扫描版或 image-only PDF 如果没有可翻译文本层，会在 parser 阶段以 `unsupported_scanned_pdf` 失败，并写入 `parser-diagnostics`，包含 `reason`、页数、文本块数、资产数和可恢复建议。OCR 尚未实现；该 fallback 是 Phase 4 的明确失败策略，避免后续 chunker/translator 产生不可理解错误。
