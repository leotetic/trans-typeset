from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from pdf_translator_schema import ArticleBrief, BlockRole, DocumentIR

from ..provider_config import ProviderConfigError, normalize_openai_base_url


class ArticleBriefError(RuntimeError):
    pass


async def build_article_brief(
    document: DocumentIR,
    *,
    target_lang: str,
    base_url: str,
    api_key: str,
    model: str,
) -> ArticleBrief:
    if not api_key.strip():
        return build_deterministic_article_brief(document)
    try:
        normalized_base_url = normalize_openai_base_url(base_url)
    except ProviderConfigError as exc:
        raise ArticleBriefError(str(exc)) from exc

    payload = {
        "model": model.strip(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You prepare concise translation context for academic papers. "
                    "Return exactly one JSON object and no markdown."
                ),
            },
            {
                "role": "user",
                "content": _article_brief_prompt(document, target_lang),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    extra_body = _minimax_extra_body(normalized_base_url, model)
    if extra_body:
        payload.update(extra_body)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{normalized_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key.strip()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        raise ArticleBriefError(
            "Article brief model request failed: "
            f"HTTP {exc.response.status_code}: {detail}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ArticleBriefError(f"Article brief model request failed: {exc}") from exc

    content = _message_content(response.json())
    brief = _parse_article_brief(content)
    return brief.model_copy(
        update={"quality_flags": _unique([*brief.quality_flags, "article_brief_model_generated"])},
        deep=True,
    )


def build_deterministic_article_brief(document: DocumentIR) -> ArticleBrief:
    title = _document_title(document)
    abstract = _first_role_text(document, BlockRole.ABSTRACT)
    headings = _role_texts(document, BlockRole.HEADING, limit=6)
    paragraphs = _role_texts(document, BlockRole.PARAGRAPH, limit=3)
    background = _compact(abstract or " ".join(paragraphs), 700)
    main_idea = _compact(" > ".join(headings) or background, 500)
    key_terms = _deterministic_key_terms(" ".join([title, *headings, background]))
    return ArticleBrief(
        title=title,
        field="unknown",
        background=background,
        main_idea=main_idea,
        contribution="",
        key_terms=key_terms,
        quality_flags=[
            "article_brief_model_skipped_deterministic_mode",
            "article_brief_low_confidence",
        ],
    )


def _article_brief_prompt(document: DocumentIR, target_lang: str) -> str:
    sample = _document_sample(document)
    expected = {
        "schema_version": "0.1",
        "title": "paper title",
        "field": "academic field or domain",
        "background": "brief background needed by a translator",
        "main_idea": "central claim, method, or topic",
        "contribution": "main contribution or finding",
        "key_terms": {
            "source professional term": "natural target-language translation or keep-original note"
        },
        "quality_flags": [],
    }
    return (
        "Read this extracted academic paper text and create translation context for "
        f"target language {target_lang}. Focus on the paper background, central idea, "
        "and professional terminology that should stay consistent across chunks. "
        "Use concise natural target-language term translations in key_terms when useful; "
        "keep proper nouns unchanged when that is standard. Do not include page numbers, "
        "coordinates, bbox, width, height, x/y, or any layout fields.\n\n"
        f"Expected JSON shape:\n{json.dumps(expected, ensure_ascii=False)}\n\n"
        f"Document text sample:\n{sample}"
    )


def _document_sample(document: DocumentIR, limit: int = 9000) -> str:
    rows: list[str] = []
    for page in document.pages:
        for block in sorted(page.blocks, key=lambda item: item.reading_order):
            text = _compact(block.text_for_translation or block.source_text, 1200)
            if not text:
                continue
            rows.append(f"[{block.role.value}] {text}")
            if len("\n".join(rows)) >= limit:
                break
        if len("\n".join(rows)) >= limit:
            break
    return _compact("\n".join(rows), limit)


def _parse_article_brief(content: object) -> ArticleBrief:
    if isinstance(content, dict):
        payload = content
    elif isinstance(content, str):
        payload = _extract_json_object(content)
    else:
        raise ArticleBriefError(
            f"Article brief response had unsupported content type: {type(content).__name__}"
        )
    if not isinstance(payload, dict):
        raise ArticleBriefError("Article brief response did not contain a JSON object")
    payload.setdefault("schema_version", "0.1")
    try:
        return ArticleBrief.model_validate(payload)
    except Exception as exc:
        raise ArticleBriefError(f"Article brief validation failed: {exc}") from exc


def _message_content(payload: dict[str, Any]) -> object:
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ArticleBriefError("Article brief response did not contain choices[0].message.content") from exc


def _extract_json_object(content: str) -> dict[str, Any] | None:
    sanitized = re.sub(r"<think\b[^>]*>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)
    try:
        parsed = json.loads(sanitized)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    decoder = json.JSONDecoder()
    for index, character in enumerate(sanitized):
        if character != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(sanitized, index)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def _document_title(document: DocumentIR) -> str:
    return _first_role_text(document, BlockRole.TITLE)


def _first_role_text(document: DocumentIR, role: BlockRole) -> str:
    texts = _role_texts(document, role, limit=1)
    return texts[0] if texts else ""


def _role_texts(document: DocumentIR, role: BlockRole, *, limit: int) -> list[str]:
    texts: list[str] = []
    for page in document.pages:
        for block in sorted(page.blocks, key=lambda item: item.reading_order):
            if block.role == role:
                text = _compact(block.text_for_translation or block.source_text, 1200)
                if text:
                    texts.append(text)
                    if len(texts) >= limit:
                        return texts
    return texts


def _deterministic_key_terms(text: str, limit: int = 12) -> dict[str, str]:
    terms: dict[str, str] = {}
    candidates = re.findall(
        r"\b(?:[A-Z][A-Za-z0-9-]{2,}|[a-z][A-Za-z0-9-]{3,})(?:\s+(?:[A-Z][A-Za-z0-9-]{2,}|[a-z][A-Za-z0-9-]{3,})){1,4}\b",
        text,
    )
    for candidate in candidates:
        normalized = re.sub(r"\s+", " ", candidate).strip()
        if not normalized or normalized.lower().startswith(("this ", "that ", "these ")):
            continue
        terms.setdefault(normalized, "")
        if len(terms) >= limit:
            break
    return terms


def _compact(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _minimax_extra_body(base_url: str, model: str) -> dict[str, Any]:
    hostname = (urlparse(base_url).hostname or "").lower()
    normalized_model = model.lower()
    is_minimax = (
        hostname in {"api.minimax.io", "api.minimaxi.com"}
        or hostname.endswith(".minimax.io")
        or hostname.endswith(".minimaxi.com")
        or "minimax-m" in normalized_model
        or "minimax/m" in normalized_model
    )
    if not is_minimax:
        return {}
    extra: dict[str, Any] = {"reasoning_split": True}
    if "minimax-m3" in normalized_model or "minimax/m3" in normalized_model:
        extra["thinking"] = {"type": "disabled"}
    return extra


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result
