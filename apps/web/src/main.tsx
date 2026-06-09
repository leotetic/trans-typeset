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
  History,
  Loader2,
  Search,
  RefreshCw,
  SlidersHorizontal,
  Settings2,
  Upload,
  X,
  XCircle
} from "lucide-react";
import type { JobStatus, OutputKind, StyleIntent, UserConstraints } from "@trans-typesetting/schema";
import {
  ApiError,
  cancelJob,
  createDocument,
  createDocumentsBatch,
  createImageWorkflow,
  createTextWorkflow,
  getDocumentArtifact,
  getHealth,
  getJob,
  getRuntimeConfig,
  listDocumentArtifacts,
  listJobs,
  retryJob,
  updateRuntimeConfig,
  verifyDownload,
  verifyPreview
} from "./api";
import type { ArtifactSummary, RuntimeConfig } from "./api";
import "./styles.css";

type UploadIssue = {
  kind: "error" | "info";
  target?: "requirements" | "document";
  message: string;
};

type HealthState = "checking" | "online" | "offline";
type PreviewState = "idle" | "loading" | "ready" | "error";
type InspectorState = "idle" | "loading" | "ready" | "error";
type InputMode = "text" | "image" | "pdf";

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

const languageOptions = [
  { label: "简体中文", value: "zh-CN" },
  { label: "繁體中文", value: "zh-TW" },
  { label: "日本語", value: "ja-JP" },
  { label: "한국어", value: "ko-KR" },
  { label: "English", value: "en-US" }
];

const languageLabels = new Map(languageOptions.map((option) => [option.value, option.label]));

const inputModes: Array<{ label: string; value: InputMode }> = [
  { label: "Text", value: "text" },
  { label: "Image", value: "image" },
  { label: "PDF", value: "pdf" }
];

