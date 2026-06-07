from __future__ import annotations

from pathlib import Path

from pdf_renderer import RenderDocument, render_to_html, render_to_pdf
from pdf_translator_schema import TranslationLayoutPlan

from ..config import settings
from ..models import JobState, JobStatus
from ..storage import storage
from .chunker import build_chunks
from .parser import parse_pdf
from .translator import build_translator


def update_status(
    job_id: str,
    filename: str,
    status: JobState,
    progress: float,
    message: str,
    doc_id: str | None = None,
    error: str | None = None,
) -> None:
    storage.save_status(
        JobStatus(
            job_id=job_id,
            doc_id=doc_id,
            filename=filename,
            status=status,
            progress=progress,
            message=message,
            error=error,
        )
    )


async def process_document_job(
    job_id: str,
    doc_id: str,
    filename: str,
    pdf_path: Path,
    target_lang: str,
) -> None:
    try:
        update_status(job_id, filename, JobState.PARSING, 0.15, "Parsing PDF", doc_id)
        document = parse_pdf(pdf_path, doc_id)
        storage.save_document_ir(document)

        update_status(job_id, filename, JobState.TRANSLATING, 0.35, "Chunking document", doc_id)
        chunks = build_chunks(document, target_lang=target_lang)
        if not chunks:
            raise ValueError("Document has no translatable chunks")
        translator = build_translator(
            settings.openai_base_url, settings.openai_api_key, settings.openai_model
        )

        plans: list[TranslationLayoutPlan] = []
        for index, chunk in enumerate(chunks):
            translated = await translator.translate(chunk)
            plans.append(translated)
            progress = 0.35 + (index + 1) / len(chunks) * 0.4
            update_status(
                job_id,
                filename,
                JobState.TRANSLATING,
                progress,
                f"Translated chunk {index + 1} of {len(chunks)}",
                doc_id,
            )

        update_status(job_id, filename, JobState.RENDERING, 0.82, "Rendering preview", doc_id)
        render_document = RenderDocument.from_ir_and_plans(document, plans, target_lang)
        html = render_to_html(render_document)
        storage.save_preview_html(doc_id, html)
        storage.write_json(doc_id, "translation-plans.json", [plan.model_dump() for plan in plans])

        update_status(job_id, filename, JobState.RENDERING, 0.92, "Rendering PDF", doc_id)
        pdf_output = storage.output_pdf_path(doc_id)
        await render_to_pdf(html, pdf_output)

        update_status(job_id, filename, JobState.COMPLETED, 1.0, "Completed", doc_id)
    except Exception as exc:
        update_status(
            job_id,
            filename,
            JobState.FAILED,
            1.0,
            "Failed",
            doc_id,
            error=str(exc),
        )
