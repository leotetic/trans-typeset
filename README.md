# Trans Typesetting

本仓库是一个脱离 Zotero 的本地 PDF 文献翻译与排版系统 MVP。它从数字版英文论文中提取结构化 `DocumentIR`，按论文分块调用 OpenAI-compatible 模型，校验 `TranslationLayoutPlan@0.1`，再通过 HTML/CSS 分页渲染为纯译文 PDF。

## Repository Layout

- `apps/web`: React/Vite 本地 Web 前端。
- `services/api`: FastAPI 后端、任务队列、PDF 解析、翻译编排。
- `packages/schema`: 共享 schema、Pydantic models、TypeScript types。
- `packages/renderer`: HTML/CSS 分页渲染器与 PDF 导出。
- `docs`: 架构和 worktree 同步开发说明。

## Quick Start

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

常用配置在 `.env` 中：

```bash
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
ALLOWED_TARGET_LANGS=zh-CN,zh-TW,ja-JP,ko-KR,en-US
MAX_UPLOAD_BYTES=52428800
TRANSLATION_CONCURRENCY=2
TRANSLATOR_MAX_ATTEMPTS=2
VITE_DEV_HOST=127.0.0.1
VITE_DEV_PORT=5173
VITE_API_PROXY_TARGET=http://127.0.0.1:8000
```

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
- `GET /api/documents/{doc_id}/artifacts`: 返回当前文档可用 artifact。
- `GET /api/documents/{doc_id}/artifacts/document-ir`
- `GET /api/documents/{doc_id}/artifacts/semantic-analysis`
- `GET /api/documents/{doc_id}/artifacts/translation-chunks`
- `GET /api/documents/{doc_id}/artifacts/translation-plans`
- `GET /api/documents/{doc_id}/artifacts/translation-progress`
- `GET /api/documents/{doc_id}/artifacts/parser-diagnostics`
- `GET /api/documents/{doc_id}/artifacts/renderer-diagnostics`
- `GET /api/documents/{doc_id}/artifacts/pdf-export-diagnostics`
- `GET /api/documents/{doc_id}/assets/{filename}`: 返回 parser 提取的 PDF 图片资产，用于预览和 PDF 导出。

前端工作台会在任务完成后显示 schema inspector，直接查看 `DocumentIR`、semantic analysis、chunks、plans、parser diagnostics 和 renderer diagnostics。Parser diagnostics 汇总页数、文本块、资产、角色计数和复杂 PDF fallback flags；Renderer diagnostics 汇总缺失译文、角色不匹配、溢出等 quality flags。

真实模型返回会先提取 `TranslationLayoutPlan` JSON object，再经过严格 schema 校验。OpenAI-compatible endpoint 不一定保证 `message.content` 是纯 JSON；后端会处理 prose、markdown fence 或 thinking 包裹后的 plan JSON，MiniMax-M3 会额外关闭 thinking 并启用 reasoning split。可修复的 chunk 级问题，例如误带坐标字段、缺失 block 或遗漏 preserve token，会被后端修复为合法 `TranslationLayoutPlan` 并写入 `quality_flags`；不可提取、不可解析或请求失败会按 chunk 重试后落到任务错误状态。`TRANSLATION_CONCURRENCY` 控制 chunk 并发翻译，`TRANSLATOR_MAX_ATTEMPTS` 控制每个 chunk 的模型调用尝试次数。

`GET/PUT /api/config` 支持持久化本地运行配置，包括 provider/base URL/model/API key、默认语言、并发、重试、LangGraph agent repair 次数、vision analysis 开关、layout/vision 模型名和 `RenderDefaults`。API key 只写入本地 `data/config/runtime-config.json`，不会在响应中返回。前端配置面板可编辑字体栈、行高、段距和 overflow 最小字号缩放；chunker 和 renderer 使用同一份已持久化的 `RenderDefaults`，保证 prompt 中的 render defaults 与最终 HTML/PDF 一致。

智能排版 workflow 由 LangGraph `StateGraph` 编排为 adapter、intent analysis、semantic recognition、plan、validation、translation、render evaluation、repair 和 export 节点。未配置模型 key 时会使用 deterministic semantic/layout fallback；图片 vision analysis 默认关闭，不会自动把用户图片发送到远端 provider。

Renderer 当前执行确定性 overflow policy：先按 `min_font_scale` 缩放，再在页面可用空间内扩盒，仍放不下时创建 continuation page，并在 diagnostics 中标记 `font_scaled`、`box_expanded`、`continued_on_next_page` 或 `continuation_page`。

PDF export 会写入 `pdf-export-diagnostics.json`，记录 Playwright version、Node driver 路径、HTML/页面/图片统计、失败资源和输出大小。Renderer 在导出时会把 `/api/documents/{doc_id}/assets/{filename}` 图片资产内联为 data URL，避免 `page.set_content()` 缺少后端 base URL 时丢图；preview HTML 仍保留 API 路径供前端和调试接口使用。Playwright 不可用时会生成一个最小 fallback PDF 并在 diagnostics 中标记 `fallback_pdf`，便于工作流产生可下载 artifact，同时暴露真实失败原因。

Parser 会从数字版 PDF 的 image block 提取栅格图片资产，写入 `DocumentIR.pages[].assets[]`，并保存到本地输出目录。Renderer 会按 `DocumentIR` 中的 asset bbox 在原页面位置保留图片；缺失 asset path 时输出 `asset_missing_path` 诊断和占位。

Parser 的阅读顺序对数字版论文做了基础多栏感知：检测左右栏后按栏内自上而下排序，并过滤跨页重复的页眉页脚候选。底部小字号短文本会标记为 `footnote`，参考文献标题和编号条目继续标记为 `reference`，表格样文本和公式文本会分别标记为 `table` 和 `formula`。Renderer 对 `table`、`formula`、`footnote` 有专用 CSS 规则，避免全部按正文段落处理。复杂表格、公式矢量图和矢量图形当前不会被完全重建；parser 会把较大的 vector drawing 作为 `figure` placeholder asset 保留其 bbox，并在 parser diagnostics 暴露 `table_text_fallback`、`formula_text_fallback`、`vector_asset_placeholder` 和 `vector_assets_not_rasterized` 等 flags。

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

## Known Limitations

- PDF 解析依赖 PyMuPDF 的文本层；当前没有 OCR，扫描版 PDF 会明确失败并输出 `parser-diagnostics`，建议先使用 OCR 工具生成带文本层 PDF 后再上传。
- 真实模型调用使用 OpenAI-compatible `/chat/completions`，并从响应内容提取 `TranslationLayoutPlan` JSON object；未配置 `OPENAI_API_KEY` 时只生成占位译文。
- `TranslationLayoutPlan@0.1` 是 LLM 输出 contract；schema 会拒绝 `bbox`、`x`、`y`、`page` 等布局坐标字段。
- PDF 导出依赖 Playwright Chromium；首次运行前需要执行 `.venv/bin/python -m playwright install chromium`。
- Renderer 负责 HTML/CSS 分页和 PDF 导出；当前支持提取并保留栅格图片资产、保留表格/公式文本块、保留 vector drawing placeholder bbox 和输出 fallback diagnostics，但复杂表格重建、公式矢量图 rasterization 和高度保真多栏版式仍在后续增强范围。
- 任务历史和配置目前来自本地 JSON 文件；它们适合单机本地工作台，不是多用户服务器数据库。

并行 worktree 约定见 [docs/worktree.md](/Users/mac/app/trans-typeset/docs/worktree.md)。
