from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from pdf_translator_schema import (
    DocumentIR,
    InputKind,
    LayoutIntentPlan,
    SemanticLayoutAnalysis,
    TranslationLayoutPlan,
    UserIntent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepName,
    WorkflowStepStatus,
    validate_layout_intent_plan,
)

from ...models import JobState
from ...runtime_config import effective_runtime_config, render_defaults_for_intent
from ..parser import UnsupportedPdfError
from .llm import build_layout_intelligence_client
from .state import TypesettingGraphContext, TypesettingGraphState


def make_nodes(context: TypesettingGraphContext) -> dict[str, Any]:
    async def read_input(state: TypesettingGraphState) -> TypesettingGraphState:
        return await _read_input(state, context)

    def analyze_intent(state: TypesettingGraphState) -> TypesettingGraphState:
        return _analyze_intent(state, context)

    async def semantic_recognize(state: TypesettingGraphState) -> TypesettingGraphState:
        return await _semantic_recognize(state, context)

    async def build_layout_plan(state: TypesettingGraphState) -> TypesettingGraphState:
        return await _build_layout_plan(state, context)

    def validate_layout_plan(state: TypesettingGraphState) -> TypesettingGraphState:
        return _validate_layout_plan(state, context)

    async def translate_chunks(state: TypesettingGraphState) -> TypesettingGraphState:
        return await _translate_chunks_node(state, context)

    def render_preview(state: TypesettingGraphState) -> TypesettingGraphState:
        return _render_preview(state, context)

    def evaluate_render(state: TypesettingGraphState) -> TypesettingGraphState:
        return _evaluate_render(state, context)

    def repair_layout_plan(state: TypesettingGraphState) -> TypesettingGraphState:
        return _repair_layout_plan(state, context)

    async def export_pdf(state: TypesettingGraphState) -> TypesettingGraphState:
        return await _export_pdf(state, context)

    def complete(state: TypesettingGraphState) -> TypesettingGraphState:
        return _complete(state, context)

    def fail(state: TypesettingGraphState) -> TypesettingGraphState:
        return _fail(state, context)

    return {
        "read_input": read_input,
        "analyze_intent": analyze_intent,
        "semantic_recognize": semantic_recognize,
        "build_layout_plan": build_layout_plan,
        "validate_layout_plan": validate_layout_plan,
        "translate_chunks": translate_chunks,
        "render_preview": render_preview,
        "evaluate_render": evaluate_render,
        "repair_layout_plan": repair_layout_plan,
        "export_pdf": export_pdf,
        "complete": complete,
        "fail": fail,
    }


