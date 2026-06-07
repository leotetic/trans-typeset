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


def load_settings() -> Settings:
    return Settings(
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        storage_dir=Path(os.getenv("STORAGE_DIR", "data")),
        default_target_lang=os.getenv("DEFAULT_TARGET_LANG", "zh-CN"),
    )


settings = load_settings()

