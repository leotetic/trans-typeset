from __future__ import annotations

import logging
import sys

from pdf_translator_schema import (
    DEFAULT_RENDER_DEFAULTS,
    LayoutMode,
    RenderDefaults,
    TypesettingStandard,
    UserIntent,
)

from .config import settings
from .models import RuntimeConfig
from .provider_config import ProviderConfigError, normalize_openai_base_url
from .storage import Storage

_LEGACY_SANS_FONT_STACK = [
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "Arial Unicode MS",
    "sans-serif",
]
_GBT_FONT_STACK = list(DEFAULT_RENDER_DEFAULTS["font_stack"])
_DETERMINISTIC_CHUNK_MAX_CHARS = 6000
_MODEL_CHUNK_MAX_CHARS = 3500
_MODEL_TRANSLATION_CONCURRENCY_LIMIT = 4
_LEGACY_DEFAULT_OCR_PROVIDER_ORDER = ["pix2text", "openai_vision", "deterministic"]
logger = logging.getLogger(__name__)


def _render_defaults_from_payload(payload: object, target_lang: str | None = None) -> RenderDefaults:
    if isinstance(payload, dict):
        values = {
            key: value
            for key, value in payload.items()
            if key in RenderDefaults.model_fields
        }
        if target_lang is not None:
            values["target_lang"] = target_lang
        return RenderDefaults.model_validate(values)
    defaults = RenderDefaults(
        target_lang=target_lang or settings.default_target_lang,
        font_stack=list(settings.render_font_stack),
        line_height=settings.render_line_height,
        paragraph_spacing_em=settings.render_paragraph_spacing_em,
        overflow_policy={"min_font_scale": settings.render_min_font_scale},
    )
    return defaults


