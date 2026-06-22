import asyncio
import json
import sys

import pytest
from app.pipeline.agents.llm import build_layout_intelligence_client
from app.pipeline.workflow import build_layout_intent_plan, build_semantic_layout_analysis
from pdf_translator_schema import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    LayoutIntentPlan,
    PageSize,
    SemanticLayoutAnalysis,
    UserIntent,
    validate_layout_intent_plan,
)
from pdf_translator_schema.models import DocumentBlock


def _document() -> DocumentIR:
    return DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.TITLE,
                        bbox=BoundingBox(x0=10, y0=10, x1=260, y1=40),
                        reading_order=0,
                        source_text="A Semantic Agent Loop",
                    ),
                    DocumentBlock(
                        block_id="b2",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=60, x1=260, y1=120),
                        reading_order=1,
                        source_text="The agent converts semantic intent into schema.",
                    ),
                ],
            )
        ],
    )


def test_layout_intelligence_client_coerces_structured_output_without_real_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    intent = UserIntent(target_lang="zh-CN")
    deterministic_analysis = build_semantic_layout_analysis(
        document,
        intent,
        input_kind="text",
    )
    deterministic_plan = build_layout_intent_plan(
        document,
        intent,
        semantic_analysis=deterministic_analysis,
    )
    calls: list[dict] = []

    class FakeStructuredModel:
        def __init__(self, schema_cls):
            self.schema_cls = schema_cls

        async def ainvoke(self, messages):
            calls.append({"schema": self.schema_cls, "messages": messages})
            if self.schema_cls is UserIntent:
                return {
                    "schema_version": "0.2",
                    "target_lang": "zh-CN",
                    "output_kind": "typeset_document",
                    "style_intent": "academic",
                    "instruction": "Please use two columns.",
                    "column_layout": {"column_count": 2},
                    "task_intent": {"document_kind": "course_paper"},
                    "output_targets": [
                        {"format": "html_preview", "artifact_name": "preview.html"},
                        {"format": "pdf", "artifact_name": "translated.pdf"},
                    ],
                }
            if self.schema_cls is SemanticLayoutAnalysis:
                return {
                    "schema_version": "0.2",
                    "analysis_id": "doc_1_semantic_model",
                    "doc_id": "doc_1",
                    "target_lang": "zh-CN",
                    "block_signals": [
                        {
                            "source_block_id": "b1",
                            "role_candidates": ["title"],
                            "confidence": 0.91,
                        },
                        {
                            "source_block_id": "b2",
                            "role_candidates": ["paragraph"],
                            "confidence": 0.87,
                        },
                    ],
                    "structure_candidates": [
                        {
                            "section_id": "title_01",
                            "kind": "title",
                            "source_block_ids": ["b1"],
                        },
                        {
                            "section_id": "body_01",
                            "kind": "body",
                            "source_block_ids": ["b2"],
                        },
                    ],
                    "block_section_mappings": [
                        {
                            "source_block_id": "b1",
                            "section_id": "title_01",
                            "section_kind": "title",
                        },
                        {
                            "source_block_id": "b2",
                            "section_id": "body_01",
                            "section_kind": "body",
                        },
                    ],
                    "quality_flags": ["model_semantic_analysis"],
                }
            return {
                "schema_version": "0.2",
                "plan_id": "doc_1_model_plan",
                "doc_id": "doc_1",
                "target_lang": "zh-CN",
                "structure_plan": {
                    "sections": [
                        {
                            "section_id": "title_01",
                            "kind": "title",
                            "source_block_ids": ["b1"],
                        },
                        {
                            "section_id": "body_01",
                            "kind": "body",
                            "source_block_ids": ["b2"],
                        },
                    ]
                },
                "blocks": [
                    {
                        "source_block_id": "b1",
                        "role": "title",
                        "priority": 5,
                        "render_intent": "emphasis",
                    },
                    {
                        "source_block_id": "b2",
                        "role": "paragraph",
                        "priority": 3,
                        "render_intent": "normal",
                    },
                ],
                "quality_flags": ["model_layout_plan"],
            }

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            calls.append({"chat_kwargs": kwargs})

        def with_structured_output(self, schema_cls):
            return FakeStructuredModel(schema_cls)

    class FakeLangChainOpenAI:
        ChatOpenAI = FakeChatOpenAI

    monkeypatch.setitem(sys.modules, "langchain_openai", FakeLangChainOpenAI)
    client = build_layout_intelligence_client(
        {
            "openai_base_url": "https://example.test/v1",
            "openai_api_key": "fake-route-key",
            "openai_model": "fake-model",
            "layout_planner_model": "fake-layout-model",
            "vision_analyzer_model": "fake-vision-model",
            "agent_enable_vision_analysis": False,
        }
    )

    normalized_intent = asyncio.run(
        client.analyze_intent(
            target_lang="zh-CN",
            output_kind="typeset_document",
            style_intent="academic",
            instruction="Please use two columns.",
            deterministic_intent=intent,
        )
    )
    assert normalized_intent is not None
    assert isinstance(normalized_intent, UserIntent)
    assert normalized_intent.schema_version == "0.2"
    assert normalized_intent.column_layout.column_count == 2
    assert normalized_intent.task_intent.document_kind == "course_paper"

    semantic_analysis = asyncio.run(
        client.analyze_semantics(
            document=document,
            intent=intent,
            deterministic_analysis=deterministic_analysis,
            input_kind="text",
        )
    )
    plan = asyncio.run(
        client.build_layout_plan(
            document=document,
            intent=intent,
            semantic_analysis=semantic_analysis,
            deterministic_plan=deterministic_plan,
        )
    )

    assert semantic_analysis is not None
    assert plan is not None
    assert isinstance(semantic_analysis, SemanticLayoutAnalysis)
    assert isinstance(plan, LayoutIntentPlan)
    assert semantic_analysis.schema_version == "0.2"
    assert plan.schema_version == "0.2"
    assert plan.structure_plan.sections[0].section_id == "title_01"
    assert validate_layout_intent_plan(document, plan) == plan
    assert [block.source_block_id for block in plan.blocks] == ["b1", "b2"]
    assert all(
        call["chat_kwargs"]["api_key"] == "fake-route-key"
        for call in calls
        if "chat_kwargs" in call
    )
    assert {call["schema"] for call in calls if "schema" in call} == {
        UserIntent,
        SemanticLayoutAnalysis,
        LayoutIntentPlan,
    }


