# Plan v2: 行间公式识别与渲染优化

本计划聚焦当前公式链路的主要缺陷：行间普通字母、公式片段、自然语言和真正数学公式没有被稳定区分；被识别为公式后也可能没有走 LaTeX/KaTeX 渲染，最终回退到普通字体或错误文本，导致视觉上既不像论文正文，也不像数学排版。

目标是把公式处理从“启发式替换”推进到可验收的独立链路：

```text
PDF parser
  -> DocumentIR blocks / spans / assets
  -> formula detection / recognition / normalization
  -> formula preserve token
  -> TranslationChunk / TranslationLayoutPlan
  -> renderer formula node
  -> HTML preview / PDF export / diagnostics
```

## 1. 根因拆解

### 1.1 语义识别粒度不足

PDF text layer 经常把公式拆成多个 span、line 或 block。当前逻辑如果只看 block 文本，很容易把 `n`、`vn`、`dx`、`dB` 这类公式碎片当作独立正文，也可能把普通单字母变量、脚注、作者信息或自然语言解释误判成公式。

需要把判定粒度提升到 span/run/block 三层：

- span/run 层识别字体、符号密度、上下标、希腊字母、运算符、分式/根号结构。
- block 层判断是否是 display equation，而不是普通段落或作者脚注。
- 邻近 block 层合并被 PDF 切碎的同一条行间公式。

### 1.2 普通字母与数学公式没有分流

普通字母本身不等于公式。真正公式通常至少具备以下信号之一：

- 明确运算符：`=`, `+`, `-`, `×`, `/`, `∂`, `∑`, `∫`, `√`, `≤`, `≥`。
- 上下标、括号结构或函数结构：`x_i`, `E^{...}`, `f(x)`。
- 数学字体或符号字体：Math, Symbol, STIX, CM, Cambria Math 等。
- display equation 的空间特征：居中、短行、与正文有上下间距、同一公式多个残片相邻。

缺少这些信号的普通拉丁字母应保留为正文文本，避免被送入公式渲染。

### 1.3 LaTeX 规范化和渲染失败

PDF 抽取结果可能包含控制字符、乱码映射、软换行和断词，例如 `¼`、`þ`、`ð`、`\u0001`、`\u0003`。这些文本如果直接进入 `data-latex`，KaTeX 会失败，浏览器或 renderer 可能显示红色 raw LaTeX 或普通 fallback 字体。

需要在公式识别和 renderer 之间增加 renderability gate：

- 先把常见 PDF 字符规范化到可渲染 LaTeX。
- 对自然语言混入公式的候选做截断或拒绝。
- 对括号/花括号不平衡的公式做修正或降级。
- 不可渲染时回退为低调可读的图片或 plaintext fallback，不显示裸 token、红色错误文本或空框。

### 1.4 Contract 边界不够明确

公式不应由 LLM 自由改写。LLM 只应保留公式 preserve token；公式的识别、坐标、图片 fallback 和最终渲染应由 parser/enrichment/renderer 负责。

当前需要稳定两类 token：

- 旧格式：`@@FORMULA_...@@`
- 新格式：`{{formula:formula_id}}`

短期保持双格式兼容，长期可以统一到 `{{formula:...}}`。

## 2. 修改方向

### 2.1 Schema

把公式提升为 `DocumentIR` 的一等 metadata：

- 增加或完善 `FormulaIR`。
- 在 `DocumentIR.formulas[]` 保存全局公式列表。
- `DocumentBlock.formula_id` 用于 display formula block。
- `Asset.formula_id` 用于公式截图或视觉 fallback。
- `FormulaIR` 保存 `page_id`、`source_block_id`、`anchor_block_id`、`asset_id`、`source_text`、`source_text_range`、`span_ids`、`latex`、`display_mode`、`confidence`、`ocr_provider`、`source_kind`、`quality_flags`。

同步文件：

- `packages/schema/pdf_translator_schema/models.py`
- `packages/schema/typescript/src/index.ts`
- `packages/json-schema/*.schema.json`
- `docs/schema.md`
- schema tests

### 2.2 Parser 与公式 enrichment

在 parser 输出 `DocumentIR` 后执行公式 enrichment：

- 基于 span 字体和符号密度识别 inline formula。
- 基于 block role、bbox、数学信号和自然语言比例识别 display formula。
- 对相邻 formula-like block 做合并，避免漂浮碎片。
- 对 image/vector-like asset 生成可选公式候选。
- 清理纯控制字符、不可见文本、孤立乱码和明显作者/邮箱/DOI/URL 文本。

候选拒绝规则必须覆盖：

- 作者邮箱、DOI、URL、caption 普通文本。
- 长自然语言句子。
- `defined as`、`where`、`while`、`into the equation` 等解释性短语。
- 单个普通字母或无数学信号的短词。
- 只含控制字符或乱码残片的 block。

### 2.3 LaTeX normalization

增加轻量 normalization：

- `¼ -> =`
- `þ -> +`
- `∂ -> \partial`
- `∇ -> \nabla`
- 删除或替换 `\u0001`、`\u0003`、`\u0004` 等控制字符。
- 修复简单上下标、分式、根号和希腊字母。
- 检查括号、方括号、花括号平衡。
- 对混入自然语言的候选截断数学前缀，截断失败则降级。

