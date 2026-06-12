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
from ..runtime_config import effective_runtime_config, render_defaults_for_intent
from ..storage import storage
from .agents import run_typesetting_graph
from .agents.state import TypesettingGraphContext
from .chunker import build_chunks
from .formula_processing import build_formula_diagnostics
from .formulas import (
    DeterministicFormulaRecognizer,
    OpenAIFormulaRecognizer,
    enrich_document_formulas,
)
from .ocr import DeterministicOCRProvider, OCRService, OpenAIVisionOCRProvider, Pix2TextOCRProvider
from .parser import UnsupportedPdfError, build_parser_diagnostics, parse_pdf
from .translator import Translator, build_translator
from .workflow import (
    append_workflow_step,
    build_image_document,
    build_initial_workflow_run,
    build_input_source,
    build_layout_intent_plan,
    build_repair_record,
    build_semantic_layout_analysis,
    build_text_document,
    coerce_user_intent,
    make_workflow_step,
    normalized_input_payload,
    render_evaluation_summary,
    safe_validate_layout_intent_plan,
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
    layout_pdf_path: Path | None = None,
    layout_filename: str | None = None,
) -> None:
    intent = user_intent or coerce_user_intent(target_lang)
    input_source = build_input_source(
        source_id="content_source",
        input_type=InputKind.PDF,
        source_role="content",
        filename=filename,
        mime_type="application/pdf",
        path=pdf_path,
    )
    layout_input_source = build_input_source(
        source_id="layout_source",
        input_type=InputKind.PDF,
        source_role="layout_reference",
        filename=layout_filename or filename,
        mime_type="application/pdf",
        path=layout_pdf_path or pdf_path,
        quality_flags=[] if layout_pdf_path is not None else ["layout_source_fallback_to_content"],
    )
    workflow = build_initial_workflow_run(
        job_id=job_id,
        doc_id=doc_id,
        input_sources=[input_source, layout_input_source],
        intent=intent,
    )
    await _run_graph_job(
        workflow=workflow,
        job_id=job_id,
        doc_id=doc_id,
        filename=filename,
        target_lang=target_lang,
        input_kind=InputKind.PDF,
        intent=intent,
        source_path=pdf_path,
        content_source_path=pdf_path,
        layout_source_path=layout_pdf_path,
        layout_source_filename=layout_filename,
    )


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
        source_role="content",
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
    await _run_graph_job(
        workflow=workflow,
        job_id=job_id,
        doc_id=doc_id,
        filename=filename,
        target_lang=target_lang,
        input_kind=InputKind.TEXT,
        intent=intent,
        input_text=text,
    )


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
        source_role="content",
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
    await _run_graph_job(
        workflow=workflow,
        job_id=job_id,
        doc_id=doc_id,
        filename=filename,
        target_lang=target_lang,
        input_kind=InputKind.IMAGE,
        intent=intent,
        source_path=image_path,
        mime_type=mime_type,
    )


async def _run_graph_job(
    *,
    workflow: WorkflowRun,
    job_id: str,
    doc_id: str,
    filename: str,
    target_lang: str,
    input_kind: InputKind,
    intent: UserIntent,
    source_path: Path | None = None,
    content_source_path: Path | None = None,
    layout_source_path: Path | None = None,
    layout_source_filename: str | None = None,
    input_text: str = "",
    mime_type: str | None = None,
) -> None:
    runtime_config = effective_runtime_config(storage)
    await run_typesetting_graph(
        context=_graph_context(),
        job_id=job_id,
        doc_id=doc_id,
        filename=filename,
        target_lang=target_lang,
        input_kind=input_kind,
        user_intent=intent,
        workflow=workflow,
        max_repair_attempts=runtime_config["agent_max_repair_attempts"],
        source_path=source_path,
        content_source_path=content_source_path,
        layout_source_path=layout_source_path,
        layout_source_filename=layout_source_filename,
        input_text=input_text,
        mime_type=mime_type,
    )


