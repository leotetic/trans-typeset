from __future__ import annotations

import asyncio
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
from pdf_translator_schema.models import FORBIDDEN_LAYOUT_KEYS, BlockRole
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
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_attempts: int = 2,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.max_attempts = max(1, max_attempts)

    async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
        last_error: TranslationError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await self._translate_once(chunk, attempt, last_error)
            except TranslationError as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    raise
                await asyncio.sleep(0.25 * attempt)

        raise last_error or TranslationError(f"Translator failed for {chunk.chunk_id}")

    async def _translate_once(
        self,
        chunk: TranslationChunk,
        attempt: int,
        previous_error: TranslationError | None = None,
    ) -> TranslationLayoutPlan:
        prompt = self._build_prompt(chunk, previous_error)
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
        return self._validate_or_repair_content(chunk, content, attempt)

    def _validate_or_repair_content(
        self,
        chunk: TranslationChunk,
        content: str,
        attempt: int,
    ) -> TranslationLayoutPlan:
        try:
            raw_payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise TranslationError(
                f"Translator returned non-JSON content for {chunk.chunk_id}: {exc}"
            ) from exc
        if not isinstance(raw_payload, dict):
            raise TranslationError(
                f"Translator returned JSON that is not an object for {chunk.chunk_id}"
            )

        try:
            plan = TranslationLayoutPlan.model_validate(raw_payload)
            return validate_layout_plan(chunk, plan)
        except (ValidationError, LayoutPlanValidationError) as exc:
            repaired_payload = self._repair_payload(chunk, raw_payload, attempt)

        try:
            repaired_plan = TranslationLayoutPlan.model_validate(repaired_payload)
            return validate_layout_plan(chunk, repaired_plan)
        except (ValidationError, LayoutPlanValidationError) as repair_exc:
            raise TranslationError(
                f"Translator layout plan validation failed for {chunk.chunk_id}: {repair_exc}"
            ) from repair_exc

    def _repair_payload(
        self,
        chunk: TranslationChunk,
        payload: dict[str, Any],
        attempt: int,
    ) -> dict[str, Any]:
        stripped = _strip_forbidden_layout_keys(payload)
        raw_blocks = stripped.get("blocks")
        if not isinstance(raw_blocks, list):
            raw_blocks = []

        raw_blocks_by_id: dict[str, dict[str, Any]] = {}
        for raw_block in raw_blocks:
            if not isinstance(raw_block, dict):
                continue
            source_block_id = raw_block.get("source_block_id")
            if isinstance(source_block_id, str) and source_block_id in chunk.source_block_ids():
                raw_blocks_by_id.setdefault(source_block_id, raw_block)

        repaired_blocks: list[dict[str, Any]] = []
        for source in chunk.source_blocks:
            raw_block = raw_blocks_by_id.get(source.block_id, {})
            repaired_flags = _string_list(raw_block.get("quality_flags"))
            repaired_flags.append("repaired_layout_plan")
            if attempt > 1:
                repaired_flags.append(f"retry_attempt_{attempt}")

            translated_text = raw_block.get("translated_text")
            if not isinstance(translated_text, str) or not translated_text.strip():
                translated_text = source.source_text
                repaired_flags.extend(["empty_translation_repaired", "missing_translation"])

            raw_role = raw_block.get("role")
            if isinstance(raw_role, str) and raw_role in {role.value for role in BlockRole}:
                role = raw_role
                if role != source.role.value:
                    repaired_flags.append("role_mismatch")
            else:
                role = source.role.value

            render_intent = raw_block.get("render_intent")
            if render_intent not in {"normal", "compact", "emphasis", "preserve_asset"}:
                render_intent = (
                    "preserve_asset"
                    if source.role in {BlockRole.FIGURE, BlockRole.TABLE}
                    else "normal"
                )

            inline_items = _repair_inline_items(raw_block.get("inline_items"))
            planned_tokens = {
                item.get("source_token") or item.get("text")
                for item in inline_items
                if item.get("kind") != "text"
            }
            for token in source.preserve_tokens:
                if token not in translated_text and token not in planned_tokens:
                    inline_items.append(_inline_item_for_token(token).model_dump())
                    repaired_flags.append("preserve_token_repaired")

            if not raw_block:
                repaired_flags.append("missing_block_repaired")

            repaired_blocks.append(
                {
                    "source_block_id": source.block_id,
                    "translated_text": translated_text,
                    "inline_items": inline_items,
                    "role": role,
                    "render_intent": render_intent,
                    "quality_flags": _unique_strings(repaired_flags),
                }
            )

        return {
            "schema_version": "0.1",
            "chunk_id": chunk.chunk_id,
            "target_lang": chunk.target_lang,
            "blocks": repaired_blocks,
        }

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

    def _build_prompt(
        self,
        chunk: TranslationChunk,
        previous_error: TranslationError | None = None,
    ) -> str:
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
        retry_hint = (
            "\n\nPrevious attempt failed validation. Correct the JSON contract problem: "
            f"{previous_error}"
            if previous_error
            else ""
        )
        return (
            "Translate the following academic paper chunk. Preserve citation, formula, "
            "reference marker, figure, and table tokens. Cover every input block exactly once. "
            "Use glossary entries consistently when they are provided in the chunk JSON. "
            "Use the chunk context for local continuity, but translate only the listed blocks. "
            "Return valid JSON object only.\n\n"
            f"Expected JSON shape:\n{json.dumps(schema_hint, ensure_ascii=False)}\n\n"
            f"Chunk JSON:\n{chunk.model_dump_json()}"
            f"{retry_hint}"
        )


def build_translator(
    base_url: str,
    api_key: str,
    model: str,
    max_attempts: int = 2,
) -> Translator:
    if not api_key:
        return DeterministicTranslator()
    return OpenAICompatibleTranslator(
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_attempts=max_attempts,
    )


def _strip_forbidden_layout_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_forbidden_layout_keys(item)
            for key, item in value.items()
            if key not in FORBIDDEN_LAYOUT_KEYS
        }
    if isinstance(value, list):
        return [_strip_forbidden_layout_keys(item) for item in value]
    return value


def _repair_inline_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    repaired: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        stripped = _strip_forbidden_layout_keys(item)
        kind = stripped.get("kind")
        if kind not in {"text", "citation", "formula", "reference_marker", "asset_ref"}:
            kind = "text"
        text = stripped.get("text")
        source_token = stripped.get("source_token")
        asset_id = stripped.get("asset_id")
        repaired.append(
            {
                "kind": kind,
                "text": text if isinstance(text, str) else "",
                "source_token": source_token if isinstance(source_token, str) else None,
                "asset_id": asset_id if isinstance(asset_id, str) else None,
            }
        )
    return repaired


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique
