# Schema v0.1

`TranslationLayoutPlan@0.1` 是给大模型看的唯一输出 contract。它刻意停留在文本语义层，不允许返回绝对坐标。

v2 在该 contract 之上新增 workflow 智能排版骨架。`InputSource`、`AssetIR`、`UserIntent`、`WorkflowRun`、`SemanticLayoutAnalysis@0.1` 和 `LayoutIntentPlan@0.1` 用于记录输入归一化、用户意图、agent 语义识别、语义计划、步骤状态和诊断。`SemanticLayoutAnalysis` 和 `LayoutIntentPlan` 都只表达语义信号，不含 `bbox`、`x`、`y`、`page`、`width`、`height` 等坐标或分页字段。

## v2 Workflow Contract

- `InputSource`: 记录 text/image/pdf 输入类型、`source_role`、文件名、MIME、大小、hash、本地 artifact path 和诊断 flags。PDF workflow 使用 `content` 表示待翻译内容源，使用 `layout_reference` 表示可选版式语义参考源；未提供版式参考时写入 `layout_source_fallback_to_content`。
- `AssetIR`: 记录图片或参考资产、OCR/mock 摘要、alt text、来源 block 和不确定性 flags。
- `UserIntent`: 记录 `target_lang`、`output_kind`、`style_intent`、`typesetting_standard`、自然语言排版说明、保留策略和约束。后端会把包含 `GB/T 7713.1` 的说明归一化为 `gb_t_7713_1_2025`。
- `WorkflowRun`: 记录 workflow 状态、当前 step、进度、输入源、用户意图、artifact refs、错误和每个 `WorkflowStep`。
- `SemanticLayoutAnalysis`: 记录 block role candidates、section hints、asset usage hints、confidence 和质量 flags，是 debug/agent artifact，不直接传给 renderer。
- `LayoutIntentPlan`: 记录语义排版计划，不含坐标。renderer 可消费其中的 `render_intent` 和 asset usage，但最终坐标、分页、溢出和续页仍由 renderer 决定。

当前 agent loop 由 LangGraph `StateGraph` 编排为 `read_input -> analyze_intent -> semantic_recognize -> build_plan -> validate_plan -> translate -> render -> evaluate_render -> optional repair -> export_pdf -> complete`。未安装或未配置模型 provider 时，后端使用同一批节点的 deterministic fallback；配置 OpenAI-compatible provider 后，LangChain structured output 可增强 `SemanticLayoutAnalysis` 和 `LayoutIntentPlan`，但所有模型输出仍必须通过 Pydantic/schema 校验。当用户说明包含 `GB/T 7713.1` 时，plan 会写入 `gb_t_7713_1_requested`，`UserIntent.typesetting_standard` 会设为 `gb_t_7713_1_2025`，renderer defaults 会切到 `continuous_reflow`。标题/小节倾向 `emphasis`，参考文献和脚注倾向 `compact`；这些仍是语义意图，LLM 不返回坐标。

## LLM 输入

后端发送 `TranslationChunk`，每个 block 包含：

- `block_id`: renderer 用来把译文放回原始 `DocumentIR` block。
- `role`: 标题、摘要、正文、图注、公式、参考文献等语义角色。
- `source_text`: 发给模型的文本。若 parser 识别到公式，这里使用 `@@FORMULA_...@@` 占位符，而不是原始公式内容。
- `nearby_titles`: 局部上下文。
- `preserve_tokens`: 必须保留的 citation、公式占位符、reference marker、figure/table token。
- `requires_translation`: `false` 表示纯公式 block，translator 会本地保留，不发送给模型翻译。
- `context`: 文档标题、附近标题、当前 chunk 摘要和前一个 chunk 尾部，用于跨 chunk 连贯性。
- `glossary`: 术语表，translator prompt 要求按该表保持术语一致。

Parser 会在 `DocumentBlock.source_text` 保留原始文本，在 `DocumentBlock.text_for_translation` 写入带公式占位符的文本，并在 `DocumentBlock.formulas[]` 记录 `formula_id`、`placeholder`、`kind=inline|display`、原始公式文本、LaTeX、bbox、置信度和质量 flags。LLM 不应看到或改写原始公式；只需原样保留 `@@FORMULA_...@@`。

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

