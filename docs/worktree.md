# Worktree 同步开发

`main` 是集成分支。所有跨模块协作都通过 `packages/schema` 的版本化 contract 发生，不直接耦合其他模块的内部实现。

## 初始 worktree

```bash
git worktree add .worktrees/schema-contract feat/schema-contract
git worktree add .worktrees/backend-pipeline feat/backend-pipeline
git worktree add .worktrees/web-app feat/web-app
git worktree add .worktrees/renderer feat/renderer
```

## 分支职责

- `feat/schema-contract`: `DocumentIR`、`TranslationChunk`、`TranslationLayoutPlan@0.1`、默认值、校验器。
- `feat/backend-pipeline`: FastAPI、任务状态、PDF 解析、分块、翻译、schema repair。
- `feat/web-app`: 上传、配置、任务进度、预览、下载。
- `feat/renderer`: HTML/CSS 页面模板、PDF 导出、渲染验证。

## 同步规则

每天从每个 worktree 执行：

```bash
git fetch --all --prune
git rebase main
```

推荐合并顺序：

1. `feat/schema-contract`
2. `feat/renderer`
3. `feat/backend-pipeline`
4. `feat/web-app`

schema 发生 breaking change 时，先更新 `schema_version` 或添加兼容字段，再让后端和前端同步升级。

