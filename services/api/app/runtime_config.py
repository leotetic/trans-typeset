from __future__ import annotations

from pdf_translator_schema import LayoutMode, RenderDefaults, TypesettingStandard, UserIntent

from .config import settings
from .models import RuntimeConfig
from .provider_config import ProviderConfigError, normalize_openai_base_url
from .storage import Storage


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
    }
    if settings.openai_api_key_from_env:
        provider_config = {
            "openai_base_url": settings.openai_base_url,
            "openai_api_key": settings.openai_api_key,
            "openai_model": settings.openai_model,
            "layout_planner_model": settings.layout_planner_model or settings.openai_model,
            "vision_analyzer_model": settings.vision_analyzer_model or settings.openai_model,
        }

    return {
        "default_target_lang": default_target_lang,
        **provider_config,
        "translation_concurrency": int(
            persisted.get("translation_concurrency", settings.translation_concurrency)
        ),
        "translator_max_attempts": int(
            persisted.get("translator_max_attempts", settings.translator_max_attempts)
        ),
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
        "render_defaults": render_defaults,
    }


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
        return configured.model_copy(update={"layout_mode": LayoutMode.CONTINUOUS_REFLOW}, deep=True)
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
        agent_max_repair_attempts=effective["agent_max_repair_attempts"],
        agent_enable_vision_analysis=effective["agent_enable_vision_analysis"],
        layout_planner_model=effective["layout_planner_model"],
        vision_analyzer_model=effective["vision_analyzer_model"],
        render_defaults=effective["render_defaults"],
    )