async def _read_input(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    workflow = _workflow(state)
    job_id = state["job_id"]
    doc_id = state["doc_id"]
    filename = state["filename"]
    target_lang = state["target_lang"]
    intent = _intent(state)
    input_kind = InputKind(state["input_kind"])
    helpers = context.workflow_helpers
    status_chunks = _chunk_progress(state)
    try:
        context.ensure_not_canceled(job_id)
        workflow = helpers["append_workflow_step"](
            workflow,
            helpers["make_workflow_step"](
                WorkflowStepName.READ_INPUT,
                WorkflowStepStatus.RUNNING,
                progress=0.05,
                message=f"Reading {input_kind.value} input",
                output_artifacts=["normalized-input"],
            ),
        )
        context.save_workflow(doc_id, workflow)
        context.update_status(
            job_id,
            filename,
            target_lang,
            JobState.PARSING,
            0.15,
            _read_status_message(input_kind),
            doc_id,
        )
        if input_kind == InputKind.PDF:
            source_path = Path(state.get("content_source_path") or state["source_path"])
            layout_source_path = (
                Path(state["layout_source_path"])
                if state.get("layout_source_path")
                else source_path
            )
            layout_source_fallback = layout_source_path == source_path
            input_source = helpers["build_input_source"](
                source_id="content_source",
                input_type=InputKind.PDF,
                source_role="content",
                filename=filename,
                mime_type="application/pdf",
                path=source_path,
            )
            layout_input_source = helpers["build_input_source"](
                source_id="layout_source",
                input_type=InputKind.PDF,
                source_role="layout_reference",
                filename=state.get("layout_source_filename") or filename,
                mime_type="application/pdf",
                path=layout_source_path,
                quality_flags=[]
                if not layout_source_fallback
                else ["layout_source_fallback_to_content"],
            )
            try:
                parse_started = time.perf_counter()
                document = await asyncio.to_thread(
                    context.parse_pdf,
                    source_path,
                    doc_id,
                    context.storage.asset_dir(doc_id),
                )
                pdf_parse_ms = round((time.perf_counter() - parse_started) * 1000, 2)
            except UnsupportedPdfError as exc:
                context.storage.write_json(doc_id, "parser-diagnostics.json", exc.diagnostics)
                workflow = helpers["append_workflow_step"](
                    workflow,
                    helpers["make_workflow_step"](
                        WorkflowStepName.READ_INPUT,
                        WorkflowStepStatus.FAILED,
                        progress=1,
                        message="PDF adapter failed",
                        diagnostics=exc.diagnostics,
                        error=str(exc),
                    ),
                    status=WorkflowStatus.FAILED,
                )
                context.save_workflow(doc_id, workflow)
                raise
            assets = []
            parser_diagnostics = context.build_parser_diagnostics(document)
            parser_diagnostics["pdf_parse_ms"] = pdf_parse_ms
            context.update_status(
                job_id,
                filename,
                target_lang,
                JobState.PARSING,
                0.17,
                "Recognizing formulas",
                doc_id,
            )
            try:
                formula_started = time.perf_counter()
                formula_result = await context.enrich_document_formulas(
                    document,
                    doc_id=doc_id,
                    pdf_path=source_path,
                    job_id=job_id,
                    filename=filename,
                    target_lang=target_lang,
                )
                formula_enrichment_ms = round(
                    (time.perf_counter() - formula_started) * 1000,
                    2,
                )
                document = formula_result.document
                formula_diagnostics = {
                    **formula_result.diagnostics,
                    "formula_enrichment_ms": formula_enrichment_ms,
                }
                context.storage.write_json(
                    doc_id,
                    "formula-recognition.json",
                    [
                        formula.model_dump(mode="json")
                        for formula in formula_result.formulas
                    ],
                )
                context.storage.write_json(
                    doc_id,
                    "formula-diagnostics.json",
                    formula_diagnostics,
                )
                context.storage.write_json(
                    doc_id,
                    "formula-candidates.json",
                    formula_result.candidates,
                )
                context.storage.write_json(
                    doc_id,
                    "ocr-recognition.json",
                    formula_result.ocr_records,
                )
                context.storage.write_json(
                    doc_id,
                    "ocr-diagnostics.json",
                    formula_result.diagnostics.get("ocr", {}),
                )
                parser_diagnostics["formula_count"] = len(formula_result.formulas)
                parser_diagnostics["formula_candidate_count"] = (
                    formula_result.diagnostics.get("candidate_count", 0)
                )
                parser_diagnostics["formula_enrichment_ms"] = formula_enrichment_ms
                parser_diagnostics["visual_formula_recognition_enabled"] = (
                    formula_result.diagnostics.get(
                        "visual_formula_recognition_enabled",
                        False,
                    )
                )
                parser_diagnostics["formula_recognizer_type"] = (
                    formula_result.diagnostics.get("recognizer_type", "unknown")
                )
                parser_diagnostics["fallback_flags"] = _unique(
                    [
                        *parser_diagnostics.get("fallback_flags", []),
                        *formula_result.diagnostics.get("quality_flags", []),
                    ]
                )
            except Exception as exc:
                context.storage.write_json(
                    doc_id,
                    "formula-recognition.json",
                    [],
                )
                context.storage.write_json(
                    doc_id,
                    "formula-diagnostics.json",
                    {
                        "kind": "formula_diagnostics",
                        "candidate_count": 0,
                        "recognized_count": 0,
                        "formula_enrichment_ms": 0,
                        "recognizer_type": "unknown",
                        "visual_formula_recognition_enabled": False,
                        "quality_flags": ["formula_enrichment_failed"],
                        "error": str(exc),
                    },
                )
                parser_diagnostics["formula_candidate_count"] = 0
                parser_diagnostics["formula_enrichment_ms"] = 0
                parser_diagnostics["visual_formula_recognition_enabled"] = False
                parser_diagnostics["formula_recognizer_type"] = "unknown"
                parser_diagnostics["fallback_flags"] = _unique(
                    [
                        *parser_diagnostics.get("fallback_flags", []),
                        "formula_enrichment_failed",
                    ]
                )
            normalized = helpers["normalized_input_payload"](
                input_sources=[input_source, layout_input_source],
                document=document,
            )
            if layout_source_fallback:
                normalized["quality_flags"] = _unique(
                    [
                        *normalized.get("quality_flags", []),
                        "layout_source_fallback_to_content",
                    ]
                )
            else:
                normalized["layout_reference"] = {
                    "source_id": "layout_source",
                    "filename": state.get("layout_source_filename") or filename,
                    "artifact_path": str(layout_source_path),
                }
            output_artifacts = [
                "normalized-input",
                "document-ir",
                "parser-diagnostics",
                "formula-recognition",
                "formula-diagnostics",
            ]
        elif input_kind == InputKind.TEXT:
            text = state.get("input_text", "")
            input_source = helpers["build_input_source"](
                source_id="source_1",
                input_type=InputKind.TEXT,
                source_role="content",
                filename=filename,
                mime_type="text/plain",
                path=None,
                size_bytes=len(text.encode("utf-8")),
            )
            document = helpers["build_text_document"](doc_id, text, intent)
            assets = []
            parser_diagnostics = {
                "kind": "text_adapter_diagnostics",
                "text_block_count": sum(len(page.blocks) for page in document.pages),
                "page_count": len(document.pages),
                "quality_flags": [],
            }
            normalized = helpers["normalized_input_payload"](
                input_sources=[input_source],
                document=document,
                input_text=text,
            )
            output_artifacts = ["normalized-input", "document-ir", "parser-diagnostics"]
        else:
            source_path = Path(state["source_path"])
            input_source = helpers["build_input_source"](
                source_id="source_1",
                input_type=InputKind.IMAGE,
                source_role="content",
                filename=filename,
                mime_type=state.get("mime_type"),
                path=source_path,
                quality_flags=["deterministic_ocr_mock"],
            )
            document, assets = helpers["build_image_document"](
                doc_id=doc_id,
                image_path=source_path,
                storage=context.storage,
                intent=intent,
                filename=filename,
                mime_type=state.get("mime_type"),
            )
            context.storage.write_json(
                doc_id,
                "asset-ir.json",
                [asset.model_dump(mode="json") for asset in assets],
            )
            parser_diagnostics = {
                "kind": "image_adapter_diagnostics",
                "text_block_count": sum(len(page.blocks) for page in document.pages),
                "asset_count": len(assets),
                "quality_flags": ["deterministic_ocr_mock", "ocr_uncertain"],
            }
            normalized = helpers["normalized_input_payload"](
                input_sources=[input_source],
                document=document,
                assets=assets,
            )
            output_artifacts = [
                "normalized-input",
                "document-ir",
                "asset-ir",
                "parser-diagnostics",
            ]

        context.storage.save_document_ir(document)
        context.storage.write_json(doc_id, "parser-diagnostics.json", parser_diagnostics)
        context.storage.write_json(doc_id, "normalized-input.json", normalized)
        workflow = helpers["append_workflow_step"](
            workflow,
            helpers["make_workflow_step"](
                WorkflowStepName.READ_INPUT,
                WorkflowStepStatus.COMPLETED,
                progress=0.2,
                message=f"{input_kind.value.title()} adapter completed",
                output_artifacts=output_artifacts,
            ),
        )
        context.save_workflow(doc_id, workflow)
        return {
            **state,
            "workflow": workflow.model_dump(mode="json"),
            "document": document.model_dump(mode="json"),
            "chunk_progress": [item.model_dump(mode="json") for item in status_chunks],
        }
    except Exception as exc:
        workflow = context.load_saved_workflow(doc_id, workflow)
        if exc.__class__.__name__ == "JobCanceled":
            context.mark_canceled(
                job_id,
                doc_id,
                filename,
                target_lang,
                workflow,
                status_chunks,
            )
        else:
            context.mark_failed(
                job_id,
                doc_id,
                filename,
                target_lang,
                workflow,
                status_chunks,
                exc,
            )
        return {**state, "workflow": workflow.model_dump(mode="json"), "error": str(exc)}


def _analyze_intent(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    intent = _intent(state)
    doc_id = state["doc_id"]
    helpers = context.workflow_helpers
    context.storage.write_json(doc_id, "user-intent.json", intent.model_dump(mode="json"))
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
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
                "typesetting_standard": intent.typesetting_standard,
            },
        ),
    )
    context.save_workflow(doc_id, workflow)
    return {**state, "workflow": workflow.model_dump(mode="json")}


