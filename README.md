# Trans Typesetting

本仓库是一个脱离 Zotero 的本地 PDF 文献翻译与排版系统 MVP。它从数字版英文论文中提取结构化 `DocumentIR`，按论文分块调用 OpenAI-compatible 模型，校验 `TranslationLayoutPlan@0.1`，再通过 HTML/CSS 分页渲染为纯译文 PDF。

## Repository Layout

- `apps/web`: React/Vite 本地 Web 前端。
- `services/api`: FastAPI 后端、任务队列、PDF 解析、翻译编排。
- `packages/schema`: 共享 schema、Pydantic models、TypeScript types。
- `packages/renderer`: HTML/CSS 分页渲染器与 PDF 导出。
- `docs`: 架构和 worktree 同步开发说明。

## Quick Start

推荐使用 Python 3.11 或 3.12 创建本地 `.venv`。Python 3.14 与可选 Pix2Text/RapidOCR/ONNX 公式 OCR 组合仍不稳定，可能导致模型初始化慢或进程退出时出现 semaphore 清理告警。

```bash
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/python -m pip install -r services/api/requirements.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
npm install
cp .env.example .env
```

启动后端：

```bash
.venv/bin/python -m uvicorn app.main:app --reload --app-dir services/api
```

启动前端：

```bash
npm run dev:web
```

默认前端访问 `http://127.0.0.1:5173`，后端访问 `http://127.0.0.1:8000`。

前端开发服务器会把 `/api` 代理到 `VITE_API_PROXY_TARGET`，默认是 `http://127.0.0.1:8000`。如果 `.env` 中没有配置 `OPENAI_API_KEY`，后端会使用本地 deterministic translator，把每个文本块标记为目标语言的占位译文，便于端到端验证上传、分块、schema 校验和渲染流程。

PDF 工作台入口分为两个上传源：`待翻译 PDF` 是必填内容源，`版式参考 PDF` 是可选的排版语义输入源。未提供版式参考时，后端会把内容 PDF 同时作为默认版式语义源，并在 `normalized-input` / `semantic-analysis` 中标记 `layout_source_fallback_to_content`。展开“自定义强约束”后，前端会用 typed 控件提交页尺寸、目标字号、续页和图片保留策略；首版不开放 raw JSON schema 编辑。

前端现在按三条用户路径组织：仅翻译、仅智能排版、翻译并排版。提交时会写入 `workflow_mode=translate_only|typeset_only|translate_and_typeset`；仅排版会跳过 translator 并保留源文本，仅翻译仍生成 preview/PDF 但跳过模型增强的智能排版计划。Developer 区集中展示运行历史、模型/API/OCR 设置、pipeline events 和 schema/artifact inspector。

Word 输入通过 `POST /api/workflows/docx` 进入流水线。为了尽量保留 Word 的真实分页和图片位置，后端要求本机安装 headless LibreOffice/`soffice`，或通过 `LIBREOFFICE_BIN` 指向可执行文件；DOCX 会先转换为 PDF，再复用 PDF parser。当前机器没有 `soffice` 时，任务会失败并写入 `docx-conversion` artifact，提示安装/配置 converter。

图片输入和扫描版 PDF 会优先使用已配置且启用的 vision/OCR 模型抽取纯文本块；模型返回只允许文本和语义角色，不允许坐标。未配置 `AGENT_ENABLE_VISION_ANALYSIS` 或模型密钥时，系统继续使用 deterministic fallback，并在 `ocr-diagnostics`、`parser-diagnostics` 和质量 flags 中明确标记。

常用配置在 `.env` 中：

```bash
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
ALLOWED_TARGET_LANGS=zh-CN,zh-TW,ja-JP,ko-KR,en-US
MAX_UPLOAD_BYTES=52428800
TRANSLATION_CONCURRENCY=2
TRANSLATOR_MAX_ATTEMPTS=2
MINIMAX_API_KEY=
MINIMAX_ENDPOINT=https://api.minimaxi.com/v1/chat/completions
MINIMAX_MODEL=MiniMax-M3
OCR_PROVIDER_ORDER=minimax_vision,pix2text,deterministic
OCR_PROVIDER_TIMEOUT_SECONDS=12
OCR_MAX_VISUAL_CANDIDATES=12
VITE_DEV_HOST=127.0.0.1
VITE_DEV_PORT=5173
VITE_API_PROXY_TARGET=http://127.0.0.1:8000
```

