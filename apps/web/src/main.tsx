import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Download,
  ExternalLink,
  FileText,
  Globe2,
  Loader2,
  RefreshCw,
  Settings2,
  Upload,
  X,
  XCircle
} from "lucide-react";
import type { JobStatus } from "@trans-typesetting/schema";
import {
  ApiError,
  createDocument,
  getHealth,
  getJob,
  verifyDownload,
  verifyPreview
} from "./api";
import "./styles.css";

type UploadIssue = {
  kind: "error" | "info";
  message: string;
};

type HealthState = "checking" | "online" | "offline";
type PreviewState = "idle" | "loading" | "ready" | "error";

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

const languageOptions = [
  { label: "简体中文", value: "zh-CN" },
  { label: "繁體中文", value: "zh-TW" },
  { label: "日本語", value: "ja-JP" },
  { label: "한국어", value: "ko-KR" },
  { label: "English", value: "en-US" }
];

const statuses: JobStatus["status"][] = [
  "queued",
  "parsing",
  "translating",
  "rendering",
  "completed"
];

const statusCopy: Record<JobStatus["status"], string> = {
  queued: "排队中",
  parsing: "解析",
  translating: "翻译",
  rendering: "排版",
  completed: "完成",
  failed: "失败"
};

const statusDetail: Record<JobStatus["status"], string> = {
  queued: "任务已提交",
  parsing: "提取 PDF 版面",
  translating: "生成译文内容",
  rendering: "生成预览与 PDF",
  completed: "译文 PDF 已就绪",
  failed: "任务未完成"
};

