from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx
from pdf_translator_schema import FormulaRecognitionResult
from pydantic import ValidationError

from ...provider_config import ProviderConfigError, normalize_openai_base_url
from .detector import FormulaCandidate


class FormulaRecognitionError(RuntimeError):
    pass


class OpenAIFormulaRecognizer:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        asset_base_path: Path,
        timeout_seconds: float = 60.0,
    ) -> None:
        try:
            self.base_url = normalize_openai_base_url(base_url)
        except ProviderConfigError as exc:
            raise FormulaRecognitionError(str(exc)) from exc
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.asset_base_path = asset_base_path
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise FormulaRecognitionError("Formula recognition requires an API key")
        if not self.model:
            raise FormulaRecognitionError("Formula recognition requires a model")

    async def recognize(self, candidate: FormulaCandidate) -> FormulaRecognitionResult:
        image_data_url = self._candidate_image_data_url(candidate)
        if image_data_url is None:
            raise FormulaRecognitionError(
                f"Formula candidate {candidate.candidate_id} has no image crop"
            )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You convert a single academic formula image to LaTeX. "
                        "Return one JSON object only with keys: latex, display_mode, "
                        "confidence, quality_flags. Do not include page, bbox, x/y, "
                        "width/height, or any layout coordinates."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Recognize this formula. Use display_mode \"display\" "
                                "for standalone equations. Nearby extracted text: "
                                f"{candidate.source_text[:500]}"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url},
                        },
                    ],
                },
            ],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        content = _response_content(response.json())
        raw = _extract_json_object(content)
        try:
            return FormulaRecognitionResult.model_validate(raw)
        except ValidationError as exc:
            raise FormulaRecognitionError(
                f"Formula recognition response failed schema validation: {exc}"
            ) from exc

    def _candidate_image_data_url(self, candidate: FormulaCandidate) -> str | None:
        if not candidate.image_path:
            return None
        path = self._resolve_asset_path(candidate.image_path)
        if path is None or not path.exists():
            return None
        extension = path.suffix.lower()
        mime_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(extension, "image/png")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _resolve_asset_path(self, asset_path: str) -> Path | None:
        if asset_path.startswith("/api/documents/"):
            filename = asset_path.rsplit("/", 1)[-1]
            return self.asset_base_path / filename
        path = Path(asset_path)
        if path.is_absolute():
            return path
        return self.asset_base_path / path


def _response_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise FormulaRecognitionError("Formula recognizer returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "\n".join(parts)
    raise FormulaRecognitionError("Formula recognizer returned no text content")


def _extract_json_object(content: str) -> dict[str, Any]:
    stripped = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if match is None:
            raise FormulaRecognitionError("Formula recognizer did not return JSON")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise FormulaRecognitionError("Formula recognizer returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise FormulaRecognitionError("Formula recognizer JSON must be an object")
    return payload