当用户说明包含 `GB/T 7713.1` 时，renderer 在 continuous_reflow 模式下执行 GB/T 公式编号：display formula 单独成块、居中排版，编号 `(1)`、`(2)`… 顺序生成并右端对齐；译文自带源编号时保留原编号不重复编号，多 display formula block 跳过编号，均写入对应 quality flag，`renderer-diagnostics` 汇总 `formula_numbered_count`。标题/一级小节默认使用黑体字体栈（SimHei/Heiti SC fallback），正文保持宋体栈。渲染模板已强制开启 HTML autoescape，PDF 抽取文本中的 `<`、`&` 等字符不会再破坏 preview/PDF 结构。

公式识别可通过 MiniMax-M3 视觉 OCR 优先识别公式 crop，再回退 Pix2Text 和 deterministic 文本层路径；`MINIMAX_API_KEY` 未设置时会复用 `OPENAI_API_KEY`，默认 endpoint 为 `https://api.minimaxi.com/v1/chat/completions`。MiniMax provider 使用严格 JSON prompt，只接受 `latex`、`display_mode`、`confidence` 和 `quality_flags`，拒绝 bbox/page/x/y 等布局字段，输出进入 `DocumentIR.formulas[].latex` 后由 renderer 的 KaTeX 链路渲染。Pix2Text 初始化、识别超时或远端 provider 不可用都不会阻塞数字版论文主流程。系统会从文本层识别行内/行间公式，清理 PDF 常见控制字符和符号编码，保守切分自然语言边界，并把公式作为 preserve token 传给 translator。AIP remapped font 这类疑似腐败文本层会降置信度并优先视觉 crop；无法结构化渲染的公式会回退为低调的原文/图片 fallback，并写入非阻塞质量 flag。OpenAI 视觉公式识别也可通过 `OCR_PROVIDER_ORDER=openai_vision,pix2text,deterministic` 加入，条件是本地配置了 API key；公式视觉 OCR 不依赖 `AGENT_ENABLE_VISION_ANALYSIS`。

## Troubleshooting

- 端口占用：如果 `5173` 或 `8000` 已被占用，先停掉旧进程，或把 `VITE_DEV_PORT`、后端 uvicorn `--port` 和 `VITE_API_PROXY_TARGET` 改成同一组新端口。
- `listen EPERM`：优先确认前端使用 `127.0.0.1` 而不是 `0.0.0.0`。本项目默认读取 `VITE_DEV_HOST=127.0.0.1`。
- 后端未启动：前端任务面板会显示后端离线。先访问 `http://127.0.0.1:8000/api/health`，应返回 `{"status":"ok"}`。
- 前端打不开：优先访问 `http://127.0.0.1:5173`。如果改过前端端口，请访问 `VITE_DEV_PORT` 对应地址。
- 上传失败：仅支持 PDF，默认最大 50 MB，目标语言必须在 `ALLOWED_TARGET_LANGS` 中。
- PDF 导出失败：先运行 `.venv/bin/python -m playwright install chromium`，确保当前 Playwright 版本对应的 Chromium 已安装。macOS 11/12 上如果出现 `Connection closed while reading from the driver`，通常是 Playwright bundled node 与系统 libc++ 不兼容；设置 `PLAYWRIGHT_NODEJS_PATH=/usr/local/bin/node` 后重试，或使用兼容当前系统的 Playwright/Chromium 组合。
- 图片资产排查：不要只用 `file://data/outputs/.../preview.html` 判断图片是否可用；该 artifact 中的 `/api/documents/.../assets/...` 会在 `file://` 下解析失败。应通过后端 `http://127.0.0.1:8000/api/documents/{doc_id}/preview` 打开，并用 Chrome DevTools 检查 console/network 和 `img.complete`。

## Diagnostics

后端提供本地调试接口，便于审计从 PDF 到渲染的 contract：

- `GET /api/config`: 返回允许语言、上传上限、provider 模式、base URL、model 和 API key 是否已配置；不会返回密钥值。
- `PUT /api/config`: 保存本地 provider、base URL、model、API key、默认语言、并发和重试配置到 `data/config/runtime-config.json`；响应不会返回密钥值。
- `GET /api/jobs`: 返回最近任务，可用于前端刷新恢复。
- `POST /api/jobs/{job_id}/cancel`: 取消排队或运行中的任务。
- `POST /api/jobs/{job_id}/retry`: 复用原上传文件重新排队。
- `POST /api/jobs/{job_id}/retypeset`: 从历史任务复用 `DocumentIR`/上传文件/资产，按自然语言说明和 `EditScope` 创建新的原文重排派生任务。
- `GET /api/documents/{doc_id}/artifacts`: 返回当前文档可用 artifact。
- `GET /api/documents/{doc_id}/artifacts/document-ir`
- `GET /api/documents/{doc_id}/artifacts/semantic-analysis`
- `GET /api/documents/{doc_id}/artifacts/translation-chunks`
- `GET /api/documents/{doc_id}/artifacts/translation-plans`
- `GET /api/documents/{doc_id}/artifacts/translation-progress`
- `GET /api/documents/{doc_id}/artifacts/edit-scope`
- `GET /api/documents/{doc_id}/artifacts/retypeset-source`
- `GET /api/documents/{doc_id}/artifacts/docx-conversion`
- `GET /api/documents/{doc_id}/artifacts/parser-diagnostics`
- `GET /api/documents/{doc_id}/artifacts/renderer-diagnostics`
- `GET /api/documents/{doc_id}/artifacts/pdf-export-diagnostics`
- `GET /api/documents/{doc_id}/assets/{filename}`: 返回 parser 提取的 PDF 图片资产，用于预览和 PDF 导出。

