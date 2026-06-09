# Goal v3: Formula Recognition And Rendering Completion

本阶段目标是让公式处理从“有雏形”推进到“可稳定验收”。Codex 需要接管从 `test.pdf` 前四页到最终 HTML/PDF 预览的整条公式链路，反复迭代到公式能被正确识别、作为 preserve token 传递、被 renderer 稳定渲染，并且失败场景有明确 diagnostics。

v3 不重写产品方向，也不把系统改回 Zotero 插件。继续围绕既有本地流水线工作：

```text
PDF upload
  -> DocumentIR
  -> formula detection / recognition / enrichment
  -> TranslationChunk[]
  -> TranslationLayoutPlan[]
  -> renderer
  -> preview / PDF / diagnostics
```

核心原则不变：LLM 不负责坐标、分页或 bbox；公式坐标和页面位置来自 `DocumentIR`，公式语义结果来自公式识别/enrichment，最终落版由 renderer 决定。

## Current Repo State

当前仓库已有公式处理骨架：

- `services/api/app/pipeline/formula_processing.py`: 旧的 block-local `@@FORMULA_...@@` 公式 placeholder、文本层启发式识别和 diagnostics。
- `services/api/app/pipeline/formulas/detector.py`: 新的 FormulaIR candidate detector，覆盖 display text block、inline text、image/vector-like asset。
- `services/api/app/pipeline/formulas/service.py`: formula enrichment，把 candidate 识别为 `DocumentIR.formulas[]`，并把 block 文本改写成 `{{formula:formula_id}}`。
- `services/api/app/pipeline/chunker.py`: 把 `@@FORMULA_...@@` 和 `{{formula:...}}` 放进 preserve tokens，并给 translator context 注入公式 LaTeX。
- `packages/renderer/pdf_renderer/models.py`: renderer 同时支持 `@@FORMULA_...@@` 和 `{{formula:...}}`，用本地 KaTeX 或 KaTeX-like fallback 生成 HTML，并输出 `formula_render_failed`、`formula_missing_latex`、`unresolved_formula_placeholders` 等诊断。
- `packages/renderer/pdf_renderer/templates/document.html.j2`: preview 端会再次用浏览器内 KaTeX 渲染 `.formula[data-latex]`。
- `services/api/tests/test_formulas.py`、`packages/renderer/tests/test_renderer.py`: 已有基础公式检测、enrichment 和渲染测试。

上一轮完整输出可作为起点：

- 输出目录：`data/outputs/doc_3cc60e4bd34b40478edf25b52ab23bd5`
- 最新时间：2026-06-09 01:36:11
- `workflow-run.json`: `status=completed`
- `formula-diagnostics.json`: `candidate_count=47`、`recognized_count=47`、`latex_success_count=47`、`inline_count=40`、`display_count=7`
- `renderer-diagnostics.json`: `layout_issues=[]`，但有 `formula_render_failed=1`、`missing_translation=2`
- `render-evaluation.json`: `accepted=false`、`blocking_flags={"missing_translation": 2}`、`repair_recommended=true`

已知失败样本：

- `p0005_ba84d3f37b9c2` 含 `{{formula:p0005_formula_1e05b95337f3}}`，识别出的 LaTeX 是 `νizμe=Te)1`，括号不平衡，触发 `formula_render_failed`。
- `p0003_b05a3729bf7eb` 和 `p0010_b6c51cd88ff43` 的 `source_text` 是控制字符 `\u0001 \u0003`，导致最终 renderer 标记 `missing_translation`。
- `preview.html` 中存在明显误检：部分长英文句子被整体塞进 `data-latex`，例如从 `B=p` 一直吞到句尾。这说明 detector/normalizer 的 inline 边界过宽，误检会污染翻译文本和渲染。

用户最新截图暴露了更具体的视觉缺陷：

- 页面中出现红色 raw LaTeX 文本，例如 `\partial ne \partial x into the elec- tron continuity equation ...`。这表示 renderer/browser KaTeX 渲染失败后把未结构化公式当错误文本直接显示了，且失败样式过于显眼。
- 公式被拆成多段漂浮文本：同一条 display equation 被拆成若干 block 或 candidate，分别显示为红色片段、`dB\u000032`、`dx`、`coll ¼ X`、单独的 `ð`、`n`、`vn` 等残片。根因可能是 PDF text layer 把公式按 span/line/控制字符切碎，而当前 parser/detector 没有把相邻公式碎片合并，也没有过滤无语义残片。
- 页面顶部有一个空的细边框长框，疑似空公式 block、空 asset placeholder 或 failed formula fallback 占位。空框会破坏阅读流，即使 diagnostics 没有 layout overlap 也不能算验收通过。
- 有些公式内容包含自然语言短语，例如 `into the electron continuity equation`，说明 formula candidate 把正文说明吞进了 LaTeX，而不是只保留数学表达式。
- 乱码字符 `¼`、`þ`、`ð`、`\u00003x`、软连字符断词、奇怪控制字符没有被规范化，导致公式文本和中文正文混排出现不可读残片。

