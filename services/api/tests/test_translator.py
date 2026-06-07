import asyncio

import pytest
from app.pipeline import translator as translator_module
from app.pipeline.translator import (
    DeterministicTranslator,
    OpenAICompatibleTranslator,
    TranslationError,
    build_translator,
)
from pdf_translator_schema import (
    BlockRole,
    RenderDefaults,
    SourceBlock,
    TranslationChunk,
    TranslationConstraints,
    TranslationLayoutPlan,
)


def make_chunk() -> TranslationChunk:
    return TranslationChunk(
        chunk_id="doc_1_chunk_0001",
        target_lang="zh-CN",
        source_blocks=[
            SourceBlock(
                block_id="b1",
                role=BlockRole.PARAGRAPH,
                source_text="Alpha [1].",
                preserve_tokens=["[1]"],
            ),
            SourceBlock(
                block_id="b2",
                role=BlockRole.PARAGRAPH,
                source_text="Beta y = f(x).",
                preserve_tokens=["y = f(x)"],
            ),
        ],
        render_defaults=RenderDefaults(target_lang="zh-CN"),
        constraints=TranslationConstraints(),
    )


def test_deterministic_translator_covers_every_block_and_tokens() -> None:
    chunk = make_chunk()
    translator = DeterministicTranslator()

    plan = asyncio.run(translator.translate(chunk))

    assert [block.source_block_id for block in plan.blocks] == ["b1", "b2"]
    assert all(block.translated_text.startswith("【译】") for block in plan.blocks)
    tokens = {
        item.source_token
        for block in plan.blocks
        for item in block.inline_items
    }
    assert tokens == {"[1]", "y = f(x)"}


def test_build_translator_without_api_key_uses_deterministic() -> None:
    assert isinstance(
        build_translator("https://example.test/v1", "", "model"),
        DeterministicTranslator,
    )


def test_openai_translator_wraps_layout_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    chunk = make_chunk()
    invalid_plan = TranslationLayoutPlan(
        chunk_id=chunk.chunk_id,
        target_lang=chunk.target_lang,
        blocks=[
            {
                "source_block_id": "b1",
                "translated_text": "Alpha without token",
                "role": "paragraph",
            }
        ],
    )

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": invalid_plan.model_dump_json(),
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, *args, **kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(translator_module.httpx, "AsyncClient", FakeAsyncClient)
    translator = OpenAICompatibleTranslator("https://example.test/v1", "key", "model")

    with pytest.raises(TranslationError, match="layout plan validation failed"):
        asyncio.run(translator.translate(chunk))
