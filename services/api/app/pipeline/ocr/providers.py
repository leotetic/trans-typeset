from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from pdf_translator_schema.models import OCRRecognitionResult

from ..formulas.detector import FormulaCandidate
from ..formulas.normalization import latex_from_pdf_text
from ..formulas.recognizer import OpenAIFormulaRecognizer


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
                latex=candidate.source_text,
                text=candidate.source_text,
                region_kind="formula",
                provider=self.name,
                confidence=0.7 if candidate.source_text else 0.0,
                quality_flags=["ocr_text_layer_passthrough"],
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
        return OCRRecognitionResult(
            text=latex,
            latex=latex,
            region_kind="formula",
            provider=self.name,
            confidence=confidence,
            quality_flags=[] if latex else ["pix2text_formula_ocr_empty"],
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
        if any(
            flag in normalization_flags
            for flag in {
                "formula_delimiter_repaired",
                "formula_low_confidence",
                "formula_text_truncated",
            }
        ):
            confidence = min(confidence, 0.58)
            flags.append("formula_low_confidence")
        if candidate.source_kind.value in {"image_candidate", "vector_candidate"}:
            flags.extend(
                [
                    "visual_formula_not_recognized_without_model",
                    "formula_recognition_mock",
                ]
            )
            confidence = 0.1
        elif candidate.source_kind.value == "inline_text":
            flags.append("formula_inline_text_layer")
        else:
            flags.append("ocr_text_layer_passthrough")
        return OCRRecognitionResult(
            text=latex,
            latex=latex,
            region_kind="formula",
            provider=self.name,
            confidence=confidence,
            quality_flags=flags,
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
