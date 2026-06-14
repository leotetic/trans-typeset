from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import ValidationError
from pdf_translator_schema import FormulaRecognitionResult
from pdf_translator_schema.models import OCRRecognitionResult

from ..formulas.detector import FormulaCandidate
from ..formulas.normalization import formula_corruption_flags, latex_from_pdf_text
from ..formulas.recognizer import (
    FormulaRecognitionError,
    OpenAIFormulaRecognizer,
    _extract_json_object,
    _response_content,
)
from ..formulas.validation import validate_formula_latex


class OCRProvider(Protocol):
    name: str

    async def recognize_formula(
        self,
        candidate: FormulaCandidate,
        *,
        image_path: Path | None = None,
    ) -> OCRRecognitionResult:
        ...


class Pix2TextOCRProvider:
    name = "pix2text"

    def __init__(self, *, timeout_seconds: float = 12.0) -> None:
        self._engine: object | None = None
        self._init_error: str | None = None
        self.timeout_seconds = timeout_seconds

    async def recognize_formula(
        self,
        candidate: FormulaCandidate,
        *,
        image_path: Path | None = None,
    ) -> OCRRecognitionResult:
        if image_path is None:
            return OCRRecognitionResult(
                latex="",
                text=candidate.source_text,
                region_kind="formula",
                provider=self.name,
                confidence=0.0,
                quality_flags=["ocr_visual_image_missing", "ocr_provider_unavailable"],
            )
        try:
            engine = await asyncio.wait_for(
                asyncio.to_thread(self._get_engine),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            self._init_error = "pix2text init timed out"
            return OCRRecognitionResult(
                region_kind="formula",
                provider=self.name,
                confidence=0.0,
                quality_flags=["pix2text_timeout", "pix2text_init_timeout"],
            )
        except Exception as exc:
            return OCRRecognitionResult(
                region_kind="formula",
                provider=self.name,
                confidence=0.0,
                quality_flags=["ocr_provider_unavailable", str(exc)[:120]],
            )
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._recognize_with_engine, engine, image_path),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            return OCRRecognitionResult(
                region_kind="formula",
                provider=self.name,
                confidence=0.0,
                quality_flags=["pix2text_timeout", "pix2text_formula_ocr_timeout"],
            )
        except Exception as exc:
            return OCRRecognitionResult(
                region_kind="formula",
                provider=self.name,
                confidence=0.0,
                quality_flags=["pix2text_formula_ocr_failed", str(exc)[:120]],
            )
        latex, confidence = _coerce_formula_output(raw)
        validation = validate_formula_latex(
            latex,
            source_text=candidate.source_text,
            display_mode=candidate.display_mode,
        )
        quality_flags = [] if latex else ["pix2text_formula_ocr_empty"]
        quality_flags.extend(validation.quality_flags)
        return OCRRecognitionResult(
            text=latex,
            latex=latex,
            region_kind="formula",
            provider=self.name,
            confidence=confidence,
            quality_flags=_unique(quality_flags),
        )

    def _get_engine(self) -> object:
        if self._engine is not None:
            return self._engine
        if self._init_error:
            raise RuntimeError(self._init_error)
        try:
            from pix2text import Pix2Text
        except Exception as exc:
            self._init_error = f"pix2text unavailable: {exc}"
            raise RuntimeError(self._init_error) from exc
        try:
            self._engine = Pix2Text.from_config(enable_table=False, device="cpu")
        except Exception as exc:
            self._init_error = f"pix2text init failed: {exc}"
            raise RuntimeError(self._init_error) from exc
        return self._engine

    def _recognize_with_engine(self, engine: object, image_path: Path) -> object:
        if hasattr(engine, "recognize_formula"):
            return engine.recognize_formula(str(image_path))
        if hasattr(engine, "recognize"):
            return engine.recognize(str(image_path))
        if hasattr(engine, "__call__"):
            return engine(str(image_path))
        raise RuntimeError("pix2text engine has no supported recognition method")


