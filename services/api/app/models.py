from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class JobState(StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    TRANSLATING = "translating"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(BaseModel):
    job_id: str
    doc_id: str | None = None
    filename: str
    status: JobState
    progress: float = Field(ge=0, le=1)
    message: str
    error: str | None = None


class CreateDocumentResponse(BaseModel):
    job_id: str
    doc_id: str

