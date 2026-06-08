from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pdf_translator_schema import UserIntent

from ..config import settings
from ..jobs import schedule_job
from ..models import (
    ArtifactSummary,
    BatchCreateDocumentResponse,
    CreateDocumentResponse,
    DocumentArtifacts,
    JobState,
    JobStatus,
    RuntimeConfig,
    UpdateRuntimeConfig,
)
from ..provider_config import ProviderConfigError, normalize_openai_base_url
from ..pipeline.orchestrator import (
    process_document_job,
    process_image_document_job,
    process_text_document_job,
)
from ..pipeline.workflow import coerce_user_intent
from ..runtime_config import effective_runtime_config, runtime_config_response
from ..storage import storage

router = APIRouter(prefix="/api", tags=["documents"])

JSON_ARTIFACTS = {
    "normalized-input": ("normalized-input.json", "normalized-input"),
    "user-intent": ("user-intent.json", "user-intent"),
    "workflow-run": ("workflow-run.json", "workflow-run"),
    "semantic-analysis": ("semantic-analysis.json", "semantic-layout-analysis"),
    "layout-intent-plan": ("layout-intent-plan.json", "layout-intent-plan"),
    "validation-and-repair": ("validation-and-repair.json", "validation-and-repair"),
    "asset-ir": ("asset-ir.json", "asset-ir"),
    "document-ir": ("document_ir", "document-ir"),
    "translation-chunks": ("translation-chunks.json", "translation-chunks"),
    "translation-plans": ("translation-plans.json", "translation-layout-plans"),
    "translation-diagnostics": ("translation-diagnostics.json", "translation-diagnostics"),
    "layout-trace": ("layout-trace.json", "layout-trace"),
    "renderer-diagnostics": ("renderer-diagnostics.json", "renderer-diagnostics"),
    "render-evaluation": ("render-evaluation.json", "render-evaluation"),
    "pdf-export-diagnostics": ("pdf-export-diagnostics.json", "pdf-export-diagnostics"),
    "translation-progress": ("translation-progress.json", "translation-progress"),
    "parser-diagnostics": ("parser-diagnostics.json", "parser-diagnostics"),
}


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


async def ensure_image_upload(file: UploadFile) -> None:
    filename = file.filename or ""
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    allowed_suffixes = {"png", "jpg", "jpeg", "webp"}
    allowed_types = {"image/png", "image/jpeg", "image/webp", "application/octet-stream"}
    if suffix not in allowed_suffixes:
        raise HTTPException(status_code=400, detail="Only PNG, JPEG, and WebP images are supported")
    if file.content_type and file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Only PNG, JPEG, and WebP images are supported")


def ensure_target_lang(target_lang: str) -> None:
    if target_lang not in settings.allowed_target_langs:
        allowed = ", ".join(settings.allowed_target_langs)
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported target language: {target_lang}. Allowed: {allowed}",
        )


def _constraints_from_form(
    *,
    page_width_pt: float | None = None,
    page_height_pt: float | None = None,
    target_font_size_pt: float | None = None,
    allow_continuation: bool | None = None,
    preserve_images: bool | None = None,
) -> dict[str, object] | None:
    values: dict[str, object] = {}
    if page_width_pt is not None:
        values["page_width_pt"] = page_width_pt
    if page_height_pt is not None:
        values["page_height_pt"] = page_height_pt
    if target_font_size_pt is not None:
        values["target_font_size_pt"] = target_font_size_pt
    if allow_continuation is not None:
        values["allow_continuation"] = allow_continuation
    if preserve_images is not None:
        values["preserve_images"] = preserve_images
    return values or None


def _load_user_intent(doc_id: str) -> UserIntent | None:
    try:
        return UserIntent.model_validate(storage.read_output_json(doc_id, "user-intent.json"))
    except Exception:
        return None


@router.get(
    "/config",
    response_model=RuntimeConfig,
    response_model_exclude={
        "render_defaults": {"layout_mode", "page_layout", "role_styles"}
    },
)
async def get_config() -> RuntimeConfig:
    return runtime_config_response(storage)


