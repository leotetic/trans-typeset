from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader
from pdf_translator_schema import DocumentIR, LayoutIntentPlan, TranslationLayoutPlan
from pdf_translator_schema.models import RenderDefaults

from .models import RenderDocument

_CSS_CLASS_PATTERN = re.compile(r"[^a-z0-9-]+")
_API_ASSET_SRC_PATTERN = re.compile(
    r'(?P<prefix>\bsrc=)(?P<quote>["\'])(?P<src>/api/documents/[^"\']+/assets/(?P<filename>[^/"\']+))(?P=quote)'
)
_KATEX_FONT_URL_PATTERN = re.compile(r"url\((?:'|\")?fonts/(?P<filename>KaTeX_[^'\"\)]+)(?:'|\")?\)")
_PT_PER_CSS_PX = 72.0 / 96.0
_BROWSER_REFLOW_SAFETY_PT = 2.0


def _css_class(value: object) -> str:
    if hasattr(value, "value"):
        value = value.value
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
    # NOTE: select_autoescape(["html", "xml"]) silently disabled escaping for
    # "*.html.j2" templates (extension is "j2"), letting raw PDF text reach the
    # HTML output. Autoescape is therefore forced on; CSS-context interpolations
    # in the template are explicitly marked |safe.
    env = Environment(
        loader=PackageLoader("pdf_renderer", "templates"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["css_class"] = _css_class
    env.filters["css_font_stack"] = _css_font_stack
    env.globals["katex_css"] = _load_katex_asset("katex.min.css")
    env.globals["katex_js"] = _load_katex_asset("katex.min.js")
    return env


def _load_katex_asset(filename: str) -> str:
    for root in [Path.cwd(), *Path(__file__).resolve().parents]:
        candidate = root / "node_modules" / "katex" / "dist" / filename
        if candidate.is_file():
            content = candidate.read_text(encoding="utf-8")
            if filename == "katex.min.css":
                return _inline_katex_font_urls(content, candidate.parent)
            return content
    return ""


def _inline_katex_font_urls(css: str, dist_dir: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        font_path = dist_dir / "fonts" / match.group("filename")
        if not font_path.is_file():
            return match.group(0)
        mime_type = mimetypes.guess_type(font_path.name)[0] or "font/woff2"
        payload = base64.b64encode(font_path.read_bytes()).decode("ascii")
        return f"url(data:{mime_type};base64,{payload})"

    return _KATEX_FONT_URL_PATTERN.sub(replace, css)


def render_to_html(document: RenderDocument) -> str:
    template = _env().get_template("document.html.j2")
    return template.render(document=document)


async def render_preview_with_browser_layout(
    document: DocumentIR,
    plans: list[TranslationLayoutPlan],
    target_lang: str,
    *,
    render_defaults: RenderDefaults | None = None,
    layout_intent_plan: LayoutIntentPlan | None = None,
    asset_base_path: Path | None = None,
    max_iterations: int = 3,
) -> tuple[str, RenderDocument, dict[str, Any]]:
    measured_min_heights: dict[str, float] = {}
    layout_iterations: list[dict[str, Any]] = []
    render_document: RenderDocument | None = None
    html = ""
    page_diagnostics: dict[str, Any] | None = None

    for iteration in range(1, max(1, max_iterations) + 1):
        render_document = RenderDocument.from_ir_and_plans(
            document,
            plans,
            target_lang,
            render_defaults=render_defaults,
            layout_intent_plan=layout_intent_plan,
            measured_min_heights=measured_min_heights,
        )
        html = render_to_html(render_document)
        try:
            measurement = await _measure_html_layout(html, asset_base_path=asset_base_path)
        except Exception as exc:
            diagnostics = render_document.diagnostics()
            return (
                html,
                render_document,
                _merge_browser_diagnostics(
                    diagnostics,
                    page_diagnostics=None,
                    layout_iterations=layout_iterations,
                    unavailable_error=_friendly_playwright_error(exc),
                ),
            )

        page_diagnostics = measurement.get("page") if isinstance(measurement, dict) else {}
        if not isinstance(page_diagnostics, dict):
            page_diagnostics = {}
        overflows = _page_block_overflows(page_diagnostics)
        layout_iterations.append(
            {
                "iteration": iteration,
                "browser_block_overflow_count": len(overflows),
                "measured_height_override_count": len(measured_min_heights),
            }
        )
        if not overflows:
            break
        overrides = _height_overrides_from_browser_overflows(render_document, overflows)
        changed = False
        for signature, height_pt in overrides.items():
            if height_pt > measured_min_heights.get(signature, 0.0) + 0.01:
                measured_min_heights[signature] = height_pt
                changed = True
        if not changed:
            break

    if render_document is None:
        render_document = RenderDocument.from_ir_and_plans(
            document,
            plans,
            target_lang,
            render_defaults=render_defaults,
            layout_intent_plan=layout_intent_plan,
        )
        html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()
    return (
        html,
        render_document,
        _merge_browser_diagnostics(
            diagnostics,
            page_diagnostics=page_diagnostics,
            layout_iterations=layout_iterations,
        ),
    )


async def render_to_pdf(
    html: str,
    output_path: Path,
    *,
    diagnostics_path: Path | None = None,
    asset_base_path: Path | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    diagnostics = _initial_pdf_diagnostics(html, output_path, asset_base_path)
    html_for_pdf = (
        _inline_api_asset_sources(html, asset_base_path, diagnostics)
        if asset_base_path is not None
        else html
    )
    browser = None

    try:
        _select_playwright_nodejs_path(diagnostics)
        from playwright.async_api import async_playwright

        try:
            from playwright._repo_version import version as playwright_version

            diagnostics["playwright_version"] = playwright_version
        except Exception:
            diagnostics["playwright_version"] = "unknown"

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            diagnostics["browser_launched"] = True
            page = await browser.new_page()
            console_messages: list[dict[str, str]] = []
            failed_requests: list[dict[str, str | None]] = []
            page_crashes: list[str] = []
            page.on(
                "console",
                lambda message: console_messages.append(
                    {"type": message.type, "text": message.text[:500]}
                ),
            )
            page.on(
                "requestfailed",
                lambda request: failed_requests.append(
                    {"url": request.url, "failure": request.failure}
                ),
            )
            page.on("crash", lambda: page_crashes.append("page_crash"))

            await page.set_content(html_for_pdf, wait_until="load")
            diagnostics["page"] = await _collect_page_diagnostics(page)
            diagnostics["browser_validation"] = _browser_validation_from_page(
                diagnostics["page"]
            )
            await page.pdf(
                path=str(output_path),
                print_background=True,
                prefer_css_page_size=True,
            )
            diagnostics["status"] = "completed"
            diagnostics["console_messages"] = console_messages[:25]
            diagnostics["failed_requests"] = failed_requests[:50]
            diagnostics["page_crashes"] = page_crashes
    except Exception as exc:
        diagnostics["status"] = "fallback_pdf"
        diagnostics["error"] = _friendly_playwright_error(exc)
        diagnostics["browser_validation"] = {
            "status": "unavailable",
            "error": diagnostics["error"],
        }
        diagnostics["browser_validation_unavailable"] = True
        _render_fallback_pdf(html, output_path, exc)
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                diagnostics["browser_close_error"] = True
        diagnostics["duration_ms"] = round((time.monotonic() - started_at) * 1000, 2)
        diagnostics["output_bytes"] = output_path.stat().st_size if output_path.exists() else 0
        if diagnostics_path is not None:
            diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostics_path.write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return output_path


async def _measure_html_layout(
    html: str,
    *,
    asset_base_path: Path | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "kind": "browser_layout_measurement",
        "status": "running",
        "browser_launched": False,
        "asset_rewrites": {"inlined": 0, "missing": []},
    }
    html_for_browser = (
        _inline_api_asset_sources(html, asset_base_path, diagnostics)
        if asset_base_path is not None
        else html
    )
    browser = None
    _select_playwright_nodejs_path(diagnostics)
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
            diagnostics["browser_launched"] = True
            page = await browser.new_page()
            await page.set_content(html_for_browser, wait_until="load")
            diagnostics["page"] = await _collect_page_diagnostics(page)
            diagnostics["browser_validation"] = _browser_validation_from_page(
                diagnostics["page"]
            )
            diagnostics["status"] = "completed"
            return diagnostics
        finally:
            if browser is not None:
                await browser.close()


def _merge_browser_diagnostics(
    renderer_diagnostics: dict[str, Any],
    *,
    page_diagnostics: dict[str, Any] | None,
    layout_iterations: list[dict[str, Any]],
    unavailable_error: str | None = None,
) -> dict[str, Any]:
    diagnostics = dict(renderer_diagnostics)
    quality_counts = dict(diagnostics.get("quality_flag_counts") or {})
    diagnostics["layout_iterations"] = layout_iterations
    if unavailable_error is not None:
        quality_counts["browser_validation_unavailable"] = (
            quality_counts.get("browser_validation_unavailable", 0) + 1
        )
        diagnostics["quality_flag_counts"] = quality_counts
        diagnostics["browser_validation"] = {
            "status": "unavailable",
            "error": unavailable_error,
        }
        diagnostics["browser_validation_unavailable"] = True
        diagnostics["browser_block_overflow_count"] = 0
        diagnostics["browser_overflows"] = []
        diagnostics["browser_figure_group_issue_count"] = 0
        diagnostics["browser_figure_group_issues"] = []
        return diagnostics

    page_diagnostics = page_diagnostics or {}
    overflows = _annotated_browser_overflows(_page_block_overflows(page_diagnostics))
    figure_group_issues = page_diagnostics.get("figure_group_issues")
    if not isinstance(figure_group_issues, list):
        figure_group_issues = []
    if overflows:
        quality_counts["browser_overflow"] = quality_counts.get("browser_overflow", 0) + len(
            overflows
        )
    diagnostics["quality_flag_counts"] = quality_counts
    diagnostics["browser_validation"] = _browser_validation_from_page(page_diagnostics)
    diagnostics["browser_validation_unavailable"] = False
    diagnostics["browser_block_overflow_count"] = len(overflows)
    diagnostics["browser_overflows"] = overflows
    diagnostics["browser_figure_group_issue_count"] = len(figure_group_issues)
    diagnostics["browser_figure_group_issues"] = figure_group_issues
    return diagnostics


def _browser_validation_from_page(page_diagnostics: dict[str, Any]) -> dict[str, Any]:
    block_count = int(page_diagnostics.get("block_overflow_count") or 0)
    figure_count = int(page_diagnostics.get("figure_group_issue_count") or 0)
    status = "passed" if block_count == 0 and figure_count == 0 else "failed"
    return {
        "status": status,
        "block_overflow_count": block_count,
        "figure_group_issue_count": figure_count,
    }


def _page_block_overflows(page_diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    overflows = page_diagnostics.get("block_overflows")
    if not isinstance(overflows, list):
        return []
    return [overflow for overflow in overflows if isinstance(overflow, dict)]


def _height_overrides_from_browser_overflows(
    render_document: RenderDocument,
    overflows: list[dict[str, Any]],
) -> dict[str, float]:
    blocks_by_id = {
        block.block_id: block
        for page in render_document.pages
        for block in page.blocks
    }
    blocks_by_signature = {
        block.layout_signature: block
        for page in render_document.pages
        for block in page.blocks
        if block.layout_signature
    }
    overrides: dict[str, float] = {}
    for overflow in overflows:
        signature = overflow.get("layout_signature")
        block = blocks_by_signature.get(signature) if isinstance(signature, str) else None
        if block is None:
            block_id = overflow.get("block_id")
            block = blocks_by_id.get(block_id) if isinstance(block_id, str) else None
        if block is None or not block.layout_signature:
            continue
        scroll_height = _as_float(overflow.get("scroll_height"))
        client_height = _as_float(overflow.get("client_height"))
        delta_pt = max(0.0, scroll_height - client_height) * _PT_PER_CSS_PX
        height_pt = (
            block.bbox.y1
            - block.bbox.y0
            + delta_pt
            + _BROWSER_REFLOW_SAFETY_PT
        )
        overrides[block.layout_signature] = max(
            overrides.get(block.layout_signature, 0.0),
            height_pt,
        )
    return overrides


def _annotated_browser_overflows(overflows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for overflow in overflows:
        item = dict(overflow)
        scroll_height = _as_float(item.get("scroll_height"))
        client_height = _as_float(item.get("client_height"))
        item["height_delta_px"] = round(max(0.0, scroll_height - client_height), 4)
        item["height_delta_pt"] = round(item["height_delta_px"] * _PT_PER_CSS_PX, 4)
        annotated.append(item)
    return annotated


def _as_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _initial_pdf_diagnostics(
    html: str,
    output_path: Path,
    asset_base_path: Path | None,
) -> dict[str, Any]:
    return {
        "kind": "pdf_export",
        "status": "running",
        "output_path": str(output_path),
        "html_bytes": len(html.encode("utf-8")),
        "api_asset_refs": len(_API_ASSET_SRC_PATTERN.findall(html)),
        "asset_base_path": str(asset_base_path) if asset_base_path else None,
        "browser_launched": False,
        "asset_rewrites": {"inlined": 0, "missing": []},
    }


def _select_playwright_nodejs_path(diagnostics: dict[str, Any]) -> str | None:
    configured = os.getenv("PLAYWRIGHT_NODEJS_PATH")
    if configured:
        diagnostics["playwright_nodejs_path"] = configured
        diagnostics["playwright_nodejs_path_source"] = "env"
        return configured

    for candidate in _system_node_candidates():
        path = Path(candidate)
        if path.exists() and os.access(path, os.X_OK):
            os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(path)
            diagnostics["playwright_nodejs_path"] = str(path)
            diagnostics["playwright_nodejs_path_source"] = "system_fallback"
            return str(path)

    diagnostics["playwright_nodejs_path"] = None
    diagnostics["playwright_nodejs_path_source"] = "bundled"
    return None


def _system_node_candidates() -> list[str]:
    candidates = ["/usr/local/bin/node", "/opt/homebrew/bin/node"]
    detected = shutil.which("node")
    if detected:
        candidates.append(detected)
    unique: list[str] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _inline_api_asset_sources(
    html: str,
    asset_base_path: Path,
    diagnostics: dict[str, Any],
) -> str:
    rewrites = diagnostics["asset_rewrites"]
    missing: list[str] = rewrites["missing"]

    def replace(match: re.Match[str]) -> str:
        filename = Path(match.group("filename")).name
        asset_path = asset_base_path / filename
        if not asset_path.is_file():
            missing.append(filename)
            return match.group(0)
        mime_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
        payload = base64.b64encode(asset_path.read_bytes()).decode("ascii")
        rewrites["inlined"] += 1
        return (
            f"{match.group('prefix')}{match.group('quote')}"
            f"data:{mime_type};base64,{payload}"
            f"{match.group('quote')}"
        )

    return _API_ASSET_SRC_PATTERN.sub(replace, html)


async def _collect_page_diagnostics(page: Any) -> dict[str, Any]:
    return await page.evaluate(
        """() => {
          const pages = [...document.querySelectorAll('.page')];
          const blocks = [...document.querySelectorAll('.block')];
          const assets = [...document.querySelectorAll('.asset')];
          const images = [...document.images];
          const blockOverflows = blocks
            .map((block) => {
              const scrollHeight = block.scrollHeight;
              const clientHeight = block.clientHeight;
              const scrollWidth = block.scrollWidth;
              const clientWidth = block.clientWidth;
              const overflows =
                scrollHeight > clientHeight + 1 || scrollWidth > clientWidth + 1;
              if (!overflows) {
                return null;
              }
              return {
                block_id: block.getAttribute('data-block-id'),
                source_block_id: block.getAttribute('data-source-block-id'),
                layout_signature: block.getAttribute('data-layout-signature'),
                page_id: block.closest('.page')?.getAttribute('data-page-id') || null,
                scroll_height: scrollHeight,
                client_height: clientHeight,
                scroll_width: scrollWidth,
                client_width: clientWidth,
                height_delta: Math.max(0, scrollHeight - clientHeight),
                width_delta: Math.max(0, scrollWidth - clientWidth)
              };
            })
            .filter(Boolean);
          const figureGroups = {};
          assets.forEach((asset) => {
            const groupId = asset.getAttribute('data-figure-group-id');
            if (!groupId) {
              return;
            }
            figureGroups[groupId] = figureGroups[groupId] || {};
            figureGroups[groupId].asset_id = asset.getAttribute('data-asset-id');
            figureGroups[groupId].asset_page_id =
              asset.closest('.page')?.getAttribute('data-page-id') || null;
            figureGroups[groupId].caption_block_id =
              asset.getAttribute('data-caption-block-id') || null;
          });
          blocks.forEach((block) => {
            const groupId = block.getAttribute('data-figure-group-id');
            if (!groupId) {
              return;
            }
            figureGroups[groupId] = figureGroups[groupId] || {};
            figureGroups[groupId].caption_render_block_id =
              block.getAttribute('data-block-id');
            figureGroups[groupId].caption_page_id =
              block.closest('.page')?.getAttribute('data-page-id') || null;
            figureGroups[groupId].caption_for_asset_id =
              block.getAttribute('data-caption-for-asset-id') || null;
          });
          const figureGroupIssues = Object.entries(figureGroups)
            .flatMap(([groupId, group]) => {
              const issues = [];
              if (
                group.asset_page_id &&
                group.caption_page_id &&
                group.asset_page_id !== group.caption_page_id
              ) {
                issues.push({
                  kind: 'figure_group_separated',
                  figure_group_id: groupId,
                  asset_id: group.asset_id || null,
                  caption_block_id:
                    group.caption_render_block_id || group.caption_block_id || null,
                  asset_page_id: group.asset_page_id,
                  caption_page_id: group.caption_page_id
                });
              }
              if (
                group.asset_id &&
                group.caption_for_asset_id &&
                group.asset_id !== group.caption_for_asset_id
              ) {
                issues.push({
                  kind: 'asset_caption_mismatch',
                  figure_group_id: groupId,
                  asset_id: group.asset_id,
                  caption_block_id:
                    group.caption_render_block_id || group.caption_block_id || null,
                  caption_for_asset_id: group.caption_for_asset_id
                });
              }
              return issues;
            });
          return {
            pages: pages.length,
            blocks: blocks.length,
            assets: assets.length,
            images: images.length,
            incomplete_images: images.filter(
              (img) => !img.complete || img.naturalWidth === 0
            ).length,
            text_length: document.body ? document.body.innerText.length : 0,
            first_page_size: pages[0]
              ? {
                  width: getComputedStyle(pages[0]).width,
                  height: getComputedStyle(pages[0]).height
                }
              : null,
            block_overflows: blockOverflows,
            block_overflow_count: blockOverflows.length,
            figure_group_issues: figureGroupIssues,
            figure_group_issue_count: figureGroupIssues.length
          };
        }"""
    )


def _friendly_playwright_error(exc: Exception) -> str:
    message = str(exc)
    if "Executable doesn't exist" in message or "playwright install" in message:
        return (
            f"{message}\nRun `.venv/bin/python -m playwright install chromium` "
            "after installing or changing Playwright."
        )
    if "Connection closed while reading from the driver" in message:
        return (
            f"{message}\nIf this happens on macOS 11 or 12, set "
            "`PLAYWRIGHT_NODEJS_PATH` to a compatible system node binary."
        )
    return message


def _render_fallback_pdf(html: str, output_path: Path, exc: Exception) -> None:
    import fitz

    text = _html_to_plain_text(html)
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_textbox(
        fitz.Rect(54, 54, 558, 742),
        "PDF export fallback\n\n"
        f"Primary Playwright export failed: {type(exc).__name__}: {exc}\n\n"
        f"{text}",
        fontsize=10,
        fontname="helv",
        align=fitz.TEXT_ALIGN_LEFT,
    )
    document.save(output_path)
    document.close()


def _html_to_plain_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|section|article|h[1-6]|li)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()[:12000]
