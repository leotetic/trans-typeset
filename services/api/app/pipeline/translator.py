from __future__ import annotations

import json
import re
from typing import Any

import httpx
from pdf_translator_schema import (
    InlineItem,
    TranslationBlockPlan,
    TranslationChunk,
    TranslationLayoutPlan,
    validate_layout_plan,
)
from pdf_translator_schema.models import BlockRole
from pdf_translator_schema.validation import LayoutPlanValidationError
from pydantic import ValidationError


class TranslationError(RuntimeError):
    pass


class Translator:
    async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
        raise NotImplementedError


def _inline_item_for_token(token: str) -> InlineItem:
    if re.fullmatch(r"\[[0-9,\-\s;]+\]", token):
        kind = "reference_marker"
    elif re.search(r"\b\d{4}[a-z]?\b", token) or token.startswith(
        ("Fig", "Figure", "Table", "Sec", "Section")
    ):
        kind = "citation"
    elif any(
        symbol in token
        for symbol in ("=", "+", "-", "*", "/", "≤", "≥", "∑", "∫", "^", "_")
    ):
        kind = "formula"
    else:
        kind = "citation"
    return InlineItem(kind=kind, text=token, source_token=token)


class DeterministicTranslator(Translator):
    async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
        blocks: list[TranslationBlockPlan] = []
        for source in chunk.source_blocks:
            prefix = "【译】" if chunk.target_lang.startswith("zh") else f"[{chunk.target_lang}] "
            translated = prefix + source.source_text
            inline_items = [_inline_item_for_token(token) for token in source.preserve_tokens]
            blocks.append(
                TranslationBlockPlan(
                    source_block_id=source.block_id,
                    translated_text=translated,
                    inline_items=inline_items,
                    role=source.role,
                    render_intent="normal"
                    if source.role not in {BlockRole.FIGURE, BlockRole.TABLE}
                    else "preserve_asset",
                    quality_flags=["mock_translation"],
                )
            )
        plan = TranslationLayoutPlan(
            chunk_id=chunk.chunk_id,
            target_lang=chunk.target_lang,
            blocks=blocks,
        )
        try:
            return validate_layout_plan(chunk, plan)
        except LayoutPlanValidationError as exc:
            raise TranslationError(
                "Deterministic translator produced an invalid layout plan "
                f"for {chunk.chunk_id}: {exc}"
            ) from exc


class OpenAICompatibleTranslator(Translator):
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
        prompt = self._build_prompt(chunk)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You translate academic PDF text blocks. Return only JSON matching "
                        "TranslationLayoutPlan schema_version 0.1. Do not include coordinates. "
                        "Return exactly one JSON object and no markdown."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=payload
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            raise TranslationError(
                f"OpenAI-compatible translator request failed for {chunk.chunk_id}: "
                f"HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise TranslationError(
                f"OpenAI-compatible translator request failed for {chunk.chunk_id}: {exc}"
            ) from exc

        content = self._extract_message_content(response.json(), chunk.chunk_id)
        try:
            plan = TranslationLayoutPlan.model_validate_json(content)
        except ValidationError as exc:
            raise TranslationError(
                f"Translator returned JSON that does not match TranslationLayoutPlan "
                f"for {chunk.chunk_id}: {exc}"
            ) from exc

        try:
            return validate_layout_plan(chunk, plan)
        except LayoutPlanValidationError as exc:
            raise TranslationError(
                f"Translator layout plan validation failed for {chunk.chunk_id}: {exc}"
            ) from exc

    def _extract_message_content(self, payload: dict[str, Any], chunk_id: str) -> str:
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationError(
                f"Translator response for {chunk_id} did not contain choices[0].message.content"
            ) from exc
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return json.dumps(content, ensure_ascii=False)
        raise TranslationError(
            "Translator response for "
            f"{chunk_id} had non-string message content: {type(content).__name__}"
        )

    def _build_prompt(self, chunk: TranslationChunk) -> str:
        schema_hint = {
            "schema_version": "0.1",
            "chunk_id": chunk.chunk_id,
            "target_lang": chunk.target_lang,
            "blocks": [
                {
                    "source_block_id": "same as input block_id",
                    "translated_text": "translated block text",
                    "inline_items": [],
                    "role": "same semantic role",
                    "render_intent": "normal",
                    "quality_flags": [],
                }
            ],
        }
        return (
            "Translate the following academic paper chunk. Preserve citation, formula, "
            "reference marker, figure, and table tokens. Cover every input block exactly once. "
            "Return valid JSON object only.\n\n"
            f"Expected JSON shape:\n{json.dumps(schema_hint, ensure_ascii=False)}\n\n"
            f"Chunk JSON:\n{chunk.model_dump_json()}"
        )


def build_translator(base_url: str, api_key: str, model: str) -> Translator:
    if not api_key:
        return DeterministicTranslator()
    return OpenAICompatibleTranslator(base_url=base_url, api_key=api_key, model=model)
