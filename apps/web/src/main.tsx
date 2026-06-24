import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertCircle,
  Archive,
  CheckCircle2,
  ChevronRight,
  Clock3,
  ClipboardCheck,
  Download,
  Eye,
  ExternalLink,
  FileText,
  Globe2,
  History,
  Loader2,
  PlayCircle,
  Search,
  RefreshCw,
  SlidersHorizontal,
  Settings2,
  Upload,
  X,
  XCircle
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { EditScope, JobStatus, OutputKind, StyleIntent, UserConstraints, WorkflowMode } from "@trans-typesetting/schema";
import {
  ApiError,
  cancelJob,
  continueJob,
  createDocument,
  createDocxWorkflow,
  createDocumentsBatch,
  createImageWorkflow,
  createTextWorkflow,
  getDocumentArtifact,
  getHealth,
  getJobEvents,
  getJob,
  getRuntimeConfig,
  listDocumentArtifacts,
  listJobs,
  retryJob,
  retypesetJob,
  updateRuntimeConfig,
  verifyDownload,
  verifyPreview
} from "./api";
import type { ArtifactSummary, JobLogEvent, RuntimeConfig } from "./api";
import "./styles.css";

type UploadIssue = {
  kind: "error" | "info";
  target?: "requirements" | "document";
  message: string;
};

type HealthState = "checking" | "online" | "offline";
type PreviewState = "idle" | "loading" | "ready" | "error";
type InspectorState = "idle" | "loading" | "ready" | "error";
type LogState = "idle" | "loading" | "ready" | "error";
type InputMode = "text" | "image" | "pdf" | "docx";
type ActiveSection = "translate" | "typeset" | "combined" | "developer";

type SavedTaskSnapshot = {
  job: JobStatus;
  docId: string | null;
  targetLang: string;
  savedAt: string;
};

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
const LAST_TASK_STORAGE_KEY = "trans-typesetting:last-task";
const LOG_STALE_MS = 10_000;

const languageOptions = [
  { label: "简体中文", value: "zh-CN" },
  { label: "繁體中文", value: "zh-TW" },
  { label: "日本語", value: "ja-JP" },
  { label: "한국어", value: "ko-KR" },
  { label: "English", value: "en-US" }
];

const languageLabels = new Map(languageOptions.map((option) => [option.value, option.label]));

const inputModes: Array<{ label: string; value: InputMode }> = [
  { label: "PDF", value: "pdf" },
  { label: "Text", value: "text" },
  { label: "Image", value: "image" },
  { label: "Word", value: "docx" }
];

const outputKindOptions: Array<{ label: string; value: OutputKind }> = [
  { label: "原文重排", value: "typeset_document" },
  { label: "翻译排版", value: "translation" },
  { label: "参考版式", value: "layout_reference" },
  { label: "摘要版式", value: "summary_layout" }
];

const styleIntentOptions: Array<{ label: string; value: StyleIntent }> = [
  { label: "Academic", value: "academic" },
  { label: "Report", value: "report" },
  { label: "Handout", value: "handout" },
  { label: "Slide", value: "slide_like" },
  { label: "Plain", value: "plain" }
];

const statuses: JobStatus["status"][] = [
  "queued",
  "parsing",
  "translating",
  "rendering",
  "completed"
];
const jobStatusValues = new Set<JobStatus["status"]>([...statuses, "failed", "canceled"]);

type BaseUrlValidation = {
  error: string | null;
  warning: string | null;
};

const statusCopy: Record<JobStatus["status"], string> = {
  queued: "排队中",
  parsing: "解析",
  translating: "翻译",
  rendering: "排版",
  completed: "完成",
  failed: "失败",
  canceled: "已取消"
};

const statusDetail: Record<JobStatus["status"], string> = {
  queued: "任务已提交",
  parsing: "提取 PDF 版面",
  translating: "生成内容计划",
  rendering: "生成预览与 PDF",
  completed: "排版 PDF 已就绪",
  failed: "任务未完成",
  canceled: "任务已取消"
};

type PdfSlot = "content" | "layout";

type ConstraintDraft = Required<Pick<
  UserConstraints,
  "page_width_pt" | "page_height_pt" | "target_font_size_pt" | "allow_continuation" | "preserve_images"
>>;

const DEFAULT_CONSTRAINTS: ConstraintDraft = {
  page_width_pt: 612,
  page_height_pt: 792,
  target_font_size_pt: 11,
  allow_continuation: true,
  preserve_images: true
};

const sidebarItems: Array<{
  id: ActiveSection;
  label: string;
  detail: string;
  icon: LucideIcon;
}> = [
  { id: "translate", label: "Translate", detail: "Only translate", icon: Globe2 },
  { id: "typeset", label: "Intelligent Typeset", detail: "Only layout", icon: SlidersHorizontal },
  { id: "combined", label: "Translate + Typeset", detail: "Full workflow", icon: PlayCircle },
  { id: "developer", label: "Developer", detail: "Diagnostics and settings", icon: Settings2 }
];