前端工作台会在任务完成后显示 schema inspector，直接查看 `DocumentIR`、semantic analysis、chunks、plans、parser diagnostics 和 renderer diagnostics。Parser diagnostics 汇总页数、文本块、资产、角色计数和复杂 PDF fallback flags；Renderer diagnostics 汇总缺失译文、角色不一致、溢出和自然语言排版要求状态。`user-intent`、`semantic-analysis`、`layout-intent-plan` 与 `renderer-diagnostics.intent_requirements` 会把封面、摘要、目录、图表目录、实验报告结构、课程 metadata、页码、字体字号和语气等 prompt 要求标记为 satisfied、diagnostic 或 recognized，便于本地 UI 明确展示“已满足”或“需用户补充内容”。

真实模型返回会先提取 `TranslationLayoutPlan` JSON object，再经过严格 schema 校验。OpenAI-compatible endpoint 不一定保证 `message.content` 是纯 JSON；后端会处理 prose、markdown fence 或 thinking 包裹后的 plan JSON，MiniMax-M3 会额外关闭 thinking 并启用 reasoning split。可修复的 chunk 级问题，例如误带坐标字段、缺失 block 或遗漏 preserve token，会被后端修复为合法 `TranslationLayoutPlan` 并写入 `quality_flags`；不可提取、不可解析或请求失败会按 chunk 重试后落到任务错误状态。`TRANSLATION_CONCURRENCY` 控制 chunk 并发翻译，`TRANSLATOR_MAX_ATTEMPTS` 控制每个 chunk 的模型调用尝试次数。

`GET/PUT /api/config` 支持持久化本地运行配置，包括 provider/base URL/model/API key、默认语言、并发、重试、LangGraph agent repair 次数、vision analysis 开关、layout/vision 模型名和 `RenderDefaults`。API key 只写入本地 `data/config/runtime-config.json`，不会在响应中返回。前端配置面板可编辑字体栈、行高、段距和 overflow 最小字号缩放；chunker 和 renderer 使用同一份已持久化的 `RenderDefaults`，保证 prompt 中的 render defaults 与最终 HTML/PDF 一致。

智能排版 workflow 由 LangGraph `StateGraph` 编排为 adapter、intent analysis、semantic recognition、plan、validation、translation/source-preserve、render evaluation、repair 和 export 节点。`output_kind=typeset_document` 会跳过真实 translator，生成 `translated_text == source_text` 的 renderer-compatible plans，并写入 `translation_skipped` / `source_text_preserved`。未配置模型 key 时会使用 deterministic semantic/layout fallback；图片 vision analysis 默认关闭，不会自动把用户图片发送到远端 provider。

Renderer 当前执行确定性 overflow policy：先按 `min_font_scale` 缩放，再在页面可用空间内扩盒，仍放不下时创建 continuation page，并在 diagnostics 中标记 `font_scaled`、`box_expanded`、`continued_on_next_page` 或 `continuation_page`。

PDF export 会写入 `pdf-export-diagnostics.json`，记录 Playwright version、Node driver 路径、HTML/页面/图片统计、失败资源和输出大小。Renderer 在导出时会把 `/api/documents/{doc_id}/assets/{filename}` 图片资产内联为 data URL，避免 `page.set_content()` 缺少后端 base URL 时丢图；preview HTML 仍保留 API 路径供前端和调试接口使用。Playwright 不可用时会生成一个最小 fallback PDF 并在 diagnostics 中标记 `fallback_pdf`，便于工作流产生可下载 artifact，同时暴露真实失败原因。

Parser 会从数字版 PDF 的 image block 提取栅格图片资产，写入 `DocumentIR.pages[].assets[]`，并保存到本地输出目录。Renderer 会按 `DocumentIR` 中的 asset bbox 在原页面位置保留图片；缺失 asset path 时输出 `asset_missing_path` 诊断和占位。

