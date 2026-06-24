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
from .formula_processing import is_formula_like_text, repair_formula_placeholders
from .formulas.validation import FORMULA_REF_PATTERN

_HAN_PATTERN = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_PATTERN = re.compile(r"\b[A-Za-z]{3,}\b")
_PROSE_TRANSLATION_ROLES = {
    BlockRole.ABSTRACT,
    BlockRole.CAPTION,
    BlockRole.PARAGRAPH,
}
_DATE_ONLY_PATTERN = re.compile(
    r"^\s*(?:\d{1,2}\s+)?"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2},?\s+\d{4}[A-Za-z0-9\s,.;:-]*$",
    re.IGNORECASE,
)


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
    formula_ref = FORMULA_REF_PATTERN.fullmatch(token)
    if formula_ref:
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
        asset_id=formula_ref.group(1) if formula_ref else None,
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
        self._quality_diagnostics: list[dict[str, Any]] = []

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

    def drain_quality_diagnostics(self) -> list[dict[str, Any]]:
        diagnostics = list(self._quality_diagnostics)
        self._quality_diagnostics.clear()
        return diagnostics

    async def _translate_once(
        self,
        chunk: TranslationChunk,
        attempt: int,
        previous_error: TranslationError | None = None,
    ) -> TranslationLayoutPlan:
        system_prompt = (
            "You translate academic PDF text blocks. Return only JSON matching "
            "TranslationLayoutPlan schema_version 0.1. Do not include coordinates. "
            "Return exactly one JSON object and no markdown."
        )
        prompt = self._build_prompt(chunk, previous_error)
        if _is_minimax_provider(self.base_url, self.model):
            content = await self._translate_minimax_with_langchain(
                chunk,
                system_prompt,
                prompt,
            )
            plan = self._validate_or_repair_content(chunk, content, attempt)
            return await self._review_or_revise(chunk, plan, attempt)

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
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
        plan = self._validate_or_repair_content(chunk, content, attempt)
        return await self._review_or_revise(chunk, plan, attempt)

    async def _translate_minimax_with_langchain(
        self,
        chunk: TranslationChunk,
        system_prompt: str,
        prompt: str,
    ) -> object:
        try:
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                temperature=0.2,
                extra_body=_minimax_extra_body(self.model),
                disabled_params={"parallel_tool_calls": None},
            )
            response = await model.ainvoke(
                [
                    ("system", system_prompt),
                    ("user", prompt),
                ]
            )
        except Exception as exc:
            raise TranslationError(
                f"MiniMax LangChain translator request failed for {chunk.chunk_id}: {exc}"
            ) from exc
        return _message_content(response)

    def _validate_or_repair_content(
        self,
        chunk: TranslationChunk,
        content: object,
        attempt: int,
    ) -> TranslationLayoutPlan:
        raw_payload = self._extract_layout_plan_payload(chunk, content)

        try:
            plan = TranslationLayoutPlan.model_validate(raw_payload)
            return self._validate_target_language_or_repair(
                chunk,
                validate_layout_plan(chunk, plan),
                attempt,
            )
        except (ValidationError, LayoutPlanValidationError):
            repaired_payload = self._repair_payload(chunk, raw_payload, attempt)

        try:
            repaired_plan = TranslationLayoutPlan.model_validate(repaired_payload)
            return self._validate_target_language_or_repair(
                chunk,
                validate_layout_plan(chunk, repaired_plan),
                attempt,
            )
        except (ValidationError, LayoutPlanValidationError) as repair_exc:
            raise TranslationError(
                f"Translator layout plan validation failed for {chunk.chunk_id}: {repair_exc}"
            ) from repair_exc

    def _validate_target_language_or_repair(
        self,
        chunk: TranslationChunk,
        plan: TranslationLayoutPlan,
        attempt: int,
    ) -> TranslationLayoutPlan:
        mismatched_block_ids = _target_language_mismatch_block_ids(chunk, plan)
        if not mismatched_block_ids:
            return plan
        if attempt < self.max_attempts:
            raise TranslationError(
                "Translator target language quality check failed for "
                f"{chunk.chunk_id}: target_language_mismatch in "
                f"{', '.join(mismatched_block_ids)}"
            )
        repaired_plan = _target_language_fallback_plan(
            chunk,
            plan,
            mismatched_block_ids,
            attempt,
        )
        return validate_layout_plan(chunk, repaired_plan)

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
            known_formula_ids = {
                match.group(1)
                for token in source.preserve_tokens
                if (match := FORMULA_REF_PATTERN.fullmatch(token))
            }
            translated_text, placeholder_repair_count = repair_formula_placeholders(
                translated_text,
                known_formula_ids,
            )
            if placeholder_repair_count:
                repaired_flags.append("formula_placeholder_syntax_repaired")

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
                        if FORMULA_REF_PATTERN.fullmatch(token)
                        else "preserve_token_repaired"
                    )

            if (
                role == BlockRole.PARAGRAPH.value
                and is_formula_like_text(translated_text)
                and any(FORMULA_REF_PATTERN.fullmatch(token) for token in source.preserve_tokens)
            ):
                repaired_flags.append("formula_like_repaired")
            if not source.requires_translation:
                repaired_flags.append("formula_cluster_preserved")

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

    async def _review_or_revise(
        self,
        chunk: TranslationChunk,
        plan: TranslationLayoutPlan,
        attempt: int,
    ) -> TranslationLayoutPlan:
        issues = _quality_issues_for_plan(chunk, plan)
        severe_issues = [issue for issue in issues if issue["severity"] == "severe"]
        if not severe_issues:
            if issues:
                self._quality_diagnostics.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "attempt": attempt,
                        "action": "local_flags_only",
                        "issues": issues,
                        "quality_flags": _unique_strings(
                            [issue["flag"] for issue in issues]
                        ),
                    }
                )
            return _plan_with_quality_issue_flags(plan, issues)

        self._quality_diagnostics.append(
            {
                "chunk_id": chunk.chunk_id,
                "attempt": attempt,
                "action": "revision_requested",
                "issues": severe_issues,
                "quality_flags": _unique_strings(
                    [issue["flag"] for issue in severe_issues]
                ),
            }
        )
        try:
            content = await self._request_revision(chunk, plan, severe_issues)
            revised = self._validate_or_repair_content(chunk, content, attempt)
            post_issues = _quality_issues_for_plan(chunk, revised)
        except Exception as exc:
            self._quality_diagnostics.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "attempt": attempt,
                    "action": "revision_failed",
                    "error": str(exc),
                    "issues": severe_issues,
                    "quality_flags": ["translation_quality_revision_failed"],
                }
            )
            return _plan_with_quality_issue_flags(
                plan,
                severe_issues,
                extra_flags=["translation_quality_revision_failed"],
            )

        self._quality_diagnostics.append(
            {
                "chunk_id": chunk.chunk_id,
                "attempt": attempt,
                "action": "revision_applied",
                "issues": severe_issues,
                "post_review_issue_count": len(post_issues),
                "quality_flags": _unique_strings(
                    [
                        "translation_quality_revised",
                        *[issue["flag"] for issue in post_issues],
                    ]
                ),
            }
        )
        return _plan_with_quality_issue_flags(
            revised,
            post_issues,
            extra_flags=["translation_quality_revised"],
        )

    async def _request_revision(
        self,
        chunk: TranslationChunk,
        plan: TranslationLayoutPlan,
        issues: list[dict[str, Any]],
    ) -> object:
        system_prompt = (
            "You revise academic translations for fidelity, terminology, and natural "
            "target-language scholarly style. Return only JSON matching the original "
            "TranslationLayoutPlan shape."
        )
        prompt = self._build_revision_prompt(chunk, plan, issues)
        if _is_minimax_provider(self.base_url, self.model):
            return await self._translate_minimax_with_langchain(
                chunk,
                system_prompt,
                prompt,
            )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
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
        except httpx.HTTPError as exc:
            raise TranslationError(
                f"Translator revision request failed for {chunk.chunk_id}: {exc}"
            ) from exc
        return self._extract_message_content(response.json(), chunk.chunk_id)

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
            "Academic translation task:\n"
            "- Translate faithfully: preserve the author's meaning, logical relations, "
            "technical nuance, hedging, and academic tone.\n"
            "- Write natural target-language scholarly prose instead of literal or "
            "word-by-word translation.\n"
            "- Use the article background, main idea, and key terms below to keep "
            "professional terminology and sentence meaning consistent.\n"
            "- Translate only the listed source blocks; do not invent new claims or omit "
            "source content.\n"
            "- Preserve citation, formula, reference marker, figure, and table tokens exactly.\n"
            "- Blocks with requires_translation=false are formula-only blocks and must be "
            "copied exactly.\n\n"
            f"{_article_brief_section(chunk)}\n\n"
            f"Local chunk context:\n{chunk.context}\n\n"
            "JSON contract:\n"
            "Return one valid JSON object matching TranslationLayoutPlan schema_version 0.1. "
            "Cover every input block exactly once. Do not include coordinates or page fields. "
            "Do not translate, delete, rewrite, or move canonical formula refs matching "
            "{{formula:formula_id}}; copy them exactly into translated_text or inline_items. "
            "Use glossary entries consistently when they are provided.\n\n"
            f"Expected JSON shape:\n{json.dumps(schema_hint, ensure_ascii=False)}\n\n"
            f"Chunk JSON:\n{chunk.model_dump_json()}"
            f"{retry_hint}"
        )

    def _build_revision_prompt(
        self,
        chunk: TranslationChunk,
        plan: TranslationLayoutPlan,
        issues: list[dict[str, Any]],
    ) -> str:
        return (
            "Revise the draft translation for this academic paper chunk. Fix only the "
            "reported translation quality issues while preserving the JSON contract, "
            "source_block_id coverage, roles, citations, formulas, and reference markers.\n\n"
            f"{_article_brief_section(chunk)}\n\n"
            f"Issues to fix:\n{json.dumps(issues, ensure_ascii=False, indent=2)}\n\n"
            f"Original chunk JSON:\n{chunk.model_dump_json()}\n\n"
            f"Draft TranslationLayoutPlan JSON:\n{plan.model_dump_json()}"
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


def _article_brief_section(chunk: TranslationChunk) -> str:
    brief = chunk.article_brief
    if brief is None:
        return "Article background/main idea:\nNo document-level article brief was provided."
    return (
        "Article background/main idea:\n"
        f"Title: {brief.title}\n"
        f"Field/domain: {brief.field}\n"
        f"Background: {brief.background}\n"
        f"Main idea: {brief.main_idea}\n"
        f"Contribution/finding: {brief.contribution}\n"
        f"Key terms: {json.dumps(brief.key_terms, ensure_ascii=False)}"
    )


def _quality_issues_for_plan(
    chunk: TranslationChunk,
    plan: TranslationLayoutPlan,
) -> list[dict[str, Any]]:
    source_by_id = {source.block_id: source for source in chunk.source_blocks}
    issues: list[dict[str, Any]] = []
    key_terms = chunk.article_brief.key_terms if chunk.article_brief is not None else {}
    for block in plan.blocks:
        source = source_by_id.get(block.source_block_id)
        if source is None or not _requires_quality_review(source):
            continue
        source_text = _remove_preserve_tokens(source.source_text, source.preserve_tokens).strip()
        translated_text = _remove_preserve_tokens(
            block.translated_text,
            source.preserve_tokens,
        ).strip()
        if not source_text:
            continue
        if not translated_text:
            issues.append(
                _quality_issue(
                    source.block_id,
                    "translation_quality_empty",
                    "severe",
                    "Translated text is empty after removing preserve tokens.",
                )
            )
            continue
        source_len = len(source_text)
        translated_len = len(translated_text)
        if source_len >= 80 and translated_len / max(source_len, 1) < 0.18:
            issues.append(
                _quality_issue(
                    source.block_id,
                    "translation_quality_suspiciously_short",
                    "severe",
                    "Translated text is much shorter than the source block.",
                )
            )
        if source_len >= 80 and translated_len / max(source_len, 1) > 2.8:
            issues.append(
                _quality_issue(
                    source.block_id,
                    "translation_quality_suspiciously_long",
                    "warning",
                    "Translated text is much longer than the source block.",
                )
            )
        if _is_chinese_target(chunk.target_lang) and _looks_untranslated_for_chinese_target(
            source.source_text,
            block.translated_text,
            source.preserve_tokens,
        ):
            issues.append(
                _quality_issue(
                    source.block_id,
                    "translation_quality_source_leakage",
                    "severe",
                    "Chinese target text still looks mostly like untranslated source prose.",
                )
            )
        for source_term, target_term in key_terms.items():
            if not source_term or not target_term:
                continue
            if source_term.strip().lower() == target_term.strip().lower():
                continue
            if not _contains_term(source.source_text, source_term):
                continue
            if not _contains_term(block.translated_text, target_term):
                issues.append(
                    _quality_issue(
                        source.block_id,
                        "translation_quality_missing_key_term",
                        "severe",
                        f"Expected key term translation {target_term!r} for {source_term!r}.",
                    )
                )
    return _dedupe_quality_issues(issues)


def _requires_quality_review(source: Any) -> bool:
    if not getattr(source, "requires_translation", True):
        return False
    role = getattr(source, "role", BlockRole.UNKNOWN)
    if role in {BlockRole.FORMULA, BlockRole.FIGURE, BlockRole.TABLE, BlockRole.REFERENCE}:
        return False
    return True


def _quality_issue(
    block_id: str,
    flag: str,
    severity: str,
    message: str,
) -> dict[str, Any]:
    return {
        "source_block_id": block_id,
        "flag": flag,
        "severity": severity,
        "message": message,
    }


def _dedupe_quality_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (str(issue.get("source_block_id", "")), str(issue.get("flag", "")))
        if key in seen:
            continue
        result.append(issue)
        seen.add(key)
    return result


def _plan_with_quality_issue_flags(
    plan: TranslationLayoutPlan,
    issues: list[dict[str, Any]],
    *,
    extra_flags: list[str] | None = None,
) -> TranslationLayoutPlan:
    flags_by_block: dict[str, list[str]] = {}
    for issue in issues:
        block_id = str(issue.get("source_block_id", ""))
        flag = str(issue.get("flag", ""))
        if block_id and flag:
            flags_by_block.setdefault(block_id, []).append(flag)
    extra_flags = extra_flags or []
    revised_blocks: list[TranslationBlockPlan] = []
    for block in plan.blocks:
        flags = flags_by_block.get(block.source_block_id, [])
        if extra_flags and (flags or not flags_by_block):
            flags = [*flags, *extra_flags]
        if not flags:
            revised_blocks.append(block)
            continue
        revised_blocks.append(
            block.model_copy(
                update={"quality_flags": _unique_strings([*block.quality_flags, *flags])},
                deep=True,
            )
        )
    return plan.model_copy(update={"blocks": revised_blocks}, deep=True)


def _contains_term(text: str, term: str) -> bool:
    normalized_text = re.sub(r"\s+", " ", text).lower()
    normalized_term = re.sub(r"\s+", " ", term).strip().lower()
    return bool(normalized_term) and normalized_term in normalized_text


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


def _minimax_extra_body(model: str) -> dict[str, Any]:
    extra_body: dict[str, Any] = {"reasoning_split": True}
    if _is_minimax_m3_model(model):
        extra_body["thinking"] = {"type": "disabled"}
    return extra_body


def _message_content(response: object) -> object:
    if isinstance(response, dict):
        return response.get("content", response)
    if hasattr(response, "content"):
        return response.content
    return response


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


def _target_language_mismatch_block_ids(
    chunk: TranslationChunk,
    plan: TranslationLayoutPlan,
) -> list[str]:
    if not _is_chinese_target(chunk.target_lang):
        return []
    source_by_id = {source.block_id: source for source in chunk.source_blocks}
    mismatches: list[str] = []
    for block in plan.blocks:
        source = source_by_id.get(block.source_block_id)
        if source is None:
            continue
        if not _requires_chinese_translation_check(source):
            continue
        if _looks_untranslated_for_chinese_target(
            source.source_text,
            block.translated_text,
            source.preserve_tokens,
        ):
            mismatches.append(source.block_id)
    return mismatches


def _target_language_fallback_plan(
    chunk: TranslationChunk,
    plan: TranslationLayoutPlan,
    mismatched_block_ids: list[str],
    attempt: int,
) -> TranslationLayoutPlan:
    mismatched = set(mismatched_block_ids)
    source_by_id = {source.block_id: source for source in chunk.source_blocks}
    repaired_blocks: list[TranslationBlockPlan] = []
    for block in plan.blocks:
        if block.source_block_id not in mismatched:
            repaired_blocks.append(block)
            continue
        source = source_by_id[block.source_block_id]
        repaired_blocks.append(
            block.model_copy(
                update={
                    "translated_text": source.source_text,
                    "quality_flags": _unique_strings(
                        [
                            *block.quality_flags,
                            "target_language_mismatch",
                            "missing_translation",
                            f"retry_attempt_{attempt}",
                        ]
                    ),
                },
                deep=True,
            )
        )
    return plan.model_copy(update={"blocks": repaired_blocks}, deep=True)


def _is_chinese_target(target_lang: str) -> bool:
    normalized = target_lang.strip().lower().replace("_", "-")
    return normalized == "zh" or normalized.startswith("zh-") or normalized.startswith("chinese")


def _requires_chinese_translation_check(source: Any) -> bool:
    if not getattr(source, "requires_translation", True):
        return False
    role = getattr(source, "role", BlockRole.UNKNOWN)
    if role in {BlockRole.FORMULA, BlockRole.TABLE, BlockRole.FIGURE, BlockRole.REFERENCE, BlockRole.FOOTNOTE}:
        return False
    if role not in _PROSE_TRANSLATION_ROLES and len(_LATIN_WORD_PATTERN.findall(source.source_text)) < 4:
        return False
    source_text = _remove_preserve_tokens(source.source_text, source.preserve_tokens)
    stripped = source_text.strip()
    if not stripped:
        return False
    if _DATE_ONLY_PATTERN.fullmatch(stripped):
        return False
    if _looks_like_non_translatable_token_text(stripped):
        return False
    if is_formula_like_text(stripped):
        return False
    return len(_LATIN_WORD_PATTERN.findall(stripped)) >= 3


def _looks_untranslated_for_chinese_target(
    source_text: str,
    translated_text: str,
    preserve_tokens: list[str],
) -> bool:
    comparable = _remove_preserve_tokens(translated_text, preserve_tokens).strip()
    if not comparable:
        return True
    han_count = len(_HAN_PATTERN.findall(comparable))
    if han_count >= 2:
        return False
    comparable_nonspace = re.sub(r"\s+", "", comparable)
    if not comparable_nonspace:
        return True
    if han_count / max(len(comparable_nonspace), 1) >= 0.03:
        return False
    if _looks_like_non_translatable_token_text(comparable):
        return False
    if is_formula_like_text(comparable):
        return False
    latin_words = _LATIN_WORD_PATTERN.findall(comparable)
    if len(latin_words) < 3:
        return False
    source_words = {word.lower() for word in _LATIN_WORD_PATTERN.findall(source_text)}
    translated_words = {word.lower() for word in latin_words}
    overlap_ratio = len(source_words & translated_words) / max(len(translated_words), 1)
    return overlap_ratio >= 0.5 or len(latin_words) >= 5


def _remove_preserve_tokens(text: str, preserve_tokens: list[str]) -> str:
    cleaned = text
    for token in preserve_tokens:
        cleaned = cleaned.replace(token, " ")
    cleaned = FORMULA_REF_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"\[[0-9,\-\s;]+\]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned)


def _looks_like_non_translatable_token_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if re.fullmatch(r"[\W\d_]+", stripped):
        return True
    if re.fullmatch(r"(?:[A-Z][A-Za-z'’\-]+,?\s*){1,3}\d{4}[a-z]?", stripped):
        return True
    if re.fullmatch(r"(?:Fig(?:ure)?|Table|Sec(?:tion)?)\.?\s*[A-Za-z0-9.\-()]+", stripped, re.IGNORECASE):
        return True
    return False


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