后端真实模型路径使用同一套 `validate_layout_plan`。OpenAI-compatible provider 不一定提供严格 JSON mode，translator 会从 `message.content` 中提取符合 `TranslationLayoutPlan` 形状的 JSON object，再执行校验；MiniMax-M3 路径会显式关闭 thinking 并启用 reasoning split。如果模型输出可机械修复，后端会移除坐标字段、补齐缺失 block、补齐 `preserve_tokens` 对应的 inline item，并在 block `quality_flags` 写入 `repaired_layout_plan`、`missing_block_repaired`、`preserve_token_repaired` 或 `formula_placeholder_repaired`。不可解析 JSON、无法提取 plan JSON 或请求失败会按 chunk 重试，最终失败会进入 job status。

## 默认排版值

- `target_lang`: `zh-CN`
- `font_stack`: `Times New Roman`, `SimSun`, `Songti SC`, `Noto Serif CJK SC`, `Source Han Serif SC`, `serif`。英文优先 Times New Roman；中文优先宋体/宋体兼容字体，系统缺少 SimSun 时由 Songti SC、Noto Serif CJK SC 或 Source Han Serif SC fallback。
- `line_height`: `1.35`
- `paragraph_spacing_em`: `0.45`
- `layout_mode`: `continuous_reflow`
- `page_layout`: A4 `595.28 x 841.89pt`，上/左 `70.87pt`，下/右 `56.69pt`
- `role_styles`: 默认 GB/T 7713.1 中文可读重排使用 title 18pt、heading 14pt、paragraph/abstract 12pt、caption/reference/footnote 10.5pt，并按角色设置加粗、对齐、首行缩进和段前后间距。
- `role_styles.*.font_stack`: 可选的按角色字体覆盖；为空时继承文档 `font_stack`。默认 title/heading 使用黑体栈（`Times New Roman`、`SimHei`、`Heiti SC`、`Noto Sans CJK SC`、`Source Han Sans SC`、`sans-serif`），正文保持宋体栈，符合 GB/T 7713.1 标题黑体、正文宋体的惯例。
- `formula_numbering`: `none`（默认）或 `parenthesized`。`parenthesized` 时 renderer 在 continuous_reflow 下对 display formula block 顺序编号，编号 `(n)` 右端对齐（GB/T 7713.1 公式编号规则）。若译文末尾已带源编号（如 `(12)`、`（3.4）`），保留原编号并写入 `formula_number_source_preserved` flag；一个 block 含多个 display formula 时跳过编号并写入 `formula_number_skipped_multi_display`。用户说明触发 GB/T 7713.1 时后端会自动把该字段设为 `parenthesized`。
- `overflow_policy.strategy`: `scale_then_expand_then_continue`
- `overflow_policy.min_font_scale`: `0.86`

`layout_mode=continuous_reflow` 是默认 GB/T 7713.1 中文可读重排：renderer 按全局阅读顺序生成连续 A4 页面和确定性 bbox，跳过竖排时间戳、重复页脚、无 path 的 vector placeholder，对长段做句子/词边界分页，并输出居中阿拉伯页码。`layout_mode=source_bbox` 可用于保留原 PDF 页面尺寸和 bbox，但字体、字号、行高、对齐和缩进仍应优先遵循 `RenderDefaults.role_styles`，再按缩放、扩盒、续页实现 overflow policy。

`RenderDefaults` 也可以通过本地运行配置持久化。`GET /api/config` 返回当前有效默认值，`PUT /api/config` 可更新字体栈、行高、段距和 `overflow_policy.min_font_scale` 等字段。后端会把同一份 `RenderDefaults` 写入 `TranslationChunk.render_defaults` 并传给 renderer；LLM 仍只能消费这些语义级约束，不能返回坐标或页面定位字段。

## 调试 Artifact

## JSON Schema 导出

Python 包提供 JSON Schema helper，默认 CLI 导出目录是 `packages/json-schema`：

```python
from pathlib import Path

from pdf_translator_schema import all_schemas, export_schema, schema_for

layout_plan_schema = schema_for("translation-layout-plan")
schemas = all_schemas()
exported_paths = export_schema(Path("packages/json-schema"))
```

也可以直接运行：

```bash
.venv/bin/python -m pdf_translator_schema.json_schema
```

导出文件包含 v2 workflow contract、semantic/layout intent contract、`DocumentIR`、`TranslationChunk`
和 `TranslationLayoutPlan` 等公开 schema，并统一带有 `$schema` 和 `x-schema-version` metadata。

