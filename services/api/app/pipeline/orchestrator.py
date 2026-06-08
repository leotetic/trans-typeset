from __future__ import annotations

import asyncio
from pathlib import Path

from pdf_renderer import RenderDocument, render_to_html, render_to_pdf
from pdf_translator_schema import TranslationLayoutPlan

from ..models import ChunkProgress, JobState, JobStatus
from ..runtime_config import effective_runtime_config, render_defaults_for_target
from ..storage import storage
from .chunker import build_chunks
from .parser import UnsupportedPdfError, build_parser_diagnostics, parse_pdf
from .translator import build_translator


def update_status(
    job_id: str,
    filename: str,
    target_lang: str | None,
    status: JobState,
    progress: float,
    message: str,
    doc_id: str | None = None,
    error: str | None = None,
    chunks: list[ChunkProgress] | None = None,
) -> None:
    storage.save_status(
        JobStatus(
            job_id=job_id,
            doc_id=doc_id,
            filename=filename,
            target_lang=target_lang,
            status=status,
            progress=progress,
            message=message,
            error=error,
            chunks=chunks or [],
        )
    )


class JobCanceled(RuntimeError):
    pass


def ensure_not_canceled(job_id: str) -> None:
    try:
        status = storage.load_status(job_id)
    except FileNotFoundError:
        return
    if status.status == JobState.CANCELED:
        raise JobCanceled("Job canceled")


