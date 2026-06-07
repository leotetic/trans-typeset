from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    storage_dir: Path = Path("data")
    default_target_lang: str = "zh-CN"
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    allowed_target_langs: tuple[str, ...] = ("zh-CN", "zh-TW", "ja-JP", "ko-KR", "en-US")
    max_upload_bytes: int = 50 * 1024 * 1024


def parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_int(value: str, fallback: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def load_settings() -> Settings:
    default_cors = "http://localhost:5173,http://127.0.0.1:5173"
    default_langs = "zh-CN,zh-TW,ja-JP,ko-KR,en-US"
    return Settings(
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        storage_dir=Path(os.getenv("STORAGE_DIR", "data")),
        default_target_lang=os.getenv("DEFAULT_TARGET_LANG", "zh-CN"),
        cors_origins=parse_csv(os.getenv("CORS_ORIGINS", default_cors)),
        allowed_target_langs=parse_csv(os.getenv("ALLOWED_TARGET_LANGS", default_langs)),
        max_upload_bytes=parse_int(os.getenv("MAX_UPLOAD_BYTES", ""), 50 * 1024 * 1024),
    )


settings = load_settings()