class OpenAIVisionOCRProvider:
    name = "openai_vision"

    def __init__(self, recognizer: OpenAIFormulaRecognizer) -> None:
        self.recognizer = recognizer

    async def recognize_formula(
        self,
        candidate: FormulaCandidate,
        *,
        image_path: Path | None = None,
    ) -> OCRRecognitionResult:
        result = await self.recognizer.recognize(candidate)
        return OCRRecognitionResult(
            text=result.latex,
            latex=result.latex,
            region_kind="formula",
            provider=self.name,
            confidence=result.confidence,
            quality_flags=result.quality_flags,
        )


class MiniMaxVisionOCRProvider:
    name = "minimax_vision"

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://api.minimaxi.com/v1/chat/completions",
        model: str = "MiniMax-M3",
        timeout_seconds: float = 60.0,
        max_completion_tokens: int = 700,
    ) -> None:
        self.api_key = api_key.strip()
        self.endpoint = endpoint.strip() or "https://api.minimaxi.com/v1/chat/completions"
        self.model = model.strip() or "MiniMax-M3"
        self.timeout_seconds = timeout_seconds
        self.max_completion_tokens = max_completion_tokens

    async def recognize_formula(
        self,
        candidate: FormulaCandidate,
        *,
        image_path: Path | None = None,
    ) -> OCRRecognitionResult:
        if not self.api_key:
            return OCRRecognitionResult(
                region_kind="formula",
                provider=self.name,
                confidence=0.0,
                quality_flags=["minimax_api_key_missing", "ocr_provider_unavailable"],
            )
        if image_path is None or not image_path.exists():
            return OCRRecognitionResult(
                text=candidate.source_text,
                region_kind="formula",
                provider=self.name,
                confidence=0.0,
                quality_flags=["ocr_visual_image_missing", "ocr_provider_unavailable"],
            )

        payload = self._build_payload(candidate, image_path)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.endpoint,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

        content = _response_content(response.json())
        raw = _extract_json_object(content)
        try:
            recognized = FormulaRecognitionResult.model_validate(raw)
        except ValidationError as exc:
            raise FormulaRecognitionError(
                f"MiniMax formula recognition response failed schema validation: {exc}"
            ) from exc

        latex, repair_flags = _strip_formula_delimiters(recognized.latex)
        validation = validate_formula_latex(
            latex,
            source_text=candidate.source_text,
            display_mode=recognized.display_mode,
        )
        quality_flags = _unique(
            [
                *recognized.quality_flags,
                *repair_flags,
                *validation.quality_flags,
            ]
        )
        return OCRRecognitionResult(
            text=latex,
            latex=latex,
            region_kind="formula",
            provider=self.name,
            confidence=recognized.confidence,
            quality_flags=quality_flags,
        )

    def _build_payload(self, candidate: FormulaCandidate, image_path: Path) -> dict:
        return {
            "model": self.model,
            "thinking": {"type": "adaptive"},
            "messages": [
                {
                    "role": "system",
                    "content": _MINIMAX_FORMULA_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": _minimax_formula_user_prompt(candidate),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": _image_data_url(image_path)},
                        },
                    ],
                },
            ],
            "max_completion_tokens": self.max_completion_tokens,
        }


