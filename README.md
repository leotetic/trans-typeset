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

默认前端访问 `http://localhost:5173`，后端访问 `http://localhost:8000`。

前端开发服务器会把 `/api` 代理到 `http://localhost:8000`。如果 `.env` 中没有配置 `OPENAI_API_KEY`，后端会使用本地 deterministic translator，把每个文本块标记为目标语言的占位译文，便于端到端验证上传、分块、schema 校验和渲染流程。

## Development

```bash
.venv/bin/python -m pytest
.venv/bin/python -m compileall packages services
npm run typecheck:web
npm run build:web
```

## Known Limitations

- PDF 解析依赖 PyMuPDF 的文本层；当前没有 OCR，扫描版 PDF 不会被识别为正文。
- 真实模型调用使用 OpenAI-compatible `/chat/completions` JSON object 响应；未配置 `OPENAI_API_KEY` 时只生成占位译文。
- `TranslationLayoutPlan@0.1` 是 LLM 输出 contract；schema 会拒绝 `bbox`、`x`、`y`、`page` 等布局坐标字段。
- PDF 导出依赖 Playwright Chromium；首次运行前需要执行 `.venv/bin/python -m playwright install chromium`。
- Renderer 负责 HTML/CSS 分页和 PDF 导出，不保留原 PDF 中的图片、矢量图或复杂多栏版式资产。

并行 worktree 约定见 [docs/worktree.md](/Users/leotetic/app/trans-typesetting/docs/worktree.md)。
