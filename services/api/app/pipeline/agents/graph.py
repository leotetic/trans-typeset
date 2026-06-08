from __future__ import annotations

import sqlite3
from typing import Any

from pdf_translator_schema import InputKind, UserIntent, WorkflowRun

from .nodes import (
    make_nodes,
    route_after_evaluation,
    route_after_read_input,
    route_after_validation,
)
from .state import (
    TypesettingGraphContext,
    TypesettingGraphState,
    make_initial_graph_state,
)


async def run_typesetting_graph(
    *,
    context: TypesettingGraphContext,
    job_id: str,
    doc_id: str,
    filename: str,
    target_lang: str,
    input_kind: InputKind,
    user_intent: UserIntent,
    workflow: WorkflowRun,
    max_repair_attempts: int,
    source_path: Any = None,
    content_source_path: Any = None,
    layout_source_path: Any = None,
    layout_source_filename: str | None = None,
    input_text: str = "",
    mime_type: str | None = None,
) -> TypesettingGraphState:
    graph = build_typesetting_graph(context)
    initial_state = make_initial_graph_state(
        job_id=job_id,
        doc_id=doc_id,
        filename=filename,
        target_lang=target_lang,
        input_kind=input_kind,
        user_intent=user_intent,
        max_repair_attempts=max_repair_attempts,
        source_path=source_path,
        content_source_path=content_source_path,
        layout_source_path=layout_source_path,
        layout_source_filename=layout_source_filename,
        input_text=input_text,
        mime_type=mime_type,
    )
    initial_state["workflow"] = workflow.model_dump(mode="json")
    try:
        return await graph.ainvoke(
            initial_state,
            config={"configurable": {"thread_id": job_id}},
        )
    except Exception as exc:
        saved_workflow = context.load_saved_workflow(doc_id, workflow)
        status_chunks = []
        try:
            from .state import chunk_progress_from_state

            status_chunks = chunk_progress_from_state(initial_state)
        except Exception:
            status_chunks = []
        if exc.__class__.__name__ == "JobCanceled":
            context.mark_canceled(
                job_id,
                doc_id,
                filename,
                target_lang,
                saved_workflow,
                status_chunks,
            )
        else:
            context.mark_failed(
                job_id,
                doc_id,
                filename,
                target_lang,
                saved_workflow,
                status_chunks,
                exc,
            )
        return {**initial_state, "error": str(exc)}


def build_typesetting_graph(context: TypesettingGraphContext) -> Any:
    try:
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, StateGraph
    except Exception as exc:  # pragma: no cover - exercised when deps are absent.
        return _FallbackTypesettingGraph(context, str(exc))

    graph = StateGraph(TypesettingGraphState)
    nodes = make_nodes(context)
    for name, node in nodes.items():
        graph.add_node(name, node)

    graph.add_edge(START, "read_input")
    graph.add_conditional_edges(
        "read_input",
        route_after_read_input,
        {"analyze_intent": "analyze_intent", "end": END},
    )
    graph.add_edge("analyze_intent", "semantic_recognize")
    graph.add_edge("semantic_recognize", "build_layout_plan")
    graph.add_edge("build_layout_plan", "validate_layout_plan")
    graph.add_conditional_edges(
        "validate_layout_plan",
        route_after_validation,
        {
            "translate_chunks": "translate_chunks",
            "render_preview": "render_preview",
            "fail": "fail",
        },
    )
    graph.add_edge("translate_chunks", "render_preview")
    graph.add_edge("render_preview", "evaluate_render")
    graph.add_conditional_edges(
        "evaluate_render",
        route_after_evaluation,
        {
            "repair_layout_plan": "repair_layout_plan",
            "export_pdf": "export_pdf",
            "fail": "fail",
        },
    )
    graph.add_edge("repair_layout_plan", "validate_layout_plan")
    graph.add_edge("export_pdf", "complete")
    graph.add_edge("complete", END)
    graph.add_edge("fail", END)
    return graph.compile(checkpointer=_checkpoint_saver(context, MemorySaver))


def _checkpoint_saver(context: TypesettingGraphContext, memory_saver_cls: Any) -> Any:
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except Exception:
        return memory_saver_cls()

    checkpoint_dir = context.storage.root / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        checkpoint_dir / "langgraph.sqlite",
        check_same_thread=False,
    )
    return SqliteSaver(connection)


class _FallbackTypesettingGraph:
    def __init__(self, context: TypesettingGraphContext, import_error: str) -> None:
        self.context = context
        self.import_error = import_error
        self.nodes = make_nodes(context)

    async def ainvoke(
        self,
        state: TypesettingGraphState,
        config: dict[str, Any] | None = None,
    ) -> TypesettingGraphState:
        state = await _run_node(self.nodes["read_input"], state)
        if route_after_read_input(state) == "end":
            return state
        for name in (
            "analyze_intent",
            "semantic_recognize",
            "build_layout_plan",
            "validate_layout_plan",
            "translate_chunks",
            "render_preview",
            "evaluate_render",
        ):
            state = await _run_node(self.nodes[name], state)
        while route_after_evaluation(state) == "repair_layout_plan":
            state = await _run_node(self.nodes["repair_layout_plan"], state)
            state = await _run_node(self.nodes["validate_layout_plan"], state)
            next_node = route_after_validation(state)
            if next_node == "fail":
                return await _run_node(self.nodes["fail"], state)
            if next_node == "translate_chunks":
                state = await _run_node(self.nodes["translate_chunks"], state)
            for name in ("render_preview", "evaluate_render"):
                state = await _run_node(self.nodes[name], state)
        if route_after_evaluation(state) == "fail":
            return await _run_node(self.nodes["fail"], state)
        state = await _run_node(self.nodes["export_pdf"], state)
        return await _run_node(self.nodes["complete"], state)


async def _run_node(node: Any, state: TypesettingGraphState) -> TypesettingGraphState:
    result = node(state)
    if hasattr(result, "__await__"):
        return await result
    return result
