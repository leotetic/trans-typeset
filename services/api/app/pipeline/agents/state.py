from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypedDict

from pdf_translator_schema import InputKind, UserIntent

from ...models import ChunkProgress
from ...storage import Storage


class TypesettingGraphState(TypedDict, total=False):
    job_id: str
    doc_id: str
    filename: str
    target_lang: str
    input_kind: Literal["pdf", "text", "image"]
    source_path: str
    content_source_path: str
    layout_source_path: str
    layout_source_filename: str
    input_text: str
    mime_type: str | None
    user_intent: dict[str, Any]
    workflow: dict[str, Any]
    document: dict[str, Any]
    semantic_analysis: dict[str, Any]
    layout_plan: dict[str, Any]
    translation_plans: list[dict[str, Any]]
    chunk_progress: list[dict[str, Any]]
    renderer_diagnostics: dict[str, Any]
    render_evaluation: dict[str, Any]
    preview_html: str
    repairs: list[dict[str, Any]]
    repair_attempt: int
    max_repair_attempts: int
    runtime_config: dict[str, Any]
    error: str


class TypesettingGraphContext:
    def __init__(
        self,
        *,
        storage: Storage,
        update_status: Any,
        ensure_not_canceled: Any,
        mark_canceled: Any,
        mark_failed: Any,
        save_workflow: Any,
        load_saved_workflow: Any,
        parse_pdf: Any,
        build_parser_diagnostics: Any,
        build_formula_diagnostics: Any,
        build_chunks: Any,
        build_translator: Any,
        translate_chunks: Any,
        render_preview_artifacts: Any,
        render_pdf_with_optional_diagnostics: Any,
        workflow_helpers: dict[str, Any],
    ) -> None:
        self.storage = storage
        self.update_status = update_status
        self.ensure_not_canceled = ensure_not_canceled
        self.mark_canceled = mark_canceled
        self.mark_failed = mark_failed
        self.save_workflow = save_workflow
        self.load_saved_workflow = load_saved_workflow
        self.parse_pdf = parse_pdf
        self.build_parser_diagnostics = build_parser_diagnostics
        self.build_formula_diagnostics = build_formula_diagnostics
        self.build_chunks = build_chunks
        self.build_translator = build_translator
        self.translate_chunks = translate_chunks
        self.render_preview_artifacts = render_preview_artifacts
        self.render_pdf_with_optional_diagnostics = render_pdf_with_optional_diagnostics
        self.workflow_helpers = workflow_helpers


def make_initial_graph_state(
    *,
    job_id: str,
    doc_id: str,
    filename: str,
    target_lang: str,
    input_kind: InputKind,
    user_intent: UserIntent,
    max_repair_attempts: int,
    source_path: Path | None = None,
    content_source_path: Path | None = None,
    layout_source_path: Path | None = None,
    layout_source_filename: str | None = None,
    input_text: str = "",
    mime_type: str | None = None,
) -> TypesettingGraphState:
    state: TypesettingGraphState = {
        "job_id": job_id,
        "doc_id": doc_id,
        "filename": filename,
        "target_lang": target_lang,
        "input_kind": input_kind.value,
        "user_intent": user_intent.model_dump(mode="json"),
        "repair_attempt": 0,
        "max_repair_attempts": max(0, max_repair_attempts),
        "repairs": [],
    }
    if source_path is not None:
        state["source_path"] = str(source_path)
    if content_source_path is not None:
        state["content_source_path"] = str(content_source_path)
    if layout_source_path is not None:
        state["layout_source_path"] = str(layout_source_path)
    if layout_source_filename:
        state["layout_source_filename"] = layout_source_filename
    if input_text:
        state["input_text"] = input_text
    if mime_type is not None:
        state["mime_type"] = mime_type
    return state


def chunk_progress_from_state(state: TypesettingGraphState) -> list[ChunkProgress]:
    return [
        ChunkProgress.model_validate(item)
        for item in state.get("chunk_progress", [])
        if isinstance(item, dict)
    ]