任务完成后，后端会保存并通过 debug endpoint 暴露以下 JSON artifact：

- `normalized-input`: v2 adapter 归一化后的输入摘要，包含 `InputSource[]`、block/asset 数和质量 flags。
- `user-intent`: 当前任务使用的 `UserIntent`，包括默认值来源后的最终目标语言、输出类型、风格和说明。
- `workflow-run`: `WorkflowRun`，记录每个 step、attempt、输入输出 artifact、诊断和错误。
- `semantic-analysis`: `SemanticLayoutAnalysis`，记录 deterministic 或模型增强后的语义识别信号、置信度和 fallback flags。
- `layout-intent-plan`: deterministic agent 生成或修复后的 `LayoutIntentPlan`。
- `validation-and-repair`: plan validation 结果和 repair history。
- `asset-ir`: image adapter 或后续 OCR/视觉摘要 adapter 生成的 `AssetIR[]`。
- `document-ir`: parser 输出的 `DocumentIR`，是 bbox、页面尺寸和阅读顺序的事实来源。
- `translation-chunks`: chunker 发送给 translator 的 `TranslationChunk[]`，包含 `preserve_tokens`、附近标题、默认渲染值和约束。
- `translation-plans`: translator 通过 `validate_layout_plan` 后的 `TranslationLayoutPlan[]`。
- `translation-progress`: chunk 级翻译进度、失败信息和 repair/quality flag 汇总。
- `parser-diagnostics`: parser 阶段的页数、文本块、span/line metadata、资产、角色计数、复杂 PDF fallback flags，扫描版失败时也会写入该 artifact。
- `formula-candidates`: 公式 enrichment 在 OCR 前生成的 display/inline/image/vector 候选记录，用于定位漏检和误检。
- `formula-diagnostics`: 公式检测、OCR enrichment 和占位诊断，包含公式总数、行内/行间数量、LaTeX 成功数、fallback 数、低置信度项、未解析占位符和公式 OCR provider 状态。
- `ocr-recognition`: 公共 OCR service 的区域识别记录，包含 provider、confidence、attempts 和质量 flags。
- `ocr-diagnostics`: 公共 OCR service 的汇总诊断。当前公式模块是第一个消费者，整页 OCR 仅预留接口。
- `layout-trace`: renderer 的逐块排版决策日志，包括 layout mode、标准、render defaults snapshot、源页/输出页统计、跳过块/资产、页利用率、每个 source block 的分页片段、bbox、估算行数和质量 flags。
- `renderer-diagnostics`: renderer 根据 `DocumentIR + TranslationLayoutPlan` 生成的质量诊断，包括缺失译文、角色不一致、溢出 flags、页利用率、低利用率页、单片段页和被跳过 artifact 摘要。
- `render-evaluation`: 基于 renderer diagnostics 的结构化验收摘要，说明是否建议 repair。

这些 artifact 用于前端 schema inspector 和本地问题定位，不包含上传 PDF 本体或模型密钥。

## 资产保留

`DocumentIR.pages[].assets[]` 记录 parser 提取的 PDF 图片资产。每个 asset 包含稳定 `asset_id`、`page_id`、`kind`、`bbox`、可选 `path` 和 `alt_text`。`path` 指向本地 API asset endpoint，例如 `/api/documents/{doc_id}/assets/{asset_id}.png`。

Renderer 仍以 `DocumentIR` 为坐标事实来源，在原页面 bbox 位置渲染图片。缺失 asset path 时不会静默丢弃，而是输出 `asset_missing_path` 质量 flag 和占位元素。

表格样文本仍作为 `DocumentBlock(role="table")` 保留。parser 会在 `DocumentBlock.lines[]` 和 `DocumentBlock.spans[]` 中保存 PyMuPDF line/span 的文本、bbox、font、size、flags 和 origin；这些坐标是 parser-owned metadata，只供 detector/renderer 使用，不属于模型可返回字段。

