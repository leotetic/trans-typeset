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
    measured_preferred_heights: dict[str, float] = {}
    forced_full_width_block_ids: set[str] = set()
    layout_iterations: list[dict[str, Any]] = []
    render_document: RenderDocument | None = None
    html = ""
    page_diagnostics: dict[str, Any] | None = None
    final_rebuild_needed = False

    for iteration in range(1, max(1, max_iterations) + 1):
        final_rebuild_needed = False
        render_document = RenderDocument.from_ir_and_plans(
            document,
            plans,
            target_lang,
            render_defaults=render_defaults,
            layout_intent_plan=layout_intent_plan,
            measured_min_heights=measured_min_heights,
            measured_preferred_heights=measured_preferred_heights,
            forced_full_width_block_ids=forced_full_width_block_ids,
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
        visual_slacks = _page_block_visual_slacks(page_diagnostics)
        layout_iterations.append(
            {
                "iteration": iteration,
                "browser_block_overflow_count": len(overflows),
                "browser_block_visual_slack_count": len(visual_slacks),
                "measured_height_override_count": len(measured_min_heights),
                "measured_preferred_height_count": len(measured_preferred_heights),
                "forced_full_width_block_count": len(forced_full_width_block_ids),
            }
        )
        if not overflows and not visual_slacks:
            break
        overrides = _height_overrides_from_browser_overflows(render_document, overflows)
        preferred_overrides = _height_preferences_from_browser_visual_slacks(
            render_document,
            visual_slacks,
        )
        full_width_overrides = _full_width_block_overrides_from_browser_overflows(
            render_document,
            overflows,
        )
        changed = False
        for signature, height_pt in overrides.items():
            if height_pt > measured_min_heights.get(signature, 0.0) + 0.01:
                measured_min_heights[signature] = height_pt
                changed = True
        for signature, height_pt in preferred_overrides.items():
            existing = measured_preferred_heights.get(signature)
            if existing is None or height_pt < existing - 0.01:
                measured_preferred_heights[signature] = height_pt
                changed = True
        for block_id in full_width_overrides:
            if block_id not in forced_full_width_block_ids:
                forced_full_width_block_ids.add(block_id)
                changed = True
        if not changed:
            break
        final_rebuild_needed = True

    if render_document is None or final_rebuild_needed:
        render_document = RenderDocument.from_ir_and_plans(
            document,
            plans,
            target_lang,
            render_defaults=render_defaults,
            layout_intent_plan=layout_intent_plan,
            measured_min_heights=measured_min_heights,
            measured_preferred_heights=measured_preferred_heights,
            forced_full_width_block_ids=forced_full_width_block_ids,
        )
        html = render_to_html(render_document)
    diagnostics = render_document.diagnostics()
    merged_diagnostics = _merge_browser_diagnostics(
        diagnostics,
        page_diagnostics=page_diagnostics,
        layout_iterations=layout_iterations,
    )
    if final_rebuild_needed:
        merged_diagnostics["browser_layout_final_rebuild_applied"] = True
        merged_diagnostics["browser_layout_final_rebuild_measured"] = False
    return html, render_document, merged_diagnostics


async def render_to_pdf(
    html: str,
    output_path: Path,
    *,
    diagnostics_path: Path | None = None,
    asset_base_path: Path | None = None,
    source_pdf_path: Path | None = None,
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
            replay_placements = (
                await _collect_formula_replay_placements(page)
                if source_pdf_path is not None
                else []
            )
            diagnostics["formula_direct_replay"] = {
                "status": "not_requested" if source_pdf_path is None else "pending",
                "candidate_count": len(replay_placements),
                "source_pdf_path": str(source_pdf_path) if source_pdf_path is not None else None,
            }
            await page.pdf(
                path=str(output_path),
                print_background=True,
                prefer_css_page_size=True,
            )
            if source_pdf_path is not None and replay_placements:
                diagnostics["formula_direct_replay"].update(
                    _overlay_formula_source_clips(
                        output_path,
                        source_pdf_path,
                        replay_placements,
                    )
                )
            elif source_pdf_path is not None:
                diagnostics["formula_direct_replay"]["status"] = "no_candidates"
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
        diagnostics["block_visual_slack_count"] = 0
        diagnostics["block_visual_slacks"] = []
        diagnostics["browser_figure_group_issue_count"] = 0
        diagnostics["browser_figure_group_issues"] = []
        diagnostics["browser_formula_diagnostics"] = {}
        return diagnostics

    page_diagnostics = page_diagnostics or {}
    overflows = _annotated_browser_overflows(_page_block_overflows(page_diagnostics))
    visual_slacks = _annotated_browser_visual_slacks(
        _page_block_visual_slacks(page_diagnostics)
    )
    figure_group_issues = page_diagnostics.get("figure_group_issues")
    if not isinstance(figure_group_issues, list):
        figure_group_issues = []
    formula_diagnostics = page_diagnostics.get("formula_diagnostics")
    if not isinstance(formula_diagnostics, dict):
        formula_diagnostics = {}
    if overflows:
        quality_counts["browser_overflow"] = quality_counts.get("browser_overflow", 0) + len(
            overflows
        )
    if visual_slacks:
        quality_counts["visual_slack"] = quality_counts.get("visual_slack", 0) + len(
            visual_slacks
        )
    diagnostics["quality_flag_counts"] = quality_counts
    diagnostics["browser_validation"] = _browser_validation_from_page(page_diagnostics)
    diagnostics["browser_validation_unavailable"] = False
    diagnostics["browser_block_overflow_count"] = len(overflows)
    diagnostics["browser_overflows"] = overflows
    diagnostics["block_visual_slack_count"] = len(visual_slacks)
    diagnostics["block_visual_slacks"] = visual_slacks
    diagnostics["browser_figure_group_issue_count"] = len(figure_group_issues)
    diagnostics["browser_figure_group_issues"] = figure_group_issues
    diagnostics["browser_formula_diagnostics"] = formula_diagnostics
    return diagnostics


def _browser_validation_from_page(page_diagnostics: dict[str, Any]) -> dict[str, Any]:
    block_count = int(page_diagnostics.get("block_overflow_count") or 0)
    figure_count = int(page_diagnostics.get("figure_group_issue_count") or 0)
    formula_diagnostics = page_diagnostics.get("formula_diagnostics")
    if not isinstance(formula_diagnostics, dict):
        formula_diagnostics = {}
    formula_issue_count = (
        int(formula_diagnostics.get("unresolved_count") or 0)
        + int(formula_diagnostics.get("raw_tex_unrendered_count") or 0)
        + int(formula_diagnostics.get("browser_katex_failed_count") or 0)
        + int(formula_diagnostics.get("katex_error_count") or 0)
    )
    status = (
        "passed"
        if block_count == 0 and figure_count == 0 and formula_issue_count == 0
        else "failed"
    )
    return {
        "status": status,
        "block_overflow_count": block_count,
        "figure_group_issue_count": figure_count,
        "formula_issue_count": formula_issue_count,
    }


def _page_block_overflows(page_diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    overflows = page_diagnostics.get("block_overflows")
    if not isinstance(overflows, list):
        return []
    return [overflow for overflow in overflows if isinstance(overflow, dict)]


def _page_block_visual_slacks(page_diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    visual_slacks = page_diagnostics.get("block_visual_slacks")
    if not isinstance(visual_slacks, list):
        return []
    return [slack for slack in visual_slacks if isinstance(slack, dict)]


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


def _height_preferences_from_browser_visual_slacks(
    render_document: RenderDocument,
    visual_slacks: list[dict[str, Any]],
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
    preferences: dict[str, float] = {}
    for slack in visual_slacks:
        signature = slack.get("layout_signature")
        block = blocks_by_signature.get(signature) if isinstance(signature, str) else None
        if block is None:
            block_id = slack.get("block_id")
            block = blocks_by_id.get(block_id) if isinstance(block_id, str) else None
        if block is None or not block.layout_signature:
            continue
        client_height = _as_float(slack.get("client_height"))
        slack_bottom = _as_float(slack.get("slack_bottom"))
        if client_height <= 0 or slack_bottom <= 0:
            continue
        visible_height_pt = max(0.0, client_height - slack_bottom) * _PT_PER_CSS_PX
        line_height = block.line_height or 1.2
        min_height_pt = max(block.font_size_pt * line_height, 1.0)
        height_pt = max(
            min_height_pt,
            visible_height_pt + _BROWSER_REFLOW_SAFETY_PT,
        )
        preferences[block.layout_signature] = min(
            preferences.get(block.layout_signature, float("inf")),
            height_pt,
        )
    return preferences


def _full_width_block_overrides_from_browser_overflows(
    render_document: RenderDocument,
    overflows: list[dict[str, Any]],
) -> set[str]:
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
    block_ids: set[str] = set()
    for overflow in overflows:
        scroll_width = _as_float(overflow.get("scroll_width"))
        client_width = _as_float(overflow.get("client_width"))
        if scroll_width <= client_width + 1:
            continue
        signature = overflow.get("layout_signature")
        block = blocks_by_signature.get(signature) if isinstance(signature, str) else None
        if block is None:
            block_id = overflow.get("block_id")
            block = blocks_by_id.get(block_id) if isinstance(block_id, str) else None
        if block is None:
            continue
        role = getattr(block.role, "value", block.role)
        if str(role) != "formula":
            continue
        if block.source_block_id:
            block_ids.add(block.source_block_id)
    return block_ids


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


def _annotated_browser_visual_slacks(slacks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for slack in slacks:
        item = dict(slack)
        slack_bottom = _as_float(item.get("slack_bottom"))
        item["slack_bottom_pt"] = round(slack_bottom * _PT_PER_CSS_PX, 4)
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
          const visibleTextRects = (block) => {
            const walker = document.createTreeWalker(
              block,
              NodeFilter.SHOW_TEXT,
              {
                acceptNode(node) {
                  if (!node.nodeValue || !node.nodeValue.trim()) {
                    return NodeFilter.FILTER_REJECT;
                  }
                  const parent = node.parentElement;
                  if (!parent || parent.closest('.katex-mathml')) {
                    return NodeFilter.FILTER_REJECT;
                  }
                  const style = getComputedStyle(parent);
                  if (
                    style.display === 'none' ||
                    style.visibility === 'hidden' ||
                    Number.parseFloat(style.opacity || '1') === 0
                  ) {
                    return NodeFilter.FILTER_REJECT;
                  }
                  return NodeFilter.FILTER_ACCEPT;
                }
              }
            );
            const rects = [];
            while (walker.nextNode()) {
              const range = document.createRange();
              range.selectNodeContents(walker.currentNode);
              for (const rect of range.getClientRects()) {
                if (rect.width > 0 && rect.height > 0) {
                  rects.push(rect);
                }
              }
              range.detach();
            }
            return rects;
          };
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
          const blockVisualSlacks = blocks
            .map((block) => {
              const clientHeight = block.clientHeight;
              if (!clientHeight) {
                return null;
              }
              const blockRect = block.getBoundingClientRect();
              const rects = visibleTextRects(block);
              if (!rects.length) {
                return null;
              }
              const textTop = Math.min(...rects.map((rect) => rect.top));
              const textBottom = Math.max(...rects.map((rect) => rect.bottom));
              const visibleHeight = Math.max(0, textBottom - textTop);
              const slackBottom = Math.max(0, blockRect.bottom - textBottom);
              const slackRatio = slackBottom / clientHeight;
              if (slackBottom <= Math.max(8, clientHeight * 0.22)) {
                return null;
              }
              return {
                block_id: block.getAttribute('data-block-id'),
                source_block_id: block.getAttribute('data-source-block-id'),
                layout_signature: block.getAttribute('data-layout-signature'),
                page_id: block.closest('.page')?.getAttribute('data-page-id') || null,
                client_height: clientHeight,
                visible_height: visibleHeight,
                slack_bottom: slackBottom,
                slack_ratio: slackRatio
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
          const formulaNodes = [...document.querySelectorAll('.formula')];
          const formulaDiagnostics = {
            node_count: formulaNodes.length,
            katex_rendered_count: formulaNodes.filter(
              (node) => node.querySelector('.katex, .katex-display')
            ).length,
            katex_error_count: formulaNodes.filter(
              (node) => node.querySelector('.katex-error')
            ).length,
            image_fallback_count: formulaNodes.filter(
              (node) => node.querySelector('.formula-image-fallback')
            ).length,
            pdf_formula_replay_count: formulaNodes.filter(
              (node) => node.querySelector('.formula-pdf-primitive-replay')
            ).length,
            source_clip_replay_count: formulaNodes.filter(
              (node) => node.querySelector('.formula-pdf-source-clip-replay')
            ).length,
            plaintext_fallback_count: formulaNodes.filter(
              (node) => node.querySelector('.formula-plaintext-fallback')
            ).length,
            unresolved_count: formulaNodes.filter(
              (node) => node.hasAttribute('data-unresolved-formula-id')
            ).length,
            raw_tex_count: formulaNodes.filter(
              (node) => node.hasAttribute('data-raw-tex')
            ).length,
            raw_tex_unrendered_count: formulaNodes.filter(
              (node) => node.getAttribute('data-raw-tex-status') === 'unrendered'
            ).length,
            browser_katex_rendered_count: formulaNodes.filter(
              (node) => node.getAttribute('data-browser-katex-status') === 'rendered'
            ).length,
            browser_katex_failed_count: formulaNodes.filter(
              (node) => node.getAttribute('data-browser-katex-status') === 'failed'
            ).length,
            unresolved_formula_ids: formulaNodes
              .map((node) => node.getAttribute('data-unresolved-formula-id'))
              .filter(Boolean),
            raw_tex_nodes: formulaNodes
              .filter((node) => node.hasAttribute('data-raw-tex'))
              .map((node) => ({
                raw: node.getAttribute('data-raw-tex'),
                latex: node.getAttribute('data-latex'),
                status: node.getAttribute('data-raw-tex-status'),
                browser_katex_status: node.getAttribute('data-browser-katex-status')
              })),
            image_fallbacks: formulaNodes
              .filter((node) => node.querySelector('.formula-image-fallback'))
              .map((node) => {
                const fallback = node.querySelector('.formula-image-fallback');
                const img = fallback ? fallback.querySelector('img') : null;
                return {
                  formula_id: node.getAttribute('data-formula-id') ||
                    (fallback && fallback.getAttribute('data-fallback-formula-id')),
                  latex: node.getAttribute('data-latex') ||
                    (fallback && fallback.getAttribute('data-latex')),
                  display: node.getAttribute('data-display') ||
                    (fallback && fallback.getAttribute('data-display')),
                  complete: img ? img.complete : null,
                  natural_width: img ? img.naturalWidth : null,
                  natural_height: img ? img.naturalHeight : null
                };
              })
          };
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
            block_visual_slacks: blockVisualSlacks,
            block_visual_slack_count: blockVisualSlacks.length,
            figure_group_issues: figureGroupIssues,
            figure_group_issue_count: figureGroupIssues.length,
            formula_diagnostics: formulaDiagnostics
          };
        }"""
    )


async def _collect_formula_replay_placements(page: Any) -> list[dict[str, Any]]:
    placements = await page.evaluate(
        """() => {
          return [...document.querySelectorAll('.formula[data-pdf-formula="true"]')]
            .map((node) => {
              const pageNode = node.closest('.page');
              if (!pageNode) {
                return null;
              }
              const sourcePageIndex = node.getAttribute('data-pdf-formula-source-page-index');
              const sourceBbox = node.getAttribute('data-pdf-formula-source-bbox');
              if (sourcePageIndex === null || !sourceBbox) {
                return null;
              }
              const replayNode =
                node.querySelector('.formula-pdf-source-clip-replay') ||
                node.querySelector('.formula-pdf-primitive-replay') ||
                node.querySelector('.formula-image-fallback') ||
                node;
              const rect = replayNode.getBoundingClientRect();
              const pageRect = pageNode.getBoundingClientRect();
              if (rect.width <= 0 || rect.height <= 0) {
                return null;
              }
              return {
                formula_id: node.getAttribute('data-formula-id'),
                target_page_index: Number.parseInt(pageNode.getAttribute('data-page-index') || '0', 10),
                source_page_index: Number.parseInt(sourcePageIndex, 10),
                source_bbox: sourceBbox,
                x_px: rect.left - pageRect.left,
                y_px: rect.top - pageRect.top,
                width_px: rect.width,
                height_px: rect.height
              };
            })
            .filter(Boolean);
        }"""
    )
    return placements if isinstance(placements, list) else []


def _overlay_formula_source_clips(
    output_path: Path,
    source_pdf_path: Path,
    placements: list[dict[str, Any]],
) -> dict[str, Any]:
    if not output_path.exists() or not source_pdf_path.exists():
        return {
            "status": "failed",
            "error": "output or source PDF is missing",
            "succeeded_count": 0,
            "failed_count": len(placements),
        }
    try:
        import fitz
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"PyMuPDF unavailable: {exc}",
            "succeeded_count": 0,
            "failed_count": len(placements),
        }

    target_doc = None
    source_doc = None
    succeeded = 0
    failures: list[dict[str, Any]] = []
    temp_path = output_path.with_name(f"{output_path.stem}.formula-replay.tmp.pdf")
    try:
        target_doc = fitz.open(output_path)
        source_doc = fitz.open(source_pdf_path)
        for placement in placements:
            try:
                target_index = int(placement.get("target_page_index", 0))
                source_index = int(placement.get("source_page_index", 0))
                if target_index < 0 or target_index >= len(target_doc):
                    raise ValueError("target page index out of range")
                if source_index < 0 or source_index >= len(source_doc):
                    raise ValueError("source page index out of range")
                source_bbox = _parse_bbox_csv(str(placement.get("source_bbox", "")))
                if source_bbox is None:
                    raise ValueError("source bbox is malformed")
                x0 = float(placement.get("x_px", 0) or 0) * _PT_PER_CSS_PX
                y0 = float(placement.get("y_px", 0) or 0) * _PT_PER_CSS_PX
                width = float(placement.get("width_px", 0) or 0) * _PT_PER_CSS_PX
                height = float(placement.get("height_px", 0) or 0) * _PT_PER_CSS_PX
                if width <= 0 or height <= 0:
                    raise ValueError("target rect is empty")
                target_rect = fitz.Rect(x0, y0, x0 + width, y0 + height)
                source_rect = fitz.Rect(*source_bbox)
                pixmap = source_doc[source_index].get_pixmap(
                    matrix=fitz.Matrix(4, 4),
                    clip=source_rect,
                    alpha=True,
                )
                target_doc[target_index].insert_image(
                    target_rect,
                    stream=pixmap.tobytes("png"),
                    keep_proportion=True,
                    overlay=True,
                )
                succeeded += 1
            except Exception as exc:
                failures.append(
                    {
                        "formula_id": placement.get("formula_id"),
                        "error": str(exc)[:200],
                    }
                )
        if succeeded:
            target_doc.save(temp_path, garbage=4, deflate=True)
            target_doc.close()
            source_doc.close()
            target_doc = None
            source_doc = None
            temp_path.replace(output_path)
        return {
            "status": "completed" if not failures else "partial",
            "succeeded_count": succeeded,
            "failed_count": len(failures),
            "failures": failures[:25],
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error": str(exc)[:300],
            "succeeded_count": succeeded,
            "failed_count": len(placements) - succeeded,
            "failures": failures[:25],
        }
    finally:
        if target_doc is not None:
            target_doc.close()
        if source_doc is not None:
            source_doc.close()
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _parse_bbox_csv(value: str) -> tuple[float, float, float, float] | None:
    try:
        parts = [float(part.strip()) for part in value.split(",")]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    x0, y0, x1, y1 = parts
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


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
