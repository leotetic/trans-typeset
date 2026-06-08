import asyncio
import sys

import pytest
from app.pipeline.agents.llm import build_layout_intelligence_client
from app.pipeline.workflow import build_layout_intent_plan, build_semantic_layout_analysis
from pdf_translator_schema import (
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
            if self.schema_cls is SemanticLayoutAnalysis:
                return {
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
                    "quality_flags": ["model_semantic_analysis"],
                }
            return {
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

    analysis = asyncio.run(
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
            semantic_analysis=analysis,
            deterministic_plan=deterministic_plan,
        )
    )

    assert analysis is not None
    assert plan is not None
    assert isinstance(analysis, SemanticLayoutAnalysis)
    assert isinstance(plan, LayoutIntentPlan)
    assert validate_layout_intent_plan(document, plan) == plan
    assert [block.source_block_id for block in plan.blocks] == ["b1", "b2"]
    assert all(
        call["chat_kwargs"]["api_key"] == "fake-route-key"
        for call in calls
        if "chat_kwargs" in call
    )
    assert {call["schema"] for call in calls if "schema" in call} == {
        SemanticLayoutAnalysis,
        LayoutIntentPlan,
    }