async def _semantic_recognize(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    document = _document(state)
    intent = _intent(state)
    doc_id = state["doc_id"]
    helpers = context.workflow_helpers
    runtime_config = effective_runtime_config(context.storage)
    deterministic = helpers["build_semantic_layout_analysis"](
        document,
        intent,
        input_kind=InputKind(state["input_kind"]),
    )
    if state["input_kind"] == InputKind.PDF:
        deterministic = deterministic.model_copy(
            update={
                "quality_flags": _unique(
                    [
                        *deterministic.quality_flags,
                        "layout_reference_source_available"
                        if state.get("layout_source_path")
                        else "layout_source_fallback_to_content",
                    ]
                )
            },
            deep=True,
        )
    client = build_layout_intelligence_client(runtime_config)
    model_analysis = await client.analyze_semantics(
        document=document,
        intent=intent,
        deterministic_analysis=deterministic,
        input_kind=state["input_kind"],
    )
    analysis = model_analysis or deterministic
    if model_analysis is None:
        analysis = analysis.model_copy(
            update={
                "quality_flags": _unique(
                    [
                        *analysis.quality_flags,
                        "semantic_model_fallback",
                    ]
                )
            },
            deep=True,
        )
    context.storage.write_json(
        doc_id,
        "semantic-analysis.json",
        analysis.model_dump(mode="json"),
    )
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.SEMANTIC_RECOGNIZE,
            WorkflowStepStatus.COMPLETED,
            progress=0.28,
            message="Semantic layout analysis completed",
            input_artifacts=["document-ir", "user-intent"],
            output_artifacts=["semantic-analysis"],
            diagnostics={
                "confidence": analysis.confidence,
                "quality_flags": analysis.quality_flags,
                "model_used": model_analysis is not None,
            },
        ),
    )
    context.save_workflow(doc_id, workflow)
    return {
        **state,
        "workflow": workflow.model_dump(mode="json"),
        "semantic_analysis": analysis.model_dump(mode="json"),
        "runtime_config": _runtime_config_for_state(runtime_config),
    }