def test_layout_intelligence_client_uses_minimax_langchain_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    deterministic_intent = UserIntent(
        target_lang="zh-CN",
        output_kind="typeset_document",
        style_intent="academic",
        instruction="double colume",
    )
    deterministic_analysis = build_semantic_layout_analysis(
        document,
        deterministic_intent,
        input_kind="text",
    )
    deterministic_plan = build_layout_intent_plan(
        document,
        deterministic_intent,
        semantic_analysis=deterministic_analysis,
    )
    calls: list[dict] = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            calls.append({"chat_kwargs": kwargs})

        def with_structured_output(self, _schema_cls):
            raise AssertionError("MiniMax should use plain LangChain JSON invocation")

        async def ainvoke(self, messages):
            calls.append({"messages": messages})
            user_payload = json.loads(messages[1][1])
            if "json_schema" in user_payload:
                return {
                    "content": json.dumps(
                        {
                            "schema_version": "0.2",
                            "target_lang": "zh-CN",
                            "output_kind": "typeset_document",
                            "style_intent": "academic",
                            "instruction": "double colume",
                            "column_layout": {"column_count": 2},
                        }
                    )
                }
            if "source_block_ids" in user_payload:
                return {
                    "content": json.dumps(
                        {
                            "schema_version": "0.2",
                            "plan_id": "doc_1_model_plan",
                            "doc_id": "doc_1",
                            "target_lang": "zh-CN",
                            "blocks": [
                                {
                                    "source_block_id": "b1",
                                    "role": "title",
                                    "priority": 5,
                                    "render_intent": "emphasis",
                                },
                                {
                                    "source_block_id": "b2",
                                    "role": "paragraph",
                                    "priority": 3,
                                    "render_intent": "normal",
                                },
                            ],
                        }
                    )
                }
            return {
                "content": json.dumps(
                    {
                        "schema_version": "0.2",
                        "analysis_id": "doc_1_model_analysis",
                        "doc_id": "doc_1",
                        "target_lang": "zh-CN",
                        "quality_flags": ["model_semantic_analysis"],
                    }
                )
            }

    class FakeLangChainOpenAI:
        ChatOpenAI = FakeChatOpenAI

    monkeypatch.setitem(sys.modules, "langchain_openai", FakeLangChainOpenAI)
    client = build_layout_intelligence_client(
        {
            "openai_base_url": "https://api.minimaxi.com/v1",
            "openai_api_key": "fake-key",
            "openai_model": "MiniMax-M3",
            "layout_planner_model": "MiniMax-M3",
            "vision_analyzer_model": "MiniMax-M3",
            "agent_enable_vision_analysis": False,
        }
    )

    normalized_intent = asyncio.run(
        client.analyze_intent(
            target_lang="zh-CN",
            output_kind="typeset_document",
            style_intent="academic",
            instruction="double colume",
            deterministic_intent=deterministic_intent,
        )
    )
    semantic_analysis = asyncio.run(
        client.analyze_semantics(
            document=document,
            intent=normalized_intent,
            deterministic_analysis=deterministic_analysis,
            input_kind="text",
        )
    )
    layout_plan = asyncio.run(
        client.build_layout_plan(
            document=document,
            intent=normalized_intent,
            semantic_analysis=semantic_analysis,
            deterministic_plan=deterministic_plan,
        )
    )

    assert normalized_intent is not None
    assert normalized_intent.column_layout.column_count == 2
    assert semantic_analysis is not None
    assert semantic_analysis.quality_flags == ["model_semantic_analysis"]
    assert layout_plan is not None
    assert [block.source_block_id for block in layout_plan.blocks] == ["b1", "b2"]
    chat_kwargs = [call["chat_kwargs"] for call in calls if "chat_kwargs" in call]
    assert len(chat_kwargs) == 3
    assert all(
        kwargs["base_url"] == "https://api.minimaxi.com/v1"
        for kwargs in chat_kwargs
    )
    assert all(kwargs["model"] == "MiniMax-M3" for kwargs in chat_kwargs)
    assert all(kwargs["extra_body"]["reasoning_split"] is True for kwargs in chat_kwargs)
    assert all(
        kwargs["extra_body"]["thinking"] == {"type": "disabled"}
        for kwargs in chat_kwargs
    )
    assert all(
        kwargs["disabled_params"] == {"parallel_tool_calls": None}
        for kwargs in chat_kwargs
    )
    diagnostics = client.intent_diagnostics()
    assert diagnostics["intent_model_used"] is True
    assert diagnostics["intent_model_provider"] == "minimax_langchain_json"