输出质量标记：

- `formula_text_fallback`
- `formula_plaintext_fallback`
- `formula_image_fallback`
- `formula_low_confidence`
- `formula_unrenderable_latex`
- `formula_candidate_rejected`

### 2.4 Chunker 与 Translator

chunker 必须把公式 token 放入 `preserve_tokens`，并在 context 中提供必要的公式 metadata。translator prompt 和 repair 逻辑必须保证：

- 不翻译、不改写公式 token。
- 不把 token 拆开。
- plan 覆盖所有 source block 时保留公式位置。
- 缺失 token 时执行 deterministic repair。

LLM 输出仍不得包含 bbox、坐标、页码定位字段。

### 2.5 Renderer

renderer 负责把 token 替换为公式节点：

- display formula 使用 block-level display math。
- inline formula 使用行内 math。
- 优先用 KaTeX 渲染 LaTeX。
- KaTeX 不可用时使用本地 KaTeX-like fallback。
- LaTeX 不可渲染时优先使用公式图片 fallback，其次使用低调 plaintext fallback。

用户可见输出必须满足：

- 不出现裸 `@@FORMULA_...@@`。
- 不出现裸 `{{formula:...}}`。
- 不出现红色 raw LaTeX 错误文本。
- 不出现空公式边框。
- 不把公式 fallback 渲染成 Times New Roman 正文混排效果。

### 2.6 Diagnostics

新增或完善公式诊断 artifact：

- `formula-candidates.json`
- `formula-recognition.json`
- `formula-diagnostics.json`
- `ocr-recognition.json`
- `ocr-diagnostics.json`
- renderer diagnostics 中的公式渲染统计

关键指标：

- candidate count
- recognized count
- inline/display count
- LaTeX success count
- fallback count
- unresolved placeholders
- render failed count
- low confidence formula ids
- rejected candidate reasons

## 3. 测试计划

### 3.1 单元测试

Schema:

- `FormulaIR` 字段校验。
- `DocumentIR` 中 formula/block/asset 引用一致性。
- JSON Schema 和 TypeScript 类型同步。

Backend:

- 普通字母不识别为公式。
- 上下标、分式、希腊字母、数学符号识别为公式。
- 长自然语言和作者邮箱不识别为公式。
- 控制字符和乱码 block 被过滤或降级。
- `{{formula:...}}` preserve token 不丢失。
- translator repair 能补回缺失公式 token。

Renderer:

- inline/display 公式渲染 class 正确。
- 不可渲染 LaTeX 不显示红色 raw error。
- 缺失公式引用产生 diagnostics 而不是裸 token。
- 图片 fallback 和 plaintext fallback 不产生空框。

### 3.2 集成测试

用 `test.pdf` 前四页作为本地验收输入，生成临时裁剪 PDF，不提交生成物。

验收路径：

```bash
.venv/bin/python -m pytest packages/schema/tests
.venv/bin/python -m pytest services/api/tests
.venv/bin/python -m pytest packages/renderer/tests
.venv/bin/python -m compileall packages services
npm run typecheck:web
npm run build:web
```

端到端检查：

- 上传前四页 PDF。
- deterministic translator 路径跑通。
- 预览 HTML 完成。
- PDF export 完成。
- `formula-diagnostics.json` 可下载。
- `renderer-diagnostics.json` 没有 blocking formula failure。

DOM 检查：

```javascript
(() => ({
  inline: document.querySelectorAll(".formula-inline").length,
  display: document.querySelectorAll(".formula-display").length,
  failed: document.querySelectorAll(".formula-render-failed").length,
  unresolvedNewRefs: document.body.innerText.match(/\{\{formula:[^}]+\}\}/g) || [],
  unresolvedOldRefs: document.body.innerText.match(/@@FORMULA_[A-Za-z0-9_]+@@/g) || [],
  rawLatexText: document.body.innerText.match(/\\(?:partial|nabla|frac|sum|int)\b[^\n]{0,120}/g) || []
}))()
```

验收标准：

- `failed === 0`
- unresolved token 数量为 0
- 用户预览中没有红色 raw LaTeX
- 没有空公式框
- 普通字母仍保持正文样式
- 公式视觉上接近论文数学排版

## 4. 推荐实施顺序

1. 落 schema contract 和文档。
2. 加公式 candidate detection / normalization 测试。
3. 收紧 parser/enrichment 的公式边界。
4. 稳定 chunker/translator preserve token。
5. 增强 renderer KaTeX/fallback。
6. 跑前四页端到端验收。
7. 根据 diagnostics 和截图继续修小边界。

## 5. 风险与取舍

- 真实 OCR 和复杂视觉公式可以作为增强，不阻塞 deterministic 文本型论文 MVP。
- 公式语义准确性可以逐步提升；第一阶段更重要的是不误伤正文、不显示裸 token、不出现红色错误文本。
- 双 token 兼容会增加 renderer 复杂度，但能降低迁移风险。
- 后续若统一 token，需要先完成 schema、chunker、translator、renderer 的同步迁移。