@router.put(
    "/config",
    response_model=RuntimeConfig,
    response_model_exclude={
        "render_defaults": {"layout_mode", "page_layout", "role_styles"}
    },
)
async def update_config(payload: UpdateRuntimeConfig) -> RuntimeConfig:
    current = effective_runtime_config(storage)
    updates = payload.model_dump(exclude_none=True)
    if "default_target_lang" in updates:
        ensure_target_lang(updates["default_target_lang"])
    if "openai_base_url" in updates:
        try:
            updates["openai_base_url"] = normalize_openai_base_url(
                str(updates["openai_base_url"])
            )
        except ProviderConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "openai_api_key" in updates and updates["openai_api_key"] == "":
        updates["openai_api_key"] = ""
    if "render_defaults" in updates:
        render_defaults = updates["render_defaults"]
        if "default_target_lang" in updates:
            render_defaults["target_lang"] = updates["default_target_lang"]
        updates["render_defaults"] = render_defaults
    current["render_defaults"] = current["render_defaults"].model_dump()
    if "default_target_lang" in updates and "render_defaults" not in updates:
        current["render_defaults"]["target_lang"] = updates["default_target_lang"]
    current.update(updates)
    current["render_defaults"] = UpdateRuntimeConfig(
        render_defaults=current["render_defaults"]
    ).render_defaults.model_dump()
    storage.write_runtime_config(current)
    return runtime_config_response(storage)


