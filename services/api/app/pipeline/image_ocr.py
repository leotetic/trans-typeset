from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True)
class ImageOCRBlock:
    text: str
    role: str = "paragraph"


@dataclass(frozen=True)
class ImageOCRResult:
    blocks: list[ImageOCRBlock]
    provider: str
    quality_flags: list[str]
    diagnostics: dict[str, Any]


async def extract_image_text(
    *,
    image_path: Path,
    filename: str,
    mime_type: str | None,
    runtime_config: dict[str, Any],
) -> ImageOCRResult:
    if not _vision_configured(runtime_config):
        return deterministic_image_ocr_result(
            filename=filename,
            quality_flags=["vision_ocr_unconfigured", "deterministic_ocr_mock", "ocr_uncertain"],
        )
    try:
        payload = await _request_vision_ocr(
            image_path=image_path,
            filename=filename,
            mime_type=mime_type or _mime_type_for_path(image_path),
            runtime_config=runtime_config,
        )
        blocks = _blocks_from_payload(payload)
        if not blocks:
            return deterministic_image_ocr_result(
                filename=filename,
                quality_flags=["vision_ocr_empty", "deterministic_ocr_mock", "ocr_uncertain"],
            )
        quality_flags = _string_list(payload.get("quality_flags"))
        return ImageOCRResult(
            blocks=blocks,
            provider="vision_model",
            quality_flags=quality_flags or ["vision_ocr_extracted_text"],
            diagnostics={
                "kind": "image_ocr",
                "status": "completed",
                "provider": "vision_model",
                "model": _vision_model(runtime_config),
                "block_count": len(blocks),
                "quality_flags": quality_flags or ["vision_ocr_extracted_text"],
            },
        )
    except Exception as exc:
        return deterministic_image_ocr_result(
            filename=filename,
            quality_flags=["vision_ocr_failed", "deterministic_ocr_mock", "ocr_uncertain"],
            error=str(exc)[:500],
        )


def deterministic_image_ocr_result(
    *,
    filename: str,
    quality_flags: list[str] | None = None,
    error: str | None = None,
) -> ImageOCRResult:
    flags = quality_flags or ["deterministic_ocr_mock", "ocr_uncertain"]
    text = (
        f"Deterministic OCR fallback for {filename}. "
        "No configured vision OCR text was extracted."
    )
    diagnostics: dict[str, Any] = {
        "kind": "image_ocr",
        "status": "fallback",
        "provider": "deterministic",
        "block_count": 1,
        "quality_flags": flags,
    }
    if error:
        diagnostics["error"] = error
    return ImageOCRResult(
        blocks=[ImageOCRBlock(text=text)],
        provider="deterministic",
        quality_flags=flags,
        diagnostics=diagnostics,
    )


async def _request_vision_ocr(
    *,
    image_path: Path,
    filename: str,
    mime_type: str,
    runtime_config: dict[str, Any],
) -> dict[str, Any]:
    base_url, api_key = _vision_endpoint(runtime_config)
    model = _vision_model(runtime_config)
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_path.read_bytes()).decode('ascii')}"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract readable document text from the image. Return one JSON object "
                    "with blocks: [{text, role}] and quality_flags. Roles may be title, "
                    "heading, paragraph, caption, table, formula, reference, or unknown. "
                    "Never include coordinates, bbox, page fields, width, height, x, or y."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Extract text from {filename}."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    }
    async with httpx.AsyncClient(timeout=float(runtime_config.get("ocr_provider_timeout_seconds", 30))) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("vision OCR response did not contain choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return _extract_json_object(content)


def _vision_configured(runtime_config: dict[str, Any]) -> bool:
    return bool(runtime_config.get("agent_enable_vision_analysis") and _vision_endpoint(runtime_config)[1])


def _vision_endpoint(runtime_config: dict[str, Any]) -> tuple[str, str]:
    openai_key = str(runtime_config.get("openai_api_key") or "").strip()
    if openai_key:
        return str(runtime_config.get("openai_base_url") or "").rstrip("/"), openai_key
    minimax_key = str(runtime_config.get("minimax_api_key") or "").strip()
    minimax_endpoint = str(runtime_config.get("minimax_endpoint") or "").strip()
    if minimax_key and minimax_endpoint:
        return _base_url_from_chat_completions_url(minimax_endpoint), minimax_key
    return str(runtime_config.get("openai_base_url") or "").rstrip("/"), ""


def _vision_model(runtime_config: dict[str, Any]) -> str:
    return str(
        runtime_config.get("vision_analyzer_model")
        or runtime_config.get("openai_model")
        or runtime_config.get("minimax_model")
        or ""
    ).strip()


def _blocks_from_payload(payload: dict[str, Any]) -> list[ImageOCRBlock]:
    raw_blocks = payload.get("blocks")
    if isinstance(raw_blocks, list):
        blocks = []
        for item in raw_blocks:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            blocks.append(ImageOCRBlock(text=text, role=_safe_role(item.get("role"))))
        return blocks
    text = str(payload.get("text") or "").strip()
    if text:
        return [ImageOCRBlock(text=text)]
    return []


def _safe_role(value: object) -> str:
    role = str(value or "paragraph").strip().lower()
    allowed = {
        "title",
        "heading",
        "paragraph",
        "caption",
        "table",
        "formula",
        "reference",
        "unknown",
    }
    return role if role in allowed else "paragraph"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _extract_json_object(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", item.get("content", "")))
            if isinstance(item, dict)
            else str(item)
            for item in content
        )
    if not isinstance(content, str):
        raise ValueError("vision OCR content is not text or JSON")
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", content, flags=re.I | re.S).strip()
    fence_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("vision OCR JSON is not an object")
    return parsed


def _mime_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def _base_url_from_chat_completions_url(value: str) -> str:
    trimmed = value.strip().rstrip("/")
    parsed = urlparse(trimmed)
    if parsed.path.endswith("/chat/completions"):
        return trimmed[: -len("/chat/completions")].rstrip("/")
    return trimmed
