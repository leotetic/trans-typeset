import type { JobStatus } from "@trans-typesetting/schema";

export interface CreateDocumentResponse {
  job_id: string;
  doc_id: string;
}

export interface HealthResponse {
  status: "ok";
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
  "failed"
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

export async function createDocument(
  file: File,
  targetLang: string,
  options: ApiRequestInit = {}
) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("target_lang", targetLang);

  return requestJson("/api/documents", parseCreateDocumentResponse, {
    method: "POST",
    body: formData,
    timeoutMs: 60_000,
    ...options
  });
}

export async function getJob(jobId: string, options: ApiRequestInit = {}) {
  return requestJson(`/api/jobs/${jobId}`, parseJobStatus, {
    retries: 1,
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
    status: payload.status as JobStatus["status"],
    progress: payload.progress,
    message: payload.message,
    error: typeof payload.error === "string" || payload.error === null ? payload.error : undefined
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