@router.post("/documents", response_model=CreateDocumentResponse)
async def create_document(
    content_file: Annotated[UploadFile | None, File()] = None,
    layout_file: Annotated[UploadFile | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    target_lang: Annotated[str, Form()] = settings.default_target_lang,
    output_kind: Annotated[str, Form()] = "translation",
    style_intent: Annotated[str, Form()] = "academic",
    instruction: Annotated[str, Form()] = "",
    page_width_pt: Annotated[float | None, Form()] = None,
    page_height_pt: Annotated[float | None, Form()] = None,
    target_font_size_pt: Annotated[float | None, Form()] = None,
    allow_continuation: Annotated[bool | None, Form()] = None,
    preserve_images: Annotated[bool | None, Form()] = None,
) -> CreateDocumentResponse:
    ensure_target_lang(target_lang)
    content_upload = content_file or file
    if content_upload is None:
        raise HTTPException(status_code=400, detail="Content PDF is required")
    await ensure_pdf_upload(content_upload)
    if layout_file is not None:
        await ensure_pdf_upload(layout_file)
    user_intent = coerce_user_intent(
        target_lang,
        output_kind,
        style_intent,
        instruction,
        _constraints_from_form(
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
            target_font_size_pt=target_font_size_pt,
            allow_continuation=allow_continuation,
            preserve_images=preserve_images,
        ),
    )

    doc_id = storage.new_doc_id()
    job_id = storage.new_job_id()
    pdf_path = await storage.save_upload(
        doc_id,
        content_upload,
        settings.max_upload_bytes,
        role="content",
    )
    layout_pdf_path = (
        await storage.save_upload(
            doc_id,
            layout_file,
            settings.max_upload_bytes,
            role="layout",
        )
        if layout_file is not None
        else None
    )
    status = JobStatus(
        job_id=job_id,
        doc_id=doc_id,
        filename=content_upload.filename or "content.pdf",
        target_lang=target_lang,
        status=JobState.QUEUED,
        progress=0,
        message="Queued",
    )
    storage.save_status(status)
    schedule_job(
        process_document_job,
        job_id,
        doc_id,
        content_upload.filename or "content.pdf",
        pdf_path,
        target_lang,
        user_intent,
        layout_pdf_path,
        layout_file.filename if layout_file is not None else None,
    )
    return CreateDocumentResponse(job_id=job_id, doc_id=doc_id)


@router.post("/workflows/text", response_model=CreateDocumentResponse)
async def create_text_workflow(
    text: Annotated[str, Form()],
    target_lang: Annotated[str, Form()] = settings.default_target_lang,
    output_kind: Annotated[str, Form()] = "typeset_document",
    style_intent: Annotated[str, Form()] = "academic",
    instruction: Annotated[str, Form()] = "",
    page_width_pt: Annotated[float | None, Form()] = None,
    page_height_pt: Annotated[float | None, Form()] = None,
    target_font_size_pt: Annotated[float | None, Form()] = None,
    allow_continuation: Annotated[bool | None, Form()] = None,
    preserve_images: Annotated[bool | None, Form()] = None,
    filename: Annotated[str, Form()] = "text-input.txt",
) -> CreateDocumentResponse:
    ensure_target_lang(target_lang)
    if not text.strip():
        raise HTTPException(status_code=400, detail="Text input is empty")
    user_intent = coerce_user_intent(
        target_lang,
        output_kind,
        style_intent,
        instruction,
        _constraints_from_form(
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
            target_font_size_pt=target_font_size_pt,
            allow_continuation=allow_continuation,
            preserve_images=preserve_images,
        ),
    )
    doc_id = storage.new_doc_id()
    job_id = storage.new_job_id()
    storage.save_status(
        JobStatus(
            job_id=job_id,
            doc_id=doc_id,
            filename=filename,
            target_lang=target_lang,
            status=JobState.QUEUED,
            progress=0,
            message="Queued",
        )
    )
    schedule_job(
        process_text_document_job,
        job_id,
        doc_id,
        filename,
        text,
        target_lang,
        user_intent,
    )
    return CreateDocumentResponse(job_id=job_id, doc_id=doc_id)


@router.post("/workflows/image", response_model=CreateDocumentResponse)
async def create_image_workflow(
    file: Annotated[UploadFile, File()],
    target_lang: Annotated[str, Form()] = settings.default_target_lang,
    output_kind: Annotated[str, Form()] = "layout_reference",
    style_intent: Annotated[str, Form()] = "academic",
    instruction: Annotated[str, Form()] = "",
    page_width_pt: Annotated[float | None, Form()] = None,
    page_height_pt: Annotated[float | None, Form()] = None,
    target_font_size_pt: Annotated[float | None, Form()] = None,
    allow_continuation: Annotated[bool | None, Form()] = None,
    preserve_images: Annotated[bool | None, Form()] = None,
) -> CreateDocumentResponse:
    ensure_target_lang(target_lang)
    await ensure_image_upload(file)
    user_intent = coerce_user_intent(
        target_lang,
        output_kind,
        style_intent,
        instruction,
        _constraints_from_form(
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
            target_font_size_pt=target_font_size_pt,
            allow_continuation=allow_continuation,
            preserve_images=preserve_images,
        ),
    )
    doc_id = storage.new_doc_id()
    job_id = storage.new_job_id()
    image_path = await storage.save_upload_file(
        doc_id,
        file,
        settings.max_upload_bytes,
        default_suffix=".png",
    )
    storage.save_status(
        JobStatus(
            job_id=job_id,
            doc_id=doc_id,
            filename=file.filename or "image-input.png",
            target_lang=target_lang,
            status=JobState.QUEUED,
            progress=0,
            message="Queued",
        )
    )
    schedule_job(
        process_image_document_job,
        job_id,
        doc_id,
        file.filename or "image-input.png",
        image_path,
        target_lang,
        file.content_type,
        user_intent,
    )
    return CreateDocumentResponse(job_id=job_id, doc_id=doc_id)


@router.post("/documents/batch", response_model=BatchCreateDocumentResponse)
async def create_documents_batch(
    files: Annotated[list[UploadFile], File()],
    target_lang: Annotated[str, Form()] = settings.default_target_lang,
    output_kind: Annotated[str, Form()] = "translation",
    style_intent: Annotated[str, Form()] = "academic",
    instruction: Annotated[str, Form()] = "",
    page_width_pt: Annotated[float | None, Form()] = None,
    page_height_pt: Annotated[float | None, Form()] = None,
    target_font_size_pt: Annotated[float | None, Form()] = None,
    allow_continuation: Annotated[bool | None, Form()] = None,
    preserve_images: Annotated[bool | None, Form()] = None,
) -> BatchCreateDocumentResponse:
    ensure_target_lang(target_lang)
    if not files:
        raise HTTPException(status_code=400, detail="At least one PDF is required")
    user_intent = coerce_user_intent(
        target_lang,
        output_kind,
        style_intent,
        instruction,
        _constraints_from_form(
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
            target_font_size_pt=target_font_size_pt,
            allow_continuation=allow_continuation,
            preserve_images=preserve_images,
        ),
    )

    jobs: list[CreateDocumentResponse] = []
    for file in files:
        await ensure_pdf_upload(file)
        doc_id = storage.new_doc_id()
        job_id = storage.new_job_id()
        pdf_path = await storage.save_upload(
            doc_id,
            file,
            settings.max_upload_bytes,
            role="content",
        )
        status = JobStatus(
            job_id=job_id,
            doc_id=doc_id,
            filename=file.filename,
            target_lang=target_lang,
            status=JobState.QUEUED,
            progress=0,
            message="Queued",
        )
        storage.save_status(status)
        schedule_job(
            process_document_job,
            job_id,
            doc_id,
            file.filename,
            pdf_path,
            target_lang,
            user_intent,
        )
        jobs.append(CreateDocumentResponse(job_id=job_id, doc_id=doc_id))
    return BatchCreateDocumentResponse(jobs=jobs)


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str) -> JobStatus:
    try:
        return storage.load_status(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@router.get("/jobs", response_model=list[JobStatus])
async def list_jobs(limit: int = 25) -> list[JobStatus]:
    bounded_limit = min(max(limit, 1), 100)
    return storage.list_statuses(bounded_limit)


@router.post("/jobs/{job_id}/cancel", response_model=JobStatus)
async def cancel_job(job_id: str) -> JobStatus:
    try:
        status = storage.load_status(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    if status.status in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELED}:
        return status
    canceled = JobStatus(
        job_id=status.job_id,
        doc_id=status.doc_id,
        filename=status.filename,
        target_lang=status.target_lang,
        status=JobState.CANCELED,
        progress=1,
        message="Canceled",
        chunks=status.chunks,
    )
    storage.save_status(canceled)
    return canceled


@router.post("/jobs/{job_id}/retry", response_model=CreateDocumentResponse)
async def retry_job(job_id: str) -> CreateDocumentResponse:
    try:
        status = storage.load_status(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    if not status.doc_id:
        raise HTTPException(status_code=400, detail="Job has no document to retry")
    pdf_path = storage.find_upload(status.doc_id, role="content")
    if pdf_path is None:
        raise HTTPException(status_code=404, detail="Original upload not found")
    layout_pdf_path = storage.find_upload(status.doc_id, role="layout")
    target_lang = status.target_lang or settings.default_target_lang
    ensure_target_lang(target_lang)
    user_intent = _load_user_intent(status.doc_id)
    next_job_id = storage.new_job_id()
    next_status = JobStatus(
        job_id=next_job_id,
        doc_id=status.doc_id,
        filename=status.filename,
        target_lang=target_lang,
        status=JobState.QUEUED,
        progress=0,
        message="Queued retry",
    )
    storage.save_status(next_status)
    schedule_job(
        process_document_job,
        next_job_id,
        status.doc_id,
        status.filename,
        pdf_path,
        target_lang,
        user_intent,
        layout_pdf_path,
    )
    return CreateDocumentResponse(job_id=next_job_id, doc_id=status.doc_id)


@router.get("/documents/{doc_id}/preview", response_class=HTMLResponse)
async def get_preview(doc_id: str) -> HTMLResponse:
    path = storage.preview_html_path(doc_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Preview not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.head("/documents/{doc_id}/preview")
async def head_preview(doc_id: str) -> Response:
    path = storage.preview_html_path(doc_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Preview not found")
    return Response(media_type="text/html")


@router.get("/documents/{doc_id}/download")
async def download_document(doc_id: str) -> FileResponse:
    path = storage.output_pdf_path(doc_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Translated PDF not found")
    return FileResponse(path, filename=f"{doc_id}-translated.pdf", media_type="application/pdf")


@router.head("/documents/{doc_id}/download")
async def head_download(doc_id: str) -> Response:
    path = storage.output_pdf_path(doc_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Translated PDF not found")
    return Response(media_type="application/pdf")


@router.get("/documents/{doc_id}/assets/{filename}")
async def get_document_asset(doc_id: str, filename: str) -> FileResponse:
    asset_id = filename.rsplit(".", 1)[0]
    path = storage.find_asset_file(doc_id, asset_id)
    if path is None or path.name != filename:
        raise HTTPException(status_code=404, detail="Asset not found")
    media_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    if path.suffix.lower() == ".webp":
        media_type = "image/webp"
    return FileResponse(path, media_type=media_type)


@router.get("/documents/{doc_id}/artifacts", response_model=DocumentArtifacts)
async def list_document_artifacts(doc_id: str) -> DocumentArtifacts:
    artifacts: list[ArtifactSummary] = []
    for name, (filename, kind) in JSON_ARTIFACTS.items():
        available = (
            (storage.documents / f"{doc_id}.json").exists()
            if name == "document-ir"
            else storage.output_json_path(doc_id, filename).exists()
        )
        artifacts.append(
            ArtifactSummary(
                name=name,
                kind=kind,
                available=available,
                href=f"/api/documents/{doc_id}/artifacts/{name}",
            )
        )
    return DocumentArtifacts(doc_id=doc_id, artifacts=artifacts)


@router.get("/documents/{doc_id}/artifacts/{artifact_name}")
async def get_document_artifact(doc_id: str, artifact_name: str) -> object:
    if artifact_name not in JSON_ARTIFACTS:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if artifact_name == "document-ir":
        try:
            return storage.load_document_ir(doc_id).model_dump()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc

    filename, _ = JSON_ARTIFACTS[artifact_name]
    try:
        return storage.read_output_json(doc_id, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
