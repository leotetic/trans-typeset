from __future__ import annotations

from urllib.parse import urlparse, urlunparse


class ProviderConfigError(ValueError):
    pass


def normalize_openai_base_url(value: str) -> str:
    trimmed = value.strip().rstrip("/")
    if not trimmed:
        raise ProviderConfigError("Base URL is required")

    parsed = urlparse(trimmed)
    if parsed.scheme not in {"http", "https"}:
        raise ProviderConfigError("Base URL must start with http:// or https://")
    if not parsed.netloc or not parsed.hostname:
        raise ProviderConfigError("Base URL must include a host")
    if parsed.params or parsed.query or parsed.fragment:
        raise ProviderConfigError("Base URL must not include query parameters or fragments")

    path = parsed.path.rstrip("/")
    path_segments = [segment for segment in path.split("/") if segment]
    if not path_segments or path_segments[-1] != "v1":
        raise ProviderConfigError(
            "Base URL must point to an OpenAI-compatible /v1 API root, "
            "for example https://api.example.com/v1"
        )

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            "",
            "",
            "",
        )
    )