公式现在是一等 metadata。parser 会优先把可由文本层启发式识别的公式归一化为 `DocumentBlock.formulas[]` 和 `@@FORMULA_...@@` 占位符，chunker 只把占位符送给 translator，renderer 再用 block-local formula metadata 回填行内或行间公式节点。PDF 路径也可在 parser 后执行公式 enrichment，识别 text-layer display formula、公式样 image/vector candidate 和段落内 inline formula run，生成 `DocumentIR.formulas[]`、必要的 `Asset(kind="formula")` 和兼容的 `{{formula:formula_id}}` preserve token。检测会清理 PDF 控制字符和常见符号编码，拒绝作者邮箱、长自然语言解释和 operator-only 残片，并在 `source_text_range` 上保留公式 token 的原始文本边界。`FormulaIR` 保存 `formula_id`、page/block/asset/anchor 引用、原文片段、span ids、LaTeX、inline/display mode、confidence、OCR provider、source kind 和 quality flags。OCR/model 返回的 `FormulaRecognitionResult` / `OCRRecognitionResult` 继承无坐标约束，只允许语义结果。默认 OCR 顺序是 Pix2Text -> deterministic：有 display crop 时优先视觉识别，Pix2Text 初始化或识别超时会写入 `pix2text_timeout` / `ocr_provider_unavailable` 并 fallback；AIP remapped font 等疑似坏文本层会标记 `formula_text_layer_corrupt`、`formula_slash_glyph_suspect` 或 `formula_prime_glyph_suspect` 并降低置信度。OpenAI 视觉公式识别可通过 `OCR_PROVIDER_ORDER=openai_vision,pix2text,deterministic` 加入，要求 API key，但不依赖 agent 视觉分析开关。

公式相关调试输出包括 `formula-candidates.json`、`formula-recognition.json`、`formula-diagnostics.json`、`ocr-recognition.json` 和 `ocr-diagnostics.json`。OCR 识别期间会增量写入 started/failed/recognized 记录，任务状态会显示 `Recognizing formulas n/total`，避免可选视觉 OCR 卡住时前端只能看到模糊断联。Renderer 以 `DocumentIR` 的 bbox 和 reading order 为布局事实，用本地 KaTeX 渲染公式 LaTeX；standalone block 渲染 display math，段落内 ref 渲染 inline math。无效 LaTeX 或被标记为腐败文本层的 display 公式会回退为 `formula_plaintext_fallback` 或 `formula_image_fallback`，缺失公式引用会记录 unresolved diagnostics；用户预览/PDF 不应出现裸 formula placeholder、红色 raw LaTeX 错误文本、空公式边框或 `.formula-render-failed` 节点。

复杂表格结构、公式矢量图和 PDF vector graphics 仍不是完整结构化资产；较大的 vector drawing 会以 `figure` placeholder asset 保留 bbox，`parser-diagnostics.fallback_flags[]` 会暴露 `table_text_fallback`、`formula_placeholder_normalized`、`vector_asset_placeholder`、`vector_assets_not_rasterized` 等状态，便于前端和测试识别当前保真边界。

`renderer-diagnostics.layout_issues[]` 是轻量视觉回归信号，记录同页 item overlap、bbox 越页和空 block 等风险。它不是像素级截图 diff，但可作为端到端验收前的 deterministic 门禁。

`packages/renderer/tests/fixtures/visual_regression.py` 是当前命名视觉回归样例。它构造一个最小 `DocumentIR + TranslationLayoutPlan`，并通过 `scripts/visual-regression.sh` 验证 overflow continuation、重叠、丢块风险、错误分页风险、结构化角色和资产占位的 renderer diagnostics/HTML token。该门禁不写入 HTML/PDF 生成物，避免把本地 artifact 纳入提交范围。

## 阅读顺序和角色

Parser 会在 `DocumentBlock.reading_order` 中保存用于 chunker 和 renderer 的阅读顺序。当前实现对常见双栏论文按栏分组排序，并过滤跨页重复页眉页脚候选。角色识别包含 `title`、`abstract`、`heading`、`paragraph`、`caption`、`formula`、`table`、`footnote` 和 `reference` 的基础规则。

扫描版或 image-only PDF 如果没有可翻译文本层，会在 parser 阶段以 `unsupported_scanned_pdf` 失败，并写入 `parser-diagnostics`，包含 `reason`、页数、文本块数、资产数和可恢复建议。公共 OCR service 已预留 page OCR 接口，但本轮只要求公式区域 OCR 跑通；整页扫描 OCR 仍是后续增强，避免阻塞数字版论文路径。

Text input 由 text adapter 转换为带虚拟页面和稳定 block id 的 `DocumentIR`。Image input 会保存原图 asset，并在未配置 OCR provider 时写入 deterministic OCR mock 摘要和 `ocr_uncertain` flag，保证本地端到端验证仍可运行。