def _graph_context() -> TypesettingGraphContext:
    return TypesettingGraphContext(
        storage=storage,
        update_status=update_status,
        ensure_not_canceled=ensure_not_canceled,
        mark_canceled=_mark_canceled,
        mark_failed=_mark_failed,
        save_workflow=_save_workflow,
        load_saved_workflow=_load_saved_workflow,
        parse_pdf=parse_pdf,
        build_parser_diagnostics=build_parser_diagnostics,
        enrich_document_formulas=_enrich_document_formulas,
        build_formula_diagnostics=build_formula_diagnostics,
        build_chunks=build_chunks,
        build_translator=build_translator,
        translate_chunks=_translate_chunks,
        render_preview_artifacts=_render_preview_artifacts,
        render_pdf_with_optional_diagnostics=_render_pdf_with_optional_diagnostics,
        workflow_helpers={
            "append_workflow_step": append_workflow_step,
            "build_image_document": build_image_document,
            "build_initial_workflow_run": build_initial_workflow_run,
            "build_input_source": build_input_source,
            "build_layout_intent_plan": build_layout_intent_plan,
            "build_repair_record": build_repair_record,
            "build_semantic_layout_analysis": build_semantic_layout_analysis,
            "build_text_document": build_text_document,
            "initial_chunk_progress": _initial_chunk_progress,
            "make_workflow_step": make_workflow_step,
            "normalized_input_payload": normalized_input_payload,
            "render_evaluation_summary": render_evaluation_summary,
            "safe_validate_layout_intent_plan": safe_validate_layout_intent_plan,
        },
    )


