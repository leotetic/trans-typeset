from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .models import RenderDocument

_CSS_CLASS_PATTERN = re.compile(r"[^a-z0-9-]+")


def _css_class(value: object) -> str:
    if hasattr(value, "value"):
        value = getattr(value, "value")
    css_class = str(value).strip().lower().replace("_", "-")
    return _CSS_CLASS_PATTERN.sub("-", css_class).strip("-") or "unknown"


def _css_font_stack(font_stack: list[str]) -> str:
    families: list[str] = []
    for family in font_stack:
        if family in {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"}:
            families.append(family)
        else:
            families.append(f'"{family}"')
    return ", ".join(families)


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("pdf_renderer", "templates"),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["css_class"] = _css_class
    env.filters["css_font_stack"] = _css_font_stack
    return env


def render_to_html(document: RenderDocument) -> str:
    template = _env().get_template("document.html.j2")
    return template.render(document=document)


async def render_to_pdf(html: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html, wait_until="networkidle")
        await page.pdf(
            path=str(output_path),
            print_background=True,
            prefer_css_page_size=True,
        )
        await browser.close()
    return output_path
