import type { JobStatus, OutputKind, RenderDefaults, StyleIntent, UserConstraints } from "@trans-typesetting/schema";

export interface CreateDocumentResponse {
  job_id: string;
  doc_id: string;
}

export interface BatchCreateDocumentResponse {
  jobs: CreateDocumentResponse[];
}

export interface HealthResponse {
  status: "ok";
}

export interface RuntimeConfig {
  default_target_lang: string;
  allowed_target_langs: string[];
  max_upload_bytes: number;
  translator_provider: "deterministic" | "openai-compatible" | string;
  openai_base_url: string;
  openai_model: string;
  openai_api_key_configured: boolean;
  translation_concurrency: number;
  translator_max_attempts: number;
  translation_chunk_max_chars: number;
  agent_max_repair_attempts: number;
  agent_enable_vision_analysis: boolean;
  layout_planner_model: string;
  vision_analyzer_model: string;
  ocr_provider_order: string[];
  ocr_min_confidence: number;
  ocr_provider_timeout_seconds: number;
  ocr_max_visual_candidates: number;
  render_defaults: RenderDefaults;
}

export interface UpdateRuntimeConfig {
  default_target_lang?: string;
  openai_base_url?: string;
  openai_model?: string;
  openai_api_key?: string;
  translation_concurrency?: number;
  translator_max_attempts?: number;
  translation_chunk_max_chars?: number;
  agent_max_repair_attempts?: number;
  agent_enable_vision_analysis?: boolean;
  layout_planner_model?: string;
  vision_analyzer_model?: string;
  ocr_provider_order?: string[];
  ocr_min_confidence?: number;
  ocr_provider_timeout_seconds?: number;
  ocr_max_visual_candidates?: number;
  render_defaults?: RenderDefaults;
}

export interface WorkflowIntentInput {
  output_kind: OutputKind;
  style_intent: StyleIntent;
  instruction: string;
  constraints?: UserConstraints;
}

export interface CreateDocumentInput {
  contentFile: File;
  layoutReferenceFile?: File | null;
  targetLang: string;
  intent?: WorkflowIntentInput;
}

export interface ArtifactSummary {
  name: string;
  kind: string;
  available: boolean;
  href?: string | null;
}

export interface DocumentArtifacts {
  doc_id: string;
  artifacts: ArtifactSummary[];
}

export type ApiErrorKind = "http" | "network" | "timeout" | "abort" | "parse";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;

  constructor(message: string, kind: ApiErrorKind, status?: number) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}

const jobStatusValues = new Set<JobStatus["status"]>([
  "queued",
  "parsing",
  "translating",
  "rendering",
  "completed",
  "failed",
  "canceled"
]);

const DEFAULT_TIMEOUT_MS = 12_000;

interface ApiRequestInit extends RequestInit {
  retries?: number;
  timeoutMs?: number;
}

export async function getHealth(options: ApiRequestInit = {}) {
  return requestJson("/api/health", parseHealthResponse, {
    retries: 1,
    timeoutMs: 4_000,
    ...options
  });
}

export async function createDocument(input: CreateDocumentInput, options: ApiRequestInit = {}) {
  const formData = new FormData();
  formData.append("content_file", input.contentFile);
  if (input.layoutReferenceFile) {
    formData.append("layout_file", input.layoutReferenceFile);
  }
  formData.append("target_lang", input.targetLang);
  appendIntentFields(formData, input.intent);

  return requestJson("/api/documents", parseCreateDocumentResponse, {
    method: "POST",
    body: formData,
    timeoutMs: 60_000,
    ...options
  });
}

export async function createTextWorkflow(
  text: string,
  targetLang: string,
  intent: WorkflowIntentInput,
  options: ApiRequestInit = {}
) {
  const formData = new FormData();
  formData.append("text", text);
  formData.append("target_lang", targetLang);
  formData.append("filename", "text-input.txt");
  appendIntentFields(formData, intent);

  return requestJson("/api/workflows/text", parseCreateDocumentResponse, {
    method: "POST",
    body: formData,
    timeoutMs: 60_000,
    ...options
  });
}

