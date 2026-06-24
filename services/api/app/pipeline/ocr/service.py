from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdf_translator_schema.models import OCRRecognitionResult

from ..formulas.detector import FormulaCandidate
from ..formulas.validation import validate_formula_latex
from .preprocess import image_sha256, preprocess_region_image
from .providers import DeterministicOCRProvider, OCRProvider


@dataclass
class OCRService:
    providers: list[OCRProvider] = field(default_factory=lambda: [DeterministicOCRProvider()])
    asset_base_path: Path | None = None
    min_confidence: float = 0.35
    provider_timeout_seconds: float = 12.0
    max_visual_candidates: int = 4
    min_text_confidence: float = 0.65
    on_record: Callable[[dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        self.records: list[dict] = []
        self._cache: dict[str, OCRRecognitionResult] = {}
        self._visual_attempts = 0
        self._lock = asyncio.Lock()

    async def recognize_formula(
        self,
        candidate: FormulaCandidate,
        *,
        prefer_visual: bool = False,
    ) -> OCRRecognitionResult:
        image_path = self._resolve_image_path(candidate.image_path)
        visual_candidate = image_path is not None and image_path.exists()
        text_candidate = candidate.source_kind.value in {"text_layer", "inline_text"}
        preprocessed_path = image_path
        preprocess_flags: list[str] = []
        cache_key = f"text:{candidate.candidate_id}:{candidate.source_text}"
        if visual_candidate:
            preprocessed_path, preprocess_flags = preprocess_region_image(image_path)
            try:
                cache_key = f"image:{image_sha256(preprocessed_path)}:{candidate.display_mode}"
            except Exception:
                cache_key = f"image:{candidate.candidate_id}:{preprocessed_path}"
        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                cache_record = {
                    "candidate_id": candidate.candidate_id,
                    "provider": cached.provider,
                    "status": "cache_hit",
                    "confidence": cached.confidence,
                }
                self.records.append(cache_record)
            else:
                cache_record = None
        if cached is not None:
            self._emit_record(cache_record or {})
            return cached

        attempts: list[dict] = []
        best: OCRRecognitionResult | None = None
        if visual_candidate:
            async with self._lock:
                self._visual_attempts += 1
                visual_attempts = self._visual_attempts
            if self.max_visual_candidates >= 0 and visual_attempts > self.max_visual_candidates:
                best = OCRRecognitionResult(
                    latex=candidate.source_text,
                    text=candidate.source_text,
                    region_kind="formula",
                    provider="deterministic",
                    confidence=0.1,
                    quality_flags=[
                        "ocr_visual_candidate_limit_reached",
                        "formula_recognition_mock",
                    ],
                )
                attempts.append(
                    {
                        "provider": "deterministic",
                        "status": "skipped",
                        "confidence": best.confidence,
                        "quality_flags": best.quality_flags,
                    }
                )
                skipped_record = {
                    "candidate_id": candidate.candidate_id,
                    "source_kind": candidate.source_kind.value,
                    "display_mode": candidate.display_mode,
                    "status": "recognized" if best.latex or best.text else "empty",
                    "provider": best.provider,
                    "confidence": best.confidence,
                    "attempts": attempts,
                }
                async with self._lock:
                    self._cache[cache_key] = best
                    self.records.append(skipped_record)
                self._emit_record(skipped_record)
                return best

        for provider in _ordered_providers(self.providers, prefer_visual=prefer_visual):
            provider_name = getattr(provider, "name", provider.__class__.__name__)
            if (
                text_candidate
                and provider_name in {"pix2text", "openai_vision", "minimax_vision"}
                and not visual_candidate
            ):
                continue
            self._emit_record(
                {
                    "candidate_id": candidate.candidate_id,
                    "provider": provider_name,
                    "status": "started",
                }
            )
            attempt_started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    provider.recognize_formula(
                        candidate,
                        image_path=preprocessed_path
                        if preprocessed_path and preprocessed_path.exists()
                        else None,
                    ),
                    timeout=self.provider_timeout_seconds,
                )
            except TimeoutError:
                attempts.append(
                    {
                        "provider": provider_name,
                        "status": "failed",
                        "error": f"{provider_name} timed out",
                        "quality_flags": [f"{provider_name}_timeout"],
                        "elapsed_ms": round((time.perf_counter() - attempt_started) * 1000, 2),
                    }
                )
                self._emit_record(
                    {
                        "candidate_id": candidate.candidate_id,
                        "provider": provider_name,
                        "status": "failed",
                        "error": f"{provider_name} timed out",
                        "quality_flags": [f"{provider_name}_timeout"],
                    }
                )
                continue
            except Exception as exc:
                attempts.append(
                    {
                        "provider": provider_name,
                        "status": "failed",
                        "error": str(exc),
                        "quality_flags": ["ocr_provider_unavailable"],
                        "elapsed_ms": round((time.perf_counter() - attempt_started) * 1000, 2),
                    }
                )
                self._emit_record(
                    {
                        "candidate_id": candidate.candidate_id,
                        "provider": provider_name,
                        "status": "failed",
                        "error": str(exc),
                        "quality_flags": ["ocr_provider_unavailable"],
                    }
                )
                continue
            result = result.model_copy(
                update={
                    "quality_flags": _unique(
                        [*preprocess_flags, *result.quality_flags]
                    )
                },
                deep=True,
            )
            attempts.append(
                {
                    "provider": result.provider,
                    "status": "recognized" if result.latex or result.text else "empty",
                    "confidence": result.confidence,
                    "quality_flags": result.quality_flags,
                    "elapsed_ms": round((time.perf_counter() - attempt_started) * 1000, 2),
                    "validator_status": validate_formula_latex(
                        result.latex or result.text,
                        source_text=candidate.source_text,
                        display_mode=candidate.display_mode,
                    ).status,
                }
            )
            self._emit_record(
                {
                    "candidate_id": candidate.candidate_id,
                    "provider": result.provider,
                    "status": "recognized" if result.latex or result.text else "empty",
                    "confidence": result.confidence,
                    "quality_flags": result.quality_flags,
                }
            )
            validation = validate_formula_latex(
                result.latex or result.text,
                source_text=candidate.source_text,
                display_mode=candidate.display_mode,
            )
            if best is None or result.confidence > best.confidence:
                best = result
            confidence_threshold = (
                self.min_text_confidence
                if text_candidate and not visual_candidate
                else self.min_confidence
            )
            if (
                (result.latex or result.text)
                and validation.accepted
                and result.confidence >= confidence_threshold
            ):
                best = result
                break

        if best is None:
            best = OCRRecognitionResult(
                region_kind="formula",
                provider="none",
                confidence=0.0,
                quality_flags=["ocr_provider_unavailable"],
            )
        record = {
            "candidate_id": candidate.candidate_id,
            "source_kind": candidate.source_kind.value,
            "display_mode": candidate.display_mode,
            "status": "recognized" if best.latex or best.text else "empty",
            "provider": best.provider,
            "confidence": best.confidence,
            "attempts": attempts,
        }
        async with self._lock:
            self._cache[cache_key] = best
            self.records.append(record)
        self._emit_record(record)
        return best

    def diagnostics(self) -> dict:
        provider_order = [
            str(getattr(provider, "name", provider.__class__.__name__))
            for provider in self.providers
        ]
        return {
            "kind": "ocr_diagnostics",
            "active_provider_order": provider_order,
            "provider_order": provider_order,
            "record_count": len(self.records),
            "records": self.records,
            "quality_flags": _unique(
                [
                    flag
                    for record in self.records
                    for attempt in record.get("attempts", [])
                    for flag in attempt.get("quality_flags", [])
                ]
            ),
        }

    def _resolve_image_path(self, image_path: str | None) -> Path | None:
        if not image_path:
            return None
        if image_path.startswith("/api/documents/"):
            filename = image_path.rsplit("/", 1)[-1]
            return self.asset_base_path / filename if self.asset_base_path else None
        return Path(image_path)

    def _emit_record(self, record: dict[str, Any]) -> None:
        if self.on_record is None:
            return
        try:
            self.on_record(record)
        except Exception:
            return


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _ordered_providers(
    providers: list[OCRProvider],
    *,
    prefer_visual: bool,
) -> list[OCRProvider]:
    if not prefer_visual:
        return providers
    visual_names = {"pix2text", "openai_vision", "minimax_vision"}
    preferred = [
        provider
        for provider in providers
        if getattr(provider, "name", provider.__class__.__name__) in visual_names
    ]
    fallback = [
        provider
        for provider in providers
        if getattr(provider, "name", provider.__class__.__name__) not in visual_names
    ]
    return [*preferred, *fallback]