async def _build_layout_plan(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    document = _document(state)
    intent = _intent(state)
    analysis = _semantic_analysis(state)
    doc_id = state["doc_id"]
    helpers = context.workflow_helpers
    runtime_config = effective_runtime_config(context.storage)
    deterministic = helpers["build_layout_intent_plan"](
        document,
        intent,
        semantic_analysis=analysis,
    )
    client = build_layout_intelligence_client(runtime_config)
    model_plan = await client.build_layout_plan(
        document=document,
        intent=intent,
        semantic_analysis=analysis,
        deterministic_plan=deterministic,
    )
    planner_fallback = False
    if model_plan is not None:
        try:
            layout_plan = validate_layout_intent_plan(document, model_plan)
        except Exception:
            layout_plan = deterministic
            planner_fallback = True
    else:
        layout_plan = deterministic
        planner_fallback = True
    if planner_fallback:
        layout_plan = layout_plan.model_copy(
            update={
                "quality_flags": _unique(
                    [*layout_plan.quality_flags, "planner_fallback"]
                )
            },
            deep=True,
        )
    context.storage.write_json(
        doc_id,
        "layout-intent-plan.json",
        layout_plan.model_dump(mode="json"),
    )
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.BUILD_PLAN,
            WorkflowStepStatus.COMPLETED,
            progress=0.3,
            message="Layout intent plan built",
            input_artifacts=["document-ir", "user-intent", "semantic-analysis"],
            output_artifacts=["layout-intent-plan"],
            diagnostics={
                "quality_flags": layout_plan.quality_flags,
                "planner_fallback": planner_fallback,
            },
        ),
    )
    context.save_workflow(doc_id, workflow)
    return {
        **state,
        "workflow": workflow.model_dump(mode="json"),
        "layout_plan": layout_plan.model_dump(mode="json"),
        "runtime_config": _runtime_config_for_state(runtime_config),
    }


