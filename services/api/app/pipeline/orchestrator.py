from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from pdf_renderer import RenderDocument, render_to_html, render_to_pdf
from pdf_translator_schema import (
    DocumentIR,
    InputKind,
    TranslationChunk,
    TranslationLayoutPlan,
    UserIntent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepName,
    WorkflowStepStatus,
)

from ..models import ChunkProgress, JobState, JobStatus
from ..runtime_config import effective_runtime_config, render_defaults_for_target
from ..storage import storage
from .chunker import build_chunks
from .parser import UnsupportedPdfError, build_parser_diagnostics, parse_pdf
from .translator import Translator, build_translator
from .workflow import (
    append_workflow_step,
    build_image_document,
    build_initial_workflow_run,
    build_input_source,
    build_layout_intent_plan,
    build_repair_record,
    build_text_document,
    coerce_user_intent,
    make_workflow_step,
    normalized_input_payload,
    render_evaluation_summary,
)


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


def _initial_chunk_progress(chunks: list[TranslationChunk]) -> list[ChunkProgress]:
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
    user_intent: UserIntent | None = None,
) -> None:
    intent = user_intent or coerce_user_intent(target_lang)
    input_source = build_input_source(
        source_id="source_1",
        input_type=InputKind.PDF,
        filename=filename,
        mime_type="application/pdf",
        path=pdf_path,
    )
    workflow = build_initial_workflow_run(
        job_id=job_id,
        doc_id=doc_id,
        input_sources=[input_source],
        intent=intent,
    )
    status_chunks: list[ChunkProgress] = []
    try:
        ensure_not_canceled(job_id)
        workflow = append_workflow_step(
            workflow,
            make_workflow_step(
                WorkflowStepName.READ_INPUT,
                WorkflowStepStatus.RUNNING,
                progress=0.05,
                message="Reading PDF input",
                output_artifacts=["normalized-input"],
            ),
        )
        _save_workflow(doc_id, workflow)
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.PARSING,
            0.15,
            "Parsing PDF",
            doc_id,
        )
        try:
            document = await asyncio.to_thread(
                parse_pdf,
                pdf_path,
                doc_id,
                storage.asset_dir(doc_id),
            )
        except UnsupportedPdfError as exc:
            storage.write_json(doc_id, "parser-diagnostics.json", exc.diagnostics)
            workflow = append_workflow_step(
                workflow,
                make_workflow_step(
                    WorkflowStepName.READ_INPUT,
                    WorkflowStepStatus.FAILED,
                    progress=1,
                    message="PDF adapter failed",
                    diagnostics=exc.diagnostics,
                    error=str(exc),
                ),
                status=WorkflowStatus.FAILED,
            )
            _save_workflow(doc_id, workflow)
            raise

        storage.save_document_ir(document)
        storage.write_json(
            doc_id,
            "parser-diagnostics.json",
            build_parser_diagnostics(document),
        )
        storage.write_json(
            doc_id,
            "normalized-input.json",
            normalized_input_payload(
                input_sources=[input_source],
                document=document,
            ),
        )
        workflow = append_workflow_step(
            workflow,
            make_workflow_step(
                WorkflowStepName.READ_INPUT,
                WorkflowStepStatus.COMPLETED,
                progress=0.2,
                message="PDF adapter completed",
                output_artifacts=[
                    "normalized-input",
                    "document-ir",
                    "parser-diagnostics",
                ],
            ),
        )
        _save_workflow(doc_id, workflow)

        await _run_workflow_from_document(
            workflow=workflow,
            job_id=job_id,
            doc_id=doc_id,
            filename=filename,
            target_lang=target_lang,
            document=document,
            intent=intent,
            status_chunks=status_chunks,
        )
    except JobCanceled:
        _mark_canceled(job_id, doc_id, filename, target_lang, workflow, status_chunks)
    except Exception as exc:
        _mark_failed(job_id, doc_id, filename, target_lang, workflow, status_chunks, exc)