export async function createImageWorkflow(
  file: File,
  targetLang: string,
  intent: WorkflowIntentInput,
  options: ApiRequestInit = {}
) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("target_lang", targetLang);
  appendIntentFields(formData, intent);

  return requestJson("/api/workflows/image", parseCreateDocumentResponse, {
    method: "POST",
    body: formData,
    timeoutMs: 60_000,
    ...options
  });
}

export async function createDocumentsBatch(
  files: File[],
  targetLang: string,
  intent?: WorkflowIntentInput,
  options: ApiRequestInit = {}
) {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }
  formData.append("target_lang", targetLang);
  appendIntentFields(formData, intent);

  return requestJson("/api/documents/batch", parseBatchCreateDocumentResponse, {
    method: "POST",
    body: formData,
    timeoutMs: 90_000,
    ...options
  });
}

function appendIntentFields(formData: FormData, intent?: WorkflowIntentInput) {
  if (!intent) {
    return;
  }
  formData.append("output_kind", intent.output_kind);
  formData.append("style_intent", intent.style_intent);
  formData.append("instruction", intent.instruction);
  if (intent.constraints?.page_width_pt !== undefined) {
    formData.append("page_width_pt", String(intent.constraints.page_width_pt));
  }
  if (intent.constraints?.page_height_pt !== undefined) {
    formData.append("page_height_pt", String(intent.constraints.page_height_pt));
  }
  if (intent.constraints?.target_font_size_pt !== undefined) {
    formData.append("target_font_size_pt", String(intent.constraints.target_font_size_pt));
  }
  if (intent.constraints?.allow_continuation !== undefined) {
    formData.append("allow_continuation", String(intent.constraints.allow_continuation));
  }
  if (intent.constraints?.preserve_images !== undefined) {
    formData.append("preserve_images", String(intent.constraints.preserve_images));
  }
}

export async function getJob(jobId: string, options: ApiRequestInit = {}) {
  return requestJson(`/api/jobs/${jobId}`, parseJobStatus, {
    retries: 1,
    ...options
  });
}

export async function cancelJob(jobId: string, options: ApiRequestInit = {}) {
  return requestJson(`/api/jobs/${jobId}/cancel`, parseJobStatus, {
    method: "POST",
    retries: 1,
    ...options
  });
}

export async function retryJob(jobId: string, options: ApiRequestInit = {}) {
  return requestJson(`/api/jobs/${jobId}/retry`, parseCreateDocumentResponse, {
    method: "POST",
    retries: 1,
    ...options
  });
}

export async function getRuntimeConfig(options: ApiRequestInit = {}) {
  return requestJson("/api/config", parseRuntimeConfig, {
    retries: 1,
    ...options
  });
}

export async function updateRuntimeConfig(
  payload: UpdateRuntimeConfig,
  options: ApiRequestInit = {}
) {
  return requestJson("/api/config", parseRuntimeConfig, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    retries: 1,
    ...options
  });
}

export async function listJobs(options: ApiRequestInit = {}) {
  return requestJson("/api/jobs", parseJobList, {
    retries: 1,
    ...options
  });
}

export async function listDocumentArtifacts(docId: string, options: ApiRequestInit = {}) {
  return requestJson(`/api/documents/${docId}/artifacts`, parseDocumentArtifacts, {
    retries: 1,
    ...options
  });
}

export async function getDocumentArtifact(
  docId: string,
  artifactName: string,
  options: ApiRequestInit = {}
) {
  return requestJson(`/api/documents/${docId}/artifacts/${artifactName}`, (payload) => payload, {
    retries: 1,
    timeoutMs: 20_000,
    ...options
  });
}

export async function verifyPreview(docId: string, options: ApiRequestInit = {}) {
  await requestNoBody(`/api/documents/${docId}/preview`, {
    method: "HEAD",
    headers: { Accept: "text/html" },
    retries: 1,
    ...options
  });
}