def _initial_chunk_progress(chunks: list) -> list[ChunkProgress]:
    total = len(chunks)
    return [
        ChunkProgress(
            chunk_id=chunk.chunk_id,
            index=index,
            total=total,
            status="queued",
            progress=0,
            message="Queued",
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def _plan_quality_flags(plan: TranslationLayoutPlan) -> list[str]:
    flags: list[str] = []
    seen: set[str] = set()
    for block in plan.blocks:
        for flag in block.quality_flags:
            if flag and flag not in seen:
                flags.append(flag)
                seen.add(flag)
    return flags


async def process_document_job(
    job_id: str,
    doc_id: str,
    filename: str,
    pdf_path: Path,
    target_lang: str,
) -> None:
    status_chunks: list[ChunkProgress] = []
    try:
        ensure_not_canceled(job_id)
        update_status(job_id, filename, target_lang, JobState.PARSING, 0.15, "Parsing PDF", doc_id)
        try:
            document = parse_pdf(pdf_path, doc_id, storage.asset_dir(doc_id))
        except UnsupportedPdfError as exc:
            storage.write_json(doc_id, "parser-diagnostics.json", exc.diagnostics)
            raise
        storage.save_document_ir(document)
        storage.write_json(doc_id, "parser-diagnostics.json", build_parser_diagnostics(document))

        ensure_not_canceled(job_id)
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.TRANSLATING,
            0.35,
            "Chunking document",
            doc_id,
        )
        runtime_config = effective_runtime_config(storage)
        render_defaults = render_defaults_for_target(storage, target_lang)
        chunks = build_chunks(
            document,
            target_lang=target_lang,
            render_defaults=render_defaults,
        )
        if not chunks:
            raise ValueError("Document has no translatable chunks")
        storage.write_json(doc_id, "translation-chunks.json", [chunk.model_dump() for chunk in chunks])
        translator = build_translator(
            runtime_config["openai_base_url"],
            runtime_config["openai_api_key"],
            runtime_config["openai_model"],
            runtime_config["translator_max_attempts"],
        )

        chunk_progress = _initial_chunk_progress(chunks)
        status_chunks = chunk_progress
        storage.write_json(
            doc_id,
            "translation-progress.json",
            [progress.model_dump() for progress in chunk_progress],
        )
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.TRANSLATING,
            0.36,
            f"Translating 0 of {len(chunks)} chunks",
            doc_id,
            chunks=chunk_progress,
        )
        plans_by_index: list[TranslationLayoutPlan | None] = [None] * len(chunks)
        completed_chunks = 0
        semaphore = asyncio.Semaphore(max(1, runtime_config["translation_concurrency"]))

        async def translate_chunk(index: int) -> None:
            nonlocal completed_chunks
            chunk = chunks[index]
            progress_entry = chunk_progress[index]
            try:
                async with semaphore:
                    progress_entry.status = "translating"
                    progress_entry.progress = 0.25
                    progress_entry.message = "Translating"
                    update_status(
                        job_id,
                        filename,
                        target_lang,
                        JobState.TRANSLATING,
                        0.35 + completed_chunks / len(chunks) * 0.4,
                        f"Translating chunk {index + 1} of {len(chunks)}",
                        doc_id,
                        chunks=chunk_progress,
                    )
                    storage.write_json(
                        doc_id,
                        "translation-progress.json",
                        [progress.model_dump() for progress in chunk_progress],
                    )
                    translated = await translator.translate(chunk)
                    ensure_not_canceled(job_id)
            except JobCanceled:
                progress_entry.status = "canceled"
                progress_entry.progress = 1
                progress_entry.message = "Canceled"
                storage.write_json(
                    doc_id,
                    "translation-progress.json",
                    [progress.model_dump() for progress in chunk_progress],
                )
                raise
            except Exception as exc:
                progress_entry.status = "failed"
                progress_entry.progress = 1
                progress_entry.message = "Failed"
                progress_entry.error = str(exc)
                storage.write_json(
                    doc_id,
                    "translation-progress.json",
                    [progress.model_dump() for progress in chunk_progress],
                )
                raise

            plans_by_index[index] = translated
            completed_chunks += 1
            progress_entry.status = "completed"
            progress_entry.progress = 1
            progress_entry.message = "Completed"
            progress_entry.quality_flags = _plan_quality_flags(translated)
            progress = 0.35 + completed_chunks / len(chunks) * 0.4
            storage.write_json(
                doc_id,
                "translation-progress.json",
                [progress.model_dump() for progress in chunk_progress],
            )
            update_status(
                job_id,
                filename,
                target_lang,
                JobState.TRANSLATING,
                progress,
                f"Translated {completed_chunks} of {len(chunks)} chunks",
                doc_id,
                chunks=chunk_progress,
            )

        results = await asyncio.gather(
            *(translate_chunk(index) for index in range(len(chunks))),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            if any(isinstance(result, JobCanceled) for result in errors):
                raise JobCanceled("Job canceled")
            raise RuntimeError(
                f"{len(errors)} translation chunk(s) failed; first error: {errors[0]}"
            )
        plans = [plan for plan in plans_by_index if plan is not None]

        ensure_not_canceled(job_id)
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.RENDERING,
            0.82,
            "Rendering preview",
            doc_id,
            chunks=chunk_progress,
        )
        render_document = RenderDocument.from_ir_and_plans(
            document,
            plans,
            target_lang,
            render_defaults=render_defaults,
        )
        html = render_to_html(render_document)
        storage.save_preview_html(doc_id, html)
        storage.write_json(doc_id, "translation-plans.json", [plan.model_dump() for plan in plans])
        storage.write_json(doc_id, "renderer-diagnostics.json", render_document.diagnostics())

        ensure_not_canceled(job_id)
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.RENDERING,
            0.92,
            "Rendering PDF",
            doc_id,
            chunks=chunk_progress,
        )
        pdf_output = storage.output_pdf_path(doc_id)
        await render_to_pdf(html, pdf_output)

        update_status(
            job_id,
            filename,
            target_lang,
            JobState.COMPLETED,
            1.0,
            "Completed",
            doc_id,
            chunks=chunk_progress,
        )
    except JobCanceled:
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.CANCELED,
            1.0,
            "Canceled",
            doc_id,
            chunks=status_chunks,
        )
    except Exception as exc:
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.FAILED,
            1.0,
            "Failed",
            doc_id,
            error=str(exc),
            chunks=status_chunks,
        )
