from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlparse

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

from ..provider_config import ProviderConfigError, normalize_openai_base_url
from .formula_processing import FORMULA_PLACEHOLDER_PATTERN


class TranslationError(RuntimeError):
    pass


class UnparseableTranslationResponseError(TranslationError):
    def __init__(self, message: str, *, content: object) -> None:
        super().__init__(message)
        self.content = content


class Translator:
    async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
        raise NotImplementedError

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        return []


def _inline_item_for_token(token: str) -> InlineItem:
    formula_ref = re.fullmatch(r"\{\{formula:([A-Za-z0-9_.:-]+)\}\}", token)
    formula_placeholder = FORMULA_PLACEHOLDER_PATTERN.fullmatch(token)
    if formula_ref or formula_placeholder:
        kind = "formula"
    elif re.fullmatch(r"\[[0-9,\-\s;]+\]", token):
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
    return InlineItem(
        kind=kind,
        text=token,
        source_token=token,
        asset_id=formula_ref.group(1)
        if formula_ref
        else token.removeprefix("@@FORMULA_").removesuffix("@@")
        if formula_placeholder
        else None,
    )


class DeterministicTranslator(Translator):
    async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
        blocks: list[TranslationBlockPlan] = []
        for source in chunk.source_blocks:
            prefix = "【译】" if chunk.target_lang.startswith("zh") else f"[{chunk.target_lang}] "
            translated = (
                source.source_text
                if not source.requires_translation
                else prefix + source.source_text
            )
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
                    quality_flags=["formula_preserved_without_translation"]
                    if not source.requires_translation
                    else ["mock_translation"],
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
        try:
            self.base_url = normalize_openai_base_url(base_url)
        except ProviderConfigError as exc:
            raise TranslationError(str(exc)) from exc
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.max_attempts = max(1, max_attempts)
        self._diagnostics: list[dict[str, Any]] = []

    async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
        if not any(source.requires_translation for source in chunk.source_blocks):
            return _preserve_only_plan(chunk)
        if any(not source.requires_translation for source in chunk.source_blocks):
            translatable_sources = [
                source for source in chunk.source_blocks if source.requires_translation
            ]
            preserved_sources = [
                source for source in chunk.source_blocks if not source.requires_translation
            ]
            translatable_chunk = chunk.model_copy(
                update={"source_blocks": translatable_sources},
                deep=True,
            )
            preserved_chunk = chunk.model_copy(
                update={"source_blocks": preserved_sources},
                deep=True,
            )
            translated_plan = await self._translate_with_retries(translatable_chunk)
            preserved_plan = _preserve_only_plan(preserved_chunk)
            blocks_by_id = {
                block.source_block_id: block
                for block in [*translated_plan.blocks, *preserved_plan.blocks]
            }
            return validate_layout_plan(
                chunk,
                TranslationLayoutPlan(
                    chunk_id=chunk.chunk_id,
                    target_lang=chunk.target_lang,
                    blocks=[
                        blocks_by_id[source.block_id]
                        for source in chunk.source_blocks
                        if source.block_id in blocks_by_id
                    ],
                ),
            )

        return await self._translate_with_retries(chunk)

    async def _translate_with_retries(
        self, chunk: TranslationChunk
    ) -> TranslationLayoutPlan:
        last_error: TranslationError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await self._translate_once(chunk, attempt, last_error)
            except UnparseableTranslationResponseError as exc:
                last_error = exc
                self._record_unparseable_response(chunk, attempt, exc)
                if attempt >= self.max_attempts:
                    raise TranslationError(
                        "Translator response was not recoverable for "
                        f"{chunk.chunk_id}: {exc}"
                    ) from exc
                await asyncio.sleep(0.25 * attempt)
            except TranslationError as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    raise
                await asyncio.sleep(0.25 * attempt)

        raise last_error or TranslationError(f"Translator failed for {chunk.chunk_id}")

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        diagnostics = list(self._diagnostics)
        self._diagnostics.clear()
        return diagnostics

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
            "temperature": 0.2,
        }
        if _is_minimax_provider(self.base_url, self.model):
            payload["reasoning_split"] = True
            if _is_minimax_m3_model(self.model):
                payload["thinking"] = {"type": "disabled"}
        else:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        request_url = f"{self.base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    request_url, headers=headers, json=payload
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            raise TranslationError(
                f"OpenAI-compatible translator request failed for {chunk.chunk_id}: "
                f"HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            if _is_ssl_protocol_mismatch(exc):
                suggestion = _http_scheme_suggestion(self.base_url)
                hint = (
                    "HTTPS/HTTP protocol mismatch. The endpoint accepted a connection "
                    "but did not speak TLS. If this OpenAI-compatible service is running "
                    f"over plain HTTP, try Base URL {suggestion}."
                    if suggestion
                    else (
                        "HTTPS/HTTP protocol mismatch. The endpoint accepted a connection "
                        "but did not speak TLS. Check whether the Base URL scheme and port "
                        "match the provider."
                    )
                )
                raise TranslationError(
                    f"OpenAI-compatible translator request failed for {chunk.chunk_id}: "
                    f"{hint} Current request URL: {request_url}"
                ) from exc
            raise TranslationError(
                f"OpenAI-compatible translator request failed for {chunk.chunk_id}: {exc}"
            ) from exc

        content = self._extract_message_content(response.json(), chunk.chunk_id)
        return self._validate_or_repair_content(chunk, content, attempt)

    def _validate_or_repair_content(
        self,
        chunk: TranslationChunk,
        content: object,
        attempt: int,
    ) -> TranslationLayoutPlan:
        raw_payload = self._extract_layout_plan_payload(chunk, content)

        try:
            plan = TranslationLayoutPlan.model_validate(raw_payload)
            return validate_layout_plan(chunk, plan)
        except (ValidationError, LayoutPlanValidationError):
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
                    repaired_flags.append(
                        "formula_placeholder_repaired"
                        if FORMULA_PLACEHOLDER_PATTERN.fullmatch(token)
                        else "preserve_token_repaired"
                    )

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

    def _extract_message_content(self, payload: dict[str, Any], chunk_id: str) -> object:
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationError(
                f"Translator response for {chunk_id} did not contain choices[0].message.content"
            ) from exc
        if isinstance(content, (str, dict)):
            return content
        raise TranslationError(
            "Translator response for "
            f"{chunk_id} had non-string message content: {type(content).__name__}"
        )

    def _extract_layout_plan_payload(
        self,
        chunk: TranslationChunk,
        content: object,
    ) -> dict[str, Any]:
        if isinstance(content, dict):
            return content
        if not isinstance(content, str):
            raise TranslationError(
                "Translator response for "
                f"{chunk.chunk_id} had unsupported message content: {type(content).__name__}"
            )

        try:
            raw_payload = json.loads(content)
        except json.JSONDecodeError:
            raw_payload = None
        if isinstance(raw_payload, dict):
            return raw_payload
        if raw_payload is not None:
            raise UnparseableTranslationResponseError(
                f"Translator returned JSON that is not an object for {chunk.chunk_id}",
                content=content,
            )

        sanitized_content = _strip_thinking_blocks(content)
        decoder = json.JSONDecoder()
        for index, character in enumerate(sanitized_content):
            if character != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(sanitized_content, index)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and _looks_like_layout_plan_payload(candidate):
                return candidate

        salvaged = _salvage_layout_plan_payload(chunk, sanitized_content)
        if salvaged is not None:
            return salvaged

        raise UnparseableTranslationResponseError(
            f"Translator returned content without a TranslationLayoutPlan JSON object "
            f"for {chunk.chunk_id}",
            content=content,
        )

    def _record_unparseable_response(
        self,
        chunk: TranslationChunk,
        attempt: int,
        exc: UnparseableTranslationResponseError,
    ) -> None:
        text = _diagnostic_text_for_content(exc.content)
        preview = _sanitize_diagnostic_preview(text)
        self._diagnostics.append(
            {
                "chunk_id": chunk.chunk_id,
                "attempt": attempt,
                "error_type": exc.__class__.__name__,
                "error": str(exc),
                "content_type": type(exc.content).__name__,
                "content_length": len(text),
                "response_preview_length": len(preview),
                "sanitized_response_preview": preview,
                "quality_flags": _unique_strings(
                    [
                        "translator_response_unparseable",
                        *(
                            ["translator_unrecoverable_response"]
                            if attempt >= self.max_attempts
                            else []
                        ),
                    ]
                ),
            }
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
            "Do not translate, delete, rewrite, or move formula placeholders matching "
            "@@FORMULA_[A-Za-z0-9_]+@@; copy them exactly into translated_text or inline_items. "
            "Legacy formula refs such as {{formula:formula_id}} are renderer-owned LaTeX "
            "placeholders and must also be copied exactly. "
            "Blocks with requires_translation=false are formula-only blocks and must be "
            "copied exactly. "
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


def _preserve_only_plan(chunk: TranslationChunk) -> TranslationLayoutPlan:
    blocks = []
    for source in chunk.source_blocks:
        blocks.append(
            TranslationBlockPlan(
                source_block_id=source.block_id,
                translated_text=source.source_text,
                inline_items=[_inline_item_for_token(token) for token in source.preserve_tokens],
                role=source.role,
                render_intent="normal",
                quality_flags=["formula_preserved_without_translation"],
            )
        )
    return validate_layout_plan(
        chunk,
        TranslationLayoutPlan(
            chunk_id=chunk.chunk_id,
            target_lang=chunk.target_lang,
            blocks=blocks,
        ),
    )


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


def _is_ssl_protocol_mismatch(exc: httpx.HTTPError) -> bool:
    message = str(exc).lower()
    return "wrong_version_number" in message or "wrong version number" in message


def _http_scheme_suggestion(base_url: str) -> str | None:
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        return None
    return parsed._replace(scheme="http").geturl()


def _strip_thinking_blocks(content: str) -> str:
    return re.sub(r"<think\b[^>]*>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)


def _salvage_layout_plan_payload(
    chunk: TranslationChunk,
    content: str,
) -> dict[str, Any] | None:
    blocks: list[dict[str, Any]] = []
    for source in chunk.source_blocks:
        translated_text = _salvage_translated_text_for_block(content, source.block_id)
        if translated_text is None or not translated_text.strip():
            continue
        blocks.append(
            {
                "source_block_id": source.block_id,
                "translated_text": translated_text,
                "inline_items": [],
                "role": source.role.value,
                "render_intent": "preserve_asset"
                if source.role in {BlockRole.FIGURE, BlockRole.TABLE}
                else "normal",
                "quality_flags": ["translator_json_salvaged"],
            }
        )
    if blocks:
        return {
            "schema_version": "0.1",
            "chunk_id": chunk.chunk_id,
            "target_lang": chunk.target_lang,
            "blocks": blocks,
        }

    if len(chunk.source_blocks) == 1:
        plain_text = _plain_text_salvage(content)
        if plain_text:
            source = chunk.source_blocks[0]
            return {
                "schema_version": "0.1",
                "chunk_id": chunk.chunk_id,
                "target_lang": chunk.target_lang,
                "blocks": [
                    {
                        "source_block_id": source.block_id,
                        "translated_text": plain_text,
                        "inline_items": [],
                        "role": source.role.value,
                        "render_intent": "preserve_asset"
                        if source.role in {BlockRole.FIGURE, BlockRole.TABLE}
                        else "normal",
                        "quality_flags": ["translator_plain_text_salvaged"],
                    }
                ],
            }
    return None


def _salvage_translated_text_for_block(content: str, source_block_id: str) -> str | None:
    id_pattern = re.compile(
        r'"source_block_id"\s*:\s*"'
        + re.escape(source_block_id)
        + r'"'
    )
    decoder = json.JSONDecoder()
    for id_match in id_pattern.finditer(content):
        translated_key = re.search(
            r'"translated_text"\s*:',
            content[id_match.end() :],
        )
        if translated_key is None:
            continue
        value_start = id_match.end() + translated_key.end()
        while value_start < len(content) and content[value_start].isspace():
            value_start += 1
        if value_start >= len(content) or content[value_start] != '"':
            continue
        try:
            value, _end = decoder.raw_decode(content, value_start)
        except json.JSONDecodeError:
            value = _read_partial_json_string(content, value_start)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _read_partial_json_string(content: str, quote_index: int) -> str | None:
    if quote_index >= len(content) or content[quote_index] != '"':
        return None
    chars: list[str] = []
    index = quote_index + 1
    escaping = False
    while index < len(content):
        char = content[index]
        if escaping:
            chars.append(_decode_json_escape(char))
            escaping = False
        elif char == "\\":
            escaping = True
        elif char == '"':
            return "".join(chars)
        else:
            chars.append(char)
        index += 1
    partial = "".join(chars).strip()
    return partial or None


def _decode_json_escape(char: str) -> str:
    return {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }.get(char, char)


def _plain_text_salvage(content: str) -> str | None:
    stripped = re.sub(r"^```(?:text|markdown)?|```$", "", content.strip(), flags=re.MULTILINE)
    stripped = stripped.strip()
    if not stripped:
        return None
    if _looks_like_layout_plan_payload_fragment(stripped):
        return None
    if re.search(r'"source_block_id"\s*:', stripped):
        return None
    if stripped.startswith("{") or stripped.startswith("["):
        return None
    return stripped


def _looks_like_layout_plan_payload_fragment(content: str) -> bool:
    return (
        '"schema_version"' in content
        or '"blocks"' in content
        or '"translated_text"' in content
    )


def _diagnostic_text_for_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        try:
            return json.dumps(content, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return repr(content)
    return repr(content)


def _sanitize_diagnostic_preview(content: str, limit: int = 1000) -> str:
    sanitized = re.sub(
        r"<think\b[^>]*>.*?</think>",
        "<think>...</think>",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    sanitized = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [REDACTED]", sanitized)
    sanitized = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-[REDACTED]", sanitized)
    sanitized = "".join(
        character
        if character in {"\n", "\r", "\t"} or ord(character) >= 32
        else " "
        for character in sanitized
    )
    if len(sanitized) <= limit:
        return sanitized
    return sanitized[: limit - 1].rstrip() + "…"


def _looks_like_layout_plan_payload(payload: dict[str, Any]) -> bool:
    return (
        payload.get("schema_version") == "0.1"
        and isinstance(payload.get("chunk_id"), str)
        and isinstance(payload.get("target_lang"), str)
        and isinstance(payload.get("blocks"), list)
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