export async function verifyDownload(docId: string, options: ApiRequestInit = {}) {
  await requestNoBody(`/api/documents/${docId}/download`, {
    method: "HEAD",
    retries: 1,
    ...options
  });
}

async function requestJson<T>(
  input: RequestInfo | URL,
  parser: (payload: unknown) => T,
  init: ApiRequestInit = {}
): Promise<T> {
  const response = await request(input, init);
  let payload: unknown;
  try {
    payload = await response.json();
  } catch (error) {
    throw new ApiError("接口返回不是有效 JSON。", "parse");
  }

  try {
    return parser(payload);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(error instanceof Error ? error.message : "接口返回结构不正确。", "parse");
  }
}

async function requestNoBody(input: RequestInfo | URL, init: ApiRequestInit = {}) {
  await request(input, init);
}

async function request(input: RequestInfo | URL, init: ApiRequestInit = {}) {
  const { retries = 0, timeoutMs = DEFAULT_TIMEOUT_MS, signal, ...requestInit } = init;
  let lastError: ApiError | null = null;

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await fetchWithTimeout(input, requestInit, signal ?? undefined, timeoutMs);
      if (!response.ok) {
        throw new ApiError(await readErrorMessage(response), "http", response.status);
      }
      return response;
    } catch (error) {
      const apiError = normalizeError(error);
      lastError = apiError;
      if (!shouldRetry(apiError, attempt, retries)) {
        throw apiError;
      }
      await delay(350 * (attempt + 1));
    }
  }

  throw lastError ?? new ApiError("请求失败。", "network");
}

async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit,
  externalSignal: AbortSignal | undefined,
  timeoutMs: number
) {
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const abortFromExternalSignal = () => controller.abort();
  externalSignal?.addEventListener("abort", abortFromExternalSignal, { once: true });

  try {
    return await fetch(input, {
      ...init,
      signal: controller.signal
    });
  } catch (error) {
    if (timedOut) {
      throw new ApiError("请求超时，请确认后端服务是否可用。", "timeout");
    }
    if (externalSignal?.aborted) {
      throw new ApiError("请求已取消。", "abort");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    externalSignal?.removeEventListener("abort", abortFromExternalSignal);
  }
}

async function readErrorMessage(response: Response) {
  const fallback = response.status === 404 ? "请求的资源不存在。" : "请求失败。";
  const payload = await response.json().catch(() => null);
  if (isRecord(payload) && typeof payload.detail === "string") {
    return payload.detail;
  }
  if (isRecord(payload) && typeof payload.error === "string") {
    return payload.error;
  }
  if (isRecord(payload) && typeof payload.message === "string") {
    return payload.message;
  }
  return fallback;
}

function normalizeError(error: unknown) {
  if (error instanceof ApiError) {
    return error;
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return new ApiError("请求已取消。", "abort");
  }
  return new ApiError("无法连接后端服务，请确认 API 已启动。", "network");
}

function shouldRetry(error: ApiError, attempt: number, retries: number) {
  if (attempt >= retries) {
    return false;
  }
  if (error.kind === "abort" || error.kind === "parse") {
    return false;
  }
  return error.kind !== "http" || (error.status !== undefined && error.status >= 500);
}

function parseHealthResponse(payload: unknown): HealthResponse {
  if (!isRecord(payload) || payload.status !== "ok") {
    throw new Error("健康检查返回结构不正确。");
  }
  return { status: "ok" };
}

function parseCreateDocumentResponse(payload: unknown): CreateDocumentResponse {
  if (!isRecord(payload) || typeof payload.job_id !== "string" || typeof payload.doc_id !== "string") {
    throw new Error("上传接口返回缺少任务信息。");
  }

  return {
    job_id: payload.job_id,
    doc_id: payload.doc_id
  };
}

function parseBatchCreateDocumentResponse(payload: unknown): BatchCreateDocumentResponse {
  if (!isRecord(payload) || !Array.isArray(payload.jobs)) {
    throw new Error("批量上传接口返回缺少任务信息。");
  }
  return {
    jobs: payload.jobs.map(parseCreateDocumentResponse)
  };
}

function parseJobStatus(payload: unknown): JobStatus {
  if (
    !isRecord(payload) ||
    typeof payload.job_id !== "string" ||
    typeof payload.filename !== "string" ||
    typeof payload.status !== "string" ||
    !jobStatusValues.has(payload.status as JobStatus["status"]) ||
    typeof payload.progress !== "number" ||
    typeof payload.message !== "string"
  ) {
    throw new Error("任务状态返回缺少必要字段。");
  }

  return {
    job_id: payload.job_id,
    doc_id: typeof payload.doc_id === "string" || payload.doc_id === null ? payload.doc_id : undefined,
    filename: payload.filename,
    target_lang: typeof payload.target_lang === "string" || payload.target_lang === null ? payload.target_lang : undefined,
    status: payload.status as JobStatus["status"],
    progress: payload.progress,
    message: payload.message,
    error: typeof payload.error === "string" || payload.error === null ? payload.error : undefined,
    chunks: Array.isArray(payload.chunks) ? payload.chunks.map(parseChunkProgress) : []
  };
}

function parseJobList(payload: unknown): JobStatus[] {
  if (!Array.isArray(payload)) {
    throw new Error("任务历史返回结构不正确。");
  }
  return payload.map(parseJobStatus);
}

function parseRuntimeConfig(payload: unknown): RuntimeConfig {
  if (
    !isRecord(payload) ||
    typeof payload.default_target_lang !== "string" ||
    !Array.isArray(payload.allowed_target_langs) ||
    !payload.allowed_target_langs.every((item) => typeof item === "string") ||
    typeof payload.max_upload_bytes !== "number" ||
    typeof payload.translator_provider !== "string" ||
    typeof payload.openai_base_url !== "string" ||
    typeof payload.openai_model !== "string" ||
    typeof payload.openai_api_key_configured !== "boolean" ||
    typeof payload.translation_concurrency !== "number" ||
    typeof payload.translator_max_attempts !== "number" ||
    typeof payload.translation_chunk_max_chars !== "number" ||
    typeof payload.agent_max_repair_attempts !== "number" ||
    typeof payload.agent_enable_vision_analysis !== "boolean" ||
    typeof payload.layout_planner_model !== "string" ||
    typeof payload.vision_analyzer_model !== "string" ||
    !Array.isArray(payload.ocr_provider_order) ||
    !payload.ocr_provider_order.every((item) => typeof item === "string") ||
    typeof payload.ocr_min_confidence !== "number" ||
    typeof payload.ocr_provider_timeout_seconds !== "number" ||
    typeof payload.ocr_max_visual_candidates !== "number" ||
    !isRecord(payload.render_defaults)
  ) {
    throw new Error("运行配置返回结构不正确。");
  }

  const renderDefaults = parseRenderDefaults(payload.render_defaults);
  return {
    default_target_lang: payload.default_target_lang,
    allowed_target_langs: payload.allowed_target_langs,
    max_upload_bytes: payload.max_upload_bytes,
    translator_provider: payload.translator_provider,
    openai_base_url: payload.openai_base_url,
    openai_model: payload.openai_model,
    openai_api_key_configured: payload.openai_api_key_configured,
    translation_concurrency: payload.translation_concurrency,
    translator_max_attempts: payload.translator_max_attempts,
    translation_chunk_max_chars: payload.translation_chunk_max_chars,
    agent_max_repair_attempts: payload.agent_max_repair_attempts,
    agent_enable_vision_analysis: payload.agent_enable_vision_analysis,
    layout_planner_model: payload.layout_planner_model,
    vision_analyzer_model: payload.vision_analyzer_model,
    ocr_provider_order: payload.ocr_provider_order,
    ocr_min_confidence: payload.ocr_min_confidence,
    ocr_provider_timeout_seconds: payload.ocr_provider_timeout_seconds,
    ocr_max_visual_candidates: payload.ocr_max_visual_candidates,
    render_defaults: renderDefaults
  };
}

function parseRenderDefaults(payload: Record<string, unknown>): RenderDefaults {
  const fontStack = Array.isArray(payload.font_stack)
    ? payload.font_stack.filter((item): item is string => typeof item === "string")
    : [];
  if (
    typeof payload.target_lang !== "string" ||
    !fontStack.length ||
    typeof payload.line_height !== "number" ||
    typeof payload.paragraph_spacing_em !== "number" ||
    !isRecord(payload.overflow_policy)
  ) {
    throw new Error("运行配置缺少渲染默认值。");
  }

  return {
    target_lang: payload.target_lang,
    font_stack: fontStack,
    line_height: payload.line_height,
    paragraph_spacing_em: payload.paragraph_spacing_em,
    layout_mode:
      typeof payload.layout_mode === "string"
        ? (payload.layout_mode as RenderDefaults["layout_mode"])
        : undefined,
    page_layout: isRecord(payload.page_layout)
      ? (payload.page_layout as RenderDefaults["page_layout"])
      : undefined,
    role_styles: isRecord(payload.role_styles)
      ? (payload.role_styles as RenderDefaults["role_styles"])
      : undefined,
    alignment: isRecord(payload.alignment)
      ? (payload.alignment as RenderDefaults["alignment"])
      : undefined,
    overflow_policy: {
      strategy:
        typeof payload.overflow_policy.strategy === "string"
          ? (payload.overflow_policy.strategy as NonNullable<RenderDefaults["overflow_policy"]>["strategy"])
          : undefined,
      min_font_scale:
        typeof payload.overflow_policy.min_font_scale === "number"
          ? payload.overflow_policy.min_font_scale
          : undefined,
      max_font_scale:
        typeof payload.overflow_policy.max_font_scale === "number"
          ? payload.overflow_policy.max_font_scale
          : undefined,
      allow_box_expansion:
        typeof payload.overflow_policy.allow_box_expansion === "boolean"
          ? payload.overflow_policy.allow_box_expansion
          : undefined,
      allow_continuation_page:
        typeof payload.overflow_policy.allow_continuation_page === "boolean"
          ? payload.overflow_policy.allow_continuation_page
          : undefined
    },
    preserve_policy: isRecord(payload.preserve_policy)
      ? (payload.preserve_policy as RenderDefaults["preserve_policy"])
      : undefined
  };
}

function parseChunkProgress(payload: unknown) {
  if (
    !isRecord(payload) ||
    typeof payload.chunk_id !== "string" ||
    typeof payload.index !== "number" ||
    typeof payload.total !== "number" ||
    typeof payload.status !== "string" ||
    typeof payload.progress !== "number" ||
    typeof payload.message !== "string"
  ) {
    throw new Error("chunk 进度返回结构不正确。");
  }
  return {
    chunk_id: payload.chunk_id,
    index: payload.index,
    total: payload.total,
    status: payload.status,
    progress: payload.progress,
    message: payload.message,
    quality_flags: Array.isArray(payload.quality_flags)
      ? payload.quality_flags.filter((flag): flag is string => typeof flag === "string")
      : [],
    error: typeof payload.error === "string" || payload.error === null ? payload.error : undefined
  };
}

function parseDocumentArtifacts(payload: unknown): DocumentArtifacts {
  if (!isRecord(payload) || typeof payload.doc_id !== "string" || !Array.isArray(payload.artifacts)) {
    throw new Error("调试 artifact 返回结构不正确。");
  }

  return {
    doc_id: payload.doc_id,
    artifacts: payload.artifacts.map((item) => {
      if (
        !isRecord(item) ||
        typeof item.name !== "string" ||
        typeof item.kind !== "string" ||
        typeof item.available !== "boolean"
      ) {
        throw new Error("调试 artifact 条目结构不正确。");
      }
      return {
        name: item.name,
        kind: item.kind,
        available: item.available,
        href: typeof item.href === "string" || item.href === null ? item.href : undefined
      };
    })
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function delay(ms: number) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}