def _validate_layout_plan(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    document = _document(state)
    layout_plan = _layout_plan(state)
    doc_id = state["doc_id"]
    helpers = context.workflow_helpers
    repairs = list(state.get("repairs", []))
    validated, validation = helpers["safe_validate_layout_intent_plan"](
        document,
        layout_plan,
    )
    context.storage.write_json(
        doc_id,
        "layout-intent-plan.json",
        validated.model_dump(mode="json"),
    )
    context.storage.write_json(
        doc_id,
        "validation-and-repair.json",
        {"layout_intent_plan": validation, "repairs": repairs},
    )
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.VALIDATE_PLAN,
            WorkflowStepStatus.COMPLETED,
            progress=0.33,
            message="Layout intent plan validated",
            input_artifacts=["layout-intent-plan"],
            output_artifacts=["validation-and-repair"],
            diagnostics=validation,
        ),
    )
    context.save_workflow(doc_id, workflow)
    return {
        **state,
        "workflow": workflow.model_dump(mode="json"),
        "layout_plan": validated.model_dump(mode="json"),
        "repairs": repairs,
    }


async def _translate_chunks_node(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    document = _document(state)
    intent = _intent(state)
    job_id = state["job_id"]
    doc_id = state["doc_id"]
    filename = state["filename"]
    target_lang = state["target_lang"]
    helpers = context.workflow_helpers
    runtime_config = effective_runtime_config(context.storage)
    render_defaults = render_defaults_for_intent(context.storage, target_lang, intent)
    chunks = context.build_chunks(
        document,
        target_lang=target_lang,
        max_chars=runtime_config["translation_chunk_max_chars"],
        render_defaults=render_defaults,
    )
    if not chunks:
        raise ValueError("Document has no translatable chunks")
    context.storage.write_json(
        doc_id,
        "translation-chunks.json",
        [chunk.model_dump(mode="json") for chunk in chunks],
    )
    translator = context.build_translator(
        runtime_config["openai_base_url"],
        runtime_config["openai_api_key"],
        runtime_config["openai_model"],
        runtime_config["translator_max_attempts"],
    )
    status_chunks = helpers["initial_chunk_progress"](chunks)
    context.storage.write_json(
        doc_id,
        "translation-progress.json",
        [progress.model_dump(mode="json") for progress in status_chunks],
    )
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.TRANSLATE,
            WorkflowStepStatus.RUNNING,
            progress=0.36,
            message=f"Translating 0 of {len(chunks)} chunks",
            input_artifacts=["translation-chunks"],
            output_artifacts=["translation-plans", "translation-progress"],
        ),
    )
    context.save_workflow(doc_id, workflow)
    context.update_status(
        job_id,
        filename,
        target_lang,
        JobState.TRANSLATING,
        0.36,
        f"Translating 0 of {len(chunks)} chunks",
        doc_id,
        chunks=status_chunks,
    )
    plans = await context.translate_chunks(
        job_id=job_id,
        filename=filename,
        target_lang=target_lang,
        doc_id=doc_id,
        chunks=chunks,
        chunk_progress=status_chunks,
        translator=translator,
        translation_concurrency=runtime_config["translation_concurrency"],
    )
    context.storage.write_json(
        doc_id,
        "translation-plans.json",
        [plan.model_dump(mode="json") for plan in plans],
    )
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.TRANSLATE,
            WorkflowStepStatus.COMPLETED,
            progress=0.78,
            message=f"Translated {len(chunks)} chunks",
            input_artifacts=["translation-chunks"],
            output_artifacts=["translation-plans", "translation-progress"],
        ),
    )
    context.save_workflow(doc_id, workflow)
    return {
        **state,
        "workflow": workflow.model_dump(mode="json"),
        "translation_plans": [plan.model_dump(mode="json") for plan in plans],
        "chunk_progress": [progress.model_dump(mode="json") for progress in status_chunks],
        "runtime_config": _runtime_config_for_state(runtime_config),
    }


def _render_preview(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    document = _document(state)
    layout_plan = _layout_plan(state)
    plans = _translation_plans(state)
    intent = _intent(state)
    job_id = state["job_id"]
    doc_id = state["doc_id"]
    filename = state["filename"]
    target_lang = state["target_lang"]
    helpers = context.workflow_helpers
    status_chunks = _chunk_progress(state)
    context.ensure_not_canceled(job_id)
    context.update_status(
        job_id,
        filename,
        target_lang,
        JobState.RENDERING,
        0.82,
        "Rendering preview",
        doc_id,
        chunks=status_chunks,
    )
    render_defaults = render_defaults_for_intent(context.storage, target_lang, intent)
    html, renderer_diagnostics = context.render_preview_artifacts(
        doc_id,
        document,
        plans,
        target_lang,
        render_defaults,
        layout_plan,
    )
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.RENDER,
            WorkflowStepStatus.COMPLETED,
            progress=0.86,
            message="Preview rendered",
            input_artifacts=["document-ir", "layout-intent-plan", "translation-plans"],
            output_artifacts=["renderer-diagnostics", "layout-trace", "preview"],
            diagnostics={
                "quality_flag_counts": renderer_diagnostics.get(
                    "quality_flag_counts",
                    {},
                )
            },
        ),
    )
    context.save_workflow(doc_id, workflow)
    return {
        **state,
        "workflow": workflow.model_dump(mode="json"),
        "renderer_diagnostics": renderer_diagnostics,
        "preview_html": html,
    }


