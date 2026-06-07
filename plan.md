  前后端完善计划

  1. 启动稳定性
      - 将前端 dev host 改为 127.0.0.1 或从环境变量读取。
      - 在 apps/web/vite.config.ts 支持 VITE_API_PROXY_TARGET，避免后端端口写
        死。

      - README 增加排障：端口占用、EPERM、后端未启动、访问 127.0.0.1:5173。

  2. 前端完善
      - 增加 /api/health 检测，后端未启动时显示明确错误。
      - 抽出 typed API client，统一处理 JSON、错误消息、超时和重试。
      - 上传前校验文件大小、PDF 类型，并补充更明确的失败态。
      - 任务轮询增加退避、取消和手动重试。
      - 预览/下载增加 404、任务失败、后端离线的专门 UI。

  3. 后端完善
      - CORS、存储目录、默认语言、模型配置全部环境化。
      - 上传接口增加文件大小限制和目标语言白名单。
      - 后台任务错误统一落盘到 job status。
      - 增加 preview/download 的 API 测试。
      - 后续可把 BackgroundTasks 升级为真正队列，支持任务恢复和并发控制。

  4. 验收标准
      - npm run typecheck:web
      - npm run build:web
      - .venv/bin/python -m pytest
      - 本机启动后端后，前端可访问 http://127.0.0.1:5173
      - 上传 PDF 后能看到任务状态、预览和下载入口