这组截图应作为 v3 的人工视觉回归样本。最终验收必须证明这些截图中的现象已经消失，而不是仅仅让 artifact 标记为 completed。

## Objective

完成后，系统必须能在未配置 `OPENAI_API_KEY` 的 deterministic 本地路径下，稳定处理 `test.pdf` 前四页中的公式：

- 公式候选边界合理，不把长自然语言句子、作者邮箱、正文段落或残缺控制字符识别成公式。
- 公式 LaTeX 可被 KaTeX 或本地 fallback 渲染；无法可靠结构化的视觉公式可以回退图片/原文，但必须有非阻塞 quality flag。
- `TranslationChunk.preserve_tokens` 必须包含公式 token，translator plan 必须保留 token，renderer 必须把 token 替换成公式节点。
- preview HTML 和导出的 PDF 不能出现裸 `@@FORMULA_...@@`、裸 `{{formula:...}}`、空白公式块或浏览器端 `.formula-render-failed`。
- preview HTML 和导出的 PDF 不能出现红色 raw LaTeX 错误文本、空公式边框、控制字符残片独占行，或公式碎片漂浮在正文之间。
- renderer diagnostics 不再把公式问题计入阻塞；前四页验收的 `render-evaluation.accepted` 必须为 true，或 blocking flags 为空。

## User Cooperation Needed

用户需要配合的事项尽量少：

- 保留仓库根目录的 `test.pdf`，不要在验收完成前替换它。v3 默认只使用前四页，避免每轮跑完整 15 页导致迭代太慢。
- 如果希望验证真实视觉 OCR 或 OpenAI 公式识别，提供可用的本地配置/API key；否则 Codex 必须使用 deterministic 路径完成可验收的基础能力。
- 允许 Codex 在必要时启动本地后端和前端：后端 `127.0.0.1:8000`，前端 `127.0.0.1:5173` 或自动选择空闲端口。
- 允许 Codex 使用 Chrome MCP 检查本地 preview DOM、console、network 和截图；必要时可用 Computer Use 辅助观察 PDF/浏览器界面。
- 不需要用户手工判断每一个公式是否数学语义完全正确；用户只需在最终截图/PDF 上确认视觉效果是否可接受。语义准确性由 automated fixtures 和 artifact 规则先兜底。

## Implementation Framework

### 1. Establish A Reproducible Formula Fixture

先把 `test.pdf` 前四页变成固定验收输入。生成物不得提交。

建议命令：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import fitz

src = Path("test.pdf")
dst = Path("/tmp/trans-typesetting-test-first4.pdf")
with fitz.open(src) as doc:
    out = fitz.open()
    out.insert_pdf(doc, from_page=0, to_page=min(3, len(doc) - 1))
    out.save(dst)