const outputKindOptions: Array<{ label: string; value: OutputKind }> = [
  { label: "翻译排版", value: "translation" },
  { label: "文档排版", value: "typeset_document" },
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
  translating: "生成译文内容",
  rendering: "生成预览与 PDF",
  completed: "译文 PDF 已就绪",
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

function App() {
  const [inputMode, setInputMode] = useState<InputMode>("pdf");
  const [files, setFiles] = useState<File[]>([]);
  const [contentPdfFile, setContentPdfFile] = useState<File | null>(null);
  const [layoutPdfFile, setLayoutPdfFile] = useState<File | null>(null);
  const [textInput, setTextInput] = useState("Title\n\nAbstract This paper studies local smart typesetting [1].");
  const [targetLang, setTargetLang] = useState("zh-CN");
  const [outputKind, setOutputKind] = useState<OutputKind>("typeset_document");
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
    ocr_provider_order: ["deterministic"],
    ocr_min_confidence: 0.35,
    ocr_provider_timeout_seconds: 12,
    ocr_max_visual_candidates: 12
  });
  const [configIssue, setConfigIssue] = useState<string | null>(null);
  const [isSavingConfig, setIsSavingConfig] = useState(false);
  const [jobHistory, setJobHistory] = useState<JobStatus[]>([]);
  const [historyIssue, setHistoryIssue] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [isCanceling, setIsCanceling] = useState(false);
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [draggingPdfSlot, setDraggingPdfSlot] = useState<PdfSlot | null>(null);
  const [isRetryingStatus, setIsRetryingStatus] = useState(false);
  const [isRetryingJob, setIsRetryingJob] = useState(false);
  const [previewState, setPreviewState] = useState<PreviewState>("idle");
  const [previewIssue, setPreviewIssue] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [artifactIssue, setArtifactIssue] = useState<string | null>(null);
  const [selectedArtifact, setSelectedArtifact] = useState("renderer-diagnostics");
  const [inspectorState, setInspectorState] = useState<InspectorState>("idle");
  const [inspectorPayload, setInspectorPayload] = useState<string>("");
  const [inspectorIssue, setInspectorIssue] = useState<string | null>(null);
  const [uploadIssue, setUploadIssue] = useState<UploadIssue | null>(null);
  const [taskIssue, setTaskIssue] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const contentPdfInputRef = useRef<HTMLInputElement | null>(null);
  const layoutPdfInputRef = useRef<HTMLInputElement | null>(null);
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
  const isPdfWorkflow = inputMode === "pdf";
  const hasSubmitInput =
    inputMode === "text"
      ? Boolean(textInput.trim())
      : isPdfWorkflow
        ? Boolean(contentPdfFile)
        : files.length > 0;
  const canSubmit = hasSubmitInput && !isUploading && !isTaskRunning && healthState === "online";
  const isComplete = job?.status === "completed" && Boolean(previewUrl);
  const hasBackendFailure = healthState === "offline";
  const artifactsReady = isComplete && previewState === "ready" && !previewIssue;
  const baseUrlValidation = useMemo(
    () => validateOpenAIBaseUrl(configDraft.openai_base_url),
    [configDraft.openai_base_url]
  );
  const configuredLanguages = runtimeConfig?.allowed_target_langs.length
    ? runtimeConfig.allowed_target_langs
    : languageOptions.map((option) => option.value);
  const maxUploadBytes = runtimeConfig?.max_upload_bytes ?? MAX_UPLOAD_BYTES;

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
      setUploadIssue({ kind: "info", target: "document", message: "已选择待翻译 PDF，可以开始排版。" });
    } else {
      setLayoutPdfFile(selectedFile);
      setUploadIssue({ kind: "info", target: "requirements", message: "已选择版式参考 PDF，后端会作为语义排版源。" });
    }
    setTaskIssue(null);
  }

  function handleFiles(nextFiles: FileList | File[] | null | undefined) {
    const selectedFiles = Array.from(nextFiles ?? []);
    if (!selectedFiles.length) {
      return;
    }

    const invalidFile = selectedFiles.find((nextFile) =>
      inputMode === "image" ? !isImageFile(nextFile) : !isPdfFile(nextFile)
    );
    if (invalidFile) {
      setFiles([]);
      setUploadIssue({
        kind: "error",
        target: "requirements",
        message: inputMode === "image" ? "仅支持 PNG、JPEG 或 WebP 图片。" : "仅支持 PDF 文件，请重新选择。"
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
          : selectedFiles.length === 1
            ? "已选择 PDF，可以开始翻译。"
            : `已选择 ${selectedFiles.length} 个 PDF，可以批量翻译。`
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
        target: isPdfWorkflow ? "document" : "requirements",
        message:
          inputMode === "text"
            ? "请输入要排版的文本。"
            : isPdfWorkflow
              ? "请选择待翻译 PDF。"
              : "请选择输入文件。"
      });
      return;
    }

    if (
      inputMode === "image" &&
      files.some((selectedFile) =>
        !isImageFile(selectedFile) || selectedFile.size > maxUploadBytes
      )
    ) {
      handleFiles(files);
      return;
    }

    if (
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
    setJob(null);
    setDocId(null);

    try {
      if (!(await checkHealth())) {
        throw new Error("后端服务不可用，请先启动 API。");
      }
      const intent = {
        output_kind: outputKind,
        style_intent: styleIntent,
        instruction,
        constraints: isConstraintsOpen ? constraintDraft : undefined
      };
      if (inputMode === "text") {
        const payload = await createTextWorkflow(textInput, targetLang, intent);
        setDocId(payload.doc_id);
        await refreshJob(payload.job_id);
      } else if (inputMode === "image") {
        const payload = await createImageWorkflow(files[0], targetLang, intent);
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

  return (
    <main className="shell">
      <section className="workspace" aria-label="Smart typesetting workspace">
        <aside className="control-panel" aria-label="Smart typesetting controls">
          <header className="app-header">
            <div className="app-mark" aria-hidden="true">
              <FileText size={22} strokeWidth={2.1} />
            </div>
            <div className="app-title">
              <h1>Trans Typesetting</h1>
              <p>本地智能翻译与排版工作台</p>
            </div>
          </header>

          <section className="tool-section" aria-labelledby="config-heading">
            <SectionTitle id="config-heading" icon={<Settings2 size={16} />} title="文献配置" />
            {isPdfWorkflow ? (
              <>
                <PdfUploadSlot
                  label="待翻译 PDF"
                  meta="必填，作为翻译与排版内容源"
                  file={contentPdfFile}
                  required
                  issueKind={uploadIssue?.kind}
                  isDragging={draggingPdfSlot === "content"}
                  inputRef={contentPdfInputRef}
                  onSelect={(files) => handlePdfSlotFiles("content", files)}
                  onClear={() => clearPdfSlot("content")}
                  onDragOver={(event) => handlePdfDragOver(event, "content")}
                  onDragLeave={handlePdfDragLeave}
                  onDrop={(event) => handlePdfDrop(event, "content")}
                />
                <div className="upload-footer" id="document-upload-feedback">
                  {contentPdfFile ? (
                    <span className="hint">待翻译 PDF 已就绪。</span>
                  ) : (
                    <InlineNotice
                      issue={uploadIssue?.target === "document" ? uploadIssue : null}
                      fallback="请选择待翻译 PDF。"
                    />
                  )}
                  {contentPdfFile ? (
                    <button className="ghost-button" type="button" onClick={() => clearPdfSlot("content")}>
                      <X size={15} />
                      移除文件
                    </button>
                  ) : null}
                </div>
              </>
            ) : null}
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

          <section className="tool-section" aria-labelledby="typesetting-heading">
            <SectionTitle id="typesetting-heading" icon={<SlidersHorizontal size={16} />} title="排版需求" />
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
            {inputMode === "text" ? (
              <textarea
                className="text-input"
                value={textInput}
                onChange={(event) => {
                  setTextInput(event.target.value);
                  setUploadIssue(null);
                }}
                aria-label="Text input"
              />
            ) : isPdfWorkflow ? (
              <div className="pdf-source-grid">
                <PdfUploadSlot
                  label="版式参考 PDF"
                  meta="可选，作为排版语义输入源"
                  file={layoutPdfFile}
                  issueKind={uploadIssue?.kind}
                  isDragging={draggingPdfSlot === "layout"}
                  inputRef={layoutPdfInputRef}
                  onSelect={(files) => handlePdfSlotFiles("layout", files)}
                  onClear={() => clearPdfSlot("layout")}
                  onDragOver={(event) => handlePdfDragOver(event, "layout")}
                  onDragLeave={handlePdfDragLeave}
                  onDrop={(event) => handlePdfDrop(event, "layout")}
                />
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
                            : `${files.length} 个 ${inputMode === "image" ? "图片" : "PDF"} 文件`}
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
                          {inputMode === "image" ? "选择或拖入图片" : "选择或拖入英文 PDF"}
                        </span>
                        <span className="file-meta">
                          {inputMode === "image" ? "支持 .png .jpg .webp" : "支持 .pdf 文件"}
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
                  accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"
                  onChange={(event) => handleFiles(event.target.files)}
                />
              </>
            )}
            <div className="upload-footer" id="upload-feedback">
              <InlineNotice
                issue={uploadIssue?.target !== "document" ? uploadIssue : null}
                fallback={isPdfWorkflow ? "版式参考 PDF 可选。" : "等待输入。"}
              />
              {files.length || (isPdfWorkflow && layoutPdfFile) ? (
                <button className="ghost-button" type="button" onClick={clearRequirementsInput}>
                  <X size={15} />
                  移除输入
                </button>
              ) : null}
            </div>
            <label className="field">
              <span>
                <Globe2 size={16} />
                目标语言
              </span>
              <select value={targetLang} onChange={(event) => setTargetLang(event.target.value)}>
                {configuredLanguages.map((lang) => (
                  <option key={lang} value={lang}>
                    {languageLabels.get(lang) ?? lang}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>输出类型</span>
              <select value={outputKind} onChange={(event) => setOutputKind(event.target.value as OutputKind)}>
                {outputKindOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>版式风格</span>
              <select value={styleIntent} onChange={(event) => setStyleIntent(event.target.value as StyleIntent)}>
                {styleIntentOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>排版说明</span>
              <textarea
                className="intent-input"
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
          </section>

          <section className="tool-section" aria-labelledby="history-heading">
            <SectionTitle id="history-heading" icon={<History size={16} />} title="历史" />
            <HistoryList
              jobs={jobHistory}
              issue={historyIssue}
              activeJobId={job?.job_id ?? null}
              onRefresh={() => void refreshHistory()}
              onRestore={(historyJob) => {
                setJob(historyJob);
                setDocId(historyJob.doc_id ?? null);
                setTaskIssue(historyJob.error ?? null);
                setPreviewIssue(null);
                setPreviewState(historyJob.status === "completed" ? "loading" : "idle");
              }}
            />
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
              <span>{isUploading ? "提交中" : job ? "重新执行" : "开始排版"}</span>
            </button>
            <JobProgress
              job={job}
              isUploading={isUploading}
              isRetryingStatus={isRetryingStatus}
              isCanceling={isCanceling}
              isRetryingJob={isRetryingJob}
              issue={taskIssue}
              onRetryStatus={retryStatus}
              onCancel={cancelCurrentJob}
              onRetryJob={retryCurrentJob}
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
          <SchemaInspector
            artifacts={artifacts}
            issue={artifactIssue}
            selectedArtifact={selectedArtifact}
            state={inspectorState}
            payload={inspectorPayload}
            payloadIssue={inspectorIssue}
            onSelect={setSelectedArtifact}
          />
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

function PdfUploadSlot({
  label,
  meta,
  file,
  required = false,
  issueKind,
  isDragging,
  inputRef,
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
        accept="application/pdf,.pdf"
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
          value={draft.openai_model}
          onChange={(event) =>
            onDraftChange((current) => ({ ...current, openai_model: event.target.value }))
          }
        />
      </label>
      <label>
        <span>API Key</span>
        <input
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
  activeJobId,
  onRefresh,
  onRestore
}: {
  jobs: JobStatus[];
  issue: string | null;
  activeJobId: string | null;
  onRefresh: () => void;
  onRestore: (job: JobStatus) => void;
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

function JobProgress({
  job,
  isUploading,
  isRetryingStatus,
  isCanceling,
  isRetryingJob,
  issue,
  onRetryStatus,
  onCancel,
  onRetryJob
}: {
  job: JobStatus | null;
  isUploading: boolean;
  isRetryingStatus: boolean;
  isCanceling: boolean;
  isRetryingJob: boolean;
  issue: string | null;
  onRetryStatus: () => void;
  onCancel: () => void;
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
  const canRetryJob = Boolean(job && ["failed", "canceled"].includes(job.status));

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
      {canRetryJob ? (
        <button className="secondary-action" type="button" onClick={onRetryJob} disabled={isRetryingJob}>
          {isRetryingJob ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}
          <span>{isRetryingJob ? "排队中" : "重新排队"}</span>
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

function SchemaInspector({
  artifacts,
  issue,
  selectedArtifact,
  state,
  payload,
  payloadIssue,
  onSelect
}: {
  artifacts: ArtifactSummary[];
  issue: string | null;
  selectedArtifact: string;
  state: InspectorState;
  payload: string;
  payloadIssue: string | null;
  onSelect: (artifactName: string) => void;
}) {
  const availableArtifacts = artifacts.filter((artifact) => artifact.available);
  return (
    <aside className="schema-inspector" aria-label="Schema inspector">
      <div className="inspector-head">
        <div>
          <span>Inspector</span>
          <strong>Schema 与诊断</strong>
        </div>
        <Search size={18} aria-hidden="true" />
      </div>
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
    </aside>
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

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
