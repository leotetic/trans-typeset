from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import dotenv_values, find_dotenv
except Exception:  # pragma: no cover - dotenv is an application dependency.
    dotenv_values = None
    find_dotenv = None


@dataclass(frozen=True)
class Settings:
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_api_key_from_env: bool = False
    openai_model: str = "gpt-4.1-mini"
    storage_dir: Path = Path("data")
    default_target_lang: str = "zh-CN"
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    allowed_target_langs: tuple[str, ...] = ("zh-CN", "zh-TW", "ja-JP", "ko-KR", "en-US")
    max_upload_bytes: int = 50 * 1024 * 1024
    translation_concurrency: int = 2
    translator_max_attempts: int = 2
    render_font_stack: tuple[str, ...] = (
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Arial Unicode MS",
        "sans-serif",
    )
    render_line_height: float = 1.35
    render_paragraph_spacing_em: float = 0.45
    render_min_font_scale: float = 0.86


def parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_int(value: str, fallback: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def parse_float(value: str, fallback: float) -> float:
    try:
        parsed = float(value)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def _dotenv_values() -> dict[str, str]:
    if dotenv_values is None or find_dotenv is None:
        return {}
    env_path = find_dotenv(usecwd=True)
    if not env_path:
        return {}
    return {
        key: value
        for key, value in dotenv_values(env_path).items()
        if isinstance(value, str)
    }


def _provider_value(dotenv_config: dict[str, str], key: str, fallback: str) -> str:
    value = dotenv_config.get(key)
    if value is not None:
        return value
    return os.getenv(key, fallback)


def load_settings() -> Settings:
    default_cors = "http://localhost:5173,http://127.0.0.1:5173"
    default_langs = "zh-CN,zh-TW,ja-JP,ko-KR,en-US"
    dotenv_config = _dotenv_values()
    openai_api_key = _provider_value(dotenv_config, "OPENAI_API_KEY", "").strip()
    openai_model = (
        _provider_value(dotenv_config, "OPENAI_MODEL", "gpt-4.1-mini").strip()
        or "gpt-4.1-mini"
    )
    return Settings(
        openai_base_url=_provider_value(
            dotenv_config,
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
        )
        .strip()
        .rstrip("/"),
        openai_api_key=openai_api_key,
        openai_api_key_from_env=bool(openai_api_key),
        openai_model=openai_model,
        storage_dir=Path(os.getenv("STORAGE_DIR", "data")),
        default_target_lang=os.getenv("DEFAULT_TARGET_LANG", "zh-CN"),
        cors_origins=parse_csv(os.getenv("CORS_ORIGINS", default_cors)),
        allowed_target_langs=parse_csv(os.getenv("ALLOWED_TARGET_LANGS", default_langs)),
        max_upload_bytes=parse_int(os.getenv("MAX_UPLOAD_BYTES", ""), 50 * 1024 * 1024),
        translation_concurrency=parse_int(os.getenv("TRANSLATION_CONCURRENCY", ""), 2),
        translator_max_attempts=parse_int(os.getenv("TRANSLATOR_MAX_ATTEMPTS", ""), 2),
        render_font_stack=parse_csv(
            os.getenv(
                "RENDER_FONT_STACK",
                "Noto Sans CJK SC,Source Han Sans SC,Arial Unicode MS,sans-serif",
            )
        ),
        render_line_height=parse_float(os.getenv("RENDER_LINE_HEIGHT", ""), 1.35),
        render_paragraph_spacing_em=parse_float(
            os.getenv("RENDER_PARAGRAPH_SPACING_EM", ""),
            0.45,
        ),
        render_min_font_scale=parse_float(os.getenv("RENDER_MIN_FONT_SCALE", ""), 0.86),
    )


settings = load_settings()