print(dst)
PY
```

如果后续写自动化验收脚本，输入路径应默认读取 `test.pdf` 并在临时目录生成前四页 PDF，避免把裁剪后的 PDF 提交进仓库。

### 2. Add Failing Tests Before Fixes

优先补会失败的测试，避免只凭视觉调试。

Backend formula tests:

- 在 `services/api/tests/test_formulas.py` 增加 inline formula 边界回归：
  - `B=p, defined as ...` 只能识别 `B=p`，不能吞掉后面的英文解释。
  - `νizμe=Te)1` 这类括号不平衡片段应被修正、缩短、标记低置信度，或不作为可直接 KaTeX 渲染的公式。
  - `a)Authors to whom correspondence should be addressed: ...@...` 不应识别为 display formula。
  - `\u0001 \u0003`、纯控制字符或不可见文本不得进入 translatable block。
  - `\partial ne \partial x into the electron continuity equation ...` 必须被切断为公式 token 加普通正文，不能整句作为公式。
  - `coll ¼ X`、`dB\u000032`、`dx`、单独 `ð`、`n`、`vn` 等碎片需要被合并进相邻 display formula、降级为普通文本，或过滤为不可翻译噪声；不能作为独立公式/段落漂浮渲染。
- 在 `services/api/tests/test_chunker.py` 或 orchestrator/local pipeline 测试中确认 `{{formula:...}}` preserve token 不丢失。
- 若新增前四页 pipeline 验收，放在 `services/api/tests` 或 `scripts/`，并默认使用 deterministic translator。

Renderer tests:

- 在 `packages/renderer/tests/test_renderer.py` 增加不平衡/不可渲染公式 fallback 回归，要求不会产生裸 token，且 diagnostics 可区分 `formula_render_failed` 与可接受 fallback。
- 增加 diagnostics 断言：`unresolved_formula_placeholders == []`，`formula_rendered_count >= formula_count_on_rendered_pages`，HTML 中没有裸公式 token。
- 增加视觉/HTML 回归：不可渲染公式不能以红色 raw LaTeX 大段显示；空公式 fallback 不能渲染成空边框长框；控制字符残片不能成为独立 visible block。

### 3. Tighten Formula Candidate Detection

重点收紧 `services/api/app/pipeline/formulas/detector.py` 和必要时 `formula_processing.py`：

- inline candidate 必须有明确数学边界。遇到逗号、句号、分号、英文解释短语、citation、`defined as`、`while`、`where`、`and` 等自然语言边界时停止。
- 单个 inline formula 不应超过合理长度。建议默认上限 32 到 48 个字符；超过时只有明确 math font spans 或 display block 才可接受。
- 对 `@` 邮箱、作者脚注、DOI/URL、caption 普通文本、长英文句子、控制字符建立 reject rules。
- 对 PDF 文本层常见乱码做 normalize，例如 `¼` 表示 `=`，`þ` 表示 `+`，``/``/`` 等控制字符要清理或标记不可翻译。
- 对 display formula 做 block/line 合并：相邻、同页、bbox 垂直距离很小、字号/字体接近、数学符号密度高的碎片应先合并为一个 formula candidate，再识别/渲染。不要让 `dB...`、`dx`、`coll ...`、`n`、`vn` 这类公式残片各自变成独立正文块。
- 对纯噪声 block 做早期过滤：只含控制字符、单个乱码符号、无字母数字且不构成有效数学符号的 block，应进入 parser diagnostics 的 filtered/noise 统计，而不是进入 chunker/translator/renderer。
- span-based detection 优先于 regex fallback；regex fallback 必须更保守。
- image/vector candidate 在 deterministic 路径下可以保留为 mock/fallback，但不能阻塞文本论文 MVP。

### 4. Normalize LaTeX For Renderability

在 recognizer 或 renderer 之前增加轻量 normalization：

- 把常见 PDF 字符映射为 KaTeX 可解析 LaTeX：`¼ -> =`、`þ -> +`、`// ->` 删除或替换为空格、`∂ -> \partial`、`∇ -> \nabla`、希腊字母保留或转义均可但要可渲染。
- 把截图中出现的 PDF 编码残片纳入 normalization/reject rules：`ð` 不能孤立显示，`\u000032` 这类控制字符加数字不能作为文本露出，软断词如 `elec- tron` 不应进入 formula latex。
- 对自然语言混入公式的候选执行二次切分：如果 LaTeX 字符串中出现 `into the`、`equation`、`defined as`、`where`、`while`、长英文单词序列等，应拆分出数学前缀/后缀，或降级为普通正文，不允许进入 `.formula[data-latex]`。
- 不生成不平衡括号/花括号/方括号的 `data-latex`。无法修正时：
  - 对 inline 公式回退为 escaped source text，并标记 `formula_text_fallback` 或 `formula_low_confidence`；
  - 对 display/asset 公式优先使用 formula image fallback；
  - 不允许裸 token 留在 HTML。
- renderer fallback 必须是低调、可读、非红色错误态。红色只允许用于开发 diagnostics overlay，不允许出现在用户预览/PDF 的正文内容中。
- 如果公式无法结构化渲染但原 PDF crop 可用，优先显示公式 crop 图片；如果 crop 不可用，显示规范化后的黑色 monospace/plain fallback，并在 diagnostics 标记 `formula_plaintext_fallback`，但不能显示空框。
- 明确区分两个质量层级：
  - 非阻塞 fallback：公式可见但不一定结构化完美。
  - 阻塞失败：裸 token、空白公式、`.formula-render-failed`、未解析 placeholder、导致 missing translation。

### 5. Keep Placeholder Contract Stable

当前有两种公式 token：

- 旧：`@@FORMULA_...@@`，绑定 `DocumentBlock.formulas[]`
- 新：`{{formula:formula_id}}`，绑定 `DocumentIR.formulas[]`

v3 可以选择统一到新格式，也可以继续双格式兼容，但必须满足：