Parser 的阅读顺序对数字版论文做了基础多栏感知：检测左右栏后按栏内自上而下排序，并过滤跨页重复的页眉页脚候选。底部小字号短文本会标记为 `footnote`，参考文献标题和编号条目继续标记为 `reference`，表格样文本会标记为 `table`。公式会进入独立 enrichment：系统识别 text-layer formula block、公式样 image/vector candidate，生成 `DocumentIR.formulas[]`、公式 asset 和 `{{formula:formula_id}}` preserve token；默认 OCR 顺序为 Pix2Text -> deterministic，文本层疑似坏字形时会标记 `formula_text_layer_corrupt` / `formula_slash_glyph_suspect` 并优先视觉识别。Renderer 用 KaTeX-compatible HTML/CSS 渲染 LaTeX，失败或被标记为腐败 display 公式时回退原文本/图片并输出公式质量 flag。

扫描版或 image-only PDF 会在 parser 阶段明确失败为 `unsupported_scanned_pdf`，并写入 `parser-diagnostics.json`，其中包含 `reason`、页数、文本块数、资产数和 `next_step`。当前 OCR 尚未实现；该 fallback 使前端能展示可恢复错误，而不是让任务在 chunking 阶段以模糊错误失败。

Renderer diagnostics 还包含基础布局回归检查：同页 block/asset 重叠、bbox 越页和空渲染块会出现在 `layout_issues` 中，用于发现溢出、重叠、丢块和错误分页风险。

`packages/renderer/tests/fixtures/visual_regression.py` 提供确定性视觉回归样例，覆盖 overflow continuation、block/asset overlap、缺失译文回退、空 block、bbox 越页、table/formula/footnote 角色和 image/vector placeholder 资产。`npm run visual-regression` 会单独运行这个门禁；`npm run acceptance` 会先运行它，再执行完整 Python/前端验收。

Chunker 会把文档标题、附近标题、当前 chunk 摘要和前一个 chunk 尾部写入 `TranslationChunk.context`，并支持传入术语表 `glossary`。真实模型 prompt 明确要求按 glossary 保持术语一致，并只翻译当前 chunk 的 listed blocks。

Phase 4 本地产品能力包括：多 PDF 批量上传、持久任务历史、启动时自动恢复未完成任务、取消、失败/取消后的重新排队，以及前端运行配置编辑。批量接口是 `POST /api/documents/batch`。

## Development

```bash
.venv/bin/python -m pytest
.venv/bin/python -m compileall packages services
npm run visual-regression
npm run typecheck:web
npm run build:web
```

发布前可运行同一组自动验收：

```bash
npm run acceptance
```

该验收包含一个本地生成的数字版 PDF 回归样例，覆盖真实 parser、chunker、deterministic translator、HTML renderer、任务状态和 artifact 持久化路径；测试中只替换 Playwright PDF export，以避免快速门禁依赖浏览器进程。

如果仓库根目录存在 `test.pdf`，可额外运行 v3 公式验收门禁：

```bash
bash scripts/accept-test-pdf-first-four-pages.sh
```

该脚本会在临时目录裁剪 `test.pdf` 前四页，强制使用 deterministic translator/OCR 路径，跑完整 parse -> formula enrichment -> chunk -> translate -> render -> PDF export，并断言公式 diagnostics、renderer diagnostics、preview HTML 和 `translated.pdf` 都满足无阻塞失败条件。裁剪 PDF 和输出 artifact 不会提交。

## Known Limitations

- PDF 解析依赖 PyMuPDF 的文本层；当前没有 OCR，扫描版 PDF 会明确失败并输出 `parser-diagnostics`，建议先使用 OCR 工具生成带文本层 PDF 后再上传。
- 真实模型调用使用 OpenAI-compatible `/chat/completions`，并从响应内容提取 `TranslationLayoutPlan` JSON object；未配置 `OPENAI_API_KEY` 时只生成占位译文。
- `TranslationLayoutPlan@0.1` 是 LLM 输出 contract；schema 会拒绝 `bbox`、`x`、`y`、`page` 等布局坐标字段。
- PDF 导出依赖 Playwright Chromium；首次运行前需要执行 `.venv/bin/python -m playwright install chromium`。
- Renderer 负责 HTML/CSS 分页和 PDF 导出；当前支持提取并保留栅格图片资产、保留表格/公式文本块、公式 plaintext/image fallback、保留 vector drawing placeholder bbox 和输出 fallback diagnostics，但复杂表格重建、公式矢量图 rasterization、任意复杂公式的完美 LaTeX 语义还原和高度保真多栏版式仍在后续增强范围。
- 任务历史和配置目前来自本地 JSON 文件；它们适合单机本地工作台，不是多用户服务器数据库。

并行 worktree 约定见 [docs/worktree.md](/Users/mac/app/trans-typeset/docs/worktree.md)。