async def _enrich_document_formulas(
    document: DocumentIR,
    *,
    doc_id: str,
    pdf_path: Path | None = None,
    job_id: str | None = None,
    filename: str = "",
    target_lang: str = "",
) -> Any:
    runtime_config = effective_runtime_config(storage)
    provider_order = [str(provider) for provider in runtime_config.get("ocr_provider_order", [])]
    formula_openai_enabled = bool(
        runtime_config["openai_api_key"]
        and "openai_vision" in provider_order
    )
    openai_ocr_provider: Any | None = None
    recognizer = DeterministicFormulaRecognizer()
    recognizer_type = "deterministic"
    if formula_openai_enabled:
        openai_ocr_provider = OpenAIVisionOCRProvider(
            OpenAIFormulaRecognizer(
                base_url=runtime_config["openai_base_url"],
                api_key=runtime_config["openai_api_key"],
                model=runtime_config["vision_analyzer_model"],
                asset_base_path=storage.asset_dir(doc_id),
            )
        )
    provider_factories: dict[str, Any] = {
        "pix2text": lambda: Pix2TextOCRProvider(
            timeout_seconds=runtime_config.get("ocr_provider_timeout_seconds", 12.0)
        ),
        "deterministic": DeterministicOCRProvider,
    }
    ocr_providers: list[Any] = []
    for provider_name in provider_order:
        if provider_name == "openai_vision":
            if openai_ocr_provider is not None:
                ocr_providers.append(openai_ocr_provider)
            continue
        factory = provider_factories.get(str(provider_name))
        if factory is not None:
            ocr_providers.append(factory())
    if not any(getattr(provider, "name", "") == "deterministic" for provider in ocr_providers):
        ocr_providers.append(DeterministicOCRProvider())
    live_ocr_records: list[dict[str, Any]] = []

    def record_ocr_attempt(record: dict[str, Any]) -> None:
        live_ocr_records.append(record)
        storage.write_json(doc_id, "ocr-recognition.json", live_ocr_records)

    def update_formula_progress(index: int, total: int, _candidate: Any) -> None:
        if not job_id:
            return
        update_status(
            job_id,
            filename,
            target_lang,
            JobState.PARSING,
            0.17,
            f"Recognizing formulas {index}/{total}",
            doc_id,
        )

    ocr_service = OCRService(
        providers=ocr_providers,
        asset_base_path=storage.asset_dir(doc_id),
        min_confidence=runtime_config.get("ocr_min_confidence", 0.35),
        provider_timeout_seconds=runtime_config.get("ocr_provider_timeout_seconds", 12.0),
        max_visual_candidates=runtime_config.get("ocr_max_visual_candidates", 12),
        on_record=record_ocr_attempt,
    )
    return await enrich_document_formulas(
        document,
        doc_id=doc_id,
        asset_output_dir=storage.asset_dir(doc_id),
        pdf_path=pdf_path,
        recognizer=recognizer,
        ocr_service=ocr_service,
        recognizer_type=recognizer_type,
        visual_formula_recognition_enabled=any(
            getattr(provider, "name", "") in {"pix2text", "openai_vision"}
            for provider in ocr_providers
        ),
        on_progress=update_formula_progress,
    )


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
    render_defaults = render_defaults_for_intent(storage, target_lang, intent)
    chunks = build_chunks(
        document,
        target_lang=target_lang,
        max_chars=runtime_config["translation_chunk_max_chars"],
        render_defaults=render_defaults,
    )
    if not chunks:
        raise ValueError("Document has no translatable chunks")
    storage.write_json(doc_id, "translation-chunks.json", [chunk.model_dump() for chunk in chunks])
    storage.write_json(doc_id, "formula-diagnostics.json", build_formula_diagnostics(document))
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
    storage.write_json(doc_id, "translation-plans.json", [plan.model_dump() for plan in plans])
    html, renderer_diagnostics = _render_preview_artifacts(
        doc_id,
        document,
        plans,
        target_lang,
        render_defaults,
        layout_plan,
    )
    workflow = append_workflow_step(
        workflow,
        make_workflow_step(
            WorkflowStepName.RENDER,
            WorkflowStepStatus.COMPLETED,
            progress=0.86,
            message="Preview rendered",
            input_artifacts=["document-ir", "layout-intent-plan", "translation-plans"],
            output_artifacts=["renderer-diagnostics", "layout-trace", "preview"],
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
        html, renderer_diagnostics = _render_preview_artifacts(
            doc_id,
            document,
            plans,
            target_lang,
            render_defaults,
            layout_plan,
        )
        evaluation = render_evaluation_summary(renderer_diagnostics)
        storage.write_json(doc_id, "render-evaluation.json", evaluation)
        workflow = append_workflow_step(
            workflow,
            make_workflow_step(
                WorkflowStepName.REPAIR,
                WorkflowStepStatus.REPAIRED,
                progress=0.9,
                message="Semantic layout intent repaired and preview re-rendered",
                input_artifacts=["renderer-diagnostics", "layout-intent-plan"],
                output_artifacts=[
                    "layout-intent-plan",
                    "validation-and-repair",
                    "renderer-diagnostics",
                    "layout-trace",
                    "preview",
                ],
                diagnostics={
                    **repair_record,
                    "post_repair_evaluation": evaluation,
                },
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
    translation_diagnostics: list[dict[str, Any]] = []

    def drain_translation_diagnostics() -> None:
        drain_diagnostics = getattr(translator, "drain_diagnostics", None)
        if not callable(drain_diagnostics):
            return
        diagnostics = drain_diagnostics()
        if not diagnostics:
            return
        translation_diagnostics.extend(diagnostics)
        storage.write_json(doc_id, "translation-diagnostics.json", translation_diagnostics)

    def diagnostic_quality_flags_for_chunk(chunk_id: str) -> list[str]:
        flags: list[str] = []
        seen: set[str] = set()
        for diagnostic in translation_diagnostics:
            if diagnostic.get("chunk_id") != chunk_id:
                continue
            for flag in diagnostic.get("quality_flags", []):
                if isinstance(flag, str) and flag and flag not in seen:
                    flags.append(flag)
                    seen.add(flag)
        return flags

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
                drain_translation_diagnostics()
                ensure_not_canceled(job_id)
        except JobCanceled:
            drain_translation_diagnostics()
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
            drain_translation_diagnostics()
            progress_entry.status = "failed"
            progress_entry.progress = 1
            progress_entry.message = "Failed"
            progress_entry.error = str(exc)
            progress_entry.quality_flags = diagnostic_quality_flags_for_chunk(
                chunk.chunk_id
            )
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


def _render_preview_artifacts(
    doc_id: str,
    document: DocumentIR,
    plans: list[TranslationLayoutPlan],
    target_lang: str,
    render_defaults: Any,
    layout_plan: Any,
) -> tuple[str, dict[str, Any]]:
    render_document = RenderDocument.from_ir_and_plans(
        document,
        plans,
        target_lang,
        render_defaults=render_defaults,
        layout_intent_plan=layout_plan,
    )
    html = render_to_html(render_document)
    storage.save_preview_html(doc_id, html)
    renderer_diagnostics = render_document.diagnostics()
    storage.write_json(doc_id, "renderer-diagnostics.json", renderer_diagnostics)
    storage.write_json(doc_id, "layout-trace.json", render_document.layout_trace)
    return html, renderer_diagnostics


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
    if not status_chunks:
        status_chunks = _load_saved_chunk_progress(doc_id)
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


def _load_saved_chunk_progress(doc_id: str) -> list[ChunkProgress]:
    try:
        payload = storage.read_output_json(doc_id, "translation-progress.json")
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    progress: list[ChunkProgress] = []
    for item in payload:
        try:
            progress.append(ChunkProgress.model_validate(item))
        except Exception:
            continue
    return progress


def _save_workflow(doc_id: str, workflow: WorkflowRun) -> None:
    storage.write_json(doc_id, "workflow-run.json", workflow.model_dump())


def _load_saved_workflow(doc_id: str, fallback: WorkflowRun) -> WorkflowRun:
    try:
        return WorkflowRun.model_validate(
            storage.read_output_json(doc_id, "workflow-run.json")
        )
    except Exception:
        return fallback
