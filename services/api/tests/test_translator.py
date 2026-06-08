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


def test_openai_prompt_instructs_glossary_and_context_usage() -> None:
    chunk = make_chunk()
    translator = OpenAICompatibleTranslator("https://example.test/v1", "key", "model")

    prompt = translator._build_prompt(chunk)

    assert "Use glossary entries consistently" in prompt
    assert "Use the chunk context for local continuity" in prompt
    assert chunk.context in prompt


def test_openai_translator_trims_api_key_for_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = make_chunk()
    calls: list[dict] = []
    valid_payload = {
        "schema_version": "0.1",
        "chunk_id": chunk.chunk_id,
        "target_lang": chunk.target_lang,
        "blocks": [
            {
                "source_block_id": "b1",
                "translated_text": "译文 [1]",
                "inline_items": [],
                "role": "paragraph",
            },
            {
                "source_block_id": "b2",
                "translated_text": "译文 y = f(x)",
                "inline_items": [],
                "role": "paragraph",
            },
        ],
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": valid_payload,
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
            calls.append({"url": args[0], "headers": kwargs["headers"]})
            return FakeResponse()

    monkeypatch.setattr(translator_module.httpx, "AsyncClient", FakeAsyncClient)
    translator = OpenAICompatibleTranslator(
        " https://example.test/v1/ ", " secret-key \n", " model "
    )

    plan = asyncio.run(translator.translate(chunk))

    assert [block.source_block_id for block in plan.blocks] == ["b1", "b2"]
    assert calls == [
        {
            "url": "https://example.test/v1/chat/completions",
            "headers": {
                "Authorization": "Bearer secret-key",
                "Content-Type": "application/json",
            },
        }
    ]


def test_openai_translator_repairs_missing_blocks_tokens_and_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = make_chunk()
    invalid_payload = {
        "schema_version": "0.1",
        "chunk_id": chunk.chunk_id,
        "target_lang": chunk.target_lang,
        "page": 1,
        "blocks": [
            {
                "source_block_id": "b1",
                "translated_text": "Alpha without token",
                "role": "paragraph",
                "bbox": {"x0": 1, "y0": 2, "x1": 3, "y1": 4},
            }
        ],
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": invalid_payload,
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

    plan = asyncio.run(translator.translate(chunk))

    assert [block.source_block_id for block in plan.blocks] == ["b1", "b2"]
    b1, b2 = plan.blocks
    assert b1.translated_text == "Alpha without token"
    assert {item.source_token for item in b1.inline_items} == {"[1]"}
    assert "repaired_layout_plan" in b1.quality_flags
    assert "preserve_token_repaired" in b1.quality_flags
    assert b2.translated_text == "Beta y = f(x)."
    assert "missing_block_repaired" in b2.quality_flags
    assert "missing_translation" in b2.quality_flags


def test_openai_translator_retries_chunk_after_unrepairable_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = make_chunk()
    calls: list[dict] = []
    valid_payload = {
        "schema_version": "0.1",
        "chunk_id": chunk.chunk_id,
        "target_lang": chunk.target_lang,
        "blocks": [
            {
                "source_block_id": "b1",
                "translated_text": "译文 [1]",
                "inline_items": [],
                "role": "paragraph",
            },
            {
                "source_block_id": "b2",
                "translated_text": "译文 y = f(x)",
                "inline_items": [],
                "role": "paragraph",
            },
        ],
    }

    async def no_sleep(_seconds: float) -> None:
        return None

    class FakeResponse:
        def __init__(self, content: object) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": self.content,
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
            calls.append(kwargs["json"])
            return FakeResponse("not-json" if len(calls) == 1 else valid_payload)

    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(translator_module.httpx, "AsyncClient", FakeAsyncClient)
    translator = OpenAICompatibleTranslator(
        "https://example.test/v1", "key", "model", max_attempts=2
    )

    plan = asyncio.run(translator.translate(chunk))

    assert len(calls) == 2
    assert [block.source_block_id for block in plan.blocks] == ["b1", "b2"]
    assert "Previous attempt failed validation" in calls[1]["messages"][1]["content"]