def test_layout_intelligence_client_records_minimax_intent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeChatOpenAI:
        def __init__(self, **_kwargs):
            pass

        async def ainvoke(self, _messages):
            return {"content": "not json"}

    class FakeLangChainOpenAI:
        ChatOpenAI = FakeChatOpenAI

    monkeypatch.setitem(sys.modules, "langchain_openai", FakeLangChainOpenAI)
    client = build_layout_intelligence_client(
        {
            "openai_base_url": "https://api.minimaxi.com/v1",
            "openai_api_key": "fake-key",
            "openai_model": "MiniMax-M3",
            "layout_planner_model": "MiniMax-M3",
        }
    )

    normalized_intent = asyncio.run(
        client.analyze_intent(
            target_lang="zh-CN",
            output_kind="typeset_document",
            style_intent="academic",
            instruction="double colume",
            deterministic_intent=UserIntent(
                target_lang="zh-CN",
                output_kind="typeset_document",
                style_intent="academic",
                instruction="double colume",
            ),
        )
    )

    assert normalized_intent is None
    diagnostics = client.intent_diagnostics()
    assert diagnostics["intent_model_used"] is False
    assert diagnostics["intent_model_provider"] == "minimax_langchain_json"
    assert "intent_model_error" in diagnostics


def test_layout_intent_ignores_vector_placeholder_assets_by_default() -> None:
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=60, x1=260, y1=120),
                        reading_order=0,
                        source_text="Body text.",
                    )
                ],
                assets=[
                    Asset(
                        asset_id="vector_1",
                        page_id="p1",
                        kind="figure",
                        bbox=BoundingBox(x0=30, y0=140, x1=250, y1=200),
                        alt_text="PDF vector drawing placeholder",
                    ),
                    Asset(
                        asset_id="figure_1",
                        page_id="p1",
                        kind="image",
                        bbox=BoundingBox(x0=30, y0=220, x1=250, y1=320),
                        path="/api/documents/doc_1/assets/figure_1.png",
                    ),
                ],
            )
        ],
    )

    plan = build_layout_intent_plan(document, UserIntent(target_lang="zh-CN"))
    assets = {asset.asset_id: asset for asset in plan.assets}

    assert assets["vector_1"].usage == "ignore"
    assert "asset_suppressed_placeholder" in assets["vector_1"].quality_flags
    assert assets["figure_1"].usage == "preserve"