- chunker 能提取两种 token。
- translator prompt 明确要求原样保留两种 token。
- plan repair 能补回缺失公式 token。
- renderer 能解析两种 token，且 diagnostics 能列出未解析 token。
- 同一个公式不要被两套机制重复渲染。

如果做 contract 变更，必须同步：

- `packages/schema/pdf_translator_schema/models.py`
- `packages/schema/typescript/src/index.ts`
- `packages/json-schema/*.schema.json`
- `docs/schema.md`
- 对应测试

### 6. Integrate Into The Local Pipeline

最终不能只通过局部函数测试。必须用前四页 PDF 跑通：

- parse
- formula enrichment
- chunking
- deterministic translation
- render preview
- export PDF
- artifact diagnostics

产物应写到 `data/outputs/{doc_id}` 或临时测试目录。生成物不要提交。

## Chrome MCP And Computer Use Verification

Codex 可以用 Chrome MCP 做本地可视化验收：

1. 启动后端：

```bash
.venv/bin/python -m uvicorn app.main:app --reload --app-dir services/api
```

2. 启动前端：

```bash
npm run dev:web
```

3. 用 Chrome MCP 打开 `http://127.0.0.1:5173`，上传 `/tmp/trans-typesetting-test-first4.pdf`，选择 `zh-CN`。
4. 等任务完成后进入 preview 和 schema inspector。
5. 在 preview 页面执行 DOM 检查：

```javascript
(() => ({
  formulaNodes: document.querySelectorAll(".formula[data-latex]").length,
  failedFormulaNodes: document.querySelectorAll(".formula-render-failed").length,
  unresolvedNewRefs: document.body.innerText.match(/\{\{formula:[^}]+\}\}/g) || [],
  unresolvedOldRefs: document.body.innerText.match(/@@FORMULA_[A-Za-z0-9_]+@@/g) || [],
  rawLatexText: document.body.innerText.match(/\\(?:partial|nabla|frac|sum|int)\b[^\n]{0,120}/g) || [],
  suspiciousControlText: document.body.innerText.match(/(?:\\u0000|\u0001|\u0003|\u0004|�|ð|þ|¼)/g) || [],
  redFormulaLikeNodes: [...document.querySelectorAll(".formula, .block")]
    .filter((node) => {
      const color = getComputedStyle(node).color;
      return color === "rgb(255, 0, 0)" || color === "rgb(220, 38, 38)";
    })
    .map((node) => node.textContent.trim().slice(0, 120)),
  emptyOutlinedBlocks: [...document.querySelectorAll(".block, .asset-placeholder, .formula")]
    .filter((node) => {
      const box = node.getBoundingClientRect();
      const text = (node.textContent || "").trim();
      const style = getComputedStyle(node);
      return box.width > 120 && box.height > 12 && text.length === 0 && style.borderStyle !== "none";
    }).length,
  shortFloatingFragments: [...document.querySelectorAll(".block")]
    .map((node) => (node.textContent || "").trim())
    .filter((text) => /^(?:dx|n|vn|ð|coll\s*¼\s*X|dB\\?u?0000?32)$/i.test(text)),
  longLatex: [...document.querySelectorAll(".formula[data-latex]")]
    .map((node) => node.getAttribute("data-latex") || "")
    .filter((latex) => latex.length > 80),
}))
```

6. 验收 DOM 结果必须满足：
   - `formulaNodes > 0`
   - `failedFormulaNodes == 0`
   - `unresolvedNewRefs.length == 0`
   - `unresolvedOldRefs.length == 0`
   - `rawLatexText.length == 0`
   - `suspiciousControlText.length == 0`
   - `redFormulaLikeNodes.length == 0`
   - `emptyOutlinedBlocks == 0`
   - `shortFloatingFragments.length == 0`
   - `longLatex.length == 0`，除非该节点是明确 display formula 且有测试说明

可以用 Computer Use 辅助打开浏览器、PDF 预览或系统 PDF 查看器进行肉眼确认，但最终结论必须回写到 artifact/test，而不是只停留在人工观察。

## Acceptance Conditions

v3 goal 只有在以下条件全部满足时才算完成：

### Automated Tests

必须通过：

```bash
.venv/bin/python -m pytest services/api/tests/test_formulas.py services/api/tests/test_chunker.py services/api/tests/test_translator.py
.venv/bin/python -m pytest packages/renderer/tests/test_renderer.py packages/renderer/tests/test_visual_regression.py
.venv/bin/python -m pytest services/api/tests
.venv/bin/python -m pytest packages/renderer/tests
.venv/bin/python -m compileall packages services
npm run typecheck:web
npm run build:web
```

如果改动涉及 schema，额外通过：