function App() {
  const [activeSection, setActiveSection] = useState<ActiveSection>("translate");
  const [inputMode, setInputMode] = useState<InputMode>("pdf");
  const [files, setFiles] = useState<File[]>([]);
  const [contentPdfFile, setContentPdfFile] = useState<File | null>(null);
  const [layoutPdfFile, setLayoutPdfFile] = useState<File | null>(null);
  const [textInput, setTextInput] = useState("Title\n\nAbstract This paper studies local smart typesetting [1].");
  const [targetLang, setTargetLang] = useState("zh-CN");
  const [outputKind, setOutputKind] = useState<OutputKind>("translation");
  const [styleIntent, setStyleIntent] = useState<StyleIntent>("academic");
  const [instruction, setInstruction] = useState("按照gb-GB/T 7713.1 进行排版");
  const [isConstraintsOpen, setIsConstraintsOpen] = useState(false);
  const [constraintDraft, setConstraintDraft] = useState<ConstraintDraft>(DEFAULT_CONSTRAINTS);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [docId, setDocId] = useState<string | null>(null);
  const [healthState, setHealthState] = useState<HealthState>("checking");
  const [healthIssue, setHealthIssue] = useState<string | null>(null);
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfig | null>(null);
  const [configDraft, setConfigDraft] = useState({
    openai_base_url: "",
    openai_model: "",
    openai_api_key: "",
    translation_concurrency: 2,
    translator_max_attempts: 2,
    translation_chunk_max_chars: 6000,
    ocr_provider_order: ["pix2text", "deterministic"],
    ocr_min_confidence: 0.35,
    ocr_provider_timeout_seconds: 12,
    ocr_max_visual_candidates: 12
  });
  const [configIssue, setConfigIssue] = useState<string | null>(null);
  const [isSavingConfig, setIsSavingConfig] = useState(false);
  const [jobHistory, setJobHistory] = useState<JobStatus[]>([]);
  const [historyIssue, setHistoryIssue] = useState<string | null>(null);
  const [sessionNotice, setSessionNotice] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [isCanceling, setIsCanceling] = useState(false);
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [draggingPdfSlot, setDraggingPdfSlot] = useState<PdfSlot | null>(null);
  const [isRetryingStatus, setIsRetryingStatus] = useState(false);
  const [isContinuingJob, setIsContinuingJob] = useState(false);
  const [isRetryingJob, setIsRetryingJob] = useState(false);
  const [isRetypesettingJob, setIsRetypesettingJob] = useState(false);
  const [retypesetInstruction, setRetypesetInstruction] = useState("按当前说明重新排版，保留原文内容。");
  const [retypesetScopeMode, setRetypesetScopeMode] = useState<"all" | "pages" | "blocks">("all");
  const [retypesetPages, setRetypesetPages] = useState("");
  const [retypesetBlocks, setRetypesetBlocks] = useState("");
  const [previewState, setPreviewState] = useState<PreviewState>("idle");
  const [previewIssue, setPreviewIssue] = useState<string | null>(null);
  const [previewReloadKey, setPreviewReloadKey] = useState(0);
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [artifactIssue, setArtifactIssue] = useState<string | null>(null);
  const [selectedArtifact, setSelectedArtifact] = useState("renderer-diagnostics");
  const [renderEvaluationWarning, setRenderEvaluationWarning] = useState<string | null>(null);
  const [inspectorState, setInspectorState] = useState<InspectorState>("idle");
  const [inspectorPayload, setInspectorPayload] = useState<string>("");
  const [inspectorIssue, setInspectorIssue] = useState<string | null>(null);
  const [isInspectorCollapsed, setIsInspectorCollapsed] = useState(false);
  const [uploadIssue, setUploadIssue] = useState<UploadIssue | null>(null);
  const [taskIssue, setTaskIssue] = useState<string | null>(null);
  const [logEvents, setLogEvents] = useState<JobLogEvent[]>([]);
  const [logState, setLogState] = useState<LogState>("idle");
  const [logIssue, setLogIssue] = useState<string | null>(null);
  const [isLogCollapsed, setIsLogCollapsed] = useState(false);
  const [isLogAutoScroll, setIsLogAutoScroll] = useState(true);
  const [lastLogEventAt, setLastLogEventAt] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const inputRef = useRef<HTMLInputElement | null>(null);
  const contentPdfInputRef = useRef<HTMLInputElement | null>(null);
  const layoutPdfInputRef = useRef<HTMLInputElement | null>(null);
  const seenLogEventIdsRef = useRef<Set<string>>(new Set());
  const hiddenLogEventIdsRef = useRef<Set<string>>(new Set());
  const pollDelayRef = useRef(1200);

  const activeJobId = job?.job_id ?? null;
  const activeDocId = job?.doc_id ?? docId;
  const previewUrl = useMemo(
    () => (activeDocId ? `/api/documents/${activeDocId}/preview` : null),
    [activeDocId]
  );
  const downloadUrl = useMemo(
    () => (activeDocId ? `/api/documents/${activeDocId}/download` : null),
    [activeDocId]
  );
  const previewFrameUrl = useMemo(
    () => (previewUrl ? `${previewUrl}?reload=${previewReloadKey}` : null),
    [previewReloadKey, previewUrl]
  );
  const isTaskRunning = job
    ? ["queued", "parsing", "translating", "rendering"].includes(job.status)
    : false;
  const isLogWaiting =
    isTaskRunning &&
    healthState === "online" &&
    lastLogEventAt !== null &&
    nowMs - lastLogEventAt > LOG_STALE_MS;
  const workflowMode = workflowModeForSection(activeSection);
  const effectiveOutputKind = outputKindForWorkflowMode(workflowMode);
  const isTranslateOnly = workflowMode === "translate_only";
  const isPdfWorkflow = inputMode === "pdf";
  const isDocxWorkflow = inputMode === "docx";
  const isSourceOnlyOutput = workflowMode === "typeset_only";
  const hasSubmitInput =
    isTranslateOnly
      ? Boolean(contentPdfFile)
      : inputMode === "text"
        ? Boolean(textInput.trim())
        : isPdfWorkflow
          ? Boolean(contentPdfFile)
          : files.length > 0;
  const canSubmit = hasSubmitInput && !isUploading && !isTaskRunning && healthState === "online";
  const isComplete = job?.status === "completed" && Boolean(previewUrl);
  const outputReady = isComplete && Boolean(downloadUrl);
  const hasBackendFailure = healthState === "offline";
  const baseUrlValidation = useMemo(
    () => validateOpenAIBaseUrl(configDraft.openai_base_url),
    [configDraft.openai_base_url]
  );
  const configuredLanguages = runtimeConfig?.allowed_target_langs.length
    ? runtimeConfig.allowed_target_langs
    : languageOptions.map((option) => option.value);
  const maxUploadBytes = runtimeConfig?.max_upload_bytes ?? MAX_UPLOAD_BYTES;
  const selectedLanguageLabel = languageLabels.get(targetLang) ?? targetLang;
  const activeModeLabel = isTranslateOnly
    ? contentPdfFile
      ? isDocxFile(contentPdfFile)
        ? "Word"
        : "PDF"
      : "PDF / Word"
    : inputModes.find((mode) => mode.value === inputMode)?.label ?? inputMode;
  const activeFilename =
    job?.filename ??
    contentPdfFile?.name ??
    (files.length === 1
      ? files[0].name
      : files.length > 1
        ? `${files.length} 个文件`
        : null);
  const progressPercent = Math.round(normalizeProgress(isUploading ? 0.04 : job?.progress ?? 0) * 100);
  const activeStatusLabel = isUploading ? "提交中" : job ? statusCopy[job.status] : "待提交";
  const activeStatusDetail = isUploading
    ? "正在发送输入文件"
    : job
      ? statusDetail[job.status]
      : hasSubmitInput
        ? "输入已就绪"
        : "等待输入";
  const idleJobDetail = isTranslateOnly
    ? "上传 PDF 或 Word 后提交任务"
    : isPdfWorkflow
      ? "选择 PDF 后提交任务"
      : "添加输入后提交任务";
  const outputKindLabel = workflowModeLabel(workflowMode);
  const styleIntentLabel = styleIntentOptions.find((option) => option.value === styleIntent)?.label ?? styleIntent;
  const constraintSummary = isConstraintsOpen
    ? `${constraintDraft.page_width_pt} x ${constraintDraft.page_height_pt} pt · ${constraintDraft.target_font_size_pt} pt`
    : "默认渲染约束";
  const layoutReferenceLabel = layoutPdfFile ? "参考 PDF 已接入" : "沿用源文档版式";
  const sourcePdfCopy = sourcePdfCopyForWorkflow(workflowMode);
  const layoutPdfCopy = layoutPdfCopyForWorkflow(workflowMode);
  const shouldShowPrimarySourceHeader = workflowMode === "translate_and_typeset";

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

  const refreshConfig = useCallback(async (signal?: AbortSignal) => {
    try {
      const config = await getRuntimeConfig({ signal });
      setRuntimeConfig(config);
      setConfigDraft({
        openai_base_url: config.openai_base_url,
        openai_model: config.openai_model,
        openai_api_key: "",
        translation_concurrency: config.translation_concurrency,
        translator_max_attempts: config.translator_max_attempts,
        translation_chunk_max_chars: config.translation_chunk_max_chars,
        ocr_provider_order: config.ocr_provider_order,
        ocr_min_confidence: config.ocr_min_confidence,
        ocr_provider_timeout_seconds: config.ocr_provider_timeout_seconds,
        ocr_max_visual_candidates: config.ocr_max_visual_candidates
      });
      setConfigIssue(null);
      if (!config.allowed_target_langs.includes(targetLang)) {
        setTargetLang(config.default_target_lang);
      }
      return config;
    } catch (reason) {
      if (signal?.aborted) {
        return null;
      }
      const message = apiMessage(reason, "无法读取运行配置。");
      setConfigIssue(message);
      return null;
    }
  }, [targetLang]);

  const refreshHistory = useCallback(async (signal?: AbortSignal) => {
    try {
      const history = await listJobs({ signal });
      setJobHistory(history);
      setHistoryIssue(null);
      return history;
    } catch (reason) {
      if (signal?.aborted) {
        return [];
      }
      setHistoryIssue(apiMessage(reason, "无法读取任务历史。"));
      return [];
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

  const refreshJobEvents = useCallback(async (signal?: AbortSignal) => {
    if (!activeJobId) {
      return null;
    }
    try {
      const payload = await getJobEvents(activeJobId, 80, { signal });
      if (signal?.aborted) {
        return null;
      }
      const hasNewEvent = payload.events.some(
        (event) => !seenLogEventIdsRef.current.has(event.id)
      );
      payload.events.forEach((event) => {
        seenLogEventIdsRef.current.add(event.id);
      });
      const visibleEvents = payload.events.filter(
        (event) => !hiddenLogEventIdsRef.current.has(event.id)
      );
      setLogEvents((current) => mergeLogEvents(current, visibleEvents));
      setLogState("ready");
      setLogIssue(null);
      if (hasNewEvent) {
        const timestamp = Date.now();
        setLastLogEventAt(timestamp);
        setNowMs(timestamp);
      } else {
        setNowMs(Date.now());
      }
      return payload;
    } catch (reason) {
      if (signal?.aborted) {
        return null;
      }
      setLogState("error");
      setLogIssue(apiMessage(reason, "无法读取 pipeline events。"));
      return null;
    }
  }, [activeJobId]);

  function restoreJobSnapshot(nextJob: JobStatus, fallbackDocId: string | null = null) {
    setJob(nextJob);
    setDocId(nextJob.doc_id ?? fallbackDocId);
    setTaskIssue(nextJob.error ?? null);
    setPreviewIssue(null);
    setPreviewState(nextJob.status === "completed" ? "loading" : "idle");
  }

  useEffect(() => {
    const savedTask = readSavedTaskSnapshot();
    if (!savedTask) {
      return;
    }

    restoreJobSnapshot(savedTask.job, savedTask.docId);
    setTargetLang(isKnownLanguage(savedTask.targetLang) ? savedTask.targetLang : "zh-CN");
    setSessionNotice(`已恢复最近任务：${savedTask.job.filename}`);
  }, []);

  useEffect(() => {
    if (!job) {
      return;
    }

    writeSavedTaskSnapshot({
      job,
      docId: activeDocId ?? null,
      targetLang,
      savedAt: new Date().toISOString()
    });
  }, [activeDocId, job, targetLang]);

  useEffect(() => {
    seenLogEventIdsRef.current.clear();
    hiddenLogEventIdsRef.current.clear();
    setLogEvents([]);
    setLogIssue(null);
    setLastLogEventAt(null);
    setNowMs(Date.now());
    setLogState(activeJobId ? "loading" : "idle");
  }, [activeJobId]);

  useEffect(() => {
    if (!activeJobId) {
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    let timer: number | null = null;
    const terminal = job ? ["completed", "failed", "canceled"].includes(job.status) : false;

    const pollLogs = async () => {
      await refreshJobEvents(controller.signal);
      if (!cancelled && !terminal) {
        timer = window.setTimeout(pollLogs, 1600);
      }
    };

    void pollLogs();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [activeJobId, job?.status, refreshJobEvents]);

  useEffect(() => {
    if (!isTaskRunning) {
      return;
    }
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isTaskRunning]);

  useEffect(() => {
    void checkHealth();
  }, [checkHealth]);

  useEffect(() => {
    const controller = new AbortController();
    void refreshConfig(controller.signal);
    void refreshHistory(controller.signal);
    return () => controller.abort();
  }, [refreshConfig, refreshHistory]);

  useEffect(() => {
    if (!job || ["completed", "failed", "canceled"].includes(job.status)) {
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
          if (reason instanceof ApiError && ["network", "timeout"].includes(reason.kind)) {
            const backendReachable = await checkHealth();
            if (!cancelled) {
              if (backendReachable) {
                setTaskIssue("任务仍在后台运行，状态同步暂时中断。");
                setHealthState("online");
                setHealthIssue(null);
              } else {
                setTaskIssue(message);
              }
            }
          } else {
            setTaskIssue(message);
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
  }, [checkHealth, job, refreshJob]);

  useEffect(() => {
    setPreviewState(isComplete && previewUrl ? "loading" : "idle");
    setPreviewIssue(null);
  }, [isComplete, previewUrl]);

  useEffect(() => {
    if (!isComplete || !activeDocId) {
      setArtifacts([]);
      setArtifactIssue(null);
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
    void listDocumentArtifacts(activeDocId, { signal: controller.signal })
      .then((payload) => {
        setArtifacts(payload.artifacts);
        setArtifactIssue(null);
        const selected = payload.artifacts.find(
          (artifact) => artifact.name === selectedArtifact && artifact.available
        );
        if (!selected) {
          setSelectedArtifact(
            payload.artifacts.find((artifact) => artifact.available)?.name ?? "renderer-diagnostics"
          );
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setArtifactIssue(apiMessage(reason, "无法读取调试 artifact。"));
        }
      });

    return () => controller.abort();
  }, [activeDocId, isComplete, selectedArtifact]);

  useEffect(() => {
    if (!activeDocId || !isComplete || !artifacts.some((artifact) => artifact.name === selectedArtifact && artifact.available)) {
      setInspectorState("idle");
      setInspectorPayload("");
      setInspectorIssue(null);
      return;
    }

    const controller = new AbortController();
    setInspectorState("loading");
    setInspectorIssue(null);
    void getDocumentArtifact(activeDocId, selectedArtifact, { signal: controller.signal })
      .then((payload) => {
        setInspectorPayload(JSON.stringify(payload, null, 2));
        setInspectorState("ready");
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setInspectorState("error");
          setInspectorIssue(apiMessage(reason, "无法读取调试 artifact。"));
        }
      });

    return () => controller.abort();
  }, [activeDocId, artifacts, isComplete, selectedArtifact]);

  useEffect(() => {
    if (!activeDocId || !isComplete) {
      setRenderEvaluationWarning(null);
      return;
    }

    const controller = new AbortController();
    void getDocumentArtifact(activeDocId, "render-evaluation", { signal: controller.signal })
      .then((payload) => {
        if (!controller.signal.aborted) {
          setRenderEvaluationWarning(renderEvaluationMessage(payload));
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setRenderEvaluationWarning(null);
        }
      });

    return () => controller.abort();
  }, [activeDocId, isComplete]);

  function resetFileInput() {
    if (inputRef.current) {
      inputRef.current.value = "";
    }
    if (contentPdfInputRef.current) {
      contentPdfInputRef.current.value = "";
    }
    if (layoutPdfInputRef.current) {
      layoutPdfInputRef.current.value = "";
    }
  }

  function clearFile() {
    setFiles([]);
    setContentPdfFile(null);
    setLayoutPdfFile(null);
    setUploadIssue(null);
    resetFileInput();
  }

  function clearSavedTask() {
    removeSavedTaskSnapshot();
    setJob(null);
    setDocId(null);
    setTaskIssue(null);
    setPreviewIssue(null);
    setSessionNotice(null);
    setPreviewState("idle");
  }

  function clearPdfSlot(slot: PdfSlot, options: { preserveIssue?: boolean } = {}) {
    if (slot === "content") {
      setContentPdfFile(null);
      if (contentPdfInputRef.current) {
        contentPdfInputRef.current.value = "";
      }
    } else {
      setLayoutPdfFile(null);
      if (layoutPdfInputRef.current) {
        layoutPdfInputRef.current.value = "";
      }
    }
    if (!options.preserveIssue) {
      setUploadIssue(null);
    }
  }

  function clearRequirementsInput() {
    if (isPdfWorkflow) {
      clearPdfSlot("layout");
      return;
    }
    clearFile();
  }

  function handlePdfSlotFiles(slot: PdfSlot, nextFiles: FileList | File[] | null | undefined) {
    const selectedFile = Array.from(nextFiles ?? [])[0];
    if (!selectedFile) {
      return;
    }
    if (!isPdfFile(selectedFile)) {
      setUploadIssue({
        kind: "error",
        target: slot === "content" ? "document" : "requirements",
        message: "仅支持 PDF 文件，请重新选择。"
      });
      clearPdfSlot(slot, { preserveIssue: true });
      return;
    }
    if (selectedFile.size > maxUploadBytes) {
      setUploadIssue({
        kind: "error",
        target: slot === "content" ? "document" : "requirements",
        message: `PDF 文件不能超过 ${formatFileSize(maxUploadBytes)}。`
      });
      clearPdfSlot(slot, { preserveIssue: true });
      return;
    }
    if (slot === "content") {
      setContentPdfFile(selectedFile);
      setUploadIssue({ kind: "info", target: "document", message: sourcePdfCopy.selectedMessage });
    } else {
      setLayoutPdfFile(selectedFile);
      setUploadIssue({ kind: "info", target: "requirements", message: layoutPdfCopy.selectedMessage });
    }
    setTaskIssue(null);
  }

  function handleTranslateSourceFiles(nextFiles: FileList | File[] | null | undefined) {
    const selectedFile = Array.from(nextFiles ?? [])[0];
    if (!selectedFile) {
      return;
    }
    if (!isTranslatableDocumentFile(selectedFile)) {
      setUploadIssue({
        kind: "error",
        target: "document",
        message: "仅支持 PDF 或 Word 文档，请重新选择。"
      });
      clearPdfSlot("content", { preserveIssue: true });
      return;
    }
    if (selectedFile.size > maxUploadBytes) {
      setUploadIssue({
        kind: "error",
        target: "document",
        message: `文件不能超过 ${formatFileSize(maxUploadBytes)}。`
      });
      clearPdfSlot("content", { preserveIssue: true });
      return;
    }
    setContentPdfFile(selectedFile);
    setUploadIssue({
      kind: "info",
      target: "document",
      message: isDocxFile(selectedFile) ? "已选择 Word 文档，可以开始翻译。" : "已选择 PDF，可以开始翻译。"
    });
    setTaskIssue(null);
  }

  function handleFiles(nextFiles: FileList | File[] | null | undefined) {
    const selectedFiles = Array.from(nextFiles ?? []);
    if (!selectedFiles.length) {
      return;
    }

    const invalidFile = selectedFiles.find((nextFile) => {
      if (inputMode === "image") {
        return !isImageFile(nextFile);
      }
      if (inputMode === "docx") {
        return !isDocxFile(nextFile);
      }
      return !isPdfFile(nextFile);
    });
    if (invalidFile) {
      setFiles([]);
      setUploadIssue({
        kind: "error",
        target: "requirements",
        message:
          inputMode === "image"
            ? "仅支持 PNG、JPEG 或 WebP 图片。"
            : inputMode === "docx"
              ? "仅支持 DOCX 文件。"
              : "仅支持 PDF 文件，请重新选择。"
      });
      resetFileInput();
      return;
    }

    const oversizedFile = selectedFiles.find((nextFile) => nextFile.size > maxUploadBytes);
    if (oversizedFile) {
      setFiles([]);
      setUploadIssue({
        kind: "error",
        target: "requirements",
        message: `PDF 文件不能超过 ${formatFileSize(maxUploadBytes)}。`
      });
      resetFileInput();
      return;
    }

    setFiles(selectedFiles);
    setUploadIssue({
      kind: "info",
      target: "requirements",
      message:
        inputMode === "image"
          ? "已选择图片，可以开始智能排版。"
          : inputMode === "docx"
            ? "已选择 Word 文档，提交后会先通过 LibreOffice 转为 PDF。"
            : selectedFiles.length === 1
              ? "已选择 PDF，可以开始处理。"
              : `已选择 ${selectedFiles.length} 个 PDF，可以批量处理。`
    });
    setTaskIssue(null);
  }

  function handlePdfDragOver(event: React.DragEvent<HTMLButtonElement>, slot: PdfSlot) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setDraggingPdfSlot(slot);
  }

  function handlePdfDragLeave(event: React.DragEvent<HTMLButtonElement>) {
    const nextTarget = event.relatedTarget;
    if (!(nextTarget instanceof Node) || !event.currentTarget.contains(nextTarget)) {
      setDraggingPdfSlot(null);
    }
  }

  function handlePdfDrop(event: React.DragEvent<HTMLButtonElement>, slot: PdfSlot) {
    event.preventDefault();
    setDraggingPdfSlot(null);
    handlePdfSlotFiles(slot, event.dataTransfer.files);
  }

  function handleTranslateSourceDrop(event: React.DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setDraggingPdfSlot(null);
    handleTranslateSourceFiles(event.dataTransfer.files);
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
    handleFiles(event.dataTransfer.files);
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

  async function cancelCurrentJob() {
    if (!job || !isTaskRunning) {
      return;
    }
    setIsCanceling(true);
    try {
      const canceled = await cancelJob(job.job_id);
      setJob(canceled);
      setTaskIssue(null);
      await refreshHistory();
    } catch (reason) {
      setTaskIssue(apiMessage(reason, "取消任务失败。"));
    } finally {
      setIsCanceling(false);
    }
  }

  async function retryCurrentJob() {
    if (!job || !["failed", "canceled"].includes(job.status)) {
      return;
    }
    setIsRetryingJob(true);
    try {
      const payload = await retryJob(job.job_id);
      setDocId(payload.doc_id);
      await refreshJob(payload.job_id);
      await refreshHistory();
      setTaskIssue(null);
    } catch (reason) {
      setTaskIssue(apiMessage(reason, "重新排队失败。"));
    } finally {
      setIsRetryingJob(false);
    }
  }

  async function continueCurrentJob() {
    if (!job || !["failed", "canceled"].includes(job.status)) {
      return;
    }
    setIsContinuingJob(true);
    try {
      const payload = await continueJob(job.job_id);
      setDocId(payload.doc_id);
      await refreshJob(payload.job_id);
      await refreshHistory();
      setTaskIssue(null);
    } catch (reason) {
      setTaskIssue(apiMessage(reason, "继续处理失败。"));
    } finally {
      setIsContinuingJob(false);
    }
  }

  async function retypesetCurrentJob() {
    if (!job?.doc_id || isTaskRunning) {
      return;
    }
    const scope = parseRetypesetScope(retypesetScopeMode, retypesetPages, retypesetBlocks);
    if (typeof scope === "string") {
      setTaskIssue(scope);
      return;
    }
    setIsRetypesettingJob(true);
    setTaskIssue(null);
    try {
      if (!(await checkHealth())) {
        throw new Error("后端服务不可用，请先启动 API。");
      }
      const payload = await retypesetJob(job.job_id, {
        instruction: retypesetInstruction.trim() || instruction,
        target_lang: targetLang,
        style_intent: styleIntent,
        constraints: isConstraintsOpen ? constraintDraft : undefined,
        scope
      });
      setDocId(payload.doc_id);
      await refreshJob(payload.job_id);
      await refreshHistory();
      setSessionNotice(`已创建重排任务：${job.filename}`);
    } catch (reason) {
      setTaskIssue(apiMessage(reason, "创建重排任务失败。"));
    } finally {
      setIsRetypesettingJob(false);
    }
  }

  function reloadPreview() {
    setPreviewIssue(null);
    setPreviewState("loading");
    setPreviewReloadKey((current) => current + 1);
  }

  function clearLogBuffer() {
    setLogEvents((current) => {
      current.forEach((event) => {
        hiddenLogEventIdsRef.current.add(event.id);
      });
      return [];
    });
    setLogIssue(null);
  }

  async function refreshLogPanel() {
    if (!activeJobId) {
      return;
    }
    setLogState((current) => (current === "idle" ? "loading" : current));
    await refreshJobEvents();
  }

  async function saveRuntimeConfig() {
    if (baseUrlValidation.error) {
      setConfigIssue(baseUrlValidation.error);
      return;
    }
    setIsSavingConfig(true);
    try {
      const config = await updateRuntimeConfig({
        openai_base_url: configDraft.openai_base_url,
        openai_model: configDraft.openai_model,
        openai_api_key: configDraft.openai_api_key || undefined,
        translation_concurrency: configDraft.translation_concurrency,
        translator_max_attempts: configDraft.translator_max_attempts,
        translation_chunk_max_chars: configDraft.translation_chunk_max_chars,
        ocr_provider_order: configDraft.ocr_provider_order,
        ocr_min_confidence: configDraft.ocr_min_confidence,
        ocr_provider_timeout_seconds: configDraft.ocr_provider_timeout_seconds,
        ocr_max_visual_candidates: configDraft.ocr_max_visual_candidates
      });
      setRuntimeConfig(config);
      setConfigDraft((draft) => ({ ...draft, openai_api_key: "" }));
      setConfigIssue(null);
    } catch (reason) {
      setConfigIssue(apiMessage(reason, "保存运行配置失败。"));
    } finally {
      setIsSavingConfig(false);
    }
  }

  async function submit() {
    if (!hasSubmitInput) {
      setUploadIssue({
        kind: "error",
        target: isTranslateOnly || isPdfWorkflow ? "document" : "requirements",
        message:
          isTranslateOnly
            ? "请上传待翻译 PDF 或 Word 文档。"
            : inputMode === "text"
            ? "请输入要排版的文本。"
            : isPdfWorkflow
              ? sourcePdfCopy.missingMessage
              : "请选择输入文件。"
      });
      return;
    }

    if (
      isTranslateOnly &&
      (!contentPdfFile ||
        !isTranslatableDocumentFile(contentPdfFile) ||
        contentPdfFile.size > maxUploadBytes)
    ) {
      handleTranslateSourceFiles(contentPdfFile ? [contentPdfFile] : []);
      return;
    }

    if (
      (inputMode === "image" || inputMode === "docx") &&
      files.some((selectedFile) =>
        (inputMode === "image" ? !isImageFile(selectedFile) : !isDocxFile(selectedFile)) ||
        selectedFile.size > maxUploadBytes
      )
    ) {
      handleFiles(files);
      return;
    }

    if (
      !isTranslateOnly &&
      isPdfWorkflow &&
      (!contentPdfFile ||
        !isPdfFile(contentPdfFile) ||
        contentPdfFile.size > maxUploadBytes ||
        (layoutPdfFile !== null && (!isPdfFile(layoutPdfFile) || layoutPdfFile.size > maxUploadBytes)))
    ) {
      handlePdfSlotFiles("content", contentPdfFile ? [contentPdfFile] : []);
      if (layoutPdfFile) {
        handlePdfSlotFiles("layout", [layoutPdfFile]);
      }
      return;
    }

    setIsUploading(true);
    setUploadIssue(null);
    setTaskIssue(null);
    setSessionNotice(null);
    setJob(null);
    setDocId(null);
    removeSavedTaskSnapshot();

    try {
      if (!(await checkHealth())) {
        throw new Error("后端服务不可用，请先启动 API。");
      }
      const intent = {
        workflow_mode: workflowMode,
        output_kind: effectiveOutputKind,
        style_intent: styleIntent,
        instruction: workflowMode === "translate_only" ? "" : instruction,
        constraints: isConstraintsOpen ? constraintDraft : undefined
      };
      if (isTranslateOnly && contentPdfFile) {
        if (isDocxFile(contentPdfFile)) {
          const payload = await createDocxWorkflow(contentPdfFile, targetLang, intent);
          setDocId(payload.doc_id);
          await refreshJob(payload.job_id);
        } else {
          const payload = await createDocument({
            contentFile: contentPdfFile,
            layoutReferenceFile: null,
            targetLang,
            intent
          });
          setDocId(payload.doc_id);
          await refreshJob(payload.job_id);
        }
      } else if (inputMode === "text") {
        const payload = await createTextWorkflow(textInput, targetLang, intent);
        setDocId(payload.doc_id);
        await refreshJob(payload.job_id);
      } else if (inputMode === "image") {
        const payload = await createImageWorkflow(files[0], targetLang, intent);
        setDocId(payload.doc_id);
        await refreshJob(payload.job_id);
      } else if (inputMode === "docx") {
        const payload = await createDocxWorkflow(files[0], targetLang, intent);
        setDocId(payload.doc_id);
        await refreshJob(payload.job_id);
      } else if (contentPdfFile) {
        const payload = await createDocument({
          contentFile: contentPdfFile,
          layoutReferenceFile: layoutPdfFile,
          targetLang,
          intent
        });
        setDocId(payload.doc_id);
        await refreshJob(payload.job_id);
      } else if (files.length) {
        const payload = await createDocumentsBatch(files, targetLang, intent);
        if (payload.jobs[0]) {
          setDocId(payload.jobs[0].doc_id);
          await refreshJob(payload.jobs[0].job_id);
        }
      }
      await refreshHistory();
    } catch (reason) {
      setTaskIssue(apiMessage(reason, "上传失败。"));
    } finally {
      setIsUploading(false);
    }
  }

  const activeSidebarItem = sidebarItems.find((item) => item.id === activeSection) ?? sidebarItems[0];
  const availableArtifactCount = artifacts.filter((artifact) => artifact.available).length;
  const latestLogEvent = logEvents.at(-1);

  return (
    <main className="shell">
      <section className="workbench-shell" aria-label="Quiet Workbench">
        <aside className="workbench-sidebar" aria-label="Workbench sections">
          <div className="sidebar-brand">
            <span className="app-mark" aria-hidden="true">
              <FileText size={21} strokeWidth={2.1} />
            </span>
            <div>
              <strong>Typesetting</strong>
              <span>Local console</span>
            </div>
          </div>
          <nav className="sidebar-nav" aria-label="Primary workbench navigation">
            {sidebarItems.map((item) => {
              const Icon = item.icon;
              const isActive = activeSection === item.id;
              return (
                <button
                  key={item.id}
                  type="button"
                  className={isActive ? "active" : ""}
                  aria-current={isActive ? "page" : undefined}
                  onClick={() => setActiveSection(item.id)}
                >
                  <Icon size={18} aria-hidden="true" />
                  <span>
                    <strong>{item.label}</strong>
                    <small>{item.detail}</small>
                  </span>
                </button>
              );
            })}
          </nav>
          <div className="sidebar-status" aria-label="Current run summary">
            <HealthBadge healthState={healthState} />
            <span className="sidebar-progress">
              <span>Progress</span>
              <strong>{progressPercent}%</strong>
            </span>
          </div>
        </aside>

        <section className="workbench-main" aria-label="Preview workspace">
          <header className="command-bar">
            <div className="command-title">
              <span>{activeStatusLabel}</span>
              <strong>{activeFilename ?? "New document"}</strong>
              <small>{activeStatusDetail}</small>
            </div>
            <div className="command-meta" aria-label="Current run settings">
              <span>{activeModeLabel}</span>
              <span>{selectedLanguageLabel}</span>
              <span>{outputKindLabel}</span>
              <span>{styleIntentLabel}</span>
            </div>
            <div className="command-actions">
              <button
                className="toolbar-button"
                type="button"
                onClick={() => setActiveSection(activeSection === "developer" ? "translate" : activeSection)}
                aria-label="Open workflow controls"
              >
                <Upload size={18} />
              </button>
              <button
                className="toolbar-button"
                type="button"
                onClick={() => setActiveSection("developer")}
                aria-label="Open developer tools"
              >
                <Activity size={18} />
              </button>
              {previewUrl ? (
                <a className="toolbar-button" href={previewUrl} target="_blank" rel="noreferrer" aria-label="Open preview in a new tab">
                  <ExternalLink size={18} />
                </a>
              ) : null}
            </div>
          </header>

          <section className="preview-panel quiet-preview" aria-label="Document preview">
            {renderEvaluationWarning ? (
              <div className="render-warning" role="status">
                <AlertCircle size={16} />
                <span>{renderEvaluationWarning}</span>
              </div>
            ) : null}
            <div className="preview-frame">
              {isComplete && previewUrl ? (
                <>
                  <iframe
                    className={previewState === "ready" ? "is-ready" : ""}
                    title="PDF typesetting preview"
                    src={previewFrameUrl ?? previewUrl}
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
                    <PreviewOverlay
                      state={previewIssue ? "error" : previewState}
                      issue={previewIssue}
                      previewUrl={previewUrl}
                      downloadUrl={downloadUrl}
                      onRetry={reloadPreview}
                    />
                  ) : null}
                </>
              ) : (
                <EmptyPreview
                  job={job}
                  taskIssue={taskIssue}
                  backendOffline={hasBackendFailure}
                  previewIssue={previewIssue}
                  downloadUrl={downloadUrl}
                  canRetryStatus={Boolean(job && taskIssue && job.status !== "completed" && job.status !== "failed")}
                  canRetryJob={Boolean(job && ["failed", "canceled"].includes(job.status))}
                  isRetryingStatus={isRetryingStatus}
                  isContinuingJob={isContinuingJob}
                  isRetryingJob={isRetryingJob}
                  isCheckingBackend={healthState === "checking"}
                  onRetryStatus={retryStatus}
                  onContinueJob={continueCurrentJob}
                  onRetryJob={retryCurrentJob}
                  onRetryBackend={() => void checkHealth()}
                />
              )}
            </div>
          </section>

          <footer className="checkout-bar" aria-label="Checkout actions">
            <div className="checkout-summary">
              <span>{activeSidebarItem.label}</span>
              <strong>{job ? `${statusCopy[job.status]} · ${progressPercent}%` : "Ready for input"}</strong>
              <ol className="mini-status-strip" aria-label="Run timeline">
                {statuses.map((status) => {
                  const currentIndex = job ? statuses.indexOf(job.status as (typeof statuses)[number]) : -1;
                  const stepIndex = statuses.indexOf(status);
                  const isPassed = currentIndex >= stepIndex && currentIndex !== -1;
                  const isActive = job?.status === status;
                  return (
                    <li key={status} className={isActive ? "active" : isPassed ? "passed" : ""}>
                      <span aria-hidden="true" />
                      <small>{statusCopy[status]}</small>
                    </li>
                  );
                })}
              </ol>
            </div>
            <div className="checkout-actions">
              {downloadUrl && outputReady ? (
                <a className="checkout-action primary-checkout" href={downloadUrl}>
                  <Download size={16} />
                  <span>Download</span>
                </a>
              ) : (
                <button className="checkout-action primary-checkout" type="button" disabled>
                  <Download size={16} />
                  <span>Download</span>
                </button>
              )}
              {previewUrl && outputReady ? (
                <a className="checkout-action" href={previewUrl} target="_blank" rel="noreferrer">
                  <ExternalLink size={16} />
                  <span>Open</span>
                </a>
              ) : (
                <button className="checkout-action" type="button" disabled>
                  <ExternalLink size={16} />
                  <span>Open</span>
                </button>
              )}
              {job && ["failed", "canceled"].includes(job.status) ? (
                <button className="checkout-action" type="button" onClick={() => void retryCurrentJob()} disabled={isRetryingJob}>
                  {isRetryingJob ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
                  <span>{isRetryingJob ? "Queueing" : "Re-run"}</span>
                </button>
              ) : (
                <button className="checkout-action" type="button" onClick={() => setActiveSection(activeSection === "developer" ? "translate" : activeSection)}>
                  <RefreshCw size={16} />
                  <span>Re-run</span>
                </button>
              )}
              <button className="checkout-action" type="button" disabled>
                <ClipboardCheck size={16} />
                <span>Compare</span>
              </button>
              <button className="checkout-action" type="button" disabled>
                <Archive size={16} />
                <span>Export</span>
              </button>
            </div>
          </footer>
        </section>

        <aside className="context-drawer" aria-label={`${activeSidebarItem.label} tools`}>
          <header className="drawer-head">
            <span>{activeSidebarItem.label}</span>
            <strong>{activeSidebarItem.detail}</strong>
          </header>

          <div className="drawer-content">
            {activeSection !== "developer" ? (
              <>
                <section className="tool-section" aria-labelledby="config-heading">
                  <SectionTitle id="config-heading" icon={<FileText size={16} />} title={workflowModeTitle(workflowMode)} />
                  {!isTranslateOnly && isPdfWorkflow ? (
                    <div className="pdf-primary-source">
                      {shouldShowPrimarySourceHeader ? (
                        <div className="pdf-material-head">
                          <div>
                            <FileText size={14} />
                            <strong>{sourcePdfCopy.heading}</strong>
                          </div>
                          <small>{sourcePdfCopy.detail}</small>
                        </div>
                      ) : null}
                      <PdfUploadSlot
                        label={sourcePdfCopy.label}
                        meta={sourcePdfCopy.meta}
                        file={contentPdfFile}
                        required
                        issueKind={uploadIssue?.target === "document" ? uploadIssue.kind : undefined}
                        isDragging={draggingPdfSlot === "content"}
                        inputRef={contentPdfInputRef}
                        onSelect={(files) => handlePdfSlotFiles("content", files)}
                        onClear={() => clearPdfSlot("content")}
                        onDragOver={(event) => handlePdfDragOver(event, "content")}
                        onDragLeave={handlePdfDragLeave}
                        onDrop={(event) => handlePdfDrop(event, "content")}
                      />
                      <div className="upload-footer" id="document-upload-feedback">
                        <InlineNotice
                          issue={uploadIssue?.target === "document" ? uploadIssue : null}
                          fallback={
                            contentPdfFile
                              ? layoutPdfFile
                                ? sourcePdfCopy.readyWithReference
                                : sourcePdfCopy.readyWithoutReference
                              : sourcePdfCopy.missingMessage
                          }
                        />
                      </div>
                    </div>
                  ) : null}
                  {!isTranslateOnly ? (
                    <div className="segmented" role="tablist" aria-label="Input mode">
                      {inputModes.map((mode) => (
                        <button
                          key={mode.value}
                          type="button"
                          role="tab"
                          aria-selected={inputMode === mode.value}
                          className={inputMode === mode.value ? "active" : ""}
                          onClick={() => {
                            setInputMode(mode.value);
                            setFiles([]);
                            setContentPdfFile(null);
                            setLayoutPdfFile(null);
                            setUploadIssue(null);
                            resetFileInput();
                          }}
                        >
                          {mode.label}
                        </button>
                      ))}
                    </div>
                  ) : null}
                  <label className="field">
                    <span>
                      <Globe2 size={16} />
                      {isSourceOnlyOutput ? "文档语言" : "目标语言"}
                    </span>
                    <select name="target_lang" value={targetLang} onChange={(event) => setTargetLang(event.target.value)}>
                      {configuredLanguages.map((lang) => (
                        <option key={lang} value={lang}>
                          {languageLabels.get(lang) ?? lang}
                        </option>
                      ))}
                    </select>
                  </label>
                  {isTranslateOnly ? (
                    <div className="pdf-primary-source translate-source-intake">
                      <PdfUploadSlot
                        label="选择待翻译文件"
                        meta="支持 PDF 或 Word 文档"
                        file={contentPdfFile}
                        required
                        issueKind={uploadIssue?.target === "document" ? uploadIssue.kind : undefined}
                        isDragging={draggingPdfSlot === "content"}
                        inputRef={contentPdfInputRef}
                        inputName="source_file"
                        accept="application/pdf,.pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,.docx"
                        onSelect={(files) => handleTranslateSourceFiles(files)}
                        onClear={() => clearPdfSlot("content")}
                        onDragOver={(event) => handlePdfDragOver(event, "content")}
                        onDragLeave={handlePdfDragLeave}
                        onDrop={handleTranslateSourceDrop}
                      />
                      <div className="upload-footer" id="document-upload-feedback">
                        <InlineNotice
                          issue={uploadIssue?.target === "document" ? uploadIssue : null}
                          fallback={
                            contentPdfFile
                              ? isDocxFile(contentPdfFile)
                                ? "Word 文档已就绪。"
                                : "PDF 已就绪。"
                              : "请上传待翻译 PDF 或 Word 文档。"
                          }
                        />
                      </div>
                    </div>
                  ) : inputMode === "text" ? (
                    <>
                      <textarea
                        className="text-input"
                        value={textInput}
                        onChange={(event) => {
                          setTextInput(event.target.value);
                          setUploadIssue(null);
                        }}
                        aria-label="Text input"
                      />
                      <div className="upload-footer" id="upload-feedback">
                        <InlineNotice
                          issue={uploadIssue?.target !== "document" ? uploadIssue : null}
                          fallback={textInput.trim() ? "文本输入已就绪。" : "等待文本输入。"}
                        />
                      </div>
                    </>
                  ) : isPdfWorkflow ? (
                    <div className="pdf-source-grid supplementary-materials">
                      <div className="pdf-material-group">
                        <div className="pdf-material-head">
                          <div>
                            <SlidersHorizontal size={14} />
                            <strong>{layoutPdfCopy.heading}</strong>
                          </div>
                          <small>{layoutPdfCopy.detail}</small>
                        </div>
                        <PdfUploadSlot
                          label={layoutPdfCopy.label}
                          meta={layoutPdfCopy.meta}
                          file={layoutPdfFile}
                          issueKind={uploadIssue?.target === "requirements" ? uploadIssue.kind : undefined}
                          isDragging={draggingPdfSlot === "layout"}
                          inputRef={layoutPdfInputRef}
                          onSelect={(files) => handlePdfSlotFiles("layout", files)}
                          onClear={() => clearPdfSlot("layout")}
                          onDragOver={(event) => handlePdfDragOver(event, "layout")}
                          onDragLeave={handlePdfDragLeave}
                          onDrop={(event) => handlePdfDrop(event, "layout")}
                        />
                        <div className="upload-footer" id="layout-upload-feedback">
                          <InlineNotice
                            issue={uploadIssue?.target === "requirements" ? uploadIssue : null}
                            fallback={layoutPdfFile ? layoutPdfCopy.selectedMessage : layoutPdfCopy.idleMessage}
                          />
                        </div>
                      </div>
                    </div>
                  ) : (
                    <>
                      <button
                        className={`upload-zone${files.length ? " has-file" : ""}${isDraggingFile ? " is-dragging" : ""}${uploadIssue?.kind === "error" ? " has-error" : ""}`}
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
                          {files.length ? (
                            <>
                              <span className="file-name">
                                {files.length === 1
                                  ? files[0].name
                                  : `${files.length} 个 ${inputMode === "image" ? "图片" : inputMode === "docx" ? "Word" : "PDF"} 文件`}
                              </span>
                              <span className="file-meta">
                                {files.length === 1
                                  ? formatFileSize(files[0].size)
                                  : formatFileSize(files.reduce((total, selectedFile) => total + selectedFile.size, 0))}
                              </span>
                            </>
                          ) : (
                            <>
                              <span className="file-name">
                                {inputMode === "docx" ? "选择或拖入 Word 文档" : "选择或拖入图片"}
                              </span>
                              <span className="file-meta">
                                {inputMode === "docx" ? "支持 .docx，需要 LibreOffice 转换" : "支持 .png .jpg .webp"}
                              </span>
                            </>
                          )}
                        </span>
                        <span className="upload-action">
                          {files.length ? "更换" : "浏览"}
                          <ChevronRight size={16} />
                        </span>
                      </button>
                      <input
                        ref={inputRef}
                        className="hidden-input"
                        type="file"
                        name={inputMode === "docx" ? "docx_file" : "image_file"}
                        accept={
                          inputMode === "docx"
                            ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document,.docx"
                            : "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"
                        }
                        onChange={(event) => handleFiles(event.target.files)}
                      />
                      <div className="upload-footer" id="upload-feedback">
                        <InlineNotice
                          issue={uploadIssue?.target !== "document" ? uploadIssue : null}
                          fallback={inputMode === "docx" ? "等待 Word 文档输入。" : "等待图片输入。"}
                        />
                        {files.length ? (
                          <button className="ghost-button" type="button" onClick={clearRequirementsInput}>
                            <X size={15} />
                            移除输入
                          </button>
                        ) : null}
                      </div>
                    </>
                  )}
                </section>

                {workflowMode !== "translate_only" ? (
                  <section className="tool-section" aria-labelledby="intent-heading">
                    <SectionTitle id="intent-heading" icon={<SlidersHorizontal size={16} />} title="排版说明" />
                    <label className="field">
                      <span>自然语言要求</span>
                      <textarea
                        className="intent-input"
                        name="typesetting_instruction"
                        value={instruction}
                        onChange={(event) => setInstruction(event.target.value)}
                        aria-label="Typesetting instruction"
                      />
                    </label>
                    <label className="field">
                      <span>版式风格</span>
                      <select name="style_intent" value={styleIntent} onChange={(event) => setStyleIntent(event.target.value as StyleIntent)}>
                        {styleIntentOptions.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <ConstraintPanel
                      isOpen={isConstraintsOpen}
                      draft={constraintDraft}
                      onToggle={() => setIsConstraintsOpen((current) => !current)}
                      onChange={setConstraintDraft}
                    />
                  </section>
                ) : null}

                <section className="tool-section task-section" aria-labelledby="task-heading">
                  <SectionTitle id="task-heading" icon={<Clock3 size={16} />} title="执行状态" />
                  <BackendStatus
                    healthState={healthState}
                    issue={healthIssue}
                    onRetry={() => void checkHealth()}
                  />
                  <button className="primary" type="button" onClick={submit} disabled={!canSubmit}>
                    {isUploading ? <Loader2 className="spin" size={18} /> : <Upload size={18} />}
                    <span>{isUploading ? "提交中" : job ? "重新执行" : startButtonLabel(workflowMode)}</span>
                  </button>
                  <JobProgress
                    job={job}
                    isUploading={isUploading}
                    isRetryingStatus={isRetryingStatus}
                    isCanceling={isCanceling}
                    isContinuingJob={isContinuingJob}
                    isRetryingJob={isRetryingJob}
                    issue={taskIssue}
                    idleDetail={idleJobDetail}
                    onRetryStatus={retryStatus}
                    onCancel={cancelCurrentJob}
                    onContinueJob={continueCurrentJob}
                    onRetryJob={retryCurrentJob}
                  />
                </section>
              </>
            ) : null}

            {activeSection === "developer" ? (
              <>
                <section className="tool-section" aria-labelledby="review-heading">
                  <SectionTitle id="review-heading" icon={<ClipboardCheck size={16} />} title="Checkout" />
                  <RunSummary
                    items={[
                      { label: "状态", value: activeStatusLabel },
                      { label: "Artifact", value: `${availableArtifactCount} available` },
                      { label: "输出", value: outputKindLabel },
                      { label: "版式", value: layoutReferenceLabel }
                    ]}
                  />
                  {renderEvaluationWarning ? (
                    <div className="render-warning compact-warning" role="status">
                      <AlertCircle size={16} />
                      <span>{renderEvaluationWarning}</span>
                    </div>
                  ) : null}
                </section>
                <SchemaInspector
                  artifacts={artifacts}
                  issue={artifactIssue}
                  selectedArtifact={selectedArtifact}
                  state={inspectorState}
                  payload={inspectorPayload}
                  payloadIssue={inspectorIssue}
                  isCollapsed={isInspectorCollapsed}
                  onSelect={setSelectedArtifact}
                  onToggleCollapsed={() => setIsInspectorCollapsed((current) => !current)}
                />
                <RetypesetPanel
                  job={job}
                  scopeMode={retypesetScopeMode}
                  pages={retypesetPages}
                  blocks={retypesetBlocks}
                  instruction={retypesetInstruction}
                  isRunning={isTaskRunning}
                  isSubmitting={isRetypesettingJob}
                  healthState={healthState}
                  onScopeModeChange={setRetypesetScopeMode}
                  onPagesChange={setRetypesetPages}
                  onBlocksChange={setRetypesetBlocks}
                  onInstructionChange={setRetypesetInstruction}
                  onSubmit={() => void retypesetCurrentJob()}
                />
              </>
            ) : null}

            {activeSection === "developer" ? (
              <>
                <section className="tool-section" aria-labelledby="runs-heading">
                  <SectionTitle id="runs-heading" icon={<History size={16} />} title="Run history" />
                  <HistoryList
                    jobs={jobHistory}
                    issue={historyIssue}
                    sessionNotice={sessionNotice}
                    activeJobId={job?.job_id ?? null}
                    onRefresh={() => void refreshHistory()}
                    onRestore={(historyJob) => {
                      restoreJobSnapshot(historyJob);
                      setSessionNotice(null);
                      setActiveSection("developer");
                    }}
                    onClearSavedTask={clearSavedTask}
                  />
                </section>
                <section className="tool-section" aria-labelledby="recovery-heading">
                  <SectionTitle id="recovery-heading" icon={<RefreshCw size={16} />} title="Recovery" />
                  <JobProgress
                    job={job}
                    isUploading={isUploading}
                    isRetryingStatus={isRetryingStatus}
                    isCanceling={isCanceling}
                    isContinuingJob={isContinuingJob}
                    isRetryingJob={isRetryingJob}
                    issue={taskIssue}
                    idleDetail={idleJobDetail}
                    onRetryStatus={retryStatus}
                    onCancel={cancelCurrentJob}
                    onContinueJob={continueCurrentJob}
                    onRetryJob={retryCurrentJob}
                  />
                </section>
              </>
            ) : null}

            {activeSection === "developer" ? (
              <>
                <section className="tool-section" aria-labelledby="typesetting-heading">
                  <SectionTitle id="typesetting-heading" icon={<SlidersHorizontal size={16} />} title="Output" />
                  <div className="field-grid">
                    <label className="field">
                      <span>输出类型</span>
                      <select name="output_kind" value={outputKind} onChange={(event) => setOutputKind(event.target.value as OutputKind)}>
                        {outputKindOptions.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field">
                      <span>版式风格</span>
                      <select name="style_intent" value={styleIntent} onChange={(event) => setStyleIntent(event.target.value as StyleIntent)}>
                        {styleIntentOptions.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <label className="field">
                    <span>排版说明</span>
                    <textarea
                      className="intent-input"
                      name="typesetting_instruction"
                      value={instruction}
                      onChange={(event) => setInstruction(event.target.value)}
                      aria-label="Typesetting instruction"
                    />
                  </label>
                  <ConstraintPanel
                    isOpen={isConstraintsOpen}
                    draft={constraintDraft}
                    onToggle={() => setIsConstraintsOpen((current) => !current)}
                    onChange={setConstraintDraft}
                  />
                  <RunSummary
                    items={[
                      { label: isSourceOnlyOutput ? "文档" : "目标", value: selectedLanguageLabel },
                      { label: "输出", value: outputKindLabel },
                      { label: "风格", value: styleIntentLabel },
                      { label: "约束", value: constraintSummary }
                    ]}
                  />
                </section>

                <section className="tool-section" aria-labelledby="runtime-heading">
                  <SectionTitle id="runtime-heading" icon={<Settings2 size={16} />} title="Runtime" />
                  <RuntimeConfigCard
                    config={runtimeConfig}
                    draft={configDraft}
                    issue={configIssue}
                    validation={baseUrlValidation}
                    isSaving={isSavingConfig}
                    onDraftChange={setConfigDraft}
                    onSave={saveRuntimeConfig}
                  />
                </section>
              </>
            ) : null}

            {activeSection === "developer" ? (
              <>
                {latestLogEvent ? (
                  <section className="tool-section" aria-labelledby="latest-event-heading">
                    <SectionTitle id="latest-event-heading" icon={<Activity size={16} />} title="Latest" />
                    <RunSummary
                      items={[
                        { label: "Source", value: logSourceLabel(latestLogEvent.source) },
                        { label: "Phase", value: latestLogEvent.phase },
                        { label: "Event", value: latestLogEvent.title }
                      ]}
                    />
                  </section>
                ) : null}
                <PipelineLogDock
                  job={job}
                  events={logEvents}
                  state={logState}
                  issue={logIssue}
                  healthState={healthState}
                  isCollapsed={isLogCollapsed}
                  autoScroll={isLogAutoScroll}
                  isWaitingForUpdate={isLogWaiting}
                  onToggleCollapsed={() => setIsLogCollapsed((current) => !current)}
                  onToggleAutoScroll={() => setIsLogAutoScroll((current) => !current)}
                  onRefresh={() => void refreshLogPanel()}
                  onClear={clearLogBuffer}
                />
              </>
            ) : null}
          </div>
        </aside>
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

function HealthBadge({ healthState }: { healthState: HealthState }) {
  const isOnline = healthState === "online";
  const isChecking = healthState === "checking";

  return (
    <span className={`metric-pill health-pill${isOnline ? " online" : ""}${healthState === "offline" ? " offline" : ""}`}>
      <span>服务</span>
      <strong>
        {isChecking ? (
          <>
            <Loader2 className="spin" size={13} />
            检查中
          </>
        ) : isOnline ? (
          <>
            <CheckCircle2 size={13} />
            已连接
          </>
        ) : (
          <>
            <XCircle size={13} />
            离线
          </>
        )}
      </strong>
    </span>
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

function PdfUploadSlot({
  label,
  meta,
  file,
  required = false,
  issueKind,
  isDragging,
  inputRef,
  inputName,
  accept = "application/pdf,.pdf",
  onSelect,
  onClear,
  onDragOver,
  onDragLeave,
  onDrop
}: {
  label: string;
  meta: string;
  file: File | null;
  required?: boolean;
  issueKind?: UploadIssue["kind"];
  isDragging: boolean;
  inputRef: React.RefObject<HTMLInputElement | null>;
  inputName?: string;
  accept?: string;
  onSelect: (files: FileList | null) => void;
  onClear: () => void;
  onDragOver: (event: React.DragEvent<HTMLButtonElement>) => void;
  onDragLeave: (event: React.DragEvent<HTMLButtonElement>) => void;
  onDrop: (event: React.DragEvent<HTMLButtonElement>) => void;
}) {
  return (
    <div className="pdf-slot">
      <button
        className={`upload-zone compact${file ? " has-file" : ""}${isDragging ? " is-dragging" : ""}${issueKind === "error" && required && !file ? " has-error" : ""}`}
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        <span className="upload-icon" aria-hidden="true">
          <FileText size={24} />
        </span>
        <span className="upload-main">
          <span className="file-name">{file ? file.name : label}</span>
          <span className="file-meta">{file ? formatFileSize(file.size) : meta}</span>
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
        name={inputName ?? (required ? "content_pdf" : "layout_pdf")}
        accept={accept}
        onChange={(event) => onSelect(event.target.files)}
      />
      {file ? (
        <button className="ghost-button slot-clear" type="button" onClick={onClear}>
          <X size={14} />
          移除
        </button>
      ) : null}
    </div>
  );
}

function ConstraintPanel({
  isOpen,
  draft,
  onToggle,
  onChange
}: {
  isOpen: boolean;
  draft: ConstraintDraft;
  onToggle: () => void;
  onChange: React.Dispatch<React.SetStateAction<ConstraintDraft>>;
}) {
  return (
    <div className="constraint-panel">
      <button className="constraint-toggle" type="button" onClick={onToggle} aria-expanded={isOpen}>
        <span>自定义强约束</span>
        <ChevronRight className={isOpen ? "is-open" : ""} size={16} />
      </button>
      {isOpen ? (
        <div className="constraint-body">
          <div className="config-numbers">
            <label>
              <span>页宽 pt</span>
              <input
                name="page_width_pt"
                type="number"
                min={240}
                max={2000}
                value={draft.page_width_pt}
                onChange={(event) =>
                  onChange((current) => ({
                    ...current,
                    page_width_pt: clampNumber(event.target.value, 240, 2000)
                  }))
                }
              />
            </label>
            <label>
              <span>页高 pt</span>
              <input
                name="page_height_pt"
                type="number"
                min={240}
                max={3000}
                value={draft.page_height_pt}
                onChange={(event) =>
                  onChange((current) => ({
                    ...current,
                    page_height_pt: clampNumber(event.target.value, 240, 3000)
                  }))
                }
              />
            </label>
            <label>
              <span>字号 pt</span>
              <input
                name="target_font_size_pt"
                type="number"
                min={6}
                max={32}
                step={0.5}
                value={draft.target_font_size_pt}
                onChange={(event) =>
                  onChange((current) => ({
                    ...current,
                    target_font_size_pt: clampNumber(event.target.value, 6, 32)
                  }))
                }
              />
            </label>
          </div>
          <label className="toggle-field">
            <input
              name="allow_continuation"
              type="checkbox"
              checked={draft.allow_continuation}
              onChange={(event) =>
                onChange((current) => ({ ...current, allow_continuation: event.target.checked }))
              }
            />
            <span>允许续页</span>
          </label>
          <label className="toggle-field">
            <input
              name="preserve_images"
              type="checkbox"
              checked={draft.preserve_images}
              onChange={(event) =>
                onChange((current) => ({ ...current, preserve_images: event.target.checked }))
              }
            />
            <span>保留图片资产</span>
          </label>
        </div>
      ) : null}
    </div>
  );
}

function InlineNotice({
  issue,
  fallback = "等待 PDF 文件。"
}: {
  issue: UploadIssue | null;
  fallback?: string;
}) {
  if (!issue) {
    return <span className="hint">{fallback}</span>;
  }

  return (
    <span className={`inline-notice ${issue.kind}`}>
      {issue.kind === "error" ? <AlertCircle size={15} /> : <CheckCircle2 size={15} />}
      {issue.message}
    </span>
  );
}

function RunSummary({ items }: { items: Array<{ label: string; value: string }> }) {
  return (
    <dl className="run-summary" aria-label="Run summary">
      {items.map((item) => (
        <div className="run-chip" key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function RuntimeConfigCard({
  config,
  draft,
  issue,
  validation,
  isSaving,
  onDraftChange,
  onSave
}: {
  config: RuntimeConfig | null;
  draft: {
    openai_base_url: string;
    openai_model: string;
    openai_api_key: string;
    translation_concurrency: number;
    translator_max_attempts: number;
    translation_chunk_max_chars: number;
    ocr_provider_order: string[];
    ocr_min_confidence: number;
    ocr_provider_timeout_seconds: number;
    ocr_max_visual_candidates: number;
  };
  issue: string | null;
  validation: BaseUrlValidation;
  isSaving: boolean;
  onDraftChange: React.Dispatch<React.SetStateAction<{
    openai_base_url: string;
    openai_model: string;
    openai_api_key: string;
    translation_concurrency: number;
    translator_max_attempts: number;
    translation_chunk_max_chars: number;
    ocr_provider_order: string[];
    ocr_min_confidence: number;
    ocr_provider_timeout_seconds: number;
    ocr_max_visual_candidates: number;
  }>>;
  onSave: () => void;
}) {
  const provider = config?.translator_provider ?? "deterministic";
  const isConfigured = config?.openai_api_key_configured ?? false;
  const baseUrlMessage = validation.error ?? validation.warning;
  const updateOcrProvider = (providerName: string, enabled: boolean) => {
    onDraftChange((current) => {
      const withoutProvider = current.ocr_provider_order.filter((item) => item !== providerName);
      const nextOrder = enabled ? [providerName, ...withoutProvider] : withoutProvider;
      const withDeterministic = nextOrder.includes("deterministic")
        ? nextOrder
        : [...nextOrder, "deterministic"];
      return { ...current, ocr_provider_order: withDeterministic };
    });
  };
  return (
    <div
      className={`model-slot config-editor${issue || validation.error ? " warning" : ""}`}
      aria-label="Model configuration"
    >
      <div className="config-summary">
        <div>
          <span className="model-slot-label">模型配置</span>
          <strong>{issue ? issue : provider === "deterministic" ? "Deterministic 本地模式" : config?.openai_model}</strong>
          <small>
            {config
              ? `${config.openai_base_url} · ${formatFileSize(config.max_upload_bytes)} · 并发 ${config.translation_concurrency} · Chunk ${config.translation_chunk_max_chars} · OCR ${config.ocr_provider_order.join(">")} · ${config.ocr_provider_timeout_seconds}s/${config.ocr_max_visual_candidates}`
              : "读取后端配置中"}
          </small>
        </div>
        <span className={`slot-badge${isConfigured ? " active" : ""}`}>
          {isConfigured ? "已配置" : "本地"}
        </span>
      </div>
      <label>
        <span>Base URL</span>
        <input
          name="openai_base_url"
          value={draft.openai_base_url}
          onChange={(event) =>
            onDraftChange((current) => ({ ...current, openai_base_url: event.target.value }))
          }
        />
        {baseUrlMessage ? (
          <small className={`field-note ${validation.error ? "error" : "warning"}`}>
            {baseUrlMessage}
          </small>
        ) : null}
      </label>
      <label>
        <span>Model</span>
        <input
          name="openai_model"
          value={draft.openai_model}
          onChange={(event) =>
            onDraftChange((current) => ({ ...current, openai_model: event.target.value }))
          }
        />
      </label>
      <label>
        <span>API Key</span>
        <input
          name="openai_api_key"
          type="password"
          value={draft.openai_api_key}
          placeholder={isConfigured ? "留空保持不变" : ""}
          onChange={(event) =>
            onDraftChange((current) => ({ ...current, openai_api_key: event.target.value }))
          }
        />
      </label>
      <div className="config-numbers">
        <label>
          <span>并发</span>
          <input
            name="translation_concurrency"
            type="number"
            min={1}
            max={16}
            value={draft.translation_concurrency}
            onChange={(event) =>
              onDraftChange((current) => ({
                ...current,
                translation_concurrency: clampInt(event.target.value, 1, 16)
              }))
            }
          />
        </label>
        <label>
          <span>尝试</span>
          <input
            name="translator_max_attempts"
            type="number"
            min={1}
            max={5}
            value={draft.translator_max_attempts}
            onChange={(event) =>
              onDraftChange((current) => ({
                ...current,
                translator_max_attempts: clampInt(event.target.value, 1, 5)
              }))
            }
          />
        </label>
        <label>
          <span>Chunk</span>
          <input
            name="translation_chunk_max_chars"
            type="number"
            min={500}
            max={12000}
            value={draft.translation_chunk_max_chars}
            onChange={(event) =>
              onDraftChange((current) => ({
                ...current,
                translation_chunk_max_chars: clampInt(event.target.value, 500, 12000)
              }))
            }
          />
        </label>
      </div>
      <div className="ocr-provider-panel" aria-label="Formula OCR provider order">
        <span>公式 OCR</span>
        <label className="toggle-field">
          <input
            name="ocr_provider_minimax_vision"
            type="checkbox"
            checked={draft.ocr_provider_order.includes("minimax_vision")}
            onChange={(event) => updateOcrProvider("minimax_vision", event.target.checked)}
          />
          <span>MiniMax 视觉识别</span>
        </label>
        <label className="toggle-field">
          <input
            name="ocr_provider_openai_vision"
            type="checkbox"
            checked={draft.ocr_provider_order.includes("openai_vision")}
            onChange={(event) => updateOcrProvider("openai_vision", event.target.checked)}
          />
          <span>OpenAI 视觉识别</span>
        </label>
        <small>仅在启用对应 provider 时，低置信度公式才会发起视觉模型请求。</small>
      </div>
      <div className="config-numbers">
        <label>
          <span>OCR 秒</span>
          <input
            name="ocr_provider_timeout_seconds"
            type="number"
            min={1}
            max={120}
            value={draft.ocr_provider_timeout_seconds}
            onChange={(event) =>
              onDraftChange((current) => ({
                ...current,
                ocr_provider_timeout_seconds: clampInt(event.target.value, 1, 120)
              }))
            }
          />
        </label>
        <label>
          <span>视觉数</span>
          <input
            name="ocr_max_visual_candidates"
            type="number"
            min={0}
            max={200}
            value={draft.ocr_max_visual_candidates}
            onChange={(event) =>
              onDraftChange((current) => ({
                ...current,
                ocr_max_visual_candidates: clampInt(event.target.value, 0, 200)
              }))
            }
          />
        </label>
      </div>
      <button
        className="secondary-action"
        type="button"
        onClick={onSave}
        disabled={isSaving || !config || Boolean(validation.error)}
      >
        {isSaving ? <Loader2 className="spin" size={15} /> : <Settings2 size={15} />}
        <span>{isSaving ? "保存中" : "保存配置"}</span>
      </button>
    </div>
  );
}

function HistoryList({
  jobs,
  issue,
  sessionNotice,
  activeJobId,
  onRefresh,
  onRestore,
  onClearSavedTask
}: {
  jobs: JobStatus[];
  issue: string | null;
  sessionNotice: string | null;
  activeJobId: string | null;
  onRefresh: () => void;
  onRestore: (job: JobStatus) => void;
  onClearSavedTask: () => void;
}) {
  return (
    <div className="history-box">
      <div className="history-head">
        <span>{jobs.length ? `${jobs.length} 个任务` : "暂无任务"}</span>
        <button type="button" onClick={onRefresh} aria-label="Refresh job history">
          <RefreshCw size={14} />
        </button>
      </div>
      {issue ? <p className="history-issue">{issue}</p> : null}
      {sessionNotice ? (
        <div className="session-notice">
          <span>{sessionNotice}</span>
          <button type="button" onClick={onClearSavedTask}>
            清除
          </button>
        </div>
      ) : null}
      {jobs.length ? (
        <ul className="history-list">
          {jobs.slice(0, 5).map((historyJob) => (
            <li key={historyJob.job_id}>
              <button
                type="button"
                className={historyJob.job_id === activeJobId ? "active" : ""}
                onClick={() => onRestore(historyJob)}
              >
                <span>{historyJob.filename}</span>
                <small>{statusCopy[historyJob.status]} · {Math.round(normalizeProgress(historyJob.progress) * 100)}%</small>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function RetypesetPanel({
  job,
  scopeMode,
  pages,
  blocks,
  instruction,
  isRunning,
  isSubmitting,
  healthState,
  onScopeModeChange,
  onPagesChange,
  onBlocksChange,
  onInstructionChange,
  onSubmit
}: {
  job: JobStatus | null;
  scopeMode: NonNullable<EditScope["mode"]>;
  pages: string;
  blocks: string;
  instruction: string;
  isRunning: boolean;
  isSubmitting: boolean;
  healthState: HealthState;
  onScopeModeChange: (mode: NonNullable<EditScope["mode"]>) => void;
  onPagesChange: (value: string) => void;
  onBlocksChange: (value: string) => void;
  onInstructionChange: (value: string) => void;
  onSubmit: () => void;
}) {
  const disabled = !job?.doc_id || isRunning || isSubmitting || healthState !== "online";
  return (
    <div className="retypeset-box">
      <div className="retypeset-head">
        <span>历史重排</span>
        <small>{job?.doc_id ? job.filename : "选择一个历史任务"}</small>
      </div>
      <textarea
        className="intent-input compact"
        name="retypeset_instruction"
        value={instruction}
        onChange={(event) => onInstructionChange(event.target.value)}
        aria-label="Retypeset instruction"
      />
      <div className="segmented compact" role="tablist" aria-label="Retypeset scope">
        {(["all", "pages", "blocks"] as const).map((mode) => (
          <button
            key={mode}
            type="button"
            role="tab"
            aria-selected={scopeMode === mode}
            className={scopeMode === mode ? "active" : ""}
            onClick={() => onScopeModeChange(mode)}
          >
            {mode === "all" ? "全部" : mode === "pages" ? "页码" : "Blocks"}
          </button>
        ))}
      </div>
      {scopeMode === "pages" ? (
        <label className="field">
          <span>页码</span>
          <input
            name="retypeset_pages"
            value={pages}
            onChange={(event) => onPagesChange(event.target.value)}
            placeholder="1, 2, 5"
          />
        </label>
      ) : null}
      {scopeMode === "blocks" ? (
        <label className="field">
          <span>Block IDs</span>
          <input
            name="retypeset_blocks"
            value={blocks}
            onChange={(event) => onBlocksChange(event.target.value)}
            placeholder="p0001_b... p0002_b..."
          />
        </label>
      ) : null}
      <button className="secondary-action full-width" type="button" onClick={onSubmit} disabled={disabled}>
        {isSubmitting ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
        <span>{isSubmitting ? "创建中" : "用历史 artifact 重排"}</span>
      </button>
    </div>
  );
}

function JobProgress({
  job,
  isUploading,
  isRetryingStatus,
  isCanceling,
  isContinuingJob,
  isRetryingJob,
  issue,
  idleDetail,
  onRetryStatus,
  onCancel,
  onContinueJob,
  onRetryJob
}: {
  job: JobStatus | null;
  isUploading: boolean;
  isRetryingStatus: boolean;
  isCanceling: boolean;
  isContinuingJob: boolean;
  isRetryingJob: boolean;
  issue: string | null;
  idleDetail: string;
  onRetryStatus: () => void;
  onCancel: () => void;
  onContinueJob: () => void;
  onRetryJob: () => void;
}) {
  const status = isUploading ? "queued" : job?.status;
  const progress = normalizeProgress(isUploading ? 0.04 : job?.progress ?? 0);
  const currentIndex = status ? statuses.indexOf(status) : -1;
  const visibleMessage = issue ?? job?.error ?? job?.message ?? (isUploading ? "正在提交文件。" : null);
  const isFailed = status === "failed" || Boolean(issue && !job);
  const isCanceled = status === "canceled";
  const hasWarning = Boolean(issue && job && status !== "failed" && status !== "canceled");
  const isDone = status === "completed";
  const canRetryStatus = Boolean(job && issue && status !== "completed" && status !== "failed");
  const canCancel = Boolean(job && ["queued", "parsing", "translating", "rendering"].includes(job.status));
  const canContinueJob = Boolean(job && ["failed", "canceled"].includes(job.status));

  return (
    <div className={`job-card${isFailed || isCanceled ? " failed" : ""}${hasWarning ? " warning" : ""}${isDone ? " completed" : ""}`} aria-live="polite">
      <div className="job-card-head">
        <div className="status-icon" aria-hidden="true">
          {isDone ? (
            <CheckCircle2 size={18} />
          ) : isFailed || isCanceled ? (
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
          <strong>{status ? statusDetail[status] : idleDetail}</strong>
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
        {status === "canceled" ? (
          <li className="active failed-step">
            <span aria-hidden="true" />
            <small>取消</small>
          </li>
        ) : null}
      </ol>

      {job?.chunks?.length ? <ChunkProgressList chunks={job.chunks} /> : null}

      {visibleMessage ? <p className="job-message">{visibleMessage}</p> : null}
      {canCancel ? (
        <button className="secondary-action danger" type="button" onClick={onCancel} disabled={isCanceling}>
          {isCanceling ? <Loader2 className="spin" size={15} /> : <XCircle size={15} />}
          <span>{isCanceling ? "取消中" : "取消任务"}</span>
        </button>
      ) : null}
      {canContinueJob ? (
        <button className="secondary-action continue-action" type="button" onClick={onContinueJob} disabled={isContinuingJob}>
          {isContinuingJob ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
          <span>{isContinuingJob ? "继续中" : "继续处理"}</span>
        </button>
      ) : null}
      {canContinueJob ? (
        <button className="secondary-action" type="button" onClick={onRetryJob} disabled={isRetryingJob}>
          {isRetryingJob ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
          <span>{isRetryingJob ? "排队中" : "重新开始"}</span>
        </button>
      ) : null}
      {canRetryStatus ? (
        <button className="secondary-action" type="button" onClick={onRetryStatus} disabled={isRetryingStatus}>
          {isRetryingStatus ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
          <span>{isRetryingStatus ? "重试中" : "重试读取状态"}</span>
        </button>
      ) : null}
    </div>
  );
}

function ChunkProgressList({ chunks }: { chunks: NonNullable<JobStatus["chunks"]> }) {
  return (
    <ul className="chunk-list" aria-label="Chunk progress">
      {chunks.map((chunk) => (
        <li key={chunk.chunk_id} className={chunk.status}>
          <div>
            <span>{chunk.index}/{chunk.total}</span>
            <strong>{chunk.status}</strong>
          </div>
          <div
            className="chunk-meter"
            role="progressbar"
            aria-label={`${chunk.chunk_id} progress`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(normalizeProgress(chunk.progress) * 100)}
          >
            <span style={{ width: `${Math.round(normalizeProgress(chunk.progress) * 100)}%` }} />
          </div>
          {chunk.quality_flags?.length ? (
            <small>{chunk.quality_flags.slice(0, 3).join(", ")}</small>
          ) : chunk.error ? (
            <small>{chunk.error}</small>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function PipelineLogDock({
  job,
  events,
  state,
  issue,
  healthState,
  isCollapsed,
  autoScroll,
  isWaitingForUpdate,
  onToggleCollapsed,
  onToggleAutoScroll,
  onRefresh,
  onClear
}: {
  job: JobStatus | null;
  events: JobLogEvent[];
  state: LogState;
  issue: string | null;
  healthState: HealthState;
  isCollapsed: boolean;
  autoScroll: boolean;
  isWaitingForUpdate: boolean;
  onToggleCollapsed: () => void;
  onToggleAutoScroll: () => void;
  onRefresh: () => void;
  onClear: () => void;
}) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const latestEvent = events.at(-1);
  const hasJob = Boolean(job);
  const isOffline = healthState === "offline";
  const terminalStatus = job ? ["completed", "failed", "canceled"].includes(job.status) : false;

  useEffect(() => {
    if (!autoScroll || isCollapsed || !bodyRef.current) {
      return;
    }
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [autoScroll, events.length, isCollapsed, isWaitingForUpdate]);

  return (
    <aside className={`pipeline-log-dock${isCollapsed ? " is-collapsed" : ""}`} aria-label="Live pipeline events">
      <div className="pipeline-log-head">
        <div className="pipeline-log-title">
          <Clock3 size={16} />
          <div>
            <span>Live pipeline</span>
            <strong>{job ? `${statusCopy[job.status]} · ${Math.round(normalizeProgress(job.progress) * 100)}%` : "等待任务"}</strong>
          </div>
        </div>
        <div className="pipeline-log-summary">
          <span>{events.length ? `${events.length} events` : latestEvent?.message ?? "No events"}</span>
          {terminalStatus && job ? <span>{statusCopy[job.status]}</span> : null}
        </div>
        <div className="pipeline-log-actions">
          <button
            className={`log-text-button${autoScroll ? " active" : ""}`}
            type="button"
            onClick={onToggleAutoScroll}
            aria-pressed={autoScroll}
          >
            Auto-scroll
          </button>
          <button className="log-icon-button" type="button" onClick={onRefresh} disabled={!hasJob} aria-label="Refresh pipeline events">
            <RefreshCw size={15} />
          </button>
          <button className="log-icon-button" type="button" onClick={onClear} disabled={!events.length} aria-label="Clear visible pipeline events">
            <X size={15} />
          </button>
          <button
            className="log-icon-button"
            type="button"
            onClick={onToggleCollapsed}
            aria-label={isCollapsed ? "Expand pipeline events" : "Collapse pipeline events"}
            aria-expanded={!isCollapsed}
          >
            <ChevronRight className={isCollapsed ? "" : "is-open"} size={16} />
          </button>
        </div>
      </div>
      {isCollapsed ? null : (
        <div className="pipeline-log-body" ref={bodyRef} aria-live="polite">
          {!job ? (
            <p className="pipeline-log-empty">任务开始后会显示解析、翻译、渲染和 artifact 更新。</p>
          ) : (
            <>
              {isOffline ? (
                <p className="pipeline-log-banner warning">后端离线，pipeline events 同步暂停。</p>
              ) : null}
              {state === "loading" && !events.length ? (
                <p className="pipeline-log-empty">
                  <Loader2 className="spin" size={15} />
                  读取 pipeline events 中。
                </p>
              ) : null}
              {issue ? <p className="pipeline-log-banner error">{issue}</p> : null}
              {events.length ? (
                <ol className="pipeline-event-list">
                  {events.map((event) => (
                    <li className={`pipeline-event ${event.level}`} key={event.id}>
                      <span className="event-dot" aria-hidden="true" />
                      <div className="event-main">
                        <div className="event-line">
                          <span>{logSourceLabel(event.source)}</span>
                          <strong>{event.title}</strong>
                          <small>{formatLogProgress(event.progress)}</small>
                        </div>
                        <p>{event.message}</p>
                        {event.details.length ? (
                          <ul className="event-details">
                            {event.details.map((detail) => (
                              <li key={detail}>{detail}</li>
                            ))}
                          </ul>
                        ) : null}
                      </div>
                    </li>
                  ))}
                </ol>
              ) : state !== "loading" ? (
                <p className="pipeline-log-empty">暂无可显示的 pipeline event。</p>
              ) : null}
              {isWaitingForUpdate ? (
                <p className="pipeline-log-banner waiting">
                  <Loader2 className="spin" size={15} />
                  后端仍在处理，等待下一条 pipeline event。
                </p>
              ) : null}
            </>
          )}
        </div>
      )}
    </aside>
  );
}

function SchemaInspector({
  artifacts,
  issue,
  selectedArtifact,
  state,
  payload,
  payloadIssue,
  isCollapsed,
  onSelect,
  onToggleCollapsed
}: {
  artifacts: ArtifactSummary[];
  issue: string | null;
  selectedArtifact: string;
  state: InspectorState;
  payload: string;
  payloadIssue: string | null;
  isCollapsed: boolean;
  onSelect: (artifactName: string) => void;
  onToggleCollapsed: () => void;
}) {
  const availableArtifacts = artifacts.filter((artifact) => artifact.available);
  return (
    <aside className={`schema-inspector${isCollapsed ? " is-collapsed" : ""}`} aria-label="Schema inspector">
      <div className="inspector-head">
        <div>
          <span>Inspector</span>
          <strong>Schema 与诊断</strong>
        </div>
        <div className="inspector-actions">
          <Search size={18} aria-hidden="true" />
          <button
            className="inspector-icon-button"
            type="button"
            onClick={onToggleCollapsed}
            aria-label={isCollapsed ? "Expand schema inspector" : "Collapse schema inspector"}
            aria-expanded={!isCollapsed}
          >
            <ChevronRight className={isCollapsed ? "" : "is-open"} size={16} />
          </button>
        </div>
      </div>
      {isCollapsed ? null : (
        <div className="inspector-content" id="schema-inspector-content">
          <div className="artifact-tabs" role="tablist" aria-label="Debug artifacts">
            {artifacts.map((artifact) => (
              <button
                key={artifact.name}
                type="button"
                role="tab"
                aria-selected={artifact.name === selectedArtifact}
                disabled={!artifact.available}
                className={artifact.name === selectedArtifact ? "active" : ""}
                onClick={() => onSelect(artifact.name)}
              >
                {artifactLabel(artifact.name)}
              </button>
            ))}
          </div>
          <div className="artifact-body">
            {issue ? (
              <p className="artifact-message failed">{issue}</p>
            ) : !availableArtifacts.length ? (
              <p className="artifact-message">任务完成后显示 DocumentIR、chunks、plans 和 renderer diagnostics。</p>
            ) : state === "loading" ? (
              <p className="artifact-message">读取 artifact 中。</p>
            ) : state === "error" ? (
              <p className="artifact-message failed">{payloadIssue ?? "读取 artifact 失败。"}</p>
            ) : payload ? (
              <pre>{payload}</pre>
            ) : (
              <p className="artifact-message">选择可用 artifact。</p>
            )}
          </div>
        </div>
      )}
    </aside>
  );
}

function PreviewOverlay({
  state,
  issue,
  previewUrl,
  downloadUrl,
  onRetry
}: {
  state: PreviewState;
  issue: string | null;
  previewUrl: string | null;
  downloadUrl: string | null;
  onRetry: () => void;
}) {
  const isError = state === "error";

  return (
    <div className={`preview-overlay${isError ? " failed" : ""}`} role="status">
      {isError ? <XCircle size={30} /> : <Loader2 className="spin" size={30} />}
      <span>{isError ? issue ?? "预览加载失败" : "正在加载预览"}</span>
      {isError ? (
        <div className="overlay-actions">
          <button className="secondary-action" type="button" onClick={onRetry}>
            <RefreshCw size={15} />
            <span>重载预览</span>
          </button>
          {previewUrl ? (
            <a className="secondary-action" href={previewUrl} target="_blank" rel="noreferrer">
              <ExternalLink size={15} />
              <span>打开 HTML</span>
            </a>
          ) : null}
          {downloadUrl ? (
            <a className="secondary-action" href={downloadUrl}>
              <Download size={15} />
              <span>下载 PDF</span>
            </a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function EmptyPreview({
  job,
  taskIssue,
  backendOffline,
  previewIssue,
  downloadUrl,
  canRetryStatus,
  canRetryJob,
  isRetryingStatus,
  isContinuingJob,
  isRetryingJob,
  isCheckingBackend,
  onRetryStatus,
  onContinueJob,
  onRetryJob,
  onRetryBackend
}: {
  job: JobStatus | null;
  taskIssue: string | null;
  backendOffline: boolean;
  previewIssue: string | null;
  downloadUrl: string | null;
  canRetryStatus: boolean;
  canRetryJob: boolean;
  isRetryingStatus: boolean;
  isContinuingJob: boolean;
  isRetryingJob: boolean;
  isCheckingBackend: boolean;
  onRetryStatus: () => void;
  onContinueJob: () => void;
  onRetryJob: () => void;
  onRetryBackend: () => void;
}) {
  const hasFailure = job?.status === "failed" || Boolean(taskIssue) || backendOffline || Boolean(previewIssue);
  const message = backendOffline
    ? "后端服务不可用，请确认 API 已启动。"
    : previewIssue ?? taskIssue ?? job?.error ?? job?.message ?? "检查任务状态后重试。";

  return (
    <div className={`empty-preview${hasFailure ? " failed" : ""}`}>
      {hasFailure ? <XCircle size={42} /> : <FileText size={42} />}
      <h2>{hasFailure ? "无法生成预览" : "预览将在完成后显示"}</h2>
      <p>{hasFailure ? message : "任务完成后显示排版预览。"}</p>
      {hasFailure ? (
        <div className="overlay-actions">
          {backendOffline ? (
            <button className="secondary-action" type="button" onClick={onRetryBackend} disabled={isCheckingBackend}>
              {isCheckingBackend ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
              <span>{isCheckingBackend ? "检查中" : "检查后端"}</span>
            </button>
          ) : null}
          {canRetryJob ? (
            <button className="secondary-action continue-action" type="button" onClick={onContinueJob} disabled={isContinuingJob}>
              {isContinuingJob ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
              <span>{isContinuingJob ? "继续中" : "继续处理"}</span>
            </button>
          ) : null}
          {canRetryJob ? (
            <button className="secondary-action" type="button" onClick={onRetryJob} disabled={isRetryingJob}>
              {isRetryingJob ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
              <span>{isRetryingJob ? "排队中" : "重新开始"}</span>
            </button>
          ) : null}
          {canRetryStatus ? (
            <button className="secondary-action" type="button" onClick={onRetryStatus} disabled={isRetryingStatus}>
              {isRetryingStatus ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
              <span>{isRetryingStatus ? "重试中" : "重试状态"}</span>
            </button>
          ) : null}
          {downloadUrl ? (
            <a className="secondary-action" href={downloadUrl}>
              <Download size={15} />
              <span>下载 PDF</span>
            </a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function isPdfFile(file: File) {
  return file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
}

function isTranslatableDocumentFile(file: File) {
  return isPdfFile(file) || isDocxFile(file);
}

function isDocxFile(file: File) {
  const hasDocxName = file.name.toLowerCase().endsWith(".docx");
  return (
    file.type === "application/vnd.openxmlformats-officedocument.wordprocessingml.document" ||
    (file.type === "application/octet-stream" && hasDocxName) ||
    hasDocxName
  );
}

function isImageFile(file: File) {
  const name = file.name.toLowerCase();
  return (
    ["image/png", "image/jpeg", "image/webp"].includes(file.type) ||
    name.endsWith(".png") ||
    name.endsWith(".jpg") ||
    name.endsWith(".jpeg") ||
    name.endsWith(".webp")
  );
}

function workflowModeForSection(section: ActiveSection): WorkflowMode {
  switch (section) {
    case "translate":
      return "translate_only";
    case "typeset":
      return "typeset_only";
    case "combined":
      return "translate_and_typeset";
    case "developer":
      return "translate_and_typeset";
    default:
      return "translate_and_typeset";
  }
}

function outputKindForWorkflowMode(mode: WorkflowMode): OutputKind {
  return mode === "typeset_only" ? "typeset_document" : "translation";
}

function workflowModeLabel(mode: WorkflowMode) {
  switch (mode) {
    case "translate_only":
      return "仅翻译";
    case "typeset_only":
      return "仅排版";
    case "translate_and_typeset":
      return "翻译并排版";
    default:
      return mode;
  }
}

function workflowModeTitle(mode: WorkflowMode) {
  switch (mode) {
    case "translate_only":
      return "翻译输入";
    case "typeset_only":
      return "智能排版输入";
    case "translate_and_typeset":
      return "翻译 + 排版材料";
    default:
      return "文档输入";
  }
}

function sourcePdfCopyForWorkflow(mode: WorkflowMode) {
  switch (mode) {
    case "typeset_only":
      return {
        heading: "待排版 PDF",
        detail: "必填 · 原文内容与阅读顺序来源",
        label: "选择待排版 PDF",
        meta: "必填，作为排版内容来源",
        selectedMessage: "已选择待排版 PDF，可以开始排版。",
        missingMessage: "请选择待排版 PDF。",
        readyWithReference: "内容 PDF 与排版参考已就绪。",
        readyWithoutReference: "内容 PDF 已就绪，排版参考可选。"
      };
    case "translate_and_typeset":
      return {
        heading: "待翻译 PDF",
        detail: "必填 · 进入翻译并保留阅读顺序",
        label: "选择待翻译 PDF",
        meta: "必填，作为翻译内容来源",
        selectedMessage: "已选择待翻译 PDF，可以补充排版素材或直接开始。",
        missingMessage: "请选择待翻译 PDF。",
        readyWithReference: "待翻译 PDF 与排版素材已就绪。",
        readyWithoutReference: "待翻译 PDF 已就绪，排版素材可选。"
      };
    default:
      return {
        heading: "待翻译 PDF",
        detail: "必填 · 进入翻译流水线",
        label: "选择待翻译 PDF",
        meta: "必填，作为翻译内容来源",
        selectedMessage: "已选择待翻译 PDF，可以开始翻译。",
        missingMessage: "请选择待翻译 PDF。",
        readyWithReference: "待翻译 PDF 与参考 PDF 已就绪。",
        readyWithoutReference: "待翻译 PDF 已就绪，参考 PDF 可选。"
      };
  }
}

function layoutPdfCopyForWorkflow(mode: WorkflowMode) {
  switch (mode) {
    case "translate_and_typeset":
      return {
        heading: "排版素材 PDF",
        detail: "可选 · 只作为版式材料，不替代待翻译 PDF",
        label: "选择排版素材 PDF",
        meta: "可选，用来指导版式与结构",
        selectedMessage: "已选择排版素材 PDF，后端会作为版式参考。",
        idleMessage: "排版素材可选，不会替代待翻译 PDF。"
      };
    case "typeset_only":
      return {
        heading: "排版参考 PDF",
        detail: "可选 · 作为版式参考，不替代内容 PDF",
        label: "选择排版参考 PDF",
        meta: "可选，用来指导版式与结构",
        selectedMessage: "已选择排版参考 PDF，后端会作为版式参考。",
        idleMessage: "排版参考可选，不会替代内容 PDF。"
      };
    default:
      return {
        heading: "参考 PDF",
        detail: "可选 · 为当前处理提供版式参考",
        label: "选择参考 PDF",
        meta: "可选，用来补充版式参考",
        selectedMessage: "已选择参考 PDF，后端会作为参考材料。",
        idleMessage: "参考 PDF 可选，不会替代待翻译 PDF。"
      };
  }
}

function startButtonLabel(mode: WorkflowMode) {
  switch (mode) {
    case "translate_only":
      return "开始翻译";
    case "typeset_only":
      return "开始排版";
    case "translate_and_typeset":
      return "翻译并排版";
    default:
      return "开始处理";
  }
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

function mergeLogEvents(current: JobLogEvent[], incoming: JobLogEvent[]) {
  const byId = new Map(current.map((event) => [event.id, event]));
  for (const event of incoming) {
    byId.set(event.id, event);
  }
  return Array.from(byId.values())
    .sort((left, right) => left.sequence - right.sequence || left.id.localeCompare(right.id))
    .slice(-200);
}

function logSourceLabel(source: JobLogEvent["source"]) {
  switch (source) {
    case "job":
      return "Job";
    case "workflow":
      return "Workflow";
    case "chunk":
      return "Chunk";
    case "artifact":
      return "Artifact";
    default:
      return source;
  }
}

function formatLogProgress(progress: JobLogEvent["progress"]) {
  if (typeof progress !== "number") {
    return "";
  }
  return `${Math.round(normalizeProgress(progress) * 100)}%`;
}

function parseRetypesetScope(
  mode: EditScope["mode"],
  pages: string,
  blocks: string
): EditScope | string {
  if (mode === "pages") {
    const pageNumbers = parsePositiveIntegerList(pages);
    if (!pageNumbers.length) {
      return "请输入要重排的页码，例如 1, 2, 5。";
    }
    return { mode: "pages", page_numbers: pageNumbers };
  }
  if (mode === "blocks") {
    const blockIds = parseTokenList(blocks);
    if (!blockIds.length) {
      return "请输入要重排的 block id。";
    }
    return { mode: "blocks", block_ids: blockIds };
  }
  return { mode: "all" };
}

function parsePositiveIntegerList(value: string) {
  const tokens = parseTokenList(value);
  const numbers: number[] = [];
  for (const token of tokens) {
    const parsed = Number.parseInt(token, 10);
    if (!Number.isInteger(parsed) || parsed < 1 || String(parsed) !== token) {
      return [];
    }
    if (!numbers.includes(parsed)) {
      numbers.push(parsed);
    }
  }
  return numbers;
}

function parseTokenList(value: string) {
  return value
    .split(/[\s,，]+/)
    .map((token) => token.trim())
    .filter(Boolean);
}

function isKnownLanguage(value: string) {
  return languageOptions.some((option) => option.value === value);
}

function readSavedTaskSnapshot(): SavedTaskSnapshot | null {
  try {
    const raw = window.localStorage.getItem(LAST_TASK_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const payload = JSON.parse(raw) as Partial<SavedTaskSnapshot>;
    if (
      !payload ||
      !isSavedJobStatus(payload.job) ||
      typeof payload.targetLang !== "string" ||
      typeof payload.savedAt !== "string"
    ) {
      return null;
    }
    return {
      job: payload.job,
      docId: typeof payload.docId === "string" || payload.docId === null ? payload.docId : null,
      targetLang: payload.targetLang,
      savedAt: payload.savedAt
    };
  } catch {
    removeSavedTaskSnapshot();
    return null;
  }
}

function isSavedJobStatus(value: unknown): value is JobStatus {
  if (!value || typeof value !== "object") {
    return false;
  }
  const payload = value as Record<string, unknown>;
  return (
    typeof payload.job_id === "string" &&
    typeof payload.filename === "string" &&
    typeof payload.status === "string" &&
    (jobStatusValues as Set<string>).has(payload.status) &&
    typeof payload.progress === "number" &&
    typeof payload.message === "string"
  );
}

function writeSavedTaskSnapshot(snapshot: SavedTaskSnapshot) {
  try {
    window.localStorage.setItem(LAST_TASK_STORAGE_KEY, JSON.stringify(snapshot));
  } catch {
    // Local storage is a convenience only; failing to persist must not interrupt the workflow.
  }
}

function removeSavedTaskSnapshot() {
  try {
    window.localStorage.removeItem(LAST_TASK_STORAGE_KEY);
  } catch {
    // Ignore storage cleanup failures for private browsing or quota-restricted environments.
  }
}

function clampInt(value: string, min: number, max: number) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) {
    return min;
  }
  return Math.min(max, Math.max(min, parsed));
}

function clampNumber(value: string, min: number, max: number) {
  const parsed = Number.parseFloat(value);
  if (!Number.isFinite(parsed)) {
    return min;
  }
  return Math.min(max, Math.max(min, Number(parsed.toFixed(2))));
}

function validateOpenAIBaseUrl(value: string): BaseUrlValidation {
  const trimmed = value.trim().replace(/\/+$/, "");
  if (!trimmed) {
    return { error: "Base URL 不能为空。", warning: null };
  }
  if (!/^https?:\/\//i.test(trimmed)) {
    return { error: "Base URL 必须以 http:// 或 https:// 开头。", warning: null };
  }

  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return { error: "Base URL 不是有效 URL。", warning: null };
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { error: "Base URL 只支持 http:// 或 https://。", warning: null };
  }
  if (!parsed.hostname) {
    return { error: "Base URL 必须包含 host。", warning: null };
  }
  if (parsed.search || parsed.hash) {
    return { error: "Base URL 不应包含查询参数或 fragment。", warning: null };
  }

  const path = parsed.pathname.replace(/\/+$/, "");
  const segments = path.split("/").filter(Boolean);
  if (segments.at(-1) !== "v1") {
    return {
      error: "Base URL 应指向 OpenAI-compatible /v1 API 根路径。",
      warning: null
    };
  }

  if (parsed.protocol === "http:" && !isPrivateOrLocalHost(parsed.hostname)) {
    return {
      error: null,
      warning: "公网 HTTP 会明文传输 API Key；仅在可信网络中使用。"
    };
  }

  return { error: null, warning: null };
}

function isPrivateOrLocalHost(hostname: string) {
  const normalized = hostname.toLowerCase();
  if (normalized === "localhost" || normalized.endsWith(".localhost")) {
    return true;
  }
  if (normalized === "::1" || normalized === "[::1]") {
    return true;
  }

  const octets = normalized.split(".").map((segment) => Number(segment));
  if (octets.length !== 4 || octets.some((octet) => !Number.isInteger(octet))) {
    return false;
  }

  const [first, second] = octets;
  return (
    first === 10 ||
    first === 127 ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168) ||
    (first === 169 && second === 254)
  );
}

function renderEvaluationMessage(payload: unknown) {
  if (!isRecord(payload) || payload.accepted !== false) {
    return null;
  }
  if (payload.browser_validation_unavailable === true) {
    return "Preview and PDF are available, but browser layout validation could not run. Check Playwright/Chromium before accepting this output.";
  }
  const blockingFlags = isRecord(payload.blocking_flags) ? Object.keys(payload.blocking_flags) : [];
  const details = blockingFlags.length ? ` Blocking flags: ${blockingFlags.join(", ")}.` : "";
  return `Preview and PDF are available, but render QA did not pass.${details}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
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

function artifactLabel(name: string) {
  switch (name) {
    case "normalized-input":
      return "Input";
    case "article-brief":
      return "Article Brief";
    case "user-intent":
      return "Intent";
    case "workflow-run":
      return "Workflow";
    case "semantic-analysis":
      return "Semantic";
    case "layout-intent-plan":
      return "Layout";
    case "validation-and-repair":
      return "Repair";
    case "asset-ir":
      return "Assets";
    case "formula-candidates":
      return "Formula Candidates";
    case "formula-recognition":
      return "Formula";
    case "formula-diagnostics":
      return "Formula QA";
    case "ocr-recognition":
      return "OCR";
    case "ocr-diagnostics":
      return "OCR QA";
    case "document-ir":
      return "IR";
    case "translation-chunks":
      return "Chunks";
    case "translation-plans":
      return "Plans";
    case "translation-diagnostics":
      return "Translation Diagnostics";
    case "translation-quality-diagnostics":
      return "Quality Review";
    case "renderer-diagnostics":
      return "Diagnostics";
    case "render-evaluation":
      return "Evaluation";
    case "translation-progress":
      return "Progress";
    case "parser-diagnostics":
      return "Parser";
    default:
      return name;
  }
}

declare global {
  interface Window {
    __TRANS_TYPESETTING_ROOT__?: ReturnType<typeof createRoot>;
  }
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root was not found.");
}

const root = window.__TRANS_TYPESETTING_ROOT__ ?? createRoot(rootElement);
window.__TRANS_TYPESETTING_ROOT__ = root;

root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
