from __future__ import annotations

import json
from typing import Any

from pdf_translator_schema import (
    DocumentIR,
    LayoutIntentPlan,
    SemanticLayoutAnalysis,
    UserIntent,
)


class LayoutIntelligenceClient:
    def __init__(self, runtime_config: dict[str, Any]) -> None:
        self.runtime_config = runtime_config

    async def analyze_semantics(
        self,
        *,
        document: DocumentIR,
        intent: UserIntent,
        deterministic_analysis: SemanticLayoutAnalysis,
        input_kind: str,
    ) -> SemanticLayoutAnalysis | None:
        if not self._can_call_model(input_kind=input_kind):
            return None
        try:
            model = self._chat_model(
                self.runtime_config.get("vision_analyzer_model")
                if input_kind == "image"
                else self.runtime_config.get("layout_planner_model")
            )
            structured = model.with_structured_output(SemanticLayoutAnalysis)
            response = await structured.ainvoke(
                [
                    (
                        "system",
                        "You identify semantic layout signals. Return only the schema. "
                        "Never include coordinates, page fields, bbox, width, or height.",
                    ),
                    (
                        "user",
                        json.dumps(
                            {
                                "input_kind": input_kind,
                                "target_lang": intent.target_lang,
                                "output_kind": intent.output_kind,
                                "style_intent": intent.style_intent,
                                "instruction": intent.instruction,
                                "document_blocks": [
                                    {
                                        "block_id": block.block_id,
                                        "role": block.role,
                                        "text": block.source_text[:1200],
                                    }
                                    for page in document.pages
                                    for block in sorted(
                                        page.blocks,
                                        key=lambda item: item.reading_order,
                                    )
                                ],
                                "assets": [
                                    {
                                        "asset_id": asset.asset_id,
                                        "kind": asset.kind,
                                        "alt_text": asset.alt_text,
                                    }
                                    for page in document.pages
                                    for asset in page.assets
                                ],
                                "deterministic_baseline": deterministic_analysis.model_dump(
                                    mode="json"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ]
            )
            return _coerce_model_response(response, SemanticLayoutAnalysis)
        except Exception:
            return None

    async def build_layout_plan(
        self,
        *,
        document: DocumentIR,
        intent: UserIntent,
        semantic_analysis: SemanticLayoutAnalysis,
        deterministic_plan: LayoutIntentPlan,
    ) -> LayoutIntentPlan | None:
        if not self._can_call_model(input_kind="text"):
            return None
        try:
            model = self._chat_model(self.runtime_config.get("layout_planner_model"))
            structured = model.with_structured_output(LayoutIntentPlan)
            response = await structured.ainvoke(
                [
                    (
                        "system",
                        "You create semantic typesetting plans. Return only the schema. "
                        "Cover every source block exactly once. Never include coordinates, "
                        "page fields, bbox, width, or height.",
                    ),
                    (
                        "user",
                        json.dumps(
                            {
                                "target_lang": intent.target_lang,
                                "output_kind": intent.output_kind,
                                "style_intent": intent.style_intent,
                                "instruction": intent.instruction,
                                "document_doc_id": document.doc_id,
                                "source_block_ids": list(document.blocks_by_id()),
                                "semantic_analysis": semantic_analysis.model_dump(
                                    mode="json"
                                ),
                                "deterministic_baseline": deterministic_plan.model_dump(
                                    mode="json"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ]
            )
            return _coerce_model_response(response, LayoutIntentPlan)
        except Exception:
            return None

    def _can_call_model(self, *, input_kind: str) -> bool:
        if not self.runtime_config.get("openai_api_key"):
            return False
        if input_kind == "image" and not self.runtime_config.get(
            "agent_enable_vision_analysis"
        ):
            return False
        try:
            import langchain_openai  # noqa: F401
        except Exception:
            return False
        return True

    def _chat_model(self, model_name: object) -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            base_url=str(self.runtime_config.get("openai_base_url", "")).rstrip("/"),
            api_key=str(self.runtime_config.get("openai_api_key", "")),
            model=str(model_name or self.runtime_config.get("openai_model") or ""),
            temperature=0,
        )


def build_layout_intelligence_client(
    runtime_config: dict[str, Any],
) -> LayoutIntelligenceClient:
    return LayoutIntelligenceClient(runtime_config)


def _coerce_model_response(response: Any, model_cls: Any) -> Any:
    if isinstance(response, model_cls):
        return response
    if isinstance(response, dict):
        return model_cls.model_validate(response)
    if hasattr(response, "model_dump"):
        return model_cls.model_validate(response.model_dump())
    return model_cls.model_validate(response)