class DeterministicOCRProvider:
    name = "deterministic"

    async def recognize_formula(
        self,
        candidate: FormulaCandidate,
        *,
        image_path: Path | None = None,
    ) -> OCRRecognitionResult:
        flags: list[str] = []
        latex, normalization_flags = latex_from_pdf_text(candidate.source_text)
        flags.extend(normalization_flags)
        confidence = 0.96 if latex else 0.0
        corruption_flags = formula_corruption_flags(
            candidate.source_text,
            normalized_latex=latex,
        )
        if any(
            flag in normalization_flags
            for flag in {
                "formula_delimiter_repaired",
                "formula_low_confidence",
                "formula_text_truncated",
                "formula_text_layer_corrupt",
                "formula_slash_glyph_suspect",
                "formula_prime_glyph_suspect",
            }
        ) or corruption_flags:
            confidence = min(confidence, 0.58)
            flags.append("formula_low_confidence")
            flags.extend(corruption_flags)
        if candidate.source_kind.value in {"image_candidate", "vector_candidate"}:
            flags.extend(
                [
                    "visual_formula_not_recognized_without_model",
                    "formula_recognition_mock",
                ]
            )
            confidence = 0.0
            latex = ""
        elif candidate.source_kind.value == "inline_text":
            flags.append("formula_inline_text_layer")
        else:
            flags.append("ocr_text_layer_passthrough")
        validation = validate_formula_latex(
            latex,
            source_text=candidate.source_text,
            display_mode=candidate.display_mode,
        )
        flags.extend(validation.quality_flags)
        return OCRRecognitionResult(
            text=latex,
            latex=latex,
            region_kind="formula",
            provider=self.name,
            confidence=confidence,
            quality_flags=_unique(flags),
        )


def _coerce_formula_output(raw: object) -> tuple[str, float]:
    if isinstance(raw, str):
        return raw.strip(), 0.86 if raw.strip() else 0.0
    if isinstance(raw, dict):
        for key in ("latex", "text", "markdown", "content"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                confidence = raw.get("confidence", raw.get("score", 0.86))
                return value.strip(), _coerce_confidence(confidence)
        if isinstance(raw.get("results"), list):
            return _coerce_formula_output(raw["results"])
    if isinstance(raw, list):
        parts: list[str] = []
        confidences: list[float] = []
        for item in raw:
            latex, confidence = _coerce_formula_output(item)
            if latex:
                parts.append(latex)
                confidences.append(confidence)
        if parts:
            return " ".join(parts), sum(confidences) / len(confidences)
    return "", 0.0


def _coerce_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.86
    return max(0.0, min(1.0, confidence))


_MINIMAX_FORMULA_SYSTEM_PROMPT = (
    "You are a formula OCR engine for academic papers. Recognize exactly one formula "
    "from the provided image and return one strict JSON object only. Do not return "
    "Markdown, prose, code fences, page information, coordinates, bbox, x/y, width, "
    "height, top, right, bottom, or left fields. The JSON object must contain exactly "
    "these keys: latex, display_mode, confidence, quality_flags. latex must be "
    "KaTeX-compatible LaTeX without surrounding $, $$, \\(...\\), or \\[...\\]. "
    "Preserve subscripts, superscripts, fractions, integrals, sums, Greek letters, "
    "matrices, bracket structure, and operators. Do not translate variable names and "
    "do not include surrounding prose. If an independent equation number appears at "
    "the right edge, encode it as \\tag{n} in latex."
)


def _minimax_formula_user_prompt(candidate: FormulaCandidate) -> str:
    return (
        "Recognize this academic formula image as LaTeX. "
        f"Expected display_mode: {candidate.display_mode}. "
        "Use display_mode \"display\" for a standalone equation and \"inline\" only "
        "for an inline math fragment. Return JSON only. "
        f"Nearby extracted text, which may be noisy and incomplete: {candidate.source_text[:500]}"
    )


def _image_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _strip_formula_delimiters(latex: str) -> tuple[str, list[str]]:
    stripped = latex.strip()
    repairs: list[str] = []
    pairs = (("$$", "$$"), ("$", "$"), (r"\(", r"\)"), (r"\[", r"\]"))
    for start, end in pairs:
        if stripped.startswith(start) and stripped.endswith(end) and len(stripped) > len(start) + len(end):
            stripped = stripped[len(start) : -len(end)].strip()
            repairs.append("formula_delimiter_repaired")
            break
    return stripped, repairs


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