def _bool_from_payload(value: object, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return fallback


def _ocr_provider_order_from_payload(payload: object) -> list[str]:
    if isinstance(payload, str):
        configured = [item.strip() for item in payload.split(",") if item.strip()]
    elif isinstance(payload, (list, tuple)):
        configured = [str(item).strip() for item in payload if str(item).strip()]
    else:
        configured = list(settings.ocr_provider_order)
    if not configured or configured == _LEGACY_DEFAULT_OCR_PROVIDER_ORDER:
        return list(settings.ocr_provider_order)
    return configured


def effective_runtime_config(storage: Storage) -> dict:
    persisted = storage.read_runtime_config()
    default_target_lang = str(
        persisted.get("default_target_lang", settings.default_target_lang)
    )
    render_defaults = _render_defaults_from_payload(
        persisted.get("render_defaults"),
        default_target_lang,
    )
    try:
        openai_base_url = normalize_openai_base_url(
            str(persisted.get("openai_base_url", settings.openai_base_url))
        )
    except ProviderConfigError:
        openai_base_url = settings.openai_base_url

    provider_config = {
        "openai_base_url": openai_base_url,
        "openai_api_key": str(
            persisted.get("openai_api_key", settings.openai_api_key)
        ).strip(),
        "openai_model": str(persisted.get("openai_model", settings.openai_model)).strip(),
        "layout_planner_model": str(
            persisted.get("layout_planner_model", settings.layout_planner_model)
        ).strip()
        or settings.openai_model,
        "vision_analyzer_model": str(
            persisted.get("vision_analyzer_model", settings.vision_analyzer_model)
        ).strip()
        or settings.openai_model,
        "minimax_api_key": str(
            persisted.get("minimax_api_key", settings.minimax_api_key)
        ).strip(),
        "minimax_endpoint": str(
            persisted.get("minimax_endpoint", settings.minimax_endpoint)
        ).strip()
        or settings.minimax_endpoint,
        "minimax_model": str(
            persisted.get("minimax_model", settings.minimax_model)
        ).strip()
        or settings.minimax_model,
    }
    if settings.openai_api_key_from_env:
        provider_config = {
            "openai_base_url": settings.openai_base_url,
            "openai_api_key": settings.openai_api_key,
            "openai_model": settings.openai_model,
            "layout_planner_model": settings.layout_planner_model or settings.openai_model,
            "vision_analyzer_model": settings.vision_analyzer_model or settings.openai_model,
            "minimax_api_key": settings.minimax_api_key or settings.openai_api_key,
            "minimax_endpoint": settings.minimax_endpoint,
            "minimax_model": settings.minimax_model,
        }
    if not provider_config["minimax_api_key"]:
        provider_config["minimax_api_key"] = provider_config["openai_api_key"]

    has_model_key = bool(provider_config["openai_api_key"])
    configured_concurrency = int(
        persisted.get("translation_concurrency", settings.translation_concurrency)
    )
    translation_concurrency = (
        min(configured_concurrency, _MODEL_TRANSLATION_CONCURRENCY_LIMIT)
        if has_model_key
        else configured_concurrency
    )
    configured_chunk_max_chars = int(
        persisted.get(
            "translation_chunk_max_chars",
            settings.translation_chunk_max_chars,
        )
        or 0
    )
    translation_chunk_max_chars = (
        configured_chunk_max_chars
        if configured_chunk_max_chars > 0
        else (
            _MODEL_CHUNK_MAX_CHARS
            if has_model_key
            else _DETERMINISTIC_CHUNK_MAX_CHARS
        )
    )

    config = {
        "default_target_lang": default_target_lang,
        **provider_config,
        "translation_concurrency": translation_concurrency,
        "translator_max_attempts": int(
            persisted.get("translator_max_attempts", settings.translator_max_attempts)
        ),
        "translation_chunk_max_chars": translation_chunk_max_chars,
        "agent_max_repair_attempts": int(
            persisted.get(
                "agent_max_repair_attempts",
                settings.agent_max_repair_attempts,
            )
        ),
        "agent_enable_vision_analysis": _bool_from_payload(
            persisted.get(
                "agent_enable_vision_analysis",
                settings.agent_enable_vision_analysis,
            ),
            settings.agent_enable_vision_analysis,
        ),
        "ocr_provider_order": _ocr_provider_order_from_payload(
            persisted.get("ocr_provider_order", settings.ocr_provider_order)
        ),
        "ocr_min_confidence": float(
            persisted.get("ocr_min_confidence", settings.ocr_min_confidence)
            or settings.ocr_min_confidence
        ),
        "ocr_provider_timeout_seconds": float(
            persisted.get(
                "ocr_provider_timeout_seconds",
                settings.ocr_provider_timeout_seconds,
            )
            or settings.ocr_provider_timeout_seconds
        ),
        "ocr_max_visual_candidates": int(
            persisted.get("ocr_max_visual_candidates", settings.ocr_max_visual_candidates)
            or settings.ocr_max_visual_candidates
        ),
        "render_defaults": render_defaults,
    }
    if (
        sys.version_info >= (3, 14)
        and "pix2text" in config["ocr_provider_order"]
    ):
        logger.warning(
            "Pix2Text OCR is enabled on Python %s.%s; Python 3.11/3.12 is recommended "
            "for this optional provider.",
            sys.version_info.major,
            sys.version_info.minor,
        )
    return config


def render_defaults_for_target(storage: Storage, target_lang: str) -> RenderDefaults:
    configured = effective_runtime_config(storage)["render_defaults"]
    return configured.model_copy(update={"target_lang": target_lang}, deep=True)


def render_defaults_for_intent(
    storage: Storage,
    target_lang: str,
    intent: UserIntent,
) -> RenderDefaults:
    configured = render_defaults_for_target(storage, target_lang)
    if intent.typesetting_standard == TypesettingStandard.GB_T_7713_1_2025:
        updates: dict[str, object] = {
            "layout_mode": LayoutMode.CONTINUOUS_REFLOW,
            # GB/T 7713.1: display formulas carry sequential right-aligned "(n)".
            "formula_numbering": "parenthesized",
        }
        if configured.font_stack == _LEGACY_SANS_FONT_STACK:
            updates["font_stack"] = _GBT_FONT_STACK
        return configured.model_copy(update=updates, deep=True)
    return configured


def runtime_config_response(storage: Storage) -> RuntimeConfig:
    effective = effective_runtime_config(storage)
    return RuntimeConfig(
        default_target_lang=effective["default_target_lang"],
        allowed_target_langs=list(settings.allowed_target_langs),
        max_upload_bytes=settings.max_upload_bytes,
        translator_provider="openai-compatible"
        if effective["openai_api_key"]
        else "deterministic",
        openai_base_url=effective["openai_base_url"],
        openai_model=effective["openai_model"],
        openai_api_key_configured=bool(effective["openai_api_key"]),
        translation_concurrency=effective["translation_concurrency"],
        translator_max_attempts=effective["translator_max_attempts"],
        translation_chunk_max_chars=effective["translation_chunk_max_chars"],
        agent_max_repair_attempts=effective["agent_max_repair_attempts"],
        agent_enable_vision_analysis=effective["agent_enable_vision_analysis"],
        layout_planner_model=effective["layout_planner_model"],
        vision_analyzer_model=effective["vision_analyzer_model"],
        ocr_provider_order=list(effective["ocr_provider_order"]),
        ocr_min_confidence=effective["ocr_min_confidence"],
        ocr_provider_timeout_seconds=effective["ocr_provider_timeout_seconds"],
        ocr_max_visual_candidates=effective["ocr_max_visual_candidates"],
        render_defaults=effective["render_defaults"],
    )