```bash
.venv/bin/python -m pytest packages/schema/tests
```

发布前最好通过：

```bash
npm run acceptance
```

### First Four Pages Pipeline Gate

用 `test.pdf` 前四页跑完整本地 deterministic pipeline，必须满足：

- job 最终 `status=completed`。
- `formula-diagnostics.json` 存在，且：
  - `candidate_count > 0`
  - `recognized_count == candidate_count`，或未识别项都有非阻塞 fallback reason
  - `latex_success_count > 0`
  - `unresolved_placeholders == []`
- `renderer-diagnostics.json` 存在，且：
  - `layout_issues == []`
  - `unresolved_formula_placeholders == []`
  - `quality_flag_counts.formula_render_failed` 不存在或为 `0`
  - `quality_flag_counts.formula_missing_latex` 不存在或为 `0`
  - `quality_flag_counts.missing_translation` 不存在或为 `0`；若源 block 是纯控制字符，应在 parser/chunker 阶段过滤，而不是进入 renderer
  - `formula_rendered_count > 0`
- `render-evaluation.json` 存在，且：
  - `accepted == true`
  - `blocking_flags == {}`
  - `repair_recommended == false`
- `preview.html` 存在，且文本/DOM 中没有裸 `@@FORMULA_...@@` 或 `{{formula:...}}`。
- `preview.html` 和浏览器 DOM 中没有 raw LaTeX 命令外露，例如 `\partial ...`、`\nabla ...`、`\frac ...` 作为普通红色文本出现。
- `preview.html` 和导出 PDF 中没有截图式空公式边框、空白长框、红色公式错误文本、单独的 `dx`/`n`/`vn`/`ð`/`coll ¼ X` 等漂浮残片。
- `translated.pdf` 存在且大小非零；打开前四页没有空白页、大片遮挡或公式节点明显丢失。

### Visual QA

Chrome MCP 截图或浏览器检查必须确认：

- 前四页 preview 中公式可见。
- 行内公式不会把后续整句英文吞成公式样式。
- display formula 以独立公式块或合理 fallback 呈现。
- display formula 不会拆散成多个无上下文残片，也不会把公式上下文挤成大块红色 LaTeX。
- 没有 `.formula-render-failed` 节点。
- 没有红色 raw LaTeX、大空框、空公式占位框。
- 控制字符垃圾块不会显示为奇怪符号，也不会造成空白/缺译 block。

### Documentation

完成实现后更新相应文档：

- `README.md`: 简述公式验收能力、deterministic fallback 和已知限制。
- `docs/schema.md`: 如果调整 formula contract、placeholder 或 diagnostics，必须同步说明。
- 如新增验收脚本，在 README 或脚本注释中写清如何运行。

## Parallel Work Ranges

如果使用 subagent 并行开发，先声明写入范围：

- Formula Backend Agent：`services/api/app/pipeline/formula_processing.py`、`services/api/app/pipeline/formulas/**`、`services/api/tests/test_formulas.py`、必要的 parser/chunker tests。
- Translator/Chunker Agent：`services/api/app/pipeline/chunker.py`、`services/api/app/pipeline/translator.py`、相关测试。
- Renderer Agent：`packages/renderer/**`、`packages/renderer/tests/**`。
- Integration Agent：验收脚本、前四页 fixture 生成逻辑、端到端 artifact 检查。不要提交生成的 PDF、HTML、data outputs。
- Docs Agent：`README.md`、`docs/schema.md`、本文件后续维护。

推荐顺序：

1. 写失败测试和前四页验收脚本。
2. 修 detector/normalizer，减少误检。
3. 修 LaTeX normalization 和 renderer fallback。
4. 修 chunk/translator token preservation 和控制字符过滤。
5. 跑前四页 pipeline，使用 Chrome MCP 做 DOM/截图验收。
6. 扩大到完整测试套件和 docs 收口。

## Non-Goals

v3 不要求一次性完成：

- 扫描版 PDF OCR 产品化。
- 任意复杂公式的完美 LaTeX 语义还原。
- 复杂图表、表格和矢量图高保真重建。
- 让 LLM 输出坐标或页面位置。
- 提交用户上传 PDF、裁剪后的测试 PDF、`data/outputs`、模型密钥或本地产物。

## Final Handoff Format

Codex 完成 v3 后，最终回复必须包含：

- 改动摘要。
- 关键文件路径。
- 已运行的验证命令和结果。
- 前四页 pipeline 的最新 `doc_id` / `job_id` / artifact 目录。
- Chrome MCP 或 Computer Use 验收结论。
- 若仍有非阻塞 formula fallback，列出数量、原因和是否影响用户预览。
