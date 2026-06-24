from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field
from pdf_translator_schema import EditScope, RenderDefaults, StyleIntent, UserConstraints


class JobState(StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    TRANSLATING = "translating"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class ChunkProgress(BaseModel):
    chunk_id: str
    index: int = Field(ge=1)
    total: int = Field(ge=1)
    status: str
    progress: float = Field(ge=0, le=1)
    message: str
    quality_flags: list[str] = Field(default_factory=list)
    error: str | None = None


class JobStatus(BaseModel):
    job_id: str
    doc_id: str | None = None
    filename: str
    target_lang: str | None = None
    status: JobState
    progress: float = Field(ge=0, le=1)
    message: str
    error: str | None = None
    chunks: list[ChunkProgress] = Field(default_factory=list)


class JobLogEvent(BaseModel):
    id: str
    sequence: int
    source: Literal["job", "workflow", "chunk", "artifact"]
    level: Literal["info", "success", "warning", "error"]
    phase: str
    title: str
    message: str
    progress: float | None = Field(default=None, ge=0, le=1)
    details: list[str] = Field(default_factory=list)


class JobLogResponse(BaseModel):
    job_id: str
    doc_id: str | None = None
    status: JobState
    progress: float = Field(ge=0, le=1)
    message: str
    events: list[JobLogEvent] = Field(default_factory=list)


class CreateDocumentResponse(BaseModel):
    job_id: str
    doc_id: str


class BatchCreateDocumentResponse(BaseModel):
    jobs: list[CreateDocumentResponse]


class RetypesetJobRequest(BaseModel):
    instruction: str = ""
    style_intent: StyleIntent | None = None
    target_lang: str | None = None
    constraints: UserConstraints | None = None
    scope: EditScope = Field(default_factory=EditScope)


class RuntimeConfig(BaseModel):
    default_target_lang: str
    allowed_target_langs: list[str]
    max_upload_bytes: int
    translator_provider: str
    openai_base_url: str
    openai_model: str
    openai_api_key_configured: bool
    translation_concurrency: int
    translator_max_attempts: int
    translation_chunk_max_chars: int
    agent_max_repair_attempts: int
    agent_enable_vision_analysis: bool
    layout_planner_model: str
    vision_analyzer_model: str
    ocr_provider_order: list[str]
    ocr_min_confidence: float
    ocr_provider_timeout_seconds: float
    ocr_max_visual_candidates: int
    formula_recognition_concurrency: int
    formula_visual_ocr_concurrency: int
    render_defaults: RenderDefaults


class UpdateRuntimeConfig(BaseModel):
    default_target_lang: str | None = None
    openai_base_url: str | None = None
    openai_model: str | None = None
    openai_api_key: str | None = None
    translation_concurrency: int | None = Field(default=None, ge=1, le=16)
    translator_max_attempts: int | None = Field(default=None, ge=1, le=5)
    translation_chunk_max_chars: int | None = Field(default=None, ge=500, le=12000)
    agent_max_repair_attempts: int | None = Field(default=None, ge=0, le=5)
    agent_enable_vision_analysis: bool | None = None
    layout_planner_model: str | None = None
    vision_analyzer_model: str | None = None
    ocr_provider_order: list[str] | None = None
    ocr_min_confidence: float | None = Field(default=None, ge=0, le=1)
    ocr_provider_timeout_seconds: float | None = Field(default=None, ge=1, le=120)
    ocr_max_visual_candidates: int | None = Field(default=None, ge=0, le=200)
    formula_recognition_concurrency: int | None = Field(default=None, ge=1, le=32)
    formula_visual_ocr_concurrency: int | None = Field(default=None, ge=1, le=8)
    render_defaults: RenderDefaults | None = None


class ArtifactSummary(BaseModel):
    name: str
    kind: str
    available: bool
    href: str | None = None


class DocumentArtifacts(BaseModel):
    doc_id: str
    artifacts: list[ArtifactSummary]
