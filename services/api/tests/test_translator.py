import asyncio
import json

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


def make_valid_payload(chunk: TranslationChunk) -> dict:
    return {
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
    valid_payload = make_valid_payload(chunk)

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
            calls.append(
                {
                    "url": args[0],
                    "headers": kwargs["headers"],
                    "payload": kwargs["json"],
                }
            )
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
            "payload": {
                "model": "model",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You translate academic PDF text blocks. Return only JSON "
                            "matching TranslationLayoutPlan schema_version 0.1. Do not "
                            "include coordinates. Return exactly one JSON object and no "
                            "markdown."
                        ),
                    },
                    {"role": "user", "content": translator._build_prompt(chunk)},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
        }
    ]


def test_minimax_m3_payload_disables_thinking_and_splits_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = make_chunk()
    calls: list[dict] = []
    valid_payload = make_valid_payload(chunk)

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
            calls.append(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(translator_module.httpx, "AsyncClient", FakeAsyncClient)
    translator = OpenAICompatibleTranslator(
        "https://api.minimax.io/v1", "key", "MiniMax-M3"
    )

    plan = asyncio.run(translator.translate(chunk))

    assert [block.source_block_id for block in plan.blocks] == ["b1", "b2"]
    assert calls == [
        {
            "model": "MiniMax-M3",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You translate academic PDF text blocks. Return only JSON matching "
                        "TranslationLayoutPlan schema_version 0.1. Do not include coordinates. "
                        "Return exactly one JSON object and no markdown."
                    ),
                },
                {"role": "user", "content": translator._build_prompt(chunk)},
            ],
            "temperature": 0.2,
            "reasoning_split": True,
            "thinking": {"type": "disabled"},
        }
    ]


def test_openai_translator_extracts_plan_from_wrapped_content() -> None:
    chunk = make_chunk()
    translator = OpenAICompatibleTranslator("https://example.test/v1", "key", "model")
    valid_payload = make_valid_payload(chunk)

    wrapped = (
        '<think>{"schema_version":"0.1","chunk_id":"wrong","target_lang":"zh-CN",'
        '"blocks":[]}</think>\n'
        "Here is the plan:\n"
        f"```json\n{json.dumps(valid_payload, ensure_ascii=False)}\n```"
    )

    plan = translator._validate_or_repair_content(chunk, wrapped, attempt=1)

    assert [block.source_block_id for block in plan.blocks] == ["b1", "b2"]
    assert plan.chunk_id == chunk.chunk_id


def test_openai_translator_salvages_translated_text_from_truncated_json() -> None:
    chunk = make_chunk()
    translator = OpenAICompatibleTranslator("https://example.test/v1", "key", "model")
    truncated = (
        '{"schema_version":"0.1","chunk_id":"doc_1_chunk_0001","target_lang":"zh-CN",'
        '"blocks":['
        '{"source_block_id":"b1","translated_text":"阿尔法 [1]","role":"paragraph"},'
        '{"source_block_id":"b2","translated_text":"贝塔 y = f(x)","role":"paragraph"}'
    )

    plan = translator._validate_or_repair_content(chunk, truncated, attempt=1)

    assert [block.translated_text for block in plan.blocks] == [
        "阿尔法 [1]",
        "贝塔 y = f(x)",
    ]
    assert all("translator_json_salvaged" in block.quality_flags for block in plan.blocks)


def test_openai_translator_salvages_single_block_plain_text() -> None:
    chunk = TranslationChunk(
        chunk_id="doc_1_chunk_0001",
        target_lang="zh-CN",
        source_blocks=[
            SourceBlock(
                block_id="b1",
                role=BlockRole.PARAGRAPH,
                source_text="Alpha [1].",
                preserve_tokens=["[1]"],
            )
        ],
        render_defaults=RenderDefaults(target_lang="zh-CN"),
        constraints=TranslationConstraints(),
    )
    translator = OpenAICompatibleTranslator("https://example.test/v1", "key", "model")

    plan = translator._validate_or_repair_content(chunk, "阿尔法 [1]。", attempt=1)

    assert plan.blocks[0].translated_text == "阿尔法 [1]。"
    assert "translator_plain_text_salvaged" in plan.blocks[0].quality_flags


@pytest.mark.parametrize(
    "content",
    [
        "prefix {\"note\":\"not the plan\"} suffix",
        "no json object here",
    ],
)
def test_openai_translator_rejects_content_without_layout_plan(content: str) -> None:
    chunk = make_chunk()
    translator = OpenAICompatibleTranslator("https://example.test/v1", "key", "model")

    with pytest.raises(TranslationError, match="without a TranslationLayoutPlan JSON object"):
        translator._validate_or_repair_content(chunk, content, attempt=1)


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
    valid_payload = make_valid_payload(chunk)

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


def test_openai_translator_fails_after_repeated_unrecoverable_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = make_chunk()
    calls: list[dict] = []

    async def no_sleep(_seconds: float) -> None:
        return None

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<think>secret scratchpad</think>\n"
                                "Plain translated text without plan JSON sk-live-secret123456"
                            ),
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
            return FakeResponse()

    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(translator_module.httpx, "AsyncClient", FakeAsyncClient)
    translator = OpenAICompatibleTranslator(
        "https://example.test/v1", "key", "model", max_attempts=2
    )

    with pytest.raises(TranslationError, match="not recoverable"):
        asyncio.run(translator.translate(chunk))
    diagnostics = translator.drain_diagnostics()

    assert len(calls) == 2
    assert len(diagnostics) == 2
    assert diagnostics[0]["chunk_id"] == chunk.chunk_id
    assert diagnostics[0]["attempt"] == 1
    assert diagnostics[0]["content_type"] == "str"
    assert diagnostics[0]["quality_flags"] == ["translator_response_unparseable"]
    assert diagnostics[1]["quality_flags"] == [
        "translator_response_unparseable",
        "translator_unrecoverable_response",
    ]
    assert "<think>...</think>" in diagnostics[0]["sanitized_response_preview"]
    assert "sk-live-secret123456" not in diagnostics[0]["sanitized_response_preview"]
    assert "sk-[REDACTED]" in diagnostics[0]["sanitized_response_preview"]


def test_openai_translator_explains_ssl_protocol_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = make_chunk()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, *args, **kwargs) -> None:
            raise translator_module.httpx.ConnectError(
                "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1081)"
            )

    monkeypatch.setattr(translator_module.httpx, "AsyncClient", FakeAsyncClient)
    translator = OpenAICompatibleTranslator(
        "https://10.194.160.128:8080/v1", "key", "model", max_attempts=1
    )

    with pytest.raises(TranslationError) as exc_info:
        asyncio.run(translator.translate(chunk))

    message = str(exc_info.value)
    assert "HTTPS/HTTP protocol mismatch" in message
    assert "try Base URL http://10.194.160.128:8080/v1" in message
    assert "Current request URL: https://10.194.160.128:8080/v1/chat/completions" in message


def test_openai_translator_keeps_generic_http_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = make_chunk()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, *args, **kwargs) -> None:
            raise translator_module.httpx.ConnectError("connection refused")

    monkeypatch.setattr(translator_module.httpx, "AsyncClient", FakeAsyncClient)
    translator = OpenAICompatibleTranslator(
        "https://example.test/v1", "key", "model", max_attempts=1
    )

    with pytest.raises(TranslationError) as exc_info:
        asyncio.run(translator.translate(chunk))

    message = str(exc_info.value)
    assert "connection refused" in message
    assert "HTTPS/HTTP protocol mismatch" not in message