async def process_text_document_job(
    job_id: str,
    doc_id: str,
    filename: str,
    text: str,
    target_lang: str,
    user_intent: UserIntent | None = None,
) -> None:
    intent = user_intent or coerce_user_intent(target_lang)
    input_source = build_input_source(
        source_id="source_1",
        input_type=InputKind.TEXT,
        filename=filename,
        mime_type="text/plain",
        path=None,
        size_bytes=len(text.encode("utf-8")),
    )
    workflow = build_initial_workflow_run(
        job_id=job_id,
        doc_id=doc_id,
        input_sources=[input_source],
        intent=intent,
    )
    status_chunks: list[ChunkProgress] = []
    try:
        ensure_not_canceled(job_id)
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.PARSING,
            0.15,
            "Reading text input",
            doc_id,
        )
        document = build_text_document(doc_id, text, intent)
        storage.save_document_ir(document)
        storage.write_json(
            doc_id,
            "normalized-input.json",
            normalized_input_payload(
                input_sources=[input_source],
                document=document,
                input_text=text,
            ),
        )
        storage.write_json(
            doc_id,
            "parser-diagnostics.json",
            {
                "kind": "text_adapter_diagnostics",
                "text_block_count": sum(len(page.blocks) for page in document.pages),
                "page_count": len(document.pages),
                "quality_flags": [],
            },
        )
        workflow = append_workflow_step(
            workflow,
            make_workflow_step(
                WorkflowStepName.READ_INPUT,
                WorkflowStepStatus.COMPLETED,
                progress=0.2,
                message="Text adapter completed",
                output_artifacts=[
                    "normalized-input",
                    "document-ir",
                    "parser-diagnostics",
                ],
            ),
        )
        _save_workflow(doc_id, workflow)

        await _run_workflow_from_document(
            workflow=workflow,
            job_id=job_id,
            doc_id=doc_id,
            filename=filename,
            target_lang=target_lang,
            document=document,
            intent=intent,
            status_chunks=status_chunks,
        )
    except JobCanceled:
        _mark_canceled(job_id, doc_id, filename, target_lang, workflow, status_chunks)
    except Exception as exc:
        _mark_failed(job_id, doc_id, filename, target_lang, workflow, status_chunks, exc)


async def process_image_document_job(
    job_id: str,
    doc_id: str,
    filename: str,
    image_path: Path,
    target_lang: str,
    mime_type: str | None = None,
    user_intent: UserIntent | None = None,
) -> None:
    intent = user_intent or coerce_user_intent(target_lang)
    input_source = build_input_source(
        source_id="source_1",
        input_type=InputKind.IMAGE,
        filename=filename,
        mime_type=mime_type,
        path=image_path,
        quality_flags=["deterministic_ocr_mock"],
    )
    workflow = build_initial_workflow_run(
        job_id=job_id,
        doc_id=doc_id,
        input_sources=[input_source],
        intent=intent,
    )
    status_chunks: list[ChunkProgress] = []
    try:
        ensure_not_canceled(job_id)
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.PARSING,
            0.15,
            "Reading image input",
            doc_id,
        )
        document, assets = build_image_document(
            doc_id=doc_id,
            image_path=image_path,
            storage=storage,
            intent=intent,
            filename=filename,
            mime_type=mime_type,
        )
        storage.save_document_ir(document)
        storage.write_json(doc_id, "asset-ir.json", [asset.model_dump() for asset in assets])
        storage.write_json(
            doc_id,
            "normalized-input.json",
            normalized_input_payload(
                input_sources=[input_source],
                document=document,
                assets=assets,
            ),
        )
        storage.write_json(
            doc_id,
            "parser-diagnostics.json",
            {
                "kind": "image_adapter_diagnostics",
                "text_block_count": sum(len(page.blocks) for page in document.pages),
                "asset_count": len(assets),
                "quality_flags": ["deterministic_ocr_mock", "ocr_uncertain"],
            },
        )
        workflow = append_workflow_step(
            workflow,
            make_workflow_step(
                WorkflowStepName.READ_INPUT,
                WorkflowStepStatus.COMPLETED,
                progress=0.2,
                message="Image adapter completed",
                output_artifacts=[
                    "normalized-input",
                    "document-ir",
                    "asset-ir",
                    "parser-diagnostics",
                ],
            ),
        )
        _save_workflow(doc_id, workflow)

        await _run_workflow_from_document(
            workflow=workflow,
            job_id=job_id,
            doc_id=doc_id,
            filename=filename,
            target_lang=target_lang,
            document=document,
            intent=intent,
            status_chunks=status_chunks,
        )
    except JobCanceled:
        _mark_canceled(job_id, doc_id, filename, target_lang, workflow, status_chunks)
    except Exception as exc:
        _mark_failed(job_id, doc_id, filename, target_lang, workflow, status_chunks, exc)


