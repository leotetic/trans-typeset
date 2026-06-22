from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from pdf_translator_schema import (
    DocumentIR,
    LayoutIntentPlan,
    SemanticLayoutAnalysis,
    UserIntent,
)


class LayoutIntelligenceClient:
    def __init__(self, runtime_config: dict[str, Any]) -> None:
        self.runtime_config = runtime_config
        self._intent_diagnostics: dict[str, Any] = {}

    async def analyze_intent(
        self,
        *,
        target_lang: str,
        output_kind: str,
        style_intent: str,
        instruction: str,
        deterministic_intent: UserIntent,
    ) -> UserIntent | None:
        self._intent_diagnostics = {
            "intent_model_used": False,
            "intent_model_provider": "none",
        }
        if not instruction.strip():
            self._intent_diagnostics["intent_model_skip_reason"] = "empty_instruction"
            return None
        if not self._can_call_model(input_kind="text"):
            self._intent_diagnostics["intent_model_error"] = self._model_unavailable_reason(
                input_kind="text"
            )
            return None
        model_name = self._model_name(self.runtime_config.get("layout_planner_model"))
        self._intent_diagnostics["intent_model"] = model_name
        try:
            minimax_config = self._minimax_langchain_config(model_name)
            if minimax_config is not None:
                self._intent_diagnostics["intent_model_provider"] = minimax_config[
                    "provider"
                ]
                self._intent_diagnostics["intent_model"] = minimax_config["model"]
                response = await self._langchain_json_completion(
                    system_prompt=(
                        "Normalize natural-language typesetting intent into one JSON "
                        "object matching UserIntent schema_version 0.2. Preserve "
                        "deterministic defaults unless the instruction explicitly "
                        "changes them. Infer obvious typos and misspellings, for "
                        "example 'double colume' means a two-column layout. Never "
                        "include coordinates, page fields, bbox, width, or height."
                    ),
                    payload={
                        "target_lang": target_lang,
                        "output_kind": output_kind,
                        "style_intent": style_intent,
                        "instruction": instruction,
                        "deterministic_baseline": deterministic_intent.model_dump(
                            mode="json"
                        ),
                        "json_schema": UserIntent.model_json_schema(),
                    },
                    model_cls=UserIntent,
                    model_name=minimax_config["model"],
                    base_url=minimax_config["base_url"],
                    api_key=minimax_config["api_key"],
                    minimax=True,
                )
                self._intent_diagnostics["intent_model_used"] = True
                return _normalize_intent_response(
                    response,
                    target_lang=target_lang,
                    instruction=instruction,
                )

            self._intent_diagnostics[
                "intent_model_provider"
            ] = "openai_compatible_langchain"
            model = self._chat_model(model_name)
            structured = model.with_structured_output(UserIntent)
            response = await structured.ainvoke(
                [
                    (
                        "system",
                        "Normalize natural-language typesetting intent into the "
                        "UserIntent schema_version 0.2 schema. Preserve deterministic defaults unless "
                        "the instruction explicitly changes them. Return only the schema. "
                        "Use task_intent, output_targets, template_profile, and "
                        "bibliography_preference for academic document intent. "
                        "Never include coordinates, page fields, bbox, width, or height.",
                    ),
                    (
                        "user",
                        json.dumps(
                            {
                                "target_lang": target_lang,
                                "output_kind": output_kind,
                                "style_intent": style_intent,
                                "instruction": instruction,
                                "deterministic_baseline": deterministic_intent.model_dump(
                                    mode="json"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ]
            )
            intent = _coerce_model_response(response, UserIntent)
            self._intent_diagnostics["intent_model_used"] = True
            return _normalize_intent_response(
                intent,
                target_lang=target_lang,
                instruction=instruction,
            )
        except Exception as exc:
            self._intent_diagnostics["intent_model_error"] = _sanitize_error(exc)
            return None

    def intent_diagnostics(self) -> dict[str, Any]:
        return dict(self._intent_diagnostics)

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
            model_name = self._model_name(
                self.runtime_config.get("vision_analyzer_model")
                if input_kind == "image"
                else self.runtime_config.get("layout_planner_model")
            )
            minimax_config = self._minimax_langchain_config(model_name)
            payload = {
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
            }
            if minimax_config is not None:
                return await self._langchain_json_completion(
                    system_prompt=(
                        "You identify semantic layout signals and document structure "
                        "candidates. Return only one JSON object matching "
                        "SemanticLayoutAnalysis schema_version 0.2. Never include "
                        "coordinates, page fields, bbox, width, or height."
                    ),
                    payload=payload,
                    model_cls=SemanticLayoutAnalysis,
                    model_name=minimax_config["model"],
                    base_url=minimax_config["base_url"],
                    api_key=minimax_config["api_key"],
                    minimax=True,
                )
            model = self._chat_model(model_name)
            structured = model.with_structured_output(SemanticLayoutAnalysis)
            response = await structured.ainvoke(
                [
                    (
                        "system",
                        "You identify semantic layout signals and document structure candidates. "
                        "Return only the SemanticLayoutAnalysis schema_version 0.2 schema. "
                        "Never include coordinates, page fields, bbox, width, or height.",
                    ),
                    (
                        "user",
                        json.dumps(payload, ensure_ascii=False),
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
            model_name = self._model_name(self.runtime_config.get("layout_planner_model"))
            minimax_config = self._minimax_langchain_config(model_name)
            payload = {
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
            }
            if minimax_config is not None:
                return await self._langchain_json_completion(
                    system_prompt=(
                        "You create semantic typesetting plans. Return only one JSON "
                        "object matching LayoutIntentPlan schema_version 0.2, "
                        "including document_profile, structure_plan, page_setup, "
                        "style_system, numbering_plan, and bibliography_plan when "
                        "useful. Cover every source block exactly once. Never include "
                        "coordinates, page fields, bbox, width, or height."
                    ),
                    payload=payload,
                    model_cls=LayoutIntentPlan,
                    model_name=minimax_config["model"],
                    base_url=minimax_config["base_url"],
                    api_key=minimax_config["api_key"],
                    minimax=True,
                )
            model = self._chat_model(model_name)
            structured = model.with_structured_output(LayoutIntentPlan)
            response = await structured.ainvoke(
                [
                    (
                        "system",
                        "You create semantic typesetting plans. Return only the "
                        "LayoutIntentPlan schema_version 0.2 schema, including document_profile, "
                        "structure_plan, page_setup, style_system, numbering_plan, and "
                        "bibliography_plan when useful. "
                        "Cover every source block exactly once. Never include coordinates, "
                        "page fields, bbox, width, or height.",
                    ),
                    (
                        "user",
                        json.dumps(payload, ensure_ascii=False),
                    ),
                ]
            )
            return _coerce_model_response(response, LayoutIntentPlan)
        except Exception:
            return None

    def _can_call_model(self, *, input_kind: str) -> bool:
        if not self._api_key():
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

    def _chat_model(
        self,
        model_name: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        minimax: bool = False,
    ) -> Any:
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "base_url": (
                base_url or str(self.runtime_config.get("openai_base_url", ""))
            ).rstrip("/"),
            "api_key": api_key or self._api_key(),
            "model": model_name,
            "temperature": 0,
        }
        if minimax:
            kwargs["extra_body"] = _minimax_extra_body(model_name)
            kwargs["disabled_params"] = {"parallel_tool_calls": None}
        return ChatOpenAI(**kwargs)

    async def _langchain_json_completion(
        self,
        *,
        system_prompt: str,
        payload: dict[str, Any],
        model_cls: Any,
        model_name: str,
        base_url: str,
        api_key: str,
        minimax: bool,
    ) -> Any:
        model = self._chat_model(
            model_name,
            base_url=base_url,
            api_key=api_key,
            minimax=minimax,
        )
        response = await model.ainvoke(
            [
                ("system", system_prompt),
                ("user", json.dumps(payload, ensure_ascii=False)),
            ]
        )
        content = _message_content(response)
        return _coerce_model_response(_extract_json_object(content), model_cls)

    def _api_key(self) -> str:
        return str(
            self.runtime_config.get("openai_api_key")
            or self.runtime_config.get("minimax_api_key")
            or ""
        ).strip()

    def _model_name(self, model_name: object) -> str:
        return str(
            model_name
            or self.runtime_config.get("openai_model")
            or self.runtime_config.get("minimax_model")
            or ""
        ).strip()

    def _minimax_langchain_config(self, model_name: str) -> dict[str, str] | None:
        api_key = self._api_key()
        if not api_key:
            return None
        openai_base_url = str(self.runtime_config.get("openai_base_url", "")).rstrip("/")
        if openai_base_url and _is_minimax_provider(openai_base_url, model_name):
            return {
                "provider": "minimax_langchain_json",
                "base_url": openai_base_url,
                "api_key": api_key,
                "model": model_name,
            }
        minimax_api_key = str(self.runtime_config.get("minimax_api_key") or "").strip()
        minimax_model = str(self.runtime_config.get("minimax_model") or model_name).strip()
        minimax_endpoint = str(self.runtime_config.get("minimax_endpoint") or "").strip()
        if minimax_api_key and minimax_endpoint and _is_minimax_provider(
            minimax_endpoint,
            minimax_model,
        ):
            return {
                "provider": "minimax_langchain_json",
                "base_url": _base_url_from_chat_completions_url(minimax_endpoint),
                "api_key": minimax_api_key,
                "model": minimax_model,
            }
        return None

    def _model_unavailable_reason(self, *, input_kind: str) -> str:
        if not self._api_key():
            return "model_api_key_missing"
        if input_kind == "image" and not self.runtime_config.get(
            "agent_enable_vision_analysis"
        ):
            return "vision_analysis_disabled"
        try:
            import langchain_openai  # noqa: F401
        except Exception as exc:
            return f"langchain_openai_unavailable: {_sanitize_error(exc)}"
        return ""


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


def _normalize_intent_response(
    intent: UserIntent,
    *,
    target_lang: str,
    instruction: str,
) -> UserIntent:
    return intent.model_copy(
        update={
            "target_lang": target_lang,
            "instruction": instruction,
        },
        deep=True,
    )


def _message_content(response: Any) -> Any:
    if isinstance(response, dict):
        return response.get("content", response)
    if hasattr(response, "content"):
        return response.content
    return response


def _extract_json_object(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", item.get("content", "")))
            if isinstance(item, dict)
            else str(item)
            for item in content
        )
    if not isinstance(content, str):
        raise ValueError("model response content is not text or JSON")
    text = _strip_thinking_blocks(content).strip()
    fence_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model response JSON is not an object")
    return parsed


def _strip_thinking_blocks(content: str) -> str:
    return re.sub(
        r"<think\b[^>]*>.*?</think>",
        "",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _minimax_extra_body(model_name: str) -> dict[str, Any]:
    extra_body: dict[str, Any] = {"reasoning_split": True}
    if _is_minimax_m3_model(model_name):
        extra_body["thinking"] = {"type": "disabled"}
    return extra_body


def _base_url_from_chat_completions_url(value: str) -> str:
    trimmed = value.strip().rstrip("/")
    suffix = "/chat/completions"
    if trimmed.endswith(suffix):
        return trimmed[: -len(suffix)].rstrip("/")
    return trimmed


def _is_minimax_provider(base_url: str, model: str) -> bool:
    hostname = (urlparse(base_url).hostname or "").lower()
    normalized_model = model.lower()
    return (
        hostname in {"api.minimax.io", "api.minimaxi.com"}
        or hostname.endswith(".minimax.io")
        or hostname.endswith(".minimaxi.com")
        or "minimax-m" in normalized_model
        or "minimax/m" in normalized_model
    )


def _is_minimax_m3_model(model: str) -> bool:
    normalized = model.lower()
    return "minimax-m3" in normalized or "minimax/m3" in normalized


def _sanitize_error(exc: Exception) -> str:
    message = f"{exc.__class__.__name__}: {exc}"
    message = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", message)
    message = re.sub(r"(api[_-]?key[=:]\s*)[A-Za-z0-9._~+/=-]+", r"\1***", message)
    return message[:500]
