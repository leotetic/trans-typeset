from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .provider_config import normalize_openai_base_url

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
    minimax_api_key: str = ""
    minimax_endpoint: str = "https://api.minimaxi.com/v1/chat/completions"
    minimax_model: str = "MiniMax-M3"
    storage_dir: Path = Path("data")
    default_target_lang: str = "zh-CN"
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    allowed_target_langs: tuple[str, ...] = ("zh-CN", "zh-TW", "ja-JP", "ko-KR", "en-US")
    max_upload_bytes: int = 50 * 1024 * 1024
    translation_concurrency: int = 2
    translator_max_attempts: int = 2
    translation_chunk_max_chars: int = 0
    agent_max_repair_attempts: int = 2
    agent_enable_vision_analysis: bool = False
    layout_planner_model: str = ""
    vision_analyzer_model: str = ""
    ocr_provider_order: tuple[str, ...] = ("pix2text", "deterministic")
    ocr_min_confidence: float = 0.35
    ocr_provider_timeout_seconds: float = 12.0
    ocr_max_visual_candidates: int = 4
    extraction_backend: str = "mineru"
    mineru_backend: str = "pipeline"
    mineru_method: str = "auto"
    mineru_formula_enabled: bool = True
    mineru_table_enabled: bool = True
    mineru_timeout_seconds: int = 3600
    formula_recognition_mode: str = "pdf_primitive_replay"
    formula_recognition_concurrency: int = 8
    formula_visual_ocr_concurrency: int = 2
    render_font_stack: tuple[str, ...] = (
        "Times New Roman",
        "SimSun",
        "Songti SC",
        "Noto Serif CJK SC",
        "Source Han Serif SC",
        "serif",
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


def parse_bool(value: str, fallback: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return fallback


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
    minimax_api_key = (
        _provider_value(dotenv_config, "MINIMAX_API_KEY", "").strip()
        or _provider_value(dotenv_config, "MINIMAX_APIKEY", "").strip()
        or openai_api_key
    )
    minimax_endpoint = (
        _provider_value(
            dotenv_config,
            "MINIMAX_ENDPOINT",
            "https://api.minimaxi.com/v1/chat/completions",
        ).strip()
        or "https://api.minimaxi.com/v1/chat/completions"
    )
    minimax_model = (
        _provider_value(dotenv_config, "MINIMAX_MODEL", "MiniMax-M3").strip()
        or "MiniMax-M3"
    )
    layout_planner_model = (
        _provider_value(dotenv_config, "LAYOUT_PLANNER_MODEL", "").strip()
        or openai_model
    )
    vision_analyzer_model = (
        _provider_value(dotenv_config, "VISION_ANALYZER_MODEL", "").strip()
        or openai_model
    )
    raw_openai_base_url = _provider_value(
        dotenv_config,
        "OPENAI_BASE_URL",
        "https://api.openai.com/v1",
    )
    openai_base_url = normalize_openai_base_url(raw_openai_base_url)

    return Settings(
        openai_base_url=openai_base_url,
        openai_api_key=openai_api_key,
        openai_api_key_from_env=bool(openai_api_key),
        openai_model=openai_model,
        minimax_api_key=minimax_api_key,
        minimax_endpoint=minimax_endpoint,
        minimax_model=minimax_model,
        storage_dir=Path(os.getenv("STORAGE_DIR", "data")),
        default_target_lang=os.getenv("DEFAULT_TARGET_LANG", "zh-CN"),
        cors_origins=parse_csv(os.getenv("CORS_ORIGINS", default_cors)),
        allowed_target_langs=parse_csv(os.getenv("ALLOWED_TARGET_LANGS", default_langs)),
        max_upload_bytes=parse_int(os.getenv("MAX_UPLOAD_BYTES", ""), 50 * 1024 * 1024),
        translation_concurrency=parse_int(os.getenv("TRANSLATION_CONCURRENCY", ""), 2),
        translator_max_attempts=parse_int(os.getenv("TRANSLATOR_MAX_ATTEMPTS", ""), 2),
        translation_chunk_max_chars=parse_int(
            os.getenv("TRANSLATION_CHUNK_MAX_CHARS", ""),
            0,
        ),
        agent_max_repair_attempts=parse_int(
            os.getenv("AGENT_MAX_REPAIR_ATTEMPTS", ""),
            2,
        ),
        agent_enable_vision_analysis=parse_bool(
            os.getenv("AGENT_ENABLE_VISION_ANALYSIS", ""),
            False,
        ),
        layout_planner_model=layout_planner_model,
        vision_analyzer_model=vision_analyzer_model,
        ocr_provider_order=parse_csv(
            os.getenv(
                "OCR_PROVIDER_ORDER",
                "pix2text,deterministic",
            )
        ),
        ocr_min_confidence=parse_float(os.getenv("OCR_MIN_CONFIDENCE", ""), 0.35),
        ocr_provider_timeout_seconds=parse_float(
            os.getenv("OCR_PROVIDER_TIMEOUT_SECONDS", ""),
            12.0,
        ),
        ocr_max_visual_candidates=parse_int(
            os.getenv("OCR_MAX_VISUAL_CANDIDATES", ""),
            4,
        ),
        extraction_backend=os.getenv("EXTRACTION_BACKEND", "mineru").strip() or "mineru",
        mineru_backend=os.getenv("MINERU_BACKEND", "pipeline").strip() or "pipeline",
        mineru_method=os.getenv("MINERU_METHOD", "auto").strip() or "auto",
        mineru_formula_enabled=parse_bool(os.getenv("MINERU_FORMULA_ENABLED", ""), True),
        mineru_table_enabled=parse_bool(os.getenv("MINERU_TABLE_ENABLED", ""), True),
        mineru_timeout_seconds=parse_int(os.getenv("MINERU_TIMEOUT_SECONDS", ""), 3600),
        formula_recognition_mode=os.getenv(
            "FORMULA_RECOGNITION_MODE",
            "pdf_primitive_replay",
        ).strip()
        or "pdf_primitive_replay",
        formula_recognition_concurrency=parse_int(
            os.getenv("FORMULA_RECOGNITION_CONCURRENCY", ""),
            8,
        ),
        formula_visual_ocr_concurrency=parse_int(
            os.getenv("FORMULA_VISUAL_OCR_CONCURRENCY", ""),
            2,
        ),
        render_font_stack=parse_csv(
            os.getenv(
                "RENDER_FONT_STACK",
                "Times New Roman,SimSun,Songti SC,Noto Serif CJK SC,Source Han Serif SC,serif",
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