function App() {
  const [file, setFile] = useState<File | null>(null);
  const [targetLang, setTargetLang] = useState("zh-CN");
  const [job, setJob] = useState<JobStatus | null>(null);
  const [docId, setDocId] = useState<string | null>(null);
  const [healthState, setHealthState] = useState<HealthState>("checking");
  const [healthIssue, setHealthIssue] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [isRetryingStatus, setIsRetryingStatus] = useState(false);
  const [previewState, setPreviewState] = useState<PreviewState>("idle");
  const [previewIssue, setPreviewIssue] = useState<string | null>(null);
  const [uploadIssue, setUploadIssue] = useState<UploadIssue | null>(null);
  const [taskIssue, setTaskIssue] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const pollDelayRef = useRef(1200);

  const activeDocId = job?.doc_id ?? docId;
  const previewUrl = useMemo(
    () => (activeDocId ? `/api/documents/${activeDocId}/preview` : null),
    [activeDocId]
  );
  const downloadUrl = useMemo(
    () => (activeDocId ? `/api/documents/${activeDocId}/download` : null),
    [activeDocId]
  );
  const isTaskRunning = job
    ? ["queued", "parsing", "translating", "rendering"].includes(job.status)
    : false;
  const canSubmit = Boolean(file) && !isUploading && !isTaskRunning && healthState === "online";
  const isComplete = job?.status === "completed" && Boolean(previewUrl);
  const hasBackendFailure = healthState === "offline";
  const artifactsReady = isComplete && previewState === "ready" && !previewIssue;

  const checkHealth = useCallback(async () => {
    setHealthIssue(null);
    try {
      await getHealth();
      setHealthState("online");
      setHealthIssue(null);
      return true;
    } catch (reason) {
      setHealthState("offline");
      setHealthIssue(apiMessage(reason, "无法连接后端服务。"));
      return false;
    }
  }, []);

  const refreshJob = useCallback(async (jobId: string, signal?: AbortSignal) => {
    const nextJob = await getJob(jobId, { signal });
    setJob(nextJob);
    if (nextJob.doc_id) {
      setDocId(nextJob.doc_id);
    }
    return nextJob;
  }, []);

  useEffect(() => {
    void checkHealth();
  }, [checkHealth]);

  useEffect(() => {
    if (!job || job.status === "completed" || job.status === "failed") {
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    let timer: number | null = null;

    const poll = async () => {
      try {
        const nextJob = await refreshJob(job.job_id, controller.signal);
        if (!cancelled) {
          pollDelayRef.current = 1200;
          setTaskIssue(null);
          setHealthState("online");
          setHealthIssue(null);
          if (nextJob.error) {
            setTaskIssue(nextJob.error);
          }
        }
      } catch (reason) {
        if (!cancelled) {
          const message = apiMessage(reason, "无法读取任务状态。");
          setTaskIssue(message);
          if (reason instanceof ApiError && ["network", "timeout"].includes(reason.kind)) {
            setHealthState("offline");
            setHealthIssue(message);
          }
          pollDelayRef.current = Math.min(pollDelayRef.current * 1.7, 8000);
        }
      } finally {
        if (!cancelled) {
          timer = window.setTimeout(poll, pollDelayRef.current);
        }
      }
    };

    timer = window.setTimeout(poll, pollDelayRef.current);

    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [job, refreshJob]);

  useEffect(() => {
    setPreviewState(isComplete && previewUrl ? "loading" : "idle");
    setPreviewIssue(null);
  }, [isComplete, previewUrl]);

  useEffect(() => {
    if (!isComplete || !activeDocId) {
      return;
    }

    const controller = new AbortController();
    void Promise.all([
      verifyPreview(activeDocId, { signal: controller.signal }),
      verifyDownload(activeDocId, { signal: controller.signal })
    ]).catch((reason) => {
      if (!controller.signal.aborted) {
        setPreviewState("error");
        setPreviewIssue(previewMessage(reason));
      }
    });

    return () => controller.abort();
  }, [activeDocId, isComplete]);

  function resetFileInput() {
    if (inputRef.current) {
      inputRef.current.value = "";
    }
  }

  function clearFile() {
    setFile(null);
    setUploadIssue(null);
    resetFileInput();
  }

  function handleFile(nextFile: File | undefined) {
    if (!nextFile) {
      return;
    }

    if (!isPdfFile(nextFile)) {
      setFile(null);
      setUploadIssue({ kind: "error", message: "仅支持 PDF 文件，请重新选择。" });
      resetFileInput();
      return;
    }

    if (nextFile.size > MAX_UPLOAD_BYTES) {
      setFile(null);
      setUploadIssue({
        kind: "error",
        message: `PDF 文件不能超过 ${formatFileSize(MAX_UPLOAD_BYTES)}。`
      });
      resetFileInput();
      return;
    }

    setFile(nextFile);
    setUploadIssue({ kind: "info", message: "已选择 PDF，可以开始翻译。" });
    setTaskIssue(null);
  }

  function handleDragOver(event: React.DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setIsDraggingFile(true);
  }

  function handleDragLeave(event: React.DragEvent<HTMLButtonElement>) {
    const nextTarget = event.relatedTarget;
    if (!(nextTarget instanceof Node) || !event.currentTarget.contains(nextTarget)) {
      setIsDraggingFile(false);
    }
  }

  function handleDrop(event: React.DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setIsDraggingFile(false);
    handleFile(event.dataTransfer.files?.[0]);
  }

  async function retryStatus() {
    if (!job) {
      return;
    }

    setIsRetryingStatus(true);
    try {
      await checkHealth();
      await refreshJob(job.job_id);
      setTaskIssue(null);
    } catch (reason) {
      setTaskIssue(apiMessage(reason, "无法读取任务状态。"));
    } finally {
      setIsRetryingStatus(false);
    }
  }

  async function submit() {
    if (!file) {
      setUploadIssue({ kind: "error", message: "请选择一个英文 PDF 文件。" });
      return;
    }

    if (!isPdfFile(file) || file.size > MAX_UPLOAD_BYTES) {
      handleFile(file);
      return;
    }

    setIsUploading(true);
    setUploadIssue(null);
    setTaskIssue(null);
    setJob(null);
    setDocId(null);

    try {
      if (!(await checkHealth())) {
        throw new Error("后端服务不可用，请先启动 API。");
      }
      const payload = await createDocument(file, targetLang);
      setDocId(payload.doc_id);
      await refreshJob(payload.job_id);
    } catch (reason) {
      setTaskIssue(apiMessage(reason, "上传失败。"));
    } finally {
      setIsUploading(false);
    }
  }

  return (
    <main className="shell">
      <section className="workspace" aria-label="PDF translation workspace">
        <aside className="control-panel" aria-label="PDF translation controls">
          <header className="app-header">
            <div className="app-mark" aria-hidden="true">
              <FileText size={22} strokeWidth={2.1} />
            </div>
            <div className="app-title">
              <h1>Trans Typesetting</h1>
              <p>英文 PDF 纯译文排版</p>
            </div>
          </header>

          <section className="tool-section" aria-labelledby="upload-heading">
            <SectionTitle id="upload-heading" icon={<Upload size={16} />} title="上传" />
            <button
              className={`upload-zone${file ? " has-file" : ""}${isDraggingFile ? " is-dragging" : ""}${uploadIssue?.kind === "error" ? " has-error" : ""}`}
              type="button"
              aria-describedby="upload-feedback"
              onClick={() => inputRef.current?.click()}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
            >
              <span className="upload-icon" aria-hidden="true">
                <FileText size={28} />
              </span>
              <span className="upload-main">
                {file ? (
                  <>
                    <span className="file-name">{file.name}</span>
                    <span className="file-meta">{formatFileSize(file.size)}</span>
                  </>
                ) : (
                  <>
                    <span className="file-name">选择或拖入英文 PDF</span>
                    <span className="file-meta">支持 .pdf 文件</span>
                  </>
                )}
              </span>
              <span className="upload-action">
                {file ? "更换" : "浏览"}
                <ChevronRight size={16} />
              </span>
            </button>
            <input
              ref={inputRef}
              className="hidden-input"
              type="file"
              accept="application/pdf,.pdf"
              onChange={(event) => handleFile(event.target.files?.[0])}
            />
            <div className="upload-footer" id="upload-feedback">
              <InlineNotice issue={uploadIssue} />
              {file ? (
                <button className="ghost-button" type="button" onClick={clearFile}>
                  <X size={15} />
                  移除文件
                </button>
              ) : null}
            </div>
          </section>

          <section className="tool-section" aria-labelledby="config-heading">
            <SectionTitle id="config-heading" icon={<Settings2 size={16} />} title="配置" />
            <label className="field">
              <span>
                <Globe2 size={16} />
                目标语言
              </span>
              <select value={targetLang} onChange={(event) => setTargetLang(event.target.value)}>
                {languageOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="model-slot" aria-label="Future model configuration">
              <div>
                <span className="model-slot-label">模型配置</span>
                <strong>后续接入</strong>
              </div>
              <span className="slot-badge">预留</span>
            </div>
          </section>

          <section className="tool-section task-section" aria-labelledby="task-heading">
            <SectionTitle id="task-heading" icon={<Clock3 size={16} />} title="任务" />
            <BackendStatus
              healthState={healthState}
              issue={healthIssue}
              onRetry={() => void checkHealth()}
            />
            <button className="primary" type="button" onClick={submit} disabled={!canSubmit}>
              {isUploading ? <Loader2 className="spin" size={18} /> : <Upload size={18} />}
              <span>{isUploading ? "上传中" : job ? "重新翻译" : "开始翻译"}</span>
            </button>
            <JobProgress
              job={job}
              isUploading={isUploading}
              isRetryingStatus={isRetryingStatus}
              issue={taskIssue}
              onRetryStatus={retryStatus}
            />
            {artifactsReady && downloadUrl ? (
              <a className="download" href={downloadUrl}>
                <Download size={18} />
                <span>下载 PDF</span>
              </a>
            ) : null}
          </section>
        </aside>

        <section className="preview-panel" aria-label="Document preview">
          <div className="preview-toolbar">
            <div>
              <span>预览</span>
              <strong>{isComplete ? "纯译文排版" : "等待完成"}</strong>
            </div>
            {artifactsReady && downloadUrl && previewUrl ? (
              <div className="toolbar-actions">
                <a className="toolbar-button" href={previewUrl} target="_blank" rel="noreferrer" aria-label="Open preview in a new tab">
                  <ExternalLink size={18} />
                </a>
                <a className="toolbar-button" href={downloadUrl} aria-label="Download translated PDF">
                  <Download size={18} />
                </a>
              </div>
            ) : null}
          </div>
          <div className="preview-frame">
            {isComplete && previewUrl ? (
              <>
                <iframe
                  className={previewState === "ready" ? "is-ready" : ""}
                  title="PDF translation preview"
                  src={previewUrl}
                  onLoad={() => {
                    if (!previewIssue) {
                      setPreviewState("ready");
                    }
                  }}
                  onError={() => {
                    setPreviewState("error");
                    setPreviewIssue("预览 HTML 加载失败。");
                  }}
                />
                {previewIssue || previewState !== "ready" ? (
                  <PreviewOverlay state={previewIssue ? "error" : previewState} issue={previewIssue} />
                ) : null}
              </>
            ) : (
              <EmptyPreview
                job={job}
                taskIssue={taskIssue}
                backendOffline={hasBackendFailure}
                previewIssue={previewIssue}
              />
            )}
          </div>
        </section>
      </section>
    </main>
  );
}

function BackendStatus({
  healthState,
  issue,
  onRetry
}: {
  healthState: HealthState;
  issue: string | null;
  onRetry: () => void;
}) {
  const isOffline = healthState === "offline";
  const isChecking = healthState === "checking";
  return (
    <div className={`backend-status${isOffline ? " offline" : ""}`}>
      <span aria-hidden="true">
        {isChecking ? (
          <Loader2 className="spin" size={16} />
        ) : isOffline ? (
          <XCircle size={16} />
        ) : (
          <CheckCircle2 size={16} />
        )}
      </span>
      <p>
        {isChecking
          ? "正在检查后端服务"
          : isOffline
            ? issue ?? "后端服务不可用。"
            : "后端服务已连接"}
      </p>
      {isOffline ? (
        <button type="button" onClick={onRetry} aria-label="Retry backend health check">
          <RefreshCw size={15} />
        </button>
      ) : null}
    </div>
  );
}

function SectionTitle({ icon, id, title }: { icon: React.ReactNode; id: string; title: string }) {
  return (
    <h2 className="section-title" id={id}>
      {icon}
      {title}
    </h2>
  );
}

function InlineNotice({ issue }: { issue: UploadIssue | null }) {
  if (!issue) {
    return <span className="hint">等待 PDF 文件。</span>;
  }

  return (
    <span className={`inline-notice ${issue.kind}`}>
      {issue.kind === "error" ? <AlertCircle size={15} /> : <CheckCircle2 size={15} />}
      {issue.message}
    </span>
  );
}

function JobProgress({
  job,
  isUploading,
  isRetryingStatus,
  issue,
  onRetryStatus
}: {
  job: JobStatus | null;
  isUploading: boolean;
  isRetryingStatus: boolean;
  issue: string | null;
  onRetryStatus: () => void;
}) {
  const status = isUploading ? "queued" : job?.status;
  const progress = normalizeProgress(isUploading ? 0.04 : job?.progress ?? 0);
  const currentIndex = status ? statuses.indexOf(status) : -1;
  const visibleMessage = issue ?? job?.error ?? job?.message ?? (isUploading ? "正在提交文件。" : null);
  const isFailed = status === "failed" || Boolean(issue && !job);
  const hasWarning = Boolean(issue && job && status !== "failed");
  const isDone = status === "completed";
  const canRetryStatus = Boolean(job && issue && status !== "completed" && status !== "failed");

  return (
    <div className={`job-card${isFailed ? " failed" : ""}${hasWarning ? " warning" : ""}${isDone ? " completed" : ""}`} aria-live="polite">
      <div className="job-card-head">
        <div className="status-icon" aria-hidden="true">
          {isDone ? (
            <CheckCircle2 size={18} />
          ) : isFailed ? (
            <XCircle size={18} />
          ) : hasWarning ? (
            <AlertCircle size={18} />
          ) : status ? (
            <Loader2 className="spin" size={18} />
          ) : (
            <Clock3 size={18} />
          )}
        </div>
        <div>
          <span>{status ? statusCopy[status] : "未开始"}</span>
          <strong>{status ? statusDetail[status] : "选择 PDF 后提交任务"}</strong>
        </div>
      </div>

      <div
        className="meter"
        role="progressbar"
        aria-label="任务进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(progress * 100)}
      >
        <div style={{ width: `${Math.round(progress * 100)}%` }} />
      </div>

      <ol className="status-list" aria-label="Job states">
        {statuses.map((step, index) => {
          const isActive = status === step;
          const isPassed = isDone || (currentIndex > index && currentIndex !== -1);
          return (
            <li key={step} className={`${isActive ? "active" : ""}${isPassed ? " passed" : ""}`}>
              <span aria-hidden="true" />
              <small aria-current={isActive ? "step" : undefined}>{statusCopy[step]}</small>
            </li>
          );
        })}
        {status === "failed" ? (
          <li className="active failed-step">
            <span aria-hidden="true" />
            <small>失败</small>
          </li>
        ) : null}
      </ol>

      {visibleMessage ? <p className="job-message">{visibleMessage}</p> : null}
      {canRetryStatus ? (
        <button className="secondary-action" type="button" onClick={onRetryStatus} disabled={isRetryingStatus}>
          {isRetryingStatus ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
          <span>{isRetryingStatus ? "重试中" : "重试读取状态"}</span>
        </button>
      ) : null}
    </div>
  );
}

function PreviewOverlay({ state, issue }: { state: PreviewState; issue: string | null }) {
  const isError = state === "error";

  return (
    <div className={`preview-overlay${isError ? " failed" : ""}`} role="status">
      {isError ? <XCircle size={30} /> : <Loader2 className="spin" size={30} />}
      <span>{isError ? issue ?? "预览加载失败" : "正在加载预览"}</span>
    </div>
  );
}

function EmptyPreview({
  job,
  taskIssue,
  backendOffline,
  previewIssue
}: {
  job: JobStatus | null;
  taskIssue: string | null;
  backendOffline: boolean;
  previewIssue: string | null;
}) {
  const hasFailure = job?.status === "failed" || Boolean(taskIssue) || backendOffline || Boolean(previewIssue);
  const message = backendOffline
    ? "后端服务不可用，请确认 API 已启动。"
    : previewIssue ?? taskIssue ?? job?.error ?? job?.message;

  return (
    <div className={`empty-preview${hasFailure ? " failed" : ""}`}>
      {hasFailure ? <XCircle size={42} /> : <FileText size={42} />}
      <h2>{hasFailure ? "无法生成预览" : "预览将在完成后显示"}</h2>
      <p>{hasFailure ? message : "任务完成后显示译文预览。"}</p>
    </div>
  );
}

function isPdfFile(file: File) {
  return file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
}

function formatFileSize(bytes: number) {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function normalizeProgress(progress: number) {
  if (!Number.isFinite(progress)) {
    return 0;
  }
  return Math.min(1, Math.max(0, progress));
}

function apiMessage(reason: unknown, fallback: string) {
  if (reason instanceof Error && reason.message) {
    return reason.message;
  }
  return fallback;
}

function previewMessage(reason: unknown) {
  if (reason instanceof ApiError && reason.status === 404) {
    return "任务已完成，但预览或 PDF 文件不存在。";
  }
  return apiMessage(reason, "预览或下载资源不可用。");
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
