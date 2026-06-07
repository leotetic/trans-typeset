from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ..config import settings
from ..models import CreateDocumentResponse, JobState, JobStatus
from ..pipeline.orchestrator import process_document_job
from ..storage import storage

router = APIRouter(prefix="/api", tags=["documents"])


async def ensure_pdf_upload(file: UploadFile) -> None:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")
    if file.content_type and file.content_type not in {
        "application/pdf",
        "application/x-pdf",
        "application/octet-stream",
    }:
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")

    header = await file.read(1024)
    await file.seek(0)
    if b"%PDF-" not in header:
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")


@router.post("/documents", response_model=CreateDocumentResponse)
async def create_document(
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    target_lang: Annotated[str, Form()] = settings.default_target_lang,
) -> CreateDocumentResponse:
    await ensure_pdf_upload(file)

    doc_id = storage.new_doc_id()
    job_id = storage.new_job_id()
    pdf_path = await storage.save_upload(doc_id, file)
    status = JobStatus(
        job_id=job_id,
        doc_id=doc_id,
        filename=file.filename,
        status=JobState.QUEUED,
        progress=0,
        message="Queued",
    )
    storage.save_status(status)
    background_tasks.add_task(
        process_document_job, job_id, doc_id, file.filename, pdf_path, target_lang
    )
    return CreateDocumentResponse(job_id=job_id, doc_id=doc_id)


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str) -> JobStatus:
    try:
        return storage.load_status(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@router.get("/documents/{doc_id}/preview", response_class=HTMLResponse)
async def get_preview(doc_id: str) -> HTMLResponse:
    path = storage.preview_html_path(doc_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Preview not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/documents/{doc_id}/download")
async def download_document(doc_id: str) -> FileResponse:
    path = storage.output_pdf_path(doc_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Translated PDF not found")
    return FileResponse(path, filename=f"{doc_id}-translated.pdf", media_type="application/pdf")