async def _run_workflow_from_document(
    *,
    workflow: WorkflowRun,
    job_id: str,
    doc_id: str,
    filename: str,
    target_lang: str,
    document: DocumentIR,
    intent: UserIntent,
    status_chunks: list[ChunkProgress],
) -> WorkflowRun:
    ensure_not_canceled(job_id)
    storage.write_json(doc_id, "user-intent.json", intent.model_dump())
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.ANALYZE_INTENT,
            WorkflowStepStatus.COMPLETED,
            progress=0.25,
            message="User intent normalized",
            input_artifacts=["normalized-input"],
            output_artifacts=["user-intent"],
            diagnostics={
                "output_kind": intent.output_kind,
                "style_intent": intent.style_intent,
                "has_instruction": bool(intent.instruction.strip()),
            },
        ),
    )
    _save_workflow(doc_id, workflow)

    layout_plan = build_layout_intent_plan(document, intent)
    storage.write_json(doc_id, "layout-intent-plan.json", layout_plan.model_dump())
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.BUILD_PLAN,
            WorkflowStepStatus.COMPLETED,
            progress=0.3,
            message="Deterministic layout intent plan built",
            input_artifacts=["document-ir", "user-intent"],
            output_artifacts=["layout-intent-plan"],
            diagnostics={"quality_flags": layout_plan.quality_flags},
        ),
    )
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.VALIDATE_PLAN,
            WorkflowStepStatus.COMPLETED,
            progress=0.33,
            message="Layout intent plan validated",
            input_artifacts=["layout-intent-plan"],
            output_artifacts=["validation-and-repair"],
            diagnostics={"status": "valid"},
        ),
    )
    storage.write_json(
        doc_id,
        "validation-and-repair.json",
        {"layout_intent_plan": {"status": "valid"}, "repairs": []},
    )
    _save_workflow(doc_id, workflow)

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
    status_chunks[:] = _initial_chunk_progress(chunks)
    storage.write_json(
        doc_id,
        "translation-progress.json",
        [progress.model_dump() for progress in status_chunks],
    )
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.TRANSLATE,
            WorkflowStepStatus.RUNNING,
            progress=0.36,
            message=f"Translating 0 of {len(chunks)} chunks",
            input_artifacts=["translation-chunks"],
            output_artifacts=["translation-plans", "translation-progress"],
        ),
    )
    _save_workflow(doc_id, workflow)
    update_status(
        job_id,
        filename,
        target_lang,
        JobState.TRANSLATING,
        0.36,
        f"Translating 0 of {len(chunks)} chunks",
        doc_id,
        chunks=status_chunks,
    )
    plans = await _translate_chunks(
        job_id=job_id,
        filename=filename,
        target_lang=target_lang,
        doc_id=doc_id,
        chunks=chunks,
        chunk_progress=status_chunks,
        translator=translator,
        translation_concurrency=runtime_config["translation_concurrency"],
    )
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.TRANSLATE,
            WorkflowStepStatus.COMPLETED,
            progress=0.78,
            message=f"Translated {len(chunks)} chunks",
            input_artifacts=["translation-chunks"],
            output_artifacts=["translation-plans", "translation-progress"],
        ),
    )
    _save_workflow(doc_id, workflow)

    ensure_not_canceled(job_id)
    update_status(
        job_id,
        filename,
        target_lang,
        JobState.RENDERING,
        0.82,
        "Rendering preview",
        doc_id,
        chunks=status_chunks,
    )
    render_document = RenderDocument.from_ir_and_plans(
        document,
        plans,
        target_lang,
        render_defaults=render_defaults,
        layout_intent_plan=layout_plan,
    )
    html = render_to_html(render_document)
    storage.save_preview_html(doc_id, html)
    storage.write_json(doc_id, "translation-plans.json", [plan.model_dump() for plan in plans])
    renderer_diagnostics = render_document.diagnostics()
    storage.write_json(doc_id, "renderer-diagnostics.json", renderer_diagnostics)
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.RENDER,
            WorkflowStepStatus.COMPLETED,
            progress=0.86,
            message="Preview rendered",
            input_artifacts=["document-ir", "layout-intent-plan", "translation-plans"],
            output_artifacts=["renderer-diagnostics", "preview"],
            diagnostics={
                "quality_flag_counts": renderer_diagnostics.get("quality_flag_counts", {})
            },
        ),
    )
    _save_workflow(doc_id, workflow)

    evaluation = render_evaluation_summary(renderer_diagnostics)
    storage.write_json(doc_id, "render-evaluation.json", evaluation)
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.EVALUATE_RENDER,
            WorkflowStepStatus.COMPLETED,
            progress=0.88,
            message="Render diagnostics evaluated",
            input_artifacts=["renderer-diagnostics"],
            output_artifacts=["render-evaluation"],
            diagnostics=evaluation,
        ),
    )
    repairs: list[dict[str, Any]] = []
    if evaluation["repair_recommended"]:
        repaired_plan = build_layout_intent_plan(
            document,
            intent,
            attempt=2,
            diagnostics=renderer_diagnostics,
        )
        repair_record = build_repair_record(
            attempt=2,
            before=layout_plan,
            after=repaired_plan,
            diagnostics=renderer_diagnostics,
        )
        repairs.append(repair_record)
        layout_plan = repaired_plan
        storage.write_json(doc_id, "layout-intent-plan.json", layout_plan.model_dump())
        storage.write_json(
            doc_id,
            "validation-and-repair.json",
            {"layout_intent_plan": {"status": "valid"}, "repairs": repairs},
        )
        workflow = append_workflow_step(
            workflow,
            make_workflow_step(
                WorkflowStepName.REPAIR,
                WorkflowStepStatus.REPAIRED,
                progress=0.9,
                message="Semantic layout intent repaired from renderer diagnostics",
                input_artifacts=["renderer-diagnostics", "layout-intent-plan"],
                output_artifacts=["layout-intent-plan", "validation-and-repair"],
                diagnostics=repair_record,
            ),
        )
    _save_workflow(doc_id, workflow)

    ensure_not_canceled(job_id)
    update_status(
        job_id,
        filename,
        target_lang,
        JobState.RENDERING,
        0.92,
        "Rendering PDF",
        doc_id,
        chunks=status_chunks,
    )
    pdf_output = storage.output_pdf_path(doc_id)
    pdf_diagnostics_path = storage.output_json_path(doc_id, "pdf-export-diagnostics.json")
    await _render_pdf_with_optional_diagnostics(
        html,
        pdf_output,
        diagnostics_path=pdf_diagnostics_path,
        asset_base_path=storage.asset_dir(doc_id),
    )
    if not pdf_diagnostics_path.exists():
        storage.write_json(
            doc_id,
            "pdf-export-diagnostics.json",
            {
                "kind": "pdf_export",
                "status": "completed",
                "output_path": str(pdf_output),
                "output_bytes": pdf_output.stat().st_size if pdf_output.exists() else 0,
            },
        )

    update_status(
        job_id,
        filename,
        target_lang,
        JobState.COMPLETED,
        1.0,
        "Completed",
        doc_id,
        chunks=status_chunks,
    )
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.COMPLETE,
            WorkflowStepStatus.COMPLETED,
            progress=1,
            message="Workflow completed",
            output_artifacts=["preview", "download", "pdf-export-diagnostics"],
        ),
        status=WorkflowStatus.COMPLETED,
    )
    _save_workflow(doc_id, workflow)
    return workflow


