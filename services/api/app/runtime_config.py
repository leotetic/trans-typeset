from __future__ import annotations

from pdf_translator_schema import RenderDefaults

from .config import settings
from .models import RuntimeConfig
from .storage import Storage


def _render_defaults_from_payload(payload: object, target_lang: str | None = None) -> RenderDefaults:
    if isinstance(payload, dict):
        values = dict(payload)
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


def effective_runtime_config(storage: Storage) -> dict:
    persisted = storage.read_runtime_config()
    default_target_lang = str(
        persisted.get("default_target_lang", settings.default_target_lang)
    )
    render_defaults = _render_defaults_from_payload(
        persisted.get("render_defaults"),
        default_target_lang,
    )
    provider_config = {
        "openai_base_url": str(
            persisted.get("openai_base_url", settings.openai_base_url)
        )
        .strip()
        .rstrip("/"),
        "openai_api_key": str(
            persisted.get("openai_api_key", settings.openai_api_key)
        ).strip(),
        "openai_model": str(persisted.get("openai_model", settings.openai_model)).strip(),
    }
    if settings.openai_api_key_from_env:
        provider_config = {
            "openai_base_url": settings.openai_base_url,
            "openai_api_key": settings.openai_api_key,
            "openai_model": settings.openai_model,
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
        "render_defaults": render_defaults,
    }


def render_defaults_for_target(storage: Storage, target_lang: str) -> RenderDefaults:
    configured = effective_runtime_config(storage)["render_defaults"]
    return configured.model_copy(update={"target_lang": target_lang}, deep=True)


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
        render_defaults=effective["render_defaults"],
    )
