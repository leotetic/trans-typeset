from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

KATEX_UNAVAILABLE = "__katex_unavailable__"


@dataclass(frozen=True)
class KatexRenderResult:
    html: str | None = None
    error: str | None = None
    unavailable: bool = False


_CACHE: dict[tuple[str, bool], KatexRenderResult] = {}
_CACHE_LOCK = Lock()


def render_katex(latex: str, *, display: bool, cwd: Path | None = None) -> KatexRenderResult:
    return render_katex_many([(latex, display)], cwd=cwd)[(latex, display)]


def render_katex_many(
    items: list[tuple[str, bool]],
    *,
    cwd: Path | None = None,
) -> dict[tuple[str, bool], KatexRenderResult]:
    unique_items = [(latex, display) for latex, display in dict.fromkeys(items) if latex]
    results: dict[tuple[str, bool], KatexRenderResult] = {}
    missing: list[tuple[str, bool]] = []
    with _CACHE_LOCK:
        for item in unique_items:
            cached = _CACHE.get(item)
            if cached is None:
                missing.append(item)
            else:
                results[item] = cached
    if missing:
        rendered = _render_katex_batch(missing, cwd=cwd)
        with _CACHE_LOCK:
            _CACHE.update(rendered)
        results.update(rendered)
    for item in items:
        if item and item not in results:
            results[item] = KatexRenderResult()
    return results


def prewarm_katex(items: list[tuple[str, bool]], *, cwd: Path | None = None) -> None:
    render_katex_many(items, cwd=cwd)


def clear_katex_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _render_katex_batch(
    items: list[tuple[str, bool]],
    *,
    cwd: Path | None = None,
) -> dict[tuple[str, bool], KatexRenderResult]:
    payload = base64.b64encode(
        json.dumps(
            [
                {
                    "latex": latex,
                    "display": display,
                }
                for latex, display in items
            ],
            ensure_ascii=False,
        ).encode("utf-8")
    ).decode("ascii")
    script = (
        "let katex;"
        "try{katex=require('katex')}"
        "catch(error){process.stdout.write(JSON.stringify({unavailable:true,results:[]}));"
        "process.exit(0);}"
        "const input=JSON.parse(Buffer.from(process.argv[1],'base64').toString('utf8'));"
        "const results=input.map((item)=>{"
        "try{return {ok:true,html:katex.renderToString(item.latex,{"
        "displayMode:!!item.display,throwOnError:true,strict:'ignore',trust:false})};}"
        "catch(error){return {ok:false,error:String(error&&error.message||error)};}"
        "});"
        "process.stdout.write(JSON.stringify({unavailable:false,results}));"
    )
    try:
        completed = subprocess.run(
            ["node", "-e", script, payload],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(3, min(30, 3 + len(items) // 20)),
            cwd=cwd or _project_root(),
        )
    except Exception:
        return {item: KatexRenderResult(unavailable=True) for item in items}
    if completed.returncode != 0:
        error = (completed.stderr or "").strip() or "katex_render_failed"
        return {item: KatexRenderResult(error=error) for item in items}
    try:
        payload_out = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {
            item: KatexRenderResult(error="katex_render_failed")
            for item in items
        }
    if payload_out.get("unavailable"):
        return {item: KatexRenderResult(unavailable=True) for item in items}
    raw_results = payload_out.get("results")
    if not isinstance(raw_results, list):
        return {
            item: KatexRenderResult(error="katex_render_failed")
            for item in items
        }
    rendered: dict[tuple[str, bool], KatexRenderResult] = {}
    for item, raw in zip(items, raw_results, strict=False):
        if not isinstance(raw, dict):
            rendered[item] = KatexRenderResult(error="katex_render_failed")
        elif raw.get("ok") and isinstance(raw.get("html"), str) and raw["html"].strip():
            rendered[item] = KatexRenderResult(html=raw["html"])
        else:
            rendered[item] = KatexRenderResult(
                error=str(raw.get("error") or "katex_render_failed")
            )
    for item in items:
        rendered.setdefault(item, KatexRenderResult(error="katex_render_failed"))
    return rendered


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]