def _evaluate_render(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    doc_id = state["doc_id"]
    helpers = context.workflow_helpers
    evaluation = helpers["render_evaluation_summary"](
        state.get("renderer_diagnostics", {})
    )
    context.storage.write_json(doc_id, "render-evaluation.json", evaluation)
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.EVALUATE_RENDER,
            WorkflowStepStatus.COMPLETED,
            progress=0.88,
            message="Render diagnostics evaluated",
            input_artifacts=["renderer-diagnostics"],
            output_artifacts=["render-evaluation"],
            diagnostics=evaluation,
        ),
    )
    context.save_workflow(doc_id, workflow)
    return {
        **state,
        "workflow": workflow.model_dump(mode="json"),
        "render_evaluation": evaluation,
    }


def _repair_layout_plan(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    document = _document(state)
    intent = _intent(state)
    before = _layout_plan(state)
    analysis = _semantic_analysis(state)
    doc_id = state["doc_id"]
    helpers = context.workflow_helpers
    attempt = int(state.get("repair_attempt", 0)) + 1
    repaired = helpers["build_layout_intent_plan"](
        document,
        intent,
        attempt=attempt + 1,
        diagnostics=state.get("renderer_diagnostics", {}),
        semantic_analysis=analysis,
    )
    repair_record = helpers["build_repair_record"](
        attempt=attempt + 1,
        before=before,
        after=repaired,
        diagnostics=state.get("renderer_diagnostics", {}),
    )
    repairs = [*state.get("repairs", []), repair_record]
    context.storage.write_json(
        doc_id,
        "layout-intent-plan.json",
        repaired.model_dump(mode="json"),
    )
    context.storage.write_json(
        doc_id,
        "validation-and-repair.json",
        {"layout_intent_plan": {"status": "valid"}, "repairs": repairs},
    )
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.REPAIR,
            WorkflowStepStatus.REPAIRED,
            progress=0.9,
            attempt=attempt,
            message="Semantic layout intent repaired",
            input_artifacts=["renderer-diagnostics", "layout-intent-plan"],
            output_artifacts=[
                "layout-intent-plan",
                "validation-and-repair",
                "renderer-diagnostics",
                "layout-trace",
                "preview",
            ],
            diagnostics=repair_record,
        ),
    )
    context.save_workflow(doc_id, workflow)
    return {
        **state,
        "workflow": workflow.model_dump(mode="json"),
        "layout_plan": repaired.model_dump(mode="json"),
        "repair_attempt": attempt,
        "repairs": repairs,
    }


async def _export_pdf(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    job_id = state["job_id"]
    doc_id = state["doc_id"]
    filename = state["filename"]
    target_lang = state["target_lang"]
    helpers = context.workflow_helpers
    status_chunks = _chunk_progress(state)
    context.ensure_not_canceled(job_id)
    context.update_status(
        job_id,
        filename,
        target_lang,
        JobState.RENDERING,
        0.92,
        "Rendering PDF",
        doc_id,
        chunks=status_chunks,
    )
    pdf_output = context.storage.output_pdf_path(doc_id)
    pdf_diagnostics_path = context.storage.output_json_path(
        doc_id,
        "pdf-export-diagnostics.json",
    )
    await context.render_pdf_with_optional_diagnostics(
        state.get("preview_html", ""),
        pdf_output,
        diagnostics_path=pdf_diagnostics_path,
        asset_base_path=context.storage.asset_dir(doc_id),
    )
    if not pdf_diagnostics_path.exists():
        context.storage.write_json(
            doc_id,
            "pdf-export-diagnostics.json",
            {
                "kind": "pdf_export",
                "status": "completed",
                "output_path": str(pdf_output),
                "output_bytes": pdf_output.stat().st_size if pdf_output.exists() else 0,
            },
        )
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.EXPORT_PDF,
            WorkflowStepStatus.COMPLETED,
            progress=0.96,
            message="PDF exported",
            input_artifacts=["preview"],
            output_artifacts=["download", "pdf-export-diagnostics"],
        ),
    )
    context.save_workflow(doc_id, workflow)
    return {**state, "workflow": workflow.model_dump(mode="json")}