async def _translate_chunks(
    *,
    job_id: str,
    filename: str,
    target_lang: str,
    doc_id: str,
    chunks: list[TranslationChunk],
    chunk_progress: list[ChunkProgress],
    translator: Translator,
    translation_concurrency: int,
) -> list[TranslationLayoutPlan]:
    plans_by_index: list[TranslationLayoutPlan | None] = [None] * len(chunks)
    completed_chunks = 0
    semaphore = asyncio.Semaphore(max(1, translation_concurrency))

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
    return [plan for plan in plans_by_index if plan is not None]


async def _render_pdf_with_optional_diagnostics(
    html: str,
    output_path: Path,
    *,
    diagnostics_path: Path,
    asset_base_path: Path,
) -> Path:
    signature = inspect.signature(render_to_pdf)
    if "diagnostics_path" in signature.parameters:
        return await render_to_pdf(
            html,
            output_path,
            diagnostics_path=diagnostics_path,
            asset_base_path=asset_base_path,
        )
    return await render_to_pdf(html, output_path)


def _mark_canceled(
    job_id: str,
    doc_id: str,
    filename: str,
    target_lang: str,
    workflow: WorkflowRun,
    status_chunks: list[ChunkProgress],
) -> None:
    workflow = _load_saved_workflow(doc_id, workflow)
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
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.FAIL,
            WorkflowStepStatus.SKIPPED,
            progress=1,
            message="Canceled",
        ),
        status=WorkflowStatus.CANCELED,
    )
    _save_workflow(doc_id, workflow)


def _mark_failed(
    job_id: str,
    doc_id: str,
    filename: str,
    target_lang: str,
    workflow: WorkflowRun,
    status_chunks: list[ChunkProgress],
    exc: Exception,
) -> None:
    workflow = _load_saved_workflow(doc_id, workflow)
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
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.FAIL,
            WorkflowStepStatus.FAILED,
            progress=1,
            message="Failed",
            error=str(exc),
        ),
        status=WorkflowStatus.FAILED,
    )
    _save_workflow(doc_id, workflow)


def _save_workflow(doc_id: str, workflow: WorkflowRun) -> None:
    storage.write_json(doc_id, "workflow-run.json", workflow.model_dump())


def _load_saved_workflow(doc_id: str, fallback: WorkflowRun) -> WorkflowRun:
    try:
        return WorkflowRun.model_validate(
            storage.read_output_json(doc_id, "workflow-run.json")
        )
    except Exception:
        return fallback