def _complete(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    if state.get("error"):
        return state
    workflow = _workflow(state)
    job_id = state["job_id"]
    doc_id = state["doc_id"]
    filename = state["filename"]
    target_lang = state["target_lang"]
    helpers = context.workflow_helpers
    status_chunks = _chunk_progress(state)
    context.update_status(
        job_id,
        filename,
        target_lang,
        JobState.COMPLETED,
        1.0,
        "Completed",
        doc_id,
        chunks=status_chunks,
    )
    workflow = helpers["append_workflow_step"](
        workflow,
        helpers["make_workflow_step"](
            WorkflowStepName.COMPLETE,
            WorkflowStepStatus.COMPLETED,
            progress=1,
            message="Workflow completed",
            output_artifacts=["preview", "download", "pdf-export-diagnostics"],
        ),
        status=WorkflowStatus.COMPLETED,
    )
    context.save_workflow(doc_id, workflow)
    return {**state, "workflow": workflow.model_dump(mode="json")}


def _fail(
    state: TypesettingGraphState,
    context: TypesettingGraphContext,
) -> TypesettingGraphState:
    workflow = _workflow(state)
    context.mark_failed(
        state["job_id"],
        state["doc_id"],
        state["filename"],
        state["target_lang"],
        workflow,
        _chunk_progress(state),
        RuntimeError(state.get("error") or "Workflow failed"),
    )
    return state


def route_after_evaluation(state: TypesettingGraphState) -> str:
    if state.get("error"):
        return "fail"
    evaluation = state.get("render_evaluation", {})
    if not isinstance(evaluation, dict):
        return "fail"
    if not evaluation.get("repair_recommended"):
        return "export_pdf"
    attempt = int(state.get("repair_attempt", 0))
    max_attempts = int(state.get("max_repair_attempts", 0))
    if attempt < max_attempts:
        return "repair_layout_plan"
    return "export_pdf"


def route_after_read_input(state: TypesettingGraphState) -> str:
    return "end" if state.get("error") else "analyze_intent"


def route_after_validation(state: TypesettingGraphState) -> str:
    if state.get("error"):
        return "fail"
    if state.get("translation_plans"):
        return "render_preview"
    return "translate_chunks"


def _workflow(state: TypesettingGraphState) -> WorkflowRun:
    return WorkflowRun.model_validate(state["workflow"])


def _intent(state: TypesettingGraphState) -> UserIntent:
    return UserIntent.model_validate(state["user_intent"])


def _document(state: TypesettingGraphState) -> DocumentIR:
    return DocumentIR.model_validate(state["document"])


def _semantic_analysis(state: TypesettingGraphState) -> SemanticLayoutAnalysis:
    return SemanticLayoutAnalysis.model_validate(state["semantic_analysis"])


def _layout_plan(state: TypesettingGraphState) -> LayoutIntentPlan:
    return LayoutIntentPlan.model_validate(state["layout_plan"])


def _translation_plans(state: TypesettingGraphState) -> list[TranslationLayoutPlan]:
    return [
        TranslationLayoutPlan.model_validate(plan)
        for plan in state.get("translation_plans", [])
    ]


def _chunk_progress(state: TypesettingGraphState) -> list[Any]:
    from .state import chunk_progress_from_state

    return chunk_progress_from_state(state)


def _read_status_message(input_kind: InputKind) -> str:
    if input_kind == InputKind.PDF:
        return "Parsing PDF"
    if input_kind == InputKind.TEXT:
        return "Reading text input"
    return "Reading image input"


def _runtime_config_for_state(runtime_config: dict[str, Any]) -> dict[str, Any]:
    result = dict(runtime_config)
    render_defaults = result.get("render_defaults")
    if hasattr(render_defaults, "model_dump"):
        result["render_defaults"] = render_defaults.model_dump(mode="json")
    if result.get("openai_api_key"):
        result["openai_api_key"] = "***"
    return result


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result
