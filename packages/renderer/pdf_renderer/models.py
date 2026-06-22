from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field, replace
from math import ceil
from html import escape, unescape
from functools import lru_cache
from typing import Any

from pdf_translator_schema import (
    DocumentIR,
    LayoutIntentPlan,
    LayoutMode,
    TranslationLayoutPlan,
)
from pdf_translator_schema.models import (
    Asset,
    BlockRole,
    BoundingBox,
    DocumentBlock,
    Formula,
    PageSize,
    RenderDefaults,
    RoleStyleDefaults,
    StyleSeed,
)


_SCHEMA_RENDER_DEFAULTS = RenderDefaults()
_CONTINUATION_MARGIN_PT = 54.0
_MIN_FINAL_FRAGMENT_CHARS = 12
_MIN_FINAL_FRAGMENT_LINES = 2
_LOW_UTILIZATION_THRESHOLD = 0.18
_UNDERFILLED_BOTTOM_WHITESPACE_RATIO = 0.30
_UNDERFILLED_LARGE_BOTTOM_WHITESPACE_RATIO = 0.45
_UNDERFILLED_LARGE_BOTTOM_MAX_UTILIZATION = 0.35
_RIGHT_COLUMN_START_TOP_RATIO = 0.15
_LEFT_COLUMN_UNDERFILLED_WHITESPACE_RATIO = 0.45
_KATEX_UNAVAILABLE = "__katex_unavailable__"
_LAYOUT_EPSILON_PT = 0.01
_DISPLAY_FORMULA_MIN_LINES = 2.35
_DISPLAY_FORMULA_VERTICAL_MARGIN_EM = 0.36
_DISPLAY_FORMULA_SINGLE_BLOCK_EXTRA_LINES = 0.3
_FORMULA_LIKE_MIN_LINES = 1.15
_FORMULA_LIKE_VERTICAL_MARGIN_EM = 0.14
_FORMULA_LIKE_SPACE_BEFORE_PT = 2.0
_FORMULA_LIKE_SPACE_AFTER_PT = 2.0
_INLINE_FORMULA_ESTIMATION_MAX_CHARS = 12
_FORMULA_REFLOW_CLUSTER_MAX_VERTICAL_GAP_PT = 14.0
_DISPLAY_FORMULA_PER_NODE_MARGIN_EM = 0.16
_FORMULA_PLACEHOLDER_PATTERN = re.compile(r"@@FORMULA_[A-Za-z0-9_]+@@")
_FORMULA_RENDER_MATH_SIGNAL_PATTERN = re.compile(
    r"(?:[=≤≥∑∫√∞≈≠∂∇^_+\-*/]|\\(?:partial|nabla|frac|sum|int|sqrt|"
    r"alpha|beta|gamma|delta|epsilon|theta|lambda|mu|nu|pi|rho|sigma|"
    r"phi|omega|Delta|Omega|cdot|times)|\b[A-Za-zα-ωΑ-Ω]\s*[+\-*/^_]\s*"
    r"[A-Za-z0-9α-ωΑ-Ω])"
)
_KATEX_STRUT_STYLE_PATTERN = re.compile(
    r'class="strut"[^>]*style="(?P<style>[^"]*)"', re.IGNORECASE
)
_CSS_EM_VALUE_PATTERN = re.compile(
    r"(?P<name>height|vertical-align)\s*:\s*(?P<value>-?\d+(?:\.\d+)?)em"
)
_LATEX_TAG_PATTERN = re.compile(r"\\tag\s*\{(?P<number>[^{}]+)\}")
_LATEX_TAG_STAR_PATTERN = re.compile(r"\\tag\*\s*\{(?P<number>[^{}]+)\}")
_SOURCE_EQUATION_NUMBER_ANYWHERE_PATTERN = re.compile(
    r"[（(]\s*(?P<number>[A-Za-z]?\d+(?:[.\-]\d+)*[a-z]?)\s*[)）]"
)
_SHORT_EQUATION_TAIL_PATTERN = re.compile(r"[A-Za-z0-9α-ωΑ-Ω_{}^\\\\'\s+\-*/=:.,]+")
_TEXT_SCRIPT_MARKER_PATTERN = re.compile(
    r"(?P<base>\b[A-Za-z0-9α-ωΑ-Ω]+)?(?P<op>[_^])\{(?P<script>[^{}\n\r]{1,32})\}"
)
_FORMULA_CORRUPTION_FALLBACK_FLAGS = {
    "formula_text_layer_corrupt",
    "formula_slash_glyph_suspect",
    "formula_prime_glyph_suspect",
}
_REFLOW_HEIGHT_SAFETY_PT = 0.6
_REFLOW_FORMULA_HEIGHT_SAFETY_EM = 0.2
_FIGURE_GROUP_SPACE_BEFORE_PT = 8.0
_FIGURE_GROUP_SPACE_BETWEEN_PT = 4.0
_FIGURE_GROUP_SPACE_AFTER_PT = 8.0
_FIGURE_CAPTION_MAX_DISTANCE_PT = 90.0
_FIGURE_GROUP_MIN_DEFERRED_SCALE = 0.72


@dataclass(frozen=True)
class _PreparedReflowBlock:
    block: DocumentBlock
    text: str
    render_intent: str
    flags: list[str]
    style: RoleStyleDefaults
    html: str | None
    formula_number: str | None


@dataclass(frozen=True)
class _ReflowFigureGroup:
    asset: Asset
    caption: DocumentBlock | None
    order: tuple[float, float, float, int]


@dataclass(frozen=True)
class _PreparedReflowFigureGroup:
    group: _ReflowFigureGroup
    caption_prepared: _PreparedReflowBlock | None
    caption_required_height: float
    asset_width: float
    asset_height: float
    scale: float
    required_height: float
    group_too_tall: bool


@dataclass(frozen=True)
class _PendingReflowFigureGroup:
    prepared: _PreparedReflowFigureGroup
    deferred_from_page: str


_REFLOW_BODY_FULL_WIDTH_ROLES = {
    BlockRole.TITLE,
    BlockRole.ABSTRACT,
    BlockRole.HEADING,
}
_REFLOW_ALWAYS_FULL_WIDTH_ROLES = {
    BlockRole.CAPTION,
    BlockRole.TABLE,
    BlockRole.FIGURE,
}
_REFLOW_FORMULA_FULL_WIDTH_SOURCE_RATIO = 0.58


def _formula_source_spans_columns(
    block: DocumentBlock,
    source_page_size: PageSize | None,
) -> bool:
    if source_page_size is None or source_page_size.width <= 0:
        return False
    return _bbox_width(block.bbox) >= (
        source_page_size.width * _REFLOW_FORMULA_FULL_WIDTH_SOURCE_RATIO
    )


def _reflow_span_for_block(
    block: DocumentBlock,
    defaults: RenderDefaults,
    *,
    source_page_size: PageSize | None = None,
    force_full_width: bool = False,
) -> str:
    if defaults.column_layout.column_count <= 1:
        return "full_width"
    if block.role == BlockRole.FORMULA:
        if force_full_width or _formula_source_spans_columns(block, source_page_size):
            return "full_width"
        return "column"
    if block.role in _REFLOW_ALWAYS_FULL_WIDTH_ROLES:
        return "full_width"
    if defaults.column_layout.scope == "body" and block.role in _REFLOW_BODY_FULL_WIDTH_ROLES:
        return "full_width"
    return "column"


def _bbox_width(bbox: BoundingBox) -> float:
    return bbox.x1 - bbox.x0


def _bbox_height(bbox: BoundingBox) -> float:
    return bbox.y1 - bbox.y0


def _bbox_area(bbox: BoundingBox) -> float:
    return _bbox_width(bbox) * _bbox_height(bbox)


def _bbox_overlap_area(a: BoundingBox, b: BoundingBox) -> float:
    x_overlap = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    y_overlap = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    return x_overlap * y_overlap


def _glyph_width_factor(char: str) -> float:
    codepoint = ord(char)
    if char == "\t":
        return 2.0
    if char.isspace():
        return 0.34
    if (
        0x1100 <= codepoint <= 0x11FF
        or 0x2E80 <= codepoint <= 0xA4CF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0xFE30 <= codepoint <= 0xFE6F
        or 0xFF00 <= codepoint <= 0xFFEF
    ):
        return 1.0
    if char.isupper():
        return 0.64
    if char.isdigit():
        return 0.56
    if char.isascii():
        return 0.52
    return 0.72


def _estimated_line_count(text: str, width_pt: float, font_size_pt: float) -> int:
    if not text:
        return 0
    if width_pt <= 0 or font_size_pt <= 0:
        return 1

    lines = 0
    for hard_line in text.splitlines() or [text]:
        line_units = sum(_glyph_width_factor(char) for char in hard_line)
        line_width = line_units * font_size_pt
        lines += max(1, ceil(line_width / width_pt))
    return lines


def _normalized_text(text: str) -> str:
    return " ".join(text.split())


def _enum_value(value: object) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _layout_signature(source_block_id: str, text: str, fragment_index: int = 1) -> str:
    payload = f"{source_block_id}\0{fragment_index}\0{text}".encode("utf-8")
    digest = hashlib.sha1(payload).hexdigest()[:16]
    return f"{source_block_id}:{fragment_index}:{digest}"


_FORMULA_REF_PATTERN = re.compile(r"\{\{formula:([A-Za-z0-9_.:-]+)\}\}")
_MALFORMED_FORMULA_REF_PATTERN = re.compile(r"(?<!\{)\{formula:([A-Za-z0-9_.:-]+)\}\}")
# Trailing equation number already present in the source/translated text,
# e.g. "... (12)", "...（3.4）", "... (2a)". GB/T numbering must not duplicate it.
_SOURCE_EQUATION_NUMBER_PATTERN = re.compile(r"[（(]\s*[A-Za-z]?\d+(?:[.\-]\d+)*[a-z]?\s*[)）]\s*$")
_UNRESOLVED_FORMULA_ID_ATTR_PATTERN = re.compile(r'data-unresolved-formula-id="([^"]*)"')


@dataclass(frozen=True)
class _FormulaRenderValidation:
    accepted: bool
    fallback_reason: str | None = None


def _formula_ir_html_for_text(
    text: str,
    document: DocumentIR,
    *,
    role: BlockRole,
) -> tuple[str | None, list[str]]:
    formulas = document.formulas_by_id()
    text, repair_count = _repair_malformed_formula_refs(text, set(formulas))
    if not _FORMULA_REF_PATTERN.search(text):
        return None, []
    flags: list[str] = ["formula_placeholder_syntax_repaired"] if repair_count else []
    parts: list[str] = []
    last_index = 0
    for match in _FORMULA_REF_PATTERN.finditer(text):
        raw_html, raw_flags = _non_formula_text_html(text[last_index : match.start()])
        parts.append(raw_html)
        flags.extend(raw_flags)
        formula_id = match.group(1)
        formula = formulas.get(formula_id)
        if formula is None:
            parts.append(
                _unresolved_formula_html(
                    formula_id,
                    display=role == BlockRole.FORMULA,
                )
            )
            flags.append("unresolved_formula_placeholder")
        else:
            display = formula.display_mode == "display" or role == BlockRole.FORMULA
            rendered = None
            latex = formula.latex
            if display:
                latex, _tag_number = _strip_latex_equation_tag(latex)
            validation = _validate_formula_latex(
                latex,
                source_text=formula.source_text,
                display=display,
            )
            force_image_fallback = display and bool(
                set(getattr(formula, "quality_flags", [])) & _FORMULA_CORRUPTION_FALLBACK_FLAGS
                and validation.fallback_reason == "formula_asset_image"
            )
            if (
                not force_image_fallback
                and validation.accepted
                and latex.strip()
                and _latex_looks_renderable(latex)
            ):
                rendered = _katex_html(latex, display=display)
            if rendered is None:
                fallback, fallback_flags = _formula_fallback_html(
                    formula,
                    document,
                    fallback_reason="formula_asset_image"
                    if force_image_fallback
                    else validation.fallback_reason,
                )
                parts.append(
                    _formula_ir_span(
                        formula,
                        fallback,
                        display=display,
                        include_latex=not _uses_formula_image_fallback(fallback_flags),
                        latex_override=latex,
                    )
                )
                flags.extend(fallback_flags)
            else:
                parts.append(
                    _formula_ir_span(
                        formula,
                        rendered,
                        display=display,
                        latex_override=latex,
                    )
                )
        last_index = match.end()
    raw_html, raw_flags = _non_formula_text_html(text[last_index:])
    parts.append(raw_html)
    flags.extend(raw_flags)
    return "".join(parts), _unique_flags(flags)


def _repair_malformed_formula_refs(
    text: str,
    formula_ids: set[str],
) -> tuple[str, int]:
    repaired_count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal repaired_count
        formula_id = match.group(1)
        if formula_ids and formula_id not in formula_ids:
            return match.group(0)
        repaired_count += 1
        return f"{{{{formula:{formula_id}}}}}"

    return _MALFORMED_FORMULA_REF_PATTERN.sub(replace, text), repaired_count


def _formula_ir_span(
    formula: Any,
    inner_html: str,
    *,
    display: bool,
    include_latex: bool = True,
    latex_override: str | None = None,
) -> str:
    css_kind = "display" if display else "inline"
    latex = latex_override if latex_override is not None else getattr(formula, "latex", "")
    latex = latex or getattr(formula, "source_text", "")
    formula_id = getattr(formula, "formula_id", "")
    latex_attr = f' data-latex="{escape(latex, quote=True)}"' if include_latex else ""
    return (
        f'<span class="formula formula-{css_kind} formula-ir" '
        f'data-formula-id="{escape(formula_id, quote=True)}" '
        f'data-display="{"true" if display else "false"}"'
        f"{latex_attr}>"
        f"{inner_html}</span>"
    )


def _formula_plaintext_fallback_text(
    *candidates: object,
    formula_id: str = "",
) -> str:
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue
        if _FORMULA_PLACEHOLDER_PATTERN.fullmatch(text):
            continue
        if _FORMULA_REF_PATTERN.fullmatch(text):
            continue
        return text
    return f"formula {formula_id}" if formula_id else "formula"


def _formula_plaintext_fallback_html(text: str) -> str:
    return f'<span class="formula-plaintext-fallback">{escape(text)}</span>'


def _non_formula_text_html(text: str) -> tuple[str, list[str]]:
    if not text:
        return "", []
    if not _TEXT_SCRIPT_MARKER_PATTERN.search(text):
        return escape(text), []

    parts: list[str] = []
    cursor = 0
    changed = False
    for match in _TEXT_SCRIPT_MARKER_PATTERN.finditer(text):
        parts.append(escape(text[cursor : match.start()]))
        tag = "sub" if match.group("op") == "_" else "sup"
        base = match.group("base") or ""
        if base:
            parts.append(f"{escape(base)}<{tag}>{escape(match.group('script'))}</{tag}>")
        else:
            parts.append(f"<{tag}>{escape(match.group('script'))}</{tag}>")
        cursor = match.end()
        changed = True
    parts.append(escape(text[cursor:]))
    return "".join(parts), ["text_script_marker_rendered"] if changed else []


def _unresolved_formula_html(formula_id: str, *, display: bool) -> str:
    css_kind = "display" if display else "inline"
    fallback = _formula_plaintext_fallback_text(formula_id=formula_id)
    return (
        f'<span class="formula formula-{css_kind} formula-unresolved" '
        f'data-unresolved-formula-id="{escape(formula_id, quote=True)}">'
        f"{_formula_plaintext_fallback_html(fallback)}</span>"
    )


@lru_cache(maxsize=512)
def _katex_html(latex: str, *, display: bool) -> str | None:
    rendered = _katex_render_to_string(latex, display=display)
    if rendered == _KATEX_UNAVAILABLE:
        return _katex_like_html(latex, display=display)
    if rendered is not None:
        return rendered
    return None


def _katex_render_to_string(latex: str, *, display: bool) -> str | None:
    script = (
        "let katex;try{katex=require('katex')}catch(error){process.exit(3);}"
        "const latex=Buffer.from(process.argv[1],'base64').toString('utf8');"
        "try{process.stdout.write(katex.renderToString(latex,{displayMode:"
        + ("true" if display else "false")
        + ",throwOnError:true,strict:'ignore',trust:false}));}"
        "catch(error){process.stderr.write(String(error&&error.message||error));process.exit(2);}"
    )
    try:
        import base64

        payload = base64.b64encode(latex.encode("utf-8")).decode("ascii")
        completed = subprocess.run(
            ["node", "-e", script, payload],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except Exception:
        return None
    if completed.returncode == 3:
        return _KATEX_UNAVAILABLE
    stdout = completed.stdout or ""
    if completed.returncode != 0 or not stdout.strip():
        return None
    return stdout


def _strip_latex_equation_tag(latex: str) -> tuple[str, str | None]:
    match = _LATEX_TAG_PATTERN.search(latex) or _LATEX_TAG_STAR_PATTERN.search(latex)
    if match is None:
        return latex, None
    stripped = (latex[: match.start()] + latex[match.end() :]).strip()
    stripped = re.sub(r"\s+", " ", stripped).strip()
    number = match.group("number").strip()
    if number.startswith("(") and number.endswith(")"):
        number = number[1:-1].strip()
    return stripped, f"({number})" if number else None


def _extract_latex_equation_tag(latex: str) -> str | None:
    _stripped, number = _strip_latex_equation_tag(latex)
    return number


def _extract_source_equation_number(text: str) -> str | None:
    match = _SOURCE_EQUATION_NUMBER_ANYWHERE_PATTERN.search(text)
    if match is None:
        return None
    tail = text[match.end() :].strip(" \t\n\r,;:")
    if tail and not _SHORT_EQUATION_TAIL_PATTERN.fullmatch(tail):
        return None
    return f"({match.group('number').strip()})"


def _equation_number_value(number: str | None) -> int | None:
    if not number:
        return None
    match = re.fullmatch(r"\((\d+)\)", number.strip())
    if match is None:
        return None
    return int(match.group(1))


def _formula_counter_after_preserved_number(current: int, number: str | None) -> int:
    parsed = _equation_number_value(number)
    if parsed is None:
        return current
    return max(current, parsed)


def _formula_fallback_html(
    formula: Any,
    document: DocumentIR,
    *,
    fallback_reason: str | None = None,
) -> tuple[str, list[str]]:
    latex = getattr(formula, "latex", "") or ""
    formula_id = getattr(formula, "formula_id", "")
    fallback_text = _formula_plaintext_fallback_text(
        getattr(formula, "source_text", ""),
        latex,
        formula_id=formula_id,
    )
    asset_id = getattr(formula, "asset_id", None)
    if asset_id and fallback_reason != "source_text_plaintext":
        for page in document.pages:
            for asset in page.assets:
                if asset.asset_id == asset_id and asset.path:
                    alt_text = _formula_image_fallback_alt_text(
                        formula_id=formula_id,
                        asset_alt_text=getattr(asset, "alt_text", None),
                    )
                    return (
                        f'<span class="formula-image-fallback" '
                        f'data-fallback-formula-id="{escape(formula_id, quote=True)}">'
                        f'<img src="{escape(asset.path, quote=True)}" '
                        f'alt="{escape(alt_text, quote=True)}" />'
                        f"</span>",
                        ["formula_image_fallback"],
                    )
    return _formula_plaintext_fallback_html(fallback_text), ["formula_plaintext_fallback"]


def _formula_image_fallback_alt_text(
    *,
    formula_id: str = "",
    asset_alt_text: str | None = None,
) -> str:
    if asset_alt_text and not _looks_like_corrupt_text_layer_formula(
        asset_alt_text,
        source_text=asset_alt_text,
    ):
        return asset_alt_text
    return f"formula image {formula_id}" if formula_id else "formula image"


def _uses_formula_image_fallback(flags: list[str]) -> bool:
    return "formula_image_fallback" in flags


def _validate_formula_latex(
    latex: str,
    *,
    source_text: str = "",
    display: bool = False,
) -> _FormulaRenderValidation:
    normalized = _normalized_text(latex)
    if not normalized:
        return _FormulaRenderValidation(False, "source_text_plaintext")
    if display and _looks_like_corrupt_text_layer_formula(
        normalized,
        source_text=source_text,
    ):
        return _FormulaRenderValidation(False, "formula_asset_image")
    if _formula_latex_looks_prose_like(normalized):
        return _FormulaRenderValidation(False, "source_text_plaintext")
    signal_count = len(_FORMULA_RENDER_MATH_SIGNAL_PATTERN.findall(normalized))
    if signal_count < (1 if len(normalized) <= 24 else 2):
        return _FormulaRenderValidation(False, "source_text_plaintext")
    if not _latex_looks_renderable(normalized):
        return _FormulaRenderValidation(
            False,
            "formula_asset_image"
            if source_text.strip() and source_text.strip() != normalized
            else "source_text_plaintext",
        )
    return _FormulaRenderValidation(True, None)


_RAW_FORMULA_CORRUPTION_MARKER_PATTERN = re.compile(r"[\x01-\x04¼þðÞÐ]|@[A-Za-z]")
_FORMULA_SLASH_GLYPH_SUSPECT_PATTERN = re.compile(
    r"(?:\\partial\s+[A-Za-z]{2,}|[A-Za-z]_?[A-Za-z]\s*=\s*k(?:\^?\d|\{\d\})|"
    r"[A-Za-z]\s*0\s*[A-Za-z])"
)
_FORMULA_PRIME_GLYPH_SUSPECT_PATTERN = re.compile(r"\b[fqmn]\s*0\s*[sn]\b|\b[fqmn]0[sn]\b")


def _looks_like_corrupt_text_layer_formula(
    latex: str,
    *,
    source_text: str = "",
) -> bool:
    if _has_structured_visual_formula_latex(latex) and not _has_unrepaired_formula_corruption(
        latex
    ):
        return False
    if _has_unrepaired_formula_corruption(latex):
        return True
    return bool(
        _RAW_FORMULA_CORRUPTION_MARKER_PATTERN.search(source_text)
        and not _has_structured_visual_formula_latex(latex)
    )


def _has_unrepaired_formula_corruption(latex: str) -> bool:
    return bool(
        _FORMULA_SLASH_GLYPH_SUSPECT_PATTERN.search(latex)
        or _FORMULA_PRIME_GLYPH_SUSPECT_PATTERN.search(latex)
    )


def _has_structured_visual_formula_latex(latex: str) -> bool:
    return bool(
        re.search(
            r"\\(?:frac|dfrac|tfrac|sum|int|partial|nabla|sqrt)\b|\\begin\{",
            latex,
        )
    )


def _formula_latex_looks_prose_like(text: str) -> bool:
    words = re.findall(r"[A-Za-z]{3,}", text)
    math_signals = len(_FORMULA_RENDER_MATH_SIGNAL_PATTERN.findall(text))
    if re.search(r"[,.;]\s+[A-Za-z]{3,}", text) and len(words) >= 4:
        return True
    if len(text) > 80 and len(words) >= 6 and math_signals < 3:
        return True
    if len(words) >= 10 and math_signals <= max(1, len(words) // 8):
        return True
    return False


def _katex_like_html(latex: str, *, display: bool) -> str:
    class_name = "katex-display" if display else "katex"
    visual_html = _latex_to_visual_html(latex)
    return (
        f'<span class="{class_name}" data-latex="{escape(latex, quote=True)}">'
        f'<span class="katex-mathml"><math><semantics>'
        f'<annotation encoding="application/x-tex">{escape(latex)}</annotation>'
        f"</semantics></math></span>"
        f'<span class="katex-html" aria-hidden="true">{visual_html}</span>'
        f"</span>"
    )


_LATEX_SYMBOLS = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "theta": "θ",
    "lambda": "λ",
    "mu": "μ",
    "pi": "π",
    "sigma": "σ",
    "phi": "φ",
    "omega": "ω",
    "sum": "∑",
    "int": "∫",
    "infty": "∞",
    "le": "≤",
    "ge": "≥",
    "ne": "≠",
    "approx": "≈",
    "times": "×",
    "cdot": "·",
    "pm": "±",
    "to": "→",
    "left": "",
    "right": "",
}


def _latex_to_visual_html(latex: str) -> str:
    html, _ = _parse_latex(latex, 0, None)
    return html


def _parse_latex(text: str, index: int, stop: str | None) -> tuple[str, int]:
    parts: list[str] = []
    while index < len(text):
        char = text[index]
        if stop is not None and char == stop:
            return "".join(parts), index + 1
        if char == "\\":
            command, index = _read_command(text, index + 1)
            if command == "frac":
                numerator, index = _read_latex_group(text, index)
                denominator, index = _read_latex_group(text, index)
                parts.append(
                    '<span class="katex-frac">'
                    f'<span class="katex-num">{numerator}</span>'
                    f'<span class="katex-den">{denominator}</span>'
                    "</span>"
                )
            elif command == "sqrt":
                radicand, index = _read_latex_group(text, index)
                parts.append(f'<span class="katex-sqrt">√<span>{radicand}</span></span>')
            else:
                parts.append(escape(_LATEX_SYMBOLS.get(command, f"\\{command}")))
        elif char in {"^", "_"}:
            script, index = _read_latex_group(text, index + 1)
            tag = "sup" if char == "^" else "sub"
            parts.append(f"<{tag}>{script}</{tag}>")
        elif char == "{":
            inner, index = _parse_latex(text, index + 1, "}")
            parts.append(inner)
        elif char == "}":
            parts.append(escape(char))
            index += 1
        else:
            parts.append(escape(char))
            index += 1
    return "".join(parts), index


def _read_command(text: str, index: int) -> tuple[str, int]:
    start = index
    while index < len(text) and text[index].isalpha():
        index += 1
    if index == start and index < len(text):
        return text[index], index + 1
    return text[start:index], index


def _read_latex_group(text: str, index: int) -> tuple[str, int]:
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return "", index
    if text[index] == "{":
        return _parse_latex(text, index + 1, "}")
    if text[index] == "\\":
        command, next_index = _read_command(text, index + 1)
        return escape(_LATEX_SYMBOLS.get(command, f"\\{command}")), next_index
    return escape(text[index]), index + 1


def _latex_looks_renderable(latex: str) -> bool:
    stripped = latex.strip()
    if not stripped:
        return False
    pairs = [("{", "}"), ("(", ")"), ("[", "]")]
    for left, right in pairs:
        if stripped.count(left) != stripped.count(right):
            return False
    return True


def _text_overflows(
    text: str,
    bbox: BoundingBox,
    font_size_pt: float,
    line_height: float,
) -> bool:
    return _estimated_text_height(text, bbox, font_size_pt, line_height) > _bbox_height(bbox)


def _estimated_text_height(
    text: str,
    bbox: BoundingBox,
    font_size_pt: float,
    line_height: float,
) -> float:
    estimated_lines = _estimated_line_count(text, _bbox_width(bbox), font_size_pt)
    if estimated_lines == 0:
        return 0.0
    return estimated_lines * font_size_pt * line_height


def _estimated_content_height(
    text: str,
    bbox: BoundingBox,
    font_size_pt: float,
    line_height: float,
    document: DocumentIR,
    block: DocumentBlock,
) -> float:
    return _estimated_formula_aware_height(
        text,
        _bbox_width(bbox),
        font_size_pt,
        line_height,
        document=document,
        block=block,
    )


def _estimated_formula_aware_height(
    text: str,
    width_pt: float,
    font_size_pt: float,
    line_height: float,
    *,
    document: DocumentIR | None = None,
    block: DocumentBlock | None = None,
) -> float:
    estimation_text = (
        _formula_visual_estimation_text(text, document, block)
        if document is not None and block is not None
        else text
    )
    estimated_height = _estimated_text_height(
        estimation_text,
        BoundingBox(x0=0, y0=0, x1=max(width_pt, 1.0), y1=1),
        font_size_pt,
        line_height,
    )
    if document is None or block is None or not _contains_display_formula(text, document, block):
        return estimated_height

    estimated_formula_lines = _estimated_line_count(
        estimation_text,
        max(width_pt, 1.0),
        font_size_pt,
    )
    formula_like = _is_formula_like_text(text) or _is_formula_like_block_for_estimation(block, text)
    display_formula_count = max(1, _display_formula_count(text, document, block))
    formula_visual_heights = _display_formula_visual_heights(
        text,
        document,
        block,
        font_size_pt=font_size_pt,
    )
    has_formula_only_content = not _normalized_text(
        _FORMULA_REF_PATTERN.sub(" ", _FORMULA_PLACEHOLDER_PATTERN.sub(" ", text))
    )
    min_display_lines = _FORMULA_LIKE_MIN_LINES if formula_like else _DISPLAY_FORMULA_MIN_LINES
    vertical_margin_em = (
        _FORMULA_LIKE_VERTICAL_MARGIN_EM if formula_like else _DISPLAY_FORMULA_VERTICAL_MARGIN_EM
    )
    display_lines = max(
        min_display_lines * display_formula_count,
        float(estimated_formula_lines)
        + (_DISPLAY_FORMULA_SINGLE_BLOCK_EXTRA_LINES if not formula_like else 0.0),
    )
    display_height = display_lines * font_size_pt * max(line_height, 1.2) + font_size_pt * (
        vertical_margin_em + (display_formula_count - 1) * _DISPLAY_FORMULA_PER_NODE_MARGIN_EM
    )
    if formula_visual_heights:
        visual_height = sum(formula_visual_heights) + font_size_pt * (
            vertical_margin_em
            + max(0, len(formula_visual_heights) - 1) * _DISPLAY_FORMULA_PER_NODE_MARGIN_EM
        )
        if block.role == BlockRole.FORMULA or has_formula_only_content:
            display_height = visual_height
        else:
            display_height = max(display_height, estimated_height + visual_height)
    return max(estimated_height, display_height)


def _display_formula_visual_heights(
    text: str,
    document: DocumentIR,
    block: DocumentBlock,
    *,
    font_size_pt: float,
) -> list[float]:
    heights: list[float] = []
    formulas_by_id = document.formulas_by_id()
    for match in _FORMULA_REF_PATTERN.finditer(text):
        formula = formulas_by_id.get(match.group(1))
        if formula is None:
            if block.role == BlockRole.FORMULA:
                heights.append(_formula_latex_heuristic_height(match.group(0), font_size_pt))
            continue
        if formula.display_mode != "display" and block.role != BlockRole.FORMULA:
            continue
        heights.append(_formula_rendered_or_heuristic_height(formula.latex, font_size_pt))

    legacy_formulas = {formula.placeholder: formula for formula in block.formulas}
    for match in _FORMULA_PLACEHOLDER_PATTERN.finditer(text):
        formula = legacy_formulas.get(match.group(0))
        if formula is None:
            if block.role == BlockRole.FORMULA:
                heights.append(_formula_latex_heuristic_height(match.group(0), font_size_pt))
            continue
        if formula.kind != "display":
            continue
        heights.append(_formula_rendered_or_heuristic_height(formula.latex, font_size_pt))

    if not heights and block.role == BlockRole.FORMULA and text.strip():
        heights.append(_formula_latex_heuristic_height(text, font_size_pt))
    return heights


def _formula_rendered_or_heuristic_height(latex: str, font_size_pt: float) -> float:
    render_latex, _tag_number = _strip_latex_equation_tag(latex)
    rendered = _katex_html(render_latex, display=True) if render_latex.strip() else None
    rendered_height = _height_from_katex_html(rendered, font_size_pt) if rendered else None
    if rendered_height is not None:
        return max(rendered_height, _formula_latex_heuristic_height(render_latex, font_size_pt))
    return _formula_latex_heuristic_height(render_latex, font_size_pt)


def _height_from_katex_html(html: str, font_size_pt: float) -> float | None:
    max_em = 0.0
    for match in _KATEX_STRUT_STYLE_PATTERN.finditer(html):
        height_em = 0.0
        depth_em = 0.0
        for value_match in _CSS_EM_VALUE_PATTERN.finditer(match.group("style")):
            value = float(value_match.group("value"))
            if value_match.group("name").lower() == "height":
                height_em = max(height_em, value)
            elif value_match.group("name").lower() == "vertical-align":
                depth_em = max(depth_em, abs(value))
        max_em = max(max_em, height_em + depth_em)
    if max_em <= 0:
        return None
    return max_em * font_size_pt


def _formula_latex_heuristic_height(latex: str, font_size_pt: float) -> float:
    normalized = latex or ""
    factor = 1.45
    tall_constructs = len(re.findall(r"\\(?:dfrac|tfrac|frac|over)\b", normalized))
    if tall_constructs:
        factor = max(factor, 2.9 + min(tall_constructs - 1, 2) * 0.45)
    if re.search(r"\\(?:int|sum|prod|lim)\b", normalized):
        factor = max(factor, 2.55)
    if re.search(r"(?:[_^]\s*\{[^{}]*(?:[_^]\s*\{[^{}]+\})[^{}]*\})", normalized):
        factor = max(factor, 2.25)
    script_count = len(re.findall(r"[_^]\s*(?:\{|\\?[A-Za-z0-9])", normalized))
    if script_count >= 3:
        factor = max(factor, 1.9)
    return factor * font_size_pt


def _estimated_formula_aware_line_count(
    text: str,
    width_pt: float,
    font_size_pt: float,
    line_height: float,
    *,
    document: DocumentIR | None = None,
    block: DocumentBlock | None = None,
) -> int:
    if font_size_pt <= 0 or line_height <= 0:
        return 1
    estimated_height = _estimated_formula_aware_height(
        text,
        width_pt,
        font_size_pt,
        line_height,
        document=document,
        block=block,
    )
    return max(
        1,
        ceil(estimated_height / (font_size_pt * line_height)),
    )


def _content_overflows(
    text: str,
    bbox: BoundingBox,
    font_size_pt: float,
    line_height: float,
    document: DocumentIR,
    block: DocumentBlock,
) -> bool:
    return (
        _estimated_content_height(text, bbox, font_size_pt, line_height, document, block)
        > _bbox_height(bbox) + _LAYOUT_EPSILON_PT
    )


def _contains_display_formula(
    text: str,
    document: DocumentIR,
    block: DocumentBlock,
) -> bool:
    formulas_by_id = document.formulas_by_id()
    for match in _FORMULA_REF_PATTERN.finditer(text):
        formula = formulas_by_id.get(match.group(1))
        if formula is None:
            return block.role == BlockRole.FORMULA
        if formula.display_mode == "display":
            return True

    legacy_formulas = {formula.placeholder: formula for formula in block.formulas}
    for match in _FORMULA_PLACEHOLDER_PATTERN.finditer(text):
        formula = legacy_formulas.get(match.group(0))
        if formula is None:
            return block.role == BlockRole.FORMULA
        if formula.kind == "display":
            return True

    return False


def _display_formula_count(
    text: str,
    document: DocumentIR,
    block: DocumentBlock,
) -> int:
    count = 0
    formulas_by_id = document.formulas_by_id()
    for match in _FORMULA_REF_PATTERN.finditer(text):
        formula = formulas_by_id.get(match.group(1))
        if formula is None:
            if block.role == BlockRole.FORMULA:
                count += 1
            continue
        if formula.display_mode == "display" or block.role == BlockRole.FORMULA:
            count += 1
    legacy_formulas = {formula.placeholder: formula for formula in block.formulas}
    for match in _FORMULA_PLACEHOLDER_PATTERN.finditer(text):
        formula = legacy_formulas.get(match.group(0))
        if formula is None:
            if block.role == BlockRole.FORMULA:
                count += 1
            continue
        if formula.kind == "display":
            count += 1
    if count == 0 and block.role == BlockRole.FORMULA and text.strip():
        return 1
    return count


def _display_formula_refs(
    text: str,
    document: DocumentIR,
    block: DocumentBlock,
) -> list[Any]:
    refs: list[Any] = []
    formulas_by_id = document.formulas_by_id()
    for match in _FORMULA_REF_PATTERN.finditer(text):
        formula = formulas_by_id.get(match.group(1))
        if formula is not None and (
            formula.display_mode == "display" or block.role == BlockRole.FORMULA
        ):
            refs.append(formula)

    legacy_formulas = {formula.placeholder: formula for formula in block.formulas}
    for match in _FORMULA_PLACEHOLDER_PATTERN.finditer(text):
        formula = legacy_formulas.get(match.group(0))
        if formula is not None and formula.kind == "display":
            refs.append(formula)
    return refs


def _display_formula_source_number(
    text: str,
    document: DocumentIR,
    block: DocumentBlock,
) -> str | None:
    text_number = _extract_source_equation_number(text)
    if text_number is not None:
        return text_number
    preserved_without_number = False
    for formula in _display_formula_refs(text, document, block):
        latex = getattr(formula, "latex", "") or ""
        tag_number = _extract_latex_equation_tag(latex)
        if tag_number is not None:
            return tag_number
        source_number = _extract_source_equation_number(getattr(formula, "source_text", "") or "")
        if source_number is not None:
            return source_number
        if "formula_equation_number_preserved" in getattr(formula, "quality_flags", []):
            preserved_without_number = True
    if preserved_without_number:
        return ""
    return None


def _strip_source_equation_number_from_text(text: str) -> str:
    stripped = _SOURCE_EQUATION_NUMBER_PATTERN.sub("", text).rstrip(" \t\n\r,;:")
    return stripped if stripped else text


def _strip_source_equation_number_from_formula_text(text: str, number: str | None) -> str:
    if not text or not number:
        return text
    pattern = re.compile(r"(?:[,;:]\s*)?" + re.escape(number) + r"(?P<tail>\s*)")
    match = None
    for candidate in pattern.finditer(text):
        tail = text[candidate.end() :].strip(" \t\n\r,;:")
        if tail and not _SHORT_EQUATION_TAIL_PATTERN.fullmatch(tail):
            continue
        match = candidate
    if match is None:
        return text
    stripped = (text[: match.start()] + text[match.end() :]).rstrip(" \t\n\r,;:")
    return stripped if stripped else text


def _formula_with_stripped_source_number(formula: Any, number: str | None) -> Any:
    if not number:
        return formula
    source_text = getattr(formula, "source_text", "") or ""
    latex = getattr(formula, "latex", "") or ""
    updates: dict[str, Any] = {}
    stripped_source = _strip_source_equation_number_from_formula_text(source_text, number)
    stripped_latex = _strip_source_equation_number_from_formula_text(latex, number)
    if stripped_source != source_text:
        updates["source_text"] = stripped_source
    if stripped_latex != latex:
        updates["latex"] = stripped_latex
    if not updates:
        return formula
    if hasattr(formula, "model_copy"):
        return formula.model_copy(update=updates)
    return formula


def _document_with_stripped_formula_source_numbers(
    document: DocumentIR,
    text: str,
    block: DocumentBlock,
    number: str | None,
) -> DocumentIR:
    if not number:
        return document
    formula_refs = _display_formula_refs(text, document, block)
    if not formula_refs:
        return document
    updated_formulas = list(document.formulas)
    updated = False
    for index, formula in enumerate(updated_formulas):
        if not any(
            getattr(formula, "formula_id", None) == getattr(ref, "formula_id", None)
            for ref in formula_refs
        ):
            continue
        updated_formula = _formula_with_stripped_source_number(formula, number)
        if updated_formula is formula:
            continue
        updated_formulas[index] = updated_formula
        updated = True
    if not updated:
        return document
    return document.model_copy(update={"formulas": updated_formulas}, deep=True)


def _formula_estimation_text(
    text: str,
    document: DocumentIR,
    block: DocumentBlock,
) -> str:
    formulas_by_id = document.formulas_by_id()

    def replace_formula_ref(match: re.Match[str]) -> str:
        formula = formulas_by_id.get(match.group(1))
        if formula is None:
            return match.group(0)
        return formula.latex or formula.source_text or match.group(0)

    estimated = _FORMULA_REF_PATTERN.sub(replace_formula_ref, text)

    legacy_formulas = {formula.placeholder: formula for formula in block.formulas}

    def replace_legacy_ref(match: re.Match[str]) -> str:
        formula = legacy_formulas.get(match.group(0))
        if formula is None:
            return match.group(0)
        return formula.latex or formula.source_text or match.group(0)

    return _FORMULA_PLACEHOLDER_PATTERN.sub(replace_legacy_ref, estimated)


def _formula_visual_estimation_text(
    text: str,
    document: DocumentIR,
    block: DocumentBlock,
) -> str:
    formulas_by_id = document.formulas_by_id()

    def replace_formula_ref(match: re.Match[str]) -> str:
        formula = formulas_by_id.get(match.group(1))
        if formula is None:
            return "x" if block.role == BlockRole.FORMULA else ""
        if formula.display_mode == "display" or block.role == BlockRole.FORMULA:
            return "x" if block.role == BlockRole.FORMULA else ""
        return _inline_formula_visual_estimation_token(
            getattr(formula, "source_text", "") or getattr(formula, "latex", "")
        )

    estimated = _FORMULA_REF_PATTERN.sub(replace_formula_ref, text)

    legacy_formulas = {formula.placeholder: formula for formula in block.formulas}

    def replace_legacy_ref(match: re.Match[str]) -> str:
        formula = legacy_formulas.get(match.group(0))
        if formula is None:
            return "x" if block.role == BlockRole.FORMULA else ""
        if formula.kind == "display" or block.role == BlockRole.FORMULA:
            return "x" if block.role == BlockRole.FORMULA else ""
        return _inline_formula_visual_estimation_token(
            formula.source_text or formula.latex or formula.placeholder
        )

    return _FORMULA_PLACEHOLDER_PATTERN.sub(replace_legacy_ref, estimated)


def _inline_formula_visual_estimation_token(text: str) -> str:
    normalized = _normalized_text(text)
    if not normalized:
        return "x"
    normalized = re.sub(r"\\(?:left|right|displaystyle|textstyle)\b", "", normalized)
    normalized = re.sub(
        r"\\(?:frac|dfrac|tfrac)\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"\1/\2", normalized
    )
    normalized = re.sub(r"\\[A-Za-z]+", "x", normalized)
    normalized = re.sub(r"[{}]", "", normalized)
    normalized = re.sub(r"\s+", "", normalized).strip()
    if not normalized:
        return "x"
    if len(normalized) <= _INLINE_FORMULA_ESTIMATION_MAX_CHARS:
        return normalized
    return normalized[: _INLINE_FORMULA_ESTIMATION_MAX_CHARS - 1] + "…"


def _line_capacity(bbox: BoundingBox, font_size_pt: float) -> int:
    if font_size_pt <= 0:
        return 1
    return max(1, int(_bbox_width(bbox) / font_size_pt))


def _line_count_capacity(bbox: BoundingBox, font_size_pt: float, line_height: float) -> int:
    if font_size_pt <= 0 or line_height <= 0:
        return 1
    return max(1, int(_bbox_height(bbox) / (font_size_pt * line_height)))


def _split_text_to_fit(
    text: str,
    bbox: BoundingBox,
    font_size_pt: float,
    line_height: float,
) -> tuple[str, str]:
    if not _text_overflows(text, bbox, font_size_pt, line_height):
        return text, ""

    max_lines = _line_count_capacity(bbox, font_size_pt, line_height)
    max_units = _line_capacity(bbox, font_size_pt)
    lines_used = 1
    units_used = 0.0
    split_index = 0

    for index, char in enumerate(text):
        if char == "\n":
            lines_used += 1
            units_used = 0.0
        else:
            char_units = _glyph_width_factor(char)
            if units_used > 0 and units_used + char_units > max_units:
                lines_used += 1
                units_used = 0.0
            units_used += char_units
        if lines_used > max_lines:
            break
        split_index = index + 1

    if split_index <= 0:
        split_index = 1

    whitespace_index = max(
        text.rfind(" ", 0, split_index),
        text.rfind("\n", 0, split_index),
        text.rfind("。", 0, split_index),
        text.rfind("，", 0, split_index),
        text.rfind(".", 0, split_index),
        text.rfind(",", 0, split_index),
    )
    if whitespace_index >= max(1, int(split_index * 0.6)):
        split_index = whitespace_index + 1

    return text[:split_index].rstrip(), text[split_index:].lstrip()


def _unique_flags(flags: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for flag in flags:
        if flag and flag not in seen:
            unique.append(flag)
            seen.add(flag)
    return unique


def _continuation_bbox(size: PageSize) -> BoundingBox:
    x0 = min(_CONTINUATION_MARGIN_PT, max(0.0, size.width / 8))
    y0 = min(_CONTINUATION_MARGIN_PT, max(0.0, size.height / 8))
    x1 = max(x0 + 1, size.width - x0)
    y1 = max(y0 + 1, size.height - y0)
    return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)


def _expand_bbox_to_fit(
    text: str,
    bbox: BoundingBox,
    page_size: PageSize,
    font_size_pt: float,
    line_height: float,
    document: DocumentIR,
    block: DocumentBlock,
) -> BoundingBox | None:
    needed_height = _estimated_content_height(
        text,
        bbox,
        font_size_pt,
        line_height,
        document,
        block,
    )
    if needed_height <= _bbox_height(bbox) + _LAYOUT_EPSILON_PT:
        return bbox
    page_bottom = max(bbox.y1, page_size.height - _CONTINUATION_MARGIN_PT)
    expanded_y1 = min(page_bottom, bbox.y0 + needed_height)
    if (
        expanded_y1 > bbox.y1 + _LAYOUT_EPSILON_PT
        and expanded_y1 - bbox.y0 + _LAYOUT_EPSILON_PT >= needed_height
    ):
        return BoundingBox(x0=bbox.x0, y0=bbox.y0, x1=bbox.x1, y1=expanded_y1)
    return None


@dataclass(frozen=True)
class RenderBlock:
    block_id: str
    role: BlockRole
    bbox: BoundingBox
    text: str
    style_seed: StyleSeed
    font_size_pt: float
    html: str | None = None
    font_scale: float = 1.0
    render_intent: str = "normal"
    text_align: str | None = None
    font_weight: int | None = None
    font_style: str | None = None
    first_line_indent_em: float = 0.0
    line_height: float | None = None
    font_stack: list[str] | None = None
    formula_number: str | None = None
    figure_group_id: str | None = None
    caption_for_asset_id: str | None = None
    source_block_id: str | None = None
    layout_signature: str | None = None
    quality_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenderAsset:
    asset_id: str
    kind: str
    bbox: BoundingBox
    path: str | None = None
    alt_text: str | None = None
    figure_group_id: str | None = None
    caption_block_id: str | None = None
    quality_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenderPage:
    page_id: str
    size: PageSize
    blocks: list[RenderBlock]
    assets: list[RenderAsset] = field(default_factory=list)
    footer_text: str | None = None


@dataclass(frozen=True)
class RenderDocument:
    doc_id: str
    target_lang: str
    pages: list[RenderPage]
    font_stack: list[str] = field(default_factory=lambda: _SCHEMA_RENDER_DEFAULTS.font_stack.copy())
    line_height: float = _SCHEMA_RENDER_DEFAULTS.line_height
    paragraph_spacing_em: float = _SCHEMA_RENDER_DEFAULTS.paragraph_spacing_em
    layout_mode: str = _enum_value(_SCHEMA_RENDER_DEFAULTS.layout_mode)
    layout_trace: dict[str, Any] = field(default_factory=dict)

    def layout_issues(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for page in self.pages:
            positioned_items: list[tuple[str, str, BoundingBox]] = []
            for block in page.blocks:
                if block.bbox.x1 > page.size.width or block.bbox.y1 > page.size.height:
                    issues.append(
                        {
                            "kind": "bbox_outside_page",
                            "page_id": page.page_id,
                            "item_id": block.block_id,
                            "item_type": "block",
                        }
                    )
                if not block.text.strip():
                    issues.append(
                        {
                            "kind": "empty_render_block",
                            "page_id": page.page_id,
                            "item_id": block.block_id,
                            "item_type": "block",
                        }
                    )
                positioned_items.append(("block", block.block_id, block.bbox))
            for asset in page.assets:
                if asset.bbox.x1 > page.size.width or asset.bbox.y1 > page.size.height:
                    issues.append(
                        {
                            "kind": "bbox_outside_page",
                            "page_id": page.page_id,
                            "item_id": asset.asset_id,
                            "item_type": "asset",
                        }
                    )
                positioned_items.append(("asset", asset.asset_id, asset.bbox))

            for index, item in enumerate(positioned_items):
                item_type, item_id, item_bbox = item
                item_area = _bbox_area(item_bbox)
                if item_area <= 0:
                    continue
                for other_type, other_id, other_bbox in positioned_items[index + 1 :]:
                    overlap_area = _bbox_overlap_area(item_bbox, other_bbox)
                    if overlap_area <= 0:
                        continue
                    overlap_ratio = overlap_area / min(item_area, _bbox_area(other_bbox))
                    if overlap_ratio >= 0.12:
                        issues.append(
                            {
                                "kind": "overlap",
                                "page_id": page.page_id,
                                "item_id": item_id,
                                "item_type": item_type,
                                "other_id": other_id,
                                "other_type": other_type,
                                "overlap_ratio": round(overlap_ratio, 4),
                            }
                        )
        for trace in self.layout_trace.get("blocks", []):
            flags = trace.get("quality_flags", [])
            if "overflow_clipped" in flags:
                issues.append(
                    {
                        "kind": "overflow_clipped",
                        "page_id": trace.get("output_page_id"),
                        "item_id": trace.get("render_block_id"),
                        "item_type": "block",
                        "required_height_pt": trace.get("required_height_pt"),
                        "allocated_height_pt": trace.get("allocated_height_pt"),
                    }
                )
        for group in self.layout_trace.get("figure_groups", []):
            flags = group.get("quality_flags", [])
            if "figure_group_separated" in flags:
                issues.append(
                    {
                        "kind": "figure_group_separated",
                        "page_id": group.get("output_page_id"),
                        "item_id": group.get("asset_id"),
                        "item_type": "asset",
                        "other_id": group.get("caption_render_block_id")
                        or group.get("caption_block_id"),
                        "other_type": "block",
                    }
                )
            if "asset_caption_mismatch" in flags:
                issues.append(
                    {
                        "kind": "asset_caption_mismatch",
                        "page_id": group.get("output_page_id"),
                        "item_id": group.get("asset_id"),
                        "item_type": "asset",
                        "other_id": group.get("caption_render_block_id")
                        or group.get("caption_block_id"),
                        "other_type": "block",
                    }
                )
        return issues

    def diagnostics(self) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        quality_flag_counts: dict[str, int] = {}
        block_count = 0
        formula_rendered_count = 0
        formula_block_overflow_count = 0
        formula_height_adjusted_count = 0
        formula_multi_display_block_count = 0
        unresolved_formula_placeholders: list[dict[str, str]] = []
        page_utilization: list[dict[str, Any]] = []
        low_utilization_pages: list[str] = []
        underfilled_reflow_pages: list[str] = []
        single_fragment_pages: list[str] = []
        for page_index, page in enumerate(self.pages):
            page_flags: list[dict[str, Any]] = []
            asset_flags: list[dict[str, Any]] = []
            text_area = 0.0
            asset_area = sum(_bbox_area(asset.bbox) for asset in page.assets)
            for block in page.blocks:
                block_count += 1
                text_area += _bbox_area(block.bbox)
                block_html = block.html or ""
                formula_rendered_count += block_html.count("data-formula-id=")
                if "formula_height_adjusted" in block.quality_flags:
                    formula_height_adjusted_count += 1
                display_node_count = block_html.count("formula formula-display")
                if display_node_count > 1:
                    formula_multi_display_block_count += 1
                if "formula_height_risk" in block.quality_flags:
                    formula_block_overflow_count += 1
                for placeholder in _FORMULA_PLACEHOLDER_PATTERN.findall(block_html):
                    unresolved_formula_placeholders.append(
                        {
                            "page_id": page.page_id,
                            "block_id": block.block_id,
                            "placeholder": placeholder,
                        }
                    )
                for formula_id in _UNRESOLVED_FORMULA_ID_ATTR_PATTERN.findall(block_html):
                    formula_id = unescape(formula_id)
                    unresolved_formula_placeholders.append(
                        {
                            "page_id": page.page_id,
                            "block_id": block.block_id,
                            "formula_id": formula_id,
                            "placeholder": f"{{{{formula:{formula_id}}}}}",
                        }
                    )
                for flag in block.quality_flags:
                    quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
                if block.quality_flags:
                    page_flags.append(
                        {
                            "block_id": block.block_id,
                            "role": block.role.value,
                            "render_intent": block.render_intent,
                            "font_scale": block.font_scale,
                            "quality_flags": block.quality_flags,
                        }
                    )
            page_area = page.size.width * page.size.height
            utilization = round(text_area / page_area, 4) if page_area > 0 else 0.0
            asset_utilization = round(asset_area / page_area, 4) if page_area > 0 else 0.0
            combined_utilization = (
                round((text_area + asset_area) / page_area, 4) if page_area > 0 else 0.0
            )
            bottom_y = max(
                [
                    *[block.bbox.y1 for block in page.blocks],
                    *[asset.bbox.y1 for asset in page.assets],
                    0.0,
                ]
            )
            bottom_whitespace = max(0.0, page.size.height - bottom_y)
            bottom_whitespace_ratio = (
                round(bottom_whitespace / page.size.height, 4) if page.size.height > 0 else 0.0
            )
            page_utilization.append(
                {
                    "page_id": page.page_id,
                    "text_area_ratio": utilization,
                    "asset_area_ratio": asset_utilization,
                    "combined_area_ratio": combined_utilization,
                    "bottom_whitespace_pt": round(bottom_whitespace, 4),
                    "bottom_whitespace_ratio": bottom_whitespace_ratio,
                    "block_count": len(page.blocks),
                    "asset_count": len(page.assets),
                }
            )
            if page.blocks and utilization < _LOW_UTILIZATION_THRESHOLD:
                low_utilization_pages.append(page.page_id)
            if (
                self.layout_mode == "continuous_reflow"
                and page_index < len(self.pages) - 1
                and page.blocks
                and _is_underfilled_reflow_page(
                    combined_utilization=combined_utilization,
                    bottom_whitespace_ratio=bottom_whitespace_ratio,
                )
            ):
                underfilled_reflow_pages.append(page.page_id)
            if len(page.blocks) == 1:
                text = page.blocks[0].text.strip()
                estimated_lines = _estimated_line_count(
                    text,
                    _bbox_width(page.blocks[0].bbox),
                    page.blocks[0].font_size_pt,
                )
                if (
                    len(text) < _MIN_FINAL_FRAGMENT_CHARS
                    or estimated_lines < _MIN_FINAL_FRAGMENT_LINES
                ):
                    single_fragment_pages.append(page.page_id)
            for asset in page.assets:
                for flag in asset.quality_flags:
                    quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
                if asset.quality_flags:
                    asset_flags.append(
                        {
                            "asset_id": asset.asset_id,
                            "kind": asset.kind,
                            "quality_flags": asset.quality_flags,
                        }
                    )
            pages.append(
                {
                    "page_id": page.page_id,
                    "block_count": len(page.blocks),
                    "asset_count": len(page.assets),
                    "flagged_blocks": page_flags,
                    "flagged_assets": asset_flags,
                }
            )
        for group in self.layout_trace.get("figure_groups", []):
            for flag in group.get("quality_flags", []):
                if flag in {
                    "figure_group_separated",
                    "asset_caption_mismatch",
                    "asset_caption_missing",
                    "figure_group_split",
                }:
                    quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
        for artifact in self.layout_trace.get("suppressed_artifacts", []):
            for flag in artifact.get("quality_flags", []):
                quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
        intent_requirements = self.layout_trace.get("intent_requirements", [])
        if isinstance(intent_requirements, list):
            for requirement in intent_requirements:
                if not isinstance(requirement, dict):
                    continue
                for flag in requirement.get("quality_flags", []):
                    if isinstance(flag, str) and flag:
                        quality_flag_counts[flag] = quality_flag_counts.get(flag, 0) + 1
        if underfilled_reflow_pages:
            quality_flag_counts["underfilled_reflow_page"] = len(underfilled_reflow_pages)
        column_flow_issues = _column_flow_issues(self.layout_trace)
        right_column_start_pages = sorted(
            {
                str(issue["page_id"])
                for issue in column_flow_issues
                if issue.get("kind") == "right_column_page_start"
            }
        )
        left_column_underfilled_pages = sorted(
            {
                str(issue["page_id"])
                for issue in column_flow_issues
                if issue.get("kind") == "left_column_underfilled_before_right_column"
            }
        )
        if right_column_start_pages:
            quality_flag_counts["right_column_page_start"] = len(right_column_start_pages)
        if left_column_underfilled_pages:
            quality_flag_counts["left_column_underfilled_before_right_column"] = len(
                left_column_underfilled_pages
            )
        return {
            "doc_id": self.doc_id,
            "target_lang": self.target_lang,
            "layout_mode": self.layout_mode,
            "page_count": len(self.pages),
            "block_count": block_count,
            "quality_flag_counts": quality_flag_counts,
            "layout_issues": self.layout_issues(),
            "formula_rendered_count": formula_rendered_count,
            "formula_block_overflow_count": formula_block_overflow_count,
            "formula_height_adjusted_count": formula_height_adjusted_count,
            "formula_multi_display_block_count": formula_multi_display_block_count,
            "formula_dom_estimation_strategy": "display_node_count_and_formula_aware_height",
            "formula_numbered_count": quality_flag_counts.get("gbt_formula_numbered", 0),
            "formula_number_source_preserved_count": quality_flag_counts.get(
                "formula_number_source_preserved", 0
            ),
            "formula_like_block_count": quality_flag_counts.get("formula_like_block", 0),
            "formula_reflow_cluster_count": quality_flag_counts.get("formula_reflow_clustered", 0),
            "formula_reflow_compacted_count": quality_flag_counts.get("formula_like_compacted", 0),
            "unresolved_formula_placeholders": unresolved_formula_placeholders,
            "page_utilization": page_utilization,
            "low_utilization_pages": low_utilization_pages,
            "underfilled_reflow_pages": underfilled_reflow_pages,
            "right_column_start_pages": right_column_start_pages,
            "left_column_underfilled_pages": left_column_underfilled_pages,
            "column_flow_issues": column_flow_issues,
            "single_fragment_pages": single_fragment_pages,
            "suppressed_artifacts": self.layout_trace.get("suppressed_artifacts", []),
            "intent_requirements": intent_requirements
            if isinstance(intent_requirements, list)
            else [],
            "pages": pages,
        }

    @classmethod
    def from_ir_and_plans(
        cls,
        document: DocumentIR,
        plans: list[TranslationLayoutPlan],
        target_lang: str,
        render_defaults: RenderDefaults | None = None,
        layout_intent_plan: LayoutIntentPlan | None = None,
        measured_min_heights: dict[str, float] | None = None,
        measured_preferred_heights: dict[str, float] | None = None,
        forced_full_width_block_ids: set[str] | None = None,
    ) -> RenderDocument:
        defaults = (
            render_defaults.model_copy(update={"target_lang": target_lang}, deep=True)
            if render_defaults is not None
            else RenderDefaults(target_lang=target_lang)
        )
        if defaults.layout_mode == LayoutMode.CONTINUOUS_REFLOW:
            return _from_ir_and_plans_continuous_reflow(
                document,
                plans,
                target_lang,
                defaults,
                layout_intent_plan,
                measured_min_heights or {},
                measured_preferred_heights or {},
                forced_full_width_block_ids or set(),
            )
        min_font_scale = float(defaults.overflow_policy.min_font_scale)
        compact_font_scale = max(min_font_scale, 0.92)
        overflow_policy = defaults.overflow_policy
        translations = {block.source_block_id: block for plan in plans for block in plan.blocks}
        layout_intents = {
            block.source_block_id: block
            for block in (layout_intent_plan.blocks if layout_intent_plan else [])
        }
        asset_usages = {
            asset.asset_id: asset.usage
            for asset in (layout_intent_plan.assets if layout_intent_plan else [])
        }
        pages: list[RenderPage] = []
        formula_counter = 0
        for page in document.pages:
            render_blocks: list[RenderBlock] = []
            continuation_blocks: list[RenderBlock] = []
            for block in page.blocks:
                plan = translations.get(block.block_id)
                layout_intent = layout_intents.get(block.block_id)
                quality_flags: list[str]
                if plan is None:
                    text = block.source_text
                    render_intent = (
                        layout_intent.render_intent if layout_intent is not None else "normal"
                    )
                    quality_flags = ["missing_translation"]
                else:
                    text = (
                        plan.translated_text if plan.translated_text.strip() else block.source_text
                    )
                    render_intent = (
                        layout_intent.render_intent
                        if layout_intent is not None
                        else plan.render_intent
                    )
                    quality_flags = list(plan.quality_flags)
                    if not plan.translated_text.strip():
                        quality_flags.append("empty_translation")
                    if plan.role != block.role:
                        quality_flags.append("role_mismatch")
                if layout_intent is not None:
                    quality_flags.extend(layout_intent.quality_flags)
                style = _style_for_role(defaults, block.role)
                font_scale = 1.0
                if render_intent == "compact":
                    font_scale = compact_font_scale
                font_size_pt = style.font_size_pt * font_scale
                line_height = style.line_height

                render_bbox = block.bbox
                estimated_display_count = _display_formula_count(text, document, block)
                formula_number: str | None = None
                if estimated_display_count >= 1:
                    source_formula_number = _display_formula_source_number(text, document, block)
                    if source_formula_number is not None:
                        document = _document_with_stripped_formula_source_numbers(
                            document,
                            text,
                            block,
                            source_formula_number,
                        )
                        text = _strip_source_equation_number_from_text(text)
                        formula_number = source_formula_number or None
                        formula_counter = _formula_counter_after_preserved_number(
                            formula_counter,
                            formula_number,
                        )
                        quality_flags.append("formula_number_source_preserved")
                    elif defaults.formula_numbering == "parenthesized":
                        if estimated_display_count > 1:
                            quality_flags.append("formula_number_skipped_multi_display")
                        else:
                            formula_counter += 1
                            formula_number = f"({formula_counter})"
                            quality_flags.append("gbt_formula_numbered")

                should_expand_before_scaling = _contains_display_formula(
                    text,
                    document,
                    block,
                )

                if _content_overflows(
                    text,
                    render_bbox,
                    font_size_pt,
                    line_height,
                    document,
                    block,
                ):
                    if (
                        should_expand_before_scaling
                        and overflow_policy.allow_box_expansion
                        and overflow_policy.strategy == "scale_then_expand_then_continue"
                    ):
                        expanded_bbox = _expand_bbox_to_fit(
                            text,
                            render_bbox,
                            page.size,
                            font_size_pt,
                            line_height,
                            document,
                            block,
                        )
                        if expanded_bbox is not None:
                            render_bbox = expanded_bbox
                            quality_flags.append("box_expanded")
                    if (
                        _content_overflows(
                            text,
                            render_bbox,
                            font_size_pt,
                            line_height,
                            document,
                            block,
                        )
                        and font_scale > min_font_scale
                        and overflow_policy.strategy != "continue_without_scaling"
                    ):
                        font_scale = min_font_scale
                        font_size_pt = style.font_size_pt * font_scale
                        quality_flags.append("font_scaled")
                    if _content_overflows(
                        text,
                        render_bbox,
                        font_size_pt,
                        line_height,
                        document,
                        block,
                    ):
                        expanded_bbox = (
                            _expand_bbox_to_fit(
                                text,
                                render_bbox,
                                page.size,
                                font_size_pt,
                                line_height,
                                document,
                                block,
                            )
                            if overflow_policy.allow_box_expansion
                            and overflow_policy.strategy == "scale_then_expand_then_continue"
                            else None
                        )
                        if expanded_bbox is not None:
                            render_bbox = expanded_bbox
                            quality_flags.append("box_expanded")
                    if _content_overflows(
                        text,
                        render_bbox,
                        font_size_pt,
                        line_height,
                        document,
                        block,
                    ):
                        if overflow_policy.allow_continuation_page:
                            visible_text, overflow_text = _split_text_to_fit(
                                text,
                                render_bbox,
                                font_size_pt,
                                line_height,
                            )
                            if overflow_text:
                                text = visible_text
                                quality_flags.append("continued_on_next_page")
                                continuation_blocks.extend(
                                    _make_continuation_blocks(
                                        block,
                                        overflow_text,
                                        page.size,
                                        document,
                                        font_size_pt,
                                        font_scale,
                                        render_intent,
                                        style.alignment,
                                        700 if style.bold else 400,
                                        "italic" if style.italic else "normal",
                                        line_height,
                                    )
                                )
                                html = None
                            else:
                                quality_flags.append("overflow_clipped")
                        else:
                            quality_flags.append("overflow_clipped")

                html, formula_flags = _formula_html_for_text(text, document, block)
                quality_flags.extend(formula_flags)

                render_blocks.append(
                    RenderBlock(
                        block_id=block.block_id,
                        role=block.role,
                        bbox=render_bbox,
                        text=text,
                        style_seed=block.style_seed,
                        font_size_pt=font_size_pt,
                        font_scale=font_scale,
                        html=html,
                        render_intent=render_intent,
                        text_align=style.alignment,
                        font_weight=700 if style.bold else 400,
                        font_style="italic" if style.italic else "normal",
                        first_line_indent_em=style.first_line_indent_em,
                        line_height=line_height,
                        font_stack=style.font_stack,
                        formula_number=formula_number,
                        source_block_id=block.block_id,
                        layout_signature=_layout_signature(block.block_id, text),
                        quality_flags=_unique_flags(quality_flags),
                    )
                )
            pages.append(
                RenderPage(
                    page_id=page.page_id,
                    size=page.size,
                    blocks=render_blocks,
                    assets=_render_assets(page.assets, asset_usages),
                )
            )
            for index, continuation_block in enumerate(continuation_blocks, start=1):
                pages.append(
                    RenderPage(
                        page_id=f"{page.page_id}_cont_{index:02d}",
                        size=page.size,
                        blocks=[continuation_block],
                    )
                )
        return cls(
            doc_id=document.doc_id,
            target_lang=target_lang,
            pages=pages,
            font_stack=defaults.font_stack,
            line_height=defaults.line_height,
            paragraph_spacing_em=defaults.paragraph_spacing_em,
            layout_mode=_enum_value(defaults.layout_mode),
            layout_trace=_build_source_bbox_trace(
                document,
                pages,
                defaults,
                layout_intent_plan,
            ),
        )


def _render_assets(
    assets: list[Asset],
    asset_usages: dict[str, str] | None = None,
) -> list[RenderAsset]:
    render_assets: list[RenderAsset] = []
    for asset in assets:
        usage = (asset_usages or {}).get(asset.asset_id, "preserve")
        if usage == "ignore":
            continue
        quality_flags: list[str] = []
        if usage != "preserve":
            quality_flags.append(f"asset_usage_{usage}")
        if not asset.path:
            quality_flags.append("asset_missing_path")
        render_assets.append(
            RenderAsset(
                asset_id=asset.asset_id,
                kind=asset.kind,
                bbox=asset.bbox,
                path=asset.path,
                alt_text=asset.alt_text,
                quality_flags=quality_flags,
            )
        )
    return render_assets


def _from_ir_and_plans_continuous_reflow(
    document: DocumentIR,
    plans: list[TranslationLayoutPlan],
    target_lang: str,
    defaults: RenderDefaults,
    layout_intent_plan: LayoutIntentPlan | None,
    measured_min_heights: dict[str, float],
    measured_preferred_heights: dict[str, float],
    forced_full_width_block_ids: set[str],
) -> RenderDocument:
    translations = {block.source_block_id: block for plan in plans for block in plan.blocks}
    layout_intents = {
        block.source_block_id: block
        for block in (layout_intent_plan.blocks if layout_intent_plan else [])
    }
    asset_usages = {
        asset.asset_id: asset.usage
        for asset in (layout_intent_plan.assets if layout_intent_plan else [])
    }
    page_layout = defaults.page_layout
    page_size = PageSize(width=page_layout.width_pt, height=page_layout.height_pt)
    content_x0 = page_layout.margin_left_pt
    content_y0 = page_layout.margin_top_pt
    content_width = page_layout.width_pt - page_layout.margin_left_pt - page_layout.margin_right_pt
    content_bottom = page_layout.height_pt - page_layout.margin_bottom_pt
    column_count = int(defaults.column_layout.column_count)
    if column_count != 2:
        column_count = 1
    column_gap_pt = (
        min(
            max(0.0, float(defaults.column_layout.column_gap_pt)),
            max(0.0, content_width - 2.0),
        )
        if column_count == 2
        else 0.0
    )
    column_width = (content_width - column_gap_pt) / 2.0 if column_count == 2 else content_width

    pages: list[RenderPage] = []
    current_blocks: list[RenderBlock] = []
    current_assets: list[RenderAsset] = []
    cursor_y = content_y0
    column_cursors = [content_y0 for _ in range(column_count)]
    active_column_index = 0
    page_index = 1
    formula_counter = 0
    block_traces: list[dict[str, Any]] = []
    asset_traces: list[dict[str, Any]] = []
    suppressed_artifacts: list[dict[str, Any]] = []
    source_page_ids: set[str] = set()
    rendered_source_ids: set[str] = set()

    def column_x0(index: int) -> float:
        return content_x0 + index * (column_width + column_gap_pt)

    def save_column_cursor() -> None:
        if column_count == 2:
            column_cursors[active_column_index] = cursor_y

    def flow_bottom_y() -> float:
        return max(column_cursors) if column_count == 2 else cursor_y

    def prepare_full_width_cursor() -> None:
        nonlocal cursor_y
        if column_count == 2:
            save_column_cursor()
            cursor_y = flow_bottom_y()

    def reset_column_flow(start_y: float) -> None:
        nonlocal active_column_index, column_cursors, cursor_y
        column_cursors = [start_y for _ in range(column_count)]
        active_column_index = 0
        cursor_y = start_y

    def prepare_column_cursor() -> tuple[float, float, int | None]:
        nonlocal active_column_index, cursor_y
        if column_count == 1:
            return content_x0, content_width, None
        cursor_y = column_cursors[active_column_index]
        return column_x0(active_column_index), column_width, active_column_index

    def switch_to_next_column() -> bool:
        nonlocal active_column_index, cursor_y
        if column_count != 2 or active_column_index >= column_count - 1:
            return False
        column_cursors[active_column_index] = cursor_y
        active_column_index += 1
        cursor_y = column_cursors[active_column_index]
        return True

    def finish_page() -> None:
        nonlocal current_blocks, current_assets, cursor_y, page_index
        pages.append(
            RenderPage(
                page_id=f"r{page_index:04d}",
                size=page_size,
                blocks=current_blocks,
                assets=current_assets,
                footer_text=str(page_index),
            )
        )
        page_index += 1
        current_blocks = []
        current_assets = []
        reset_column_flow(content_y0)

    ordered_pages = sorted(document.pages, key=lambda item: item.page_id)
    source_page_sizes = {page.page_id: page.size for page in ordered_pages}
    figure_group_traces: list[dict[str, Any]] = []

    def measured_height_for_signature(
        signature: str,
        estimated_height: float,
        style: RoleStyleDefaults,
    ) -> float:
        min_height = max(style.font_size_pt * style.line_height, 1.0)
        preferred = float(measured_preferred_heights.get(signature, 0.0))
        if preferred > 0:
            estimated_height = max(min_height, min(estimated_height, preferred))
        minimum = float(measured_min_heights.get(signature, 0.0))
        if minimum > 0:
            estimated_height = max(estimated_height, minimum)
        return estimated_height

    ordered_items: list[tuple[str, DocumentBlock | Asset | _ReflowFigureGroup]] = []
    for page in ordered_pages:
        source_page_ids.add(page.page_id)
        renderable_assets: list[Asset] = []
        for asset in page.assets:
            usage = asset_usages.get(asset.asset_id, "preserve")
            suppression_reason = _reflow_asset_suppression_reason(asset)
            if usage == "ignore":
                suppressed_artifacts.append(
                    {
                        "kind": "asset_ignored",
                        "asset_id": asset.asset_id,
                        "source_page_id": asset.page_id,
                        "quality_flags": ["asset_suppressed_placeholder"] if not asset.path else [],
                    }
                )
            elif suppression_reason is not None:
                suppressed_artifacts.append(
                    {
                        "kind": "asset_ignored",
                        "asset_id": asset.asset_id,
                        "source_page_id": asset.page_id,
                        "reason": suppression_reason,
                        "quality_flags": _reflow_suppressed_asset_quality_flags(
                            asset,
                            suppression_reason,
                        ),
                    }
                )
            elif not asset.path:
                suppressed_artifacts.append(
                    {
                        "kind": "asset_without_path",
                        "asset_id": asset.asset_id,
                        "source_page_id": asset.page_id,
                    }
                )
            else:
                renderable_assets.append(asset)

        figure_groups = _build_reflow_figure_groups(page.blocks, renderable_assets)
        grouped_asset_ids = {group.asset.asset_id for group in figure_groups}
        grouped_caption_ids = {
            group.caption.block_id for group in figure_groups if group.caption is not None
        }
        page_items: list[
            tuple[tuple[float, float, float, int], str, DocumentBlock | Asset | _ReflowFigureGroup]
        ] = []
        for block in page.blocks:
            if block.block_id in grouped_caption_ids:
                continue
            page_items.append(
                (
                    (
                        float(block.reading_order),
                        block.bbox.y0,
                        block.bbox.x0,
                        0,
                    ),
                    "block",
                    block,
                )
            )
        for group in figure_groups:
            page_items.append((group.order, "figure_group", group))
        for asset in renderable_assets:
            if asset.asset_id in grouped_asset_ids:
                continue
            page_items.append((_reflow_asset_order(page.blocks, asset), "asset", asset))
        ordered_items.extend(
            (kind, item)
            for _, kind, item in sorted(
                page_items,
                key=lambda entry: (entry[0], entry[1]),
            )
        )
    ordered_items = _merge_formula_like_ordered_items(ordered_items, document, translations)

    pending_figure_groups: list[_PendingReflowFigureGroup] = []

    def prepare_figure_group(group: _ReflowFigureGroup) -> _PreparedReflowFigureGroup:
        nonlocal document, formula_counter
        asset = group.asset
        caption = group.caption
        asset_width, asset_height = _reflow_asset_dimensions(asset, page_size, content_width)
        caption_prepared: _PreparedReflowBlock | None = None
        caption_required_height = 0.0
        if caption is not None and not _should_suppress_reflow_block(caption):
            caption_prepared, document, formula_counter = _prepare_reflow_block(
                document=document,
                block=caption,
                plan=translations.get(caption.block_id),
                layout_intent=layout_intents.get(caption.block_id),
                defaults=defaults,
                content_width=content_width,
                formula_counter=formula_counter,
            )
            if caption_prepared.text.strip():
                caption_required_height = _reflow_required_height(
                    caption_prepared.text,
                    content_width,
                    caption_prepared.style,
                    document=document,
                    source_block=caption,
                )
                caption_required_height = measured_height_for_signature(
                    _layout_signature(caption.block_id, caption_prepared.text),
                    caption_required_height,
                    caption_prepared.style,
                )
            else:
                caption_prepared = None

        caption_space = (
            _FIGURE_GROUP_SPACE_BETWEEN_PT
            + (caption_prepared.style.space_before_pt if caption_prepared else 0.0)
            + caption_required_height
            + (caption_prepared.style.space_after_pt if caption_prepared else 0.0)
            if caption_prepared
            else _FIGURE_GROUP_SPACE_AFTER_PT
        )
        fixed_height = _FIGURE_GROUP_SPACE_BEFORE_PT + caption_space
        content_height = content_bottom - content_y0
        normal_required_height = fixed_height + asset_height
        scale = 1.0
        if normal_required_height > content_height + _LAYOUT_EPSILON_PT:
            available_asset_height = max(24.0, content_height - fixed_height)
            target_asset_height = max(
                asset_height * _FIGURE_GROUP_MIN_DEFERRED_SCALE, available_asset_height
            )
            target_asset_height = min(asset_height, target_asset_height)
            if target_asset_height < asset_height - _LAYOUT_EPSILON_PT:
                scale = target_asset_height / asset_height
                asset_width *= scale
                asset_height = target_asset_height
        required_height = fixed_height + asset_height
        return _PreparedReflowFigureGroup(
            group=group,
            caption_prepared=caption_prepared,
            caption_required_height=caption_required_height,
            asset_width=asset_width,
            asset_height=asset_height,
            scale=scale,
            required_height=required_height,
            group_too_tall=required_height > content_height + _LAYOUT_EPSILON_PT,
        )

    def place_figure_group(
        prepared_group: _PreparedReflowFigureGroup,
        *,
        float_placement: str,
        deferred_from_page: str | None = None,
    ) -> None:
        nonlocal cursor_y
        prepare_full_width_cursor()
        group = prepared_group.group
        asset = group.asset
        caption = group.caption
        caption_prepared = prepared_group.caption_prepared
        figure_group_id = f"figure-group-{asset.asset_id}"
        asset_flags = ["reflow_asset", "figure_grouped"]
        if caption_prepared is not None:
            asset_flags.append("figure_caption_grouped")
        if prepared_group.scale < 1.0 - _LAYOUT_EPSILON_PT:
            asset_flags.append("figure_group_scaled")
        if prepared_group.group_too_tall:
            asset_flags.append("figure_group_split")
        asset_output_page_id = f"r{page_index:04d}"
        cursor_y = _append_reflow_asset(
            asset=asset,
            page_size=page_size,
            content_x0=content_x0,
            content_y0=content_y0,
            content_width=content_width,
            content_bottom=content_bottom,
            cursor_y=cursor_y,
            current_assets=current_assets,
            current_page_id=lambda: f"r{page_index:04d}",
            asset_traces=asset_traces,
            quality_flags=asset_flags,
            figure_group_id=figure_group_id,
            caption_block_id=caption.block_id if caption else None,
            space_before=_FIGURE_GROUP_SPACE_BEFORE_PT,
            space_after=_FIGURE_GROUP_SPACE_BETWEEN_PT,
            dimensions=(prepared_group.asset_width, prepared_group.asset_height),
            scale=prepared_group.scale,
            column_count=column_count,
            span="full_width",
        )

        caption_output_page_id: str | None = None
        caption_render_block_id: str | None = None
        if caption_prepared is not None and caption is not None:
            caption_fragments = [caption_prepared.text]
            caption_split = False
            if (
                (current_blocks or current_assets)
                and cursor_y
                + caption_prepared.style.space_before_pt
                + prepared_group.caption_required_height
                > content_bottom
            ):
                if not prepared_group.group_too_tall:
                    finish_page()
                else:
                    min_caption_line_height = (
                        caption_prepared.style.font_size_pt * caption_prepared.style.line_height
                    )
                    first_caption_height = (
                        content_bottom - cursor_y - caption_prepared.style.space_before_pt
                    )
                    if first_caption_height < min_caption_line_height:
                        finish_page()
                        first_caption_height = None
                    caption_fragments = _split_reflow_text(
                        caption_prepared.text,
                        content_width,
                        content_bottom - content_y0,
                        caption_prepared.style,
                        first_max_height_pt=first_caption_height,
                        document=document,
                        source_block=caption,
                    )
                    caption_fragments = caption_fragments or [caption_prepared.text]
                    caption_split = len(caption_fragments) > 1

            if caption_split:
                caption_prepared = replace(
                    caption_prepared,
                    flags=_unique_flags(
                        [
                            *caption_prepared.flags,
                            "figure_group_split",
                            "figure_caption_continued",
                        ]
                    ),
                )

            caption_fragment_count = len(caption_fragments)
            for caption_fragment_index, caption_fragment in enumerate(
                caption_fragments,
                start=1,
            ):
                if caption_fragment_index > 1 and (current_blocks or current_assets):
                    finish_page()
                caption_required_height = _reflow_required_height(
                    caption_fragment,
                    content_width,
                    caption_prepared.style,
                    document=document,
                    source_block=caption,
                )
                caption_required_height = measured_height_for_signature(
                    _layout_signature(
                        caption.block_id,
                        caption_fragment,
                        caption_fragment_index,
                    ),
                    caption_required_height,
                    caption_prepared.style,
                )
                if (
                    (current_blocks or current_assets)
                    and cursor_y + caption_prepared.style.space_before_pt + caption_required_height
                    > content_bottom
                ):
                    finish_page()
                cursor_y += caption_prepared.style.space_before_pt
                fragment_prepared = caption_prepared
                if cursor_y + caption_required_height > content_bottom:
                    fragment_prepared = replace(
                        fragment_prepared,
                        flags=_unique_flags(
                            [
                                *fragment_prepared.flags,
                                "overflow_clipped",
                                "figure_caption_continued",
                            ]
                        ),
                    )
                caption_fragment_html = (
                    caption_prepared.html
                    if caption_fragment_count == 1
                    else _formula_html_for_text(caption_fragment, document, caption)[0]
                )
                caption_block, caption_trace, cursor_y = _render_reflow_fragment(
                    prepared=fragment_prepared,
                    fragment=caption_fragment,
                    fragment_index=caption_fragment_index,
                    fragment_count=caption_fragment_count,
                    fragment_html=caption_fragment_html,
                    document=document,
                    content_width=content_width,
                    content_x0=content_x0,
                    cursor_y=cursor_y,
                    page_index=page_index,
                    figure_group_id=figure_group_id,
                    caption_for_asset_id=asset.asset_id,
                    measured_min_heights=measured_min_heights,
                    measured_preferred_heights=measured_preferred_heights,
                    column_count=column_count,
                    span="full_width",
                )
                current_blocks.append(caption_block)
                rendered_source_ids.add(caption.block_id)
                block_traces.append(caption_trace)
                if caption_output_page_id is None:
                    caption_output_page_id = caption_trace["output_page_id"]
                if caption_render_block_id is None:
                    caption_render_block_id = caption_block.block_id

        figure_group_quality_flags = ["figure_grouped"]
        if caption is None:
            figure_group_quality_flags.append("asset_caption_missing")
        if caption_output_page_id and caption_output_page_id != asset_output_page_id:
            figure_group_quality_flags.extend(["figure_group_separated", "asset_caption_mismatch"])
        if prepared_group.scale < 1.0 - _LAYOUT_EPSILON_PT:
            figure_group_quality_flags.append("figure_group_scaled")
        if prepared_group.group_too_tall:
            figure_group_quality_flags.append("figure_group_split")
        figure_group_traces.append(
            {
                "figure_group_id": figure_group_id,
                "asset_id": asset.asset_id,
                "caption_block_id": caption.block_id if caption else None,
                "caption_render_block_id": caption_render_block_id,
                "source_page_id": asset.page_id,
                "output_page_id": asset_output_page_id,
                "caption_output_page_id": caption_output_page_id,
                "order_index": len(figure_group_traces),
                "required_height_pt": round(prepared_group.required_height, 4),
                "float_placement": float_placement,
                "deferred_from_page": deferred_from_page,
                "scale": round(prepared_group.scale, 4),
                "column_count": column_count,
                "column_index": None,
                "span": "full_width",
                "quality_flags": _unique_flags(figure_group_quality_flags),
            }
        )
        reset_column_flow(cursor_y)

    def place_pending_that_fits_current_page() -> bool:
        placed_any = False
        prepare_full_width_cursor()
        while pending_figure_groups:
            pending = pending_figure_groups[0]
            remaining_height = content_bottom - cursor_y
            if pending.prepared.required_height > remaining_height + _LAYOUT_EPSILON_PT:
                break
            pending_figure_groups.pop(0)
            place_figure_group(
                pending.prepared,
                float_placement="page_bottom" if current_blocks else "page_top",
                deferred_from_page=pending.deferred_from_page,
            )
            placed_any = True
        return placed_any

    def place_pending_at_page_top() -> None:
        while pending_figure_groups and not current_blocks and not current_assets:
            pending = pending_figure_groups.pop(0)
            place_figure_group(
                pending.prepared,
                float_placement="page_top",
                deferred_from_page=pending.deferred_from_page,
            )
            if current_blocks or current_assets:
                break

    def finish_page_with_pending() -> None:
        place_pending_that_fits_current_page()
        if current_blocks or current_assets:
            finish_page()
        place_pending_at_page_top()

    for kind, item in ordered_items:
        if pending_figure_groups and not current_blocks and not current_assets:
            place_pending_at_page_top()
        if kind == "figure_group":
            group = item
            if not isinstance(group, _ReflowFigureGroup):
                continue
            prepared_group = prepare_figure_group(group)
            prepare_full_width_cursor()
            if (
                current_blocks or current_assets
            ) and cursor_y + prepared_group.required_height > content_bottom:
                pending_figure_groups.append(
                    _PendingReflowFigureGroup(
                        prepared=prepared_group,
                        deferred_from_page=f"r{page_index:04d}",
                    )
                )
                continue
            place_figure_group(prepared_group, float_placement="inline")
            continue

        if kind == "asset":
            asset = item
            if not isinstance(asset, Asset):
                continue
            prepare_full_width_cursor()
            _, asset_height = _reflow_asset_dimensions(asset, page_size, content_width)
            if (
                current_blocks or current_assets
            ) and cursor_y + 8.0 + asset_height > content_bottom:
                finish_page_with_pending()
            cursor_y = _append_reflow_asset(
                asset=asset,
                page_size=page_size,
                content_x0=content_x0,
                content_y0=content_y0,
                content_width=content_width,
                content_bottom=content_bottom,
                cursor_y=cursor_y,
                current_assets=current_assets,
                current_page_id=lambda: f"r{page_index:04d}",
                asset_traces=asset_traces,
                column_count=column_count,
                span="full_width",
            )
            reset_column_flow(cursor_y)
            continue

        block = item
        if not isinstance(block, DocumentBlock):
            continue
        source_page_ids.add(block.page_id)
        if _should_suppress_reflow_block(block):
            suppressed_artifacts.append(
                {
                    "kind": "source_block_suppressed",
                    "source_block_id": block.block_id,
                    "source_page_id": block.page_id,
                    "reason": "running_header_footer_or_pdf_artifact",
                }
            )
            continue

        span = _reflow_span_for_block(
            block,
            defaults,
            source_page_size=source_page_sizes.get(block.page_id),
            force_full_width=block.block_id in forced_full_width_block_ids,
        )
        if span == "full_width":
            prepare_full_width_cursor()
            active_content_x0 = content_x0
            active_content_width = content_width
            active_trace_column_index: int | None = None
        else:
            active_content_x0, active_content_width, active_trace_column_index = (
                prepare_column_cursor()
            )
        plan = translations.get(block.block_id)
        layout_intent = layout_intents.get(block.block_id)
        prepared, document, formula_counter = _prepare_reflow_block(
            document=document,
            block=block,
            plan=plan,
            layout_intent=layout_intent,
            defaults=defaults,
            content_width=active_content_width,
            formula_counter=formula_counter,
        )
        if block.role == BlockRole.FORMULA and block.block_id in forced_full_width_block_ids:
            prepared = replace(
                prepared,
                flags=_unique_flags([*prepared.flags, "formula_promoted_full_width"]),
            )
        if not prepared.text.strip():
            suppressed_artifacts.append(
                {
                    "kind": "empty_text_block_suppressed",
                    "source_block_id": block.block_id,
                    "source_page_id": block.page_id,
                }
            )
            continue

        if block.role == BlockRole.REFERENCE and current_blocks:
            finish_page_with_pending()
            if span == "column":
                active_content_x0, active_content_width, active_trace_column_index = (
                    prepare_column_cursor()
                )
            else:
                prepare_full_width_cursor()

        full_required_height = _reflow_required_height(
            prepared.text,
            active_content_width,
            prepared.style,
            document=document,
            source_block=block,
        )
        keep_with_next = block.role in {BlockRole.TITLE, BlockRole.HEADING}
        if (
            keep_with_next
            and current_blocks
            and cursor_y + full_required_height + 36 > content_bottom
        ):
            finish_page_with_pending()
            if span == "column":
                active_content_x0, active_content_width, active_trace_column_index = (
                    prepare_column_cursor()
                )
            else:
                prepare_full_width_cursor()

        first_fragment_height = content_bottom - cursor_y - prepared.style.space_before_pt
        if (
            current_blocks
            and first_fragment_height
            < prepared.style.font_size_pt * prepared.style.line_height * _MIN_FINAL_FRAGMENT_LINES
        ):
            if span == "column" and switch_to_next_column():
                active_content_x0, active_content_width, active_trace_column_index = (
                    prepare_column_cursor()
                )
            else:
                finish_page_with_pending()
                if span == "column":
                    active_content_x0, active_content_width, active_trace_column_index = (
                        prepare_column_cursor()
                    )
                else:
                    prepare_full_width_cursor()
            first_fragment_height = content_bottom - cursor_y - prepared.style.space_before_pt
        if prepared.html is not None and not _should_split_inline_markup_reflow_text(
            prepared.text, block
        ):
            fragments = [prepared.text]
        elif _should_safe_split_formula_reflow_text(prepared.text, block):
            fragments = _split_formula_reflow_text(
                prepared.text,
                active_content_width,
                content_bottom - content_y0,
                prepared.style,
                first_max_height_pt=first_fragment_height if current_blocks else None,
                document=document,
                source_block=block,
            )
        else:
            fragments = _split_reflow_text(
                prepared.text,
                active_content_width,
                content_bottom - content_y0,
                prepared.style,
                first_max_height_pt=first_fragment_height if current_blocks else None,
                document=document,
                source_block=block,
            )
        fragment_index = 0
        while fragment_index < len(fragments):
            fragment = fragments[fragment_index]
            fragment_number = fragment_index + 1
            fragment_count = len(fragments)
            fragment_html = (
                prepared.html
                if fragment_count == 1
                else _formula_html_for_text(fragment, document, block)[0]
            )
            fragment_required_height = _reflow_required_height(
                fragment,
                active_content_width,
                prepared.style,
                document=document,
                source_block=block,
            )
            fragment_required_height = measured_height_for_signature(
                _layout_signature(block.block_id, fragment, fragment_number),
                fragment_required_height,
                prepared.style,
            )
            available_before_space = max(
                0.0,
                content_bottom - cursor_y - prepared.style.space_before_pt,
            )
            min_split_height = (
                prepared.style.font_size_pt * prepared.style.line_height * _MIN_FINAL_FRAGMENT_LINES
            )
            if (
                measured_min_heights
                and _should_split_inline_markup_reflow_text(fragment, block)
                and fragment_required_height > available_before_space + _LAYOUT_EPSILON_PT
                and available_before_space >= min_split_height
            ):
                if _should_safe_split_formula_reflow_text(fragment, block):
                    split_fragments = _split_formula_reflow_text_for_measured_height(
                        fragment,
                        active_content_width,
                        content_bottom - content_y0,
                        available_before_space,
                        fragment_required_height,
                        prepared.style,
                        document=document,
                        source_block=block,
                    )
                else:
                    split_fragments = _split_reflow_text_for_measured_height(
                        fragment,
                        active_content_width,
                        content_bottom - content_y0,
                        available_before_space,
                        fragment_required_height,
                        prepared.style,
                        document=document,
                        source_block=block,
                    )
                if (
                    len(split_fragments) > 1
                    and split_fragments[0].strip()
                    and split_fragments[0].strip() != fragment.strip()
                ):
                    fragments[fragment_index : fragment_index + 1] = split_fragments
                    fragment = fragments[fragment_index]
                    fragment_number = fragment_index + 1
                    fragment_count = len(fragments)
                    fragment_html = _formula_html_for_text(fragment, document, block)[0]
                    fragment_required_height = _reflow_required_height(
                        fragment,
                        active_content_width,
                        prepared.style,
                        document=document,
                        source_block=block,
                    )
                    fragment_required_height = measured_height_for_signature(
                        _layout_signature(
                            block.block_id,
                            fragment,
                            fragment_number,
                        ),
                        fragment_required_height,
                        prepared.style,
                    )
            if (
                (current_blocks or current_assets)
                and cursor_y + prepared.style.space_before_pt + fragment_required_height
                > content_bottom
            ):
                if span == "column" and switch_to_next_column():
                    active_content_x0, active_content_width, active_trace_column_index = (
                        prepare_column_cursor()
                    )
                else:
                    finish_page_with_pending()
                    if span == "column":
                        active_content_x0, active_content_width, active_trace_column_index = (
                            prepare_column_cursor()
                        )
                    else:
                        prepare_full_width_cursor()

            cursor_y += prepared.style.space_before_pt
            if cursor_y + fragment_required_height > content_bottom:
                if current_blocks or current_assets:
                    if span == "column" and switch_to_next_column():
                        active_content_x0, active_content_width, active_trace_column_index = (
                            prepare_column_cursor()
                        )
                    else:
                        finish_page_with_pending()
                        if span == "column":
                            active_content_x0, active_content_width, active_trace_column_index = (
                                prepare_column_cursor()
                            )
                        else:
                            prepare_full_width_cursor()
                    cursor_y += prepared.style.space_before_pt
                if cursor_y + fragment_required_height > content_bottom:
                    prepared = replace(
                        prepared,
                        flags=_unique_flags([*prepared.flags, "overflow_clipped"]),
                    )
            render_block, trace, cursor_y = _render_reflow_fragment(
                prepared=prepared,
                fragment=fragment,
                fragment_index=fragment_number,
                fragment_count=fragment_count,
                fragment_html=fragment_html,
                document=document,
                content_width=active_content_width,
                content_x0=active_content_x0,
                cursor_y=cursor_y,
                page_index=page_index,
                measured_min_heights=measured_min_heights,
                measured_preferred_heights=measured_preferred_heights,
                column_count=column_count,
                column_index=active_trace_column_index,
                span=span,
            )
            current_blocks.append(render_block)
            rendered_source_ids.add(block.block_id)
            block_traces.append(trace)
            if span == "column":
                save_column_cursor()
            else:
                reset_column_flow(cursor_y)
            fragment_index += 1

    while pending_figure_groups:
        if current_blocks or current_assets:
            finish_page_with_pending()
        else:
            place_pending_at_page_top()
            if current_blocks or current_assets:
                finish_page()

    if current_blocks or current_assets or not pages:
        finish_page()

    trace = _build_reflow_trace(
        document=document,
        pages=pages,
        defaults=defaults,
        source_page_ids=source_page_ids,
        rendered_source_ids=rendered_source_ids,
        block_traces=block_traces,
        asset_traces=asset_traces,
        figure_group_traces=figure_group_traces,
        suppressed_artifacts=suppressed_artifacts,
        layout_intent_plan=layout_intent_plan,
    )
    return RenderDocument(
        doc_id=document.doc_id,
        target_lang=target_lang,
        pages=pages,
        font_stack=defaults.font_stack,
        line_height=defaults.line_height,
        paragraph_spacing_em=defaults.paragraph_spacing_em,
        layout_mode=_enum_value(defaults.layout_mode),
        layout_trace=trace,
    )


def _translated_text_for_block(
    block: DocumentBlock,
    plan: Any,
    layout_intent: Any,
) -> tuple[str, str, list[str]]:
    merged_translated_text = getattr(block, "_merged_translated_text", None)
    if isinstance(merged_translated_text, str) and merged_translated_text.strip():
        render_intent = layout_intent.render_intent if layout_intent is not None else "normal"
        quality_flags = list(getattr(block, "_merged_quality_flags", []))
        if layout_intent is not None:
            quality_flags.extend(layout_intent.quality_flags)
        return merged_translated_text, render_intent, _unique_flags(quality_flags)
    quality_flags: list[str]
    if plan is None:
        text = block.source_text
        render_intent = layout_intent.render_intent if layout_intent is not None else "normal"
        quality_flags = ["missing_translation"]
    else:
        text = plan.translated_text if plan.translated_text.strip() else block.source_text
        render_intent = (
            layout_intent.render_intent if layout_intent is not None else plan.render_intent
        )
        quality_flags = list(plan.quality_flags)
        if not plan.translated_text.strip():
            quality_flags.append("empty_translation")
        if plan.role != block.role:
            quality_flags.append("role_mismatch")
    return text, render_intent, quality_flags


def _prepare_reflow_block(
    *,
    document: DocumentIR,
    block: DocumentBlock,
    plan: Any,
    layout_intent: Any,
    defaults: RenderDefaults,
    content_width: float,
    formula_counter: int,
) -> tuple[_PreparedReflowBlock, DocumentIR, int]:
    text, render_intent, flags = _translated_text_for_block(block, plan, layout_intent)
    style = _style_for_role(defaults, block.role)
    is_formula_like = _is_formula_like_reflow_block(block, text, plan)
    if is_formula_like:
        style = _formula_like_style(style)
        flags.append("formula_like_block")
    if render_intent == "compact":
        style = style.model_copy(
            update={
                "font_size_pt": max(9.0, style.font_size_pt * 0.9),
                "line_height": min(style.line_height, 1.35),
            }
        )
        flags.append("compact_reflow")
    if layout_intent is not None:
        flags.extend(layout_intent.quality_flags)

    original_height = max(0.0, block.bbox.y1 - block.bbox.y0)
    estimated_display_count = _display_formula_count(text, document, block)
    formula_number: str | None = None
    if estimated_display_count >= 1:
        source_formula_number = _display_formula_source_number(text, document, block)
        if source_formula_number is not None:
            document = _document_with_stripped_formula_source_numbers(
                document,
                text,
                block,
                source_formula_number,
            )
            text = _strip_source_equation_number_from_text(text)
            formula_number = source_formula_number or None
            formula_counter = _formula_counter_after_preserved_number(
                formula_counter,
                formula_number,
            )
            flags.append("formula_number_source_preserved")
        elif defaults.formula_numbering == "parenthesized":
            if estimated_display_count > 1:
                flags.append("formula_number_skipped_multi_display")
            else:
                formula_counter += 1
                formula_number = f"({formula_counter})"
                flags.append("gbt_formula_numbered")

    html, formula_flags = _formula_html_for_text(
        text,
        document,
        block,
    )
    flags.extend(formula_flags)

    estimated_height = _estimated_reflow_height(
        text,
        content_width,
        style,
        document=document,
        source_block=block,
    )
    if estimated_height > original_height + _LAYOUT_EPSILON_PT:
        flags.append("formula_height_adjusted")
    if estimated_display_count > 1:
        flags.append("formula_multi_display_block")
    if estimated_height > max(original_height, style.font_size_pt * style.line_height) * 1.25:
        flags.append("formula_height_risk")

    return (
        _PreparedReflowBlock(
            block=block,
            text=text,
            render_intent=render_intent,
            flags=_unique_flags(flags),
            style=style,
            html=html,
            formula_number=formula_number,
        ),
        document,
        formula_counter,
    )


def _reflow_required_height(
    text: str,
    width_pt: float,
    style: RoleStyleDefaults,
    *,
    document: DocumentIR | None = None,
    source_block: DocumentBlock | None = None,
) -> float:
    base_height = _estimated_reflow_height(
        text,
        width_pt,
        style,
        document=document,
        source_block=source_block,
    )
    return base_height + _reflow_height_safety(text, style, source_block)


def _reflow_height_safety(
    text: str,
    style: RoleStyleDefaults,
    source_block: DocumentBlock | None = None,
) -> float:
    safety = _REFLOW_HEIGHT_SAFETY_PT
    if (
        source_block is not None and _is_formula_like_block_for_estimation(source_block, text)
    ) or _contains_formula_tokens(text):
        safety += style.font_size_pt * _REFLOW_FORMULA_HEIGHT_SAFETY_EM
    return safety


def _render_reflow_fragment(
    *,
    prepared: _PreparedReflowBlock,
    fragment: str,
    fragment_index: int,
    fragment_count: int,
    fragment_html: str | None,
    document: DocumentIR,
    content_width: float,
    content_x0: float,
    cursor_y: float,
    page_index: int,
    column_count: int = 1,
    column_index: int | None = None,
    span: str = "full_width",
    figure_group_id: str | None = None,
    caption_for_asset_id: str | None = None,
    measured_min_heights: dict[str, float] | None = None,
    measured_preferred_heights: dict[str, float] | None = None,
) -> tuple[RenderBlock, dict[str, Any], float]:
    block = prepared.block
    style = prepared.style
    layout_signature = _layout_signature(block.block_id, fragment, fragment_index)
    required_height = _reflow_required_height(
        fragment,
        content_width,
        style,
        document=document,
        source_block=block,
    )
    if measured_min_heights:
        required_height = max(
            required_height,
            float(measured_min_heights.get(layout_signature, 0.0)),
        )
    if measured_preferred_heights:
        preferred_height = float(measured_preferred_heights.get(layout_signature, 0.0))
        if preferred_height > 0:
            min_height = max(style.font_size_pt * style.line_height, 1.0)
            required_height = max(min_height, min(required_height, preferred_height))
            if measured_min_heights:
                required_height = max(
                    required_height,
                    float(measured_min_heights.get(layout_signature, 0.0)),
                )
    bbox = BoundingBox(
        x0=content_x0,
        y0=cursor_y,
        x1=content_x0 + content_width,
        y1=cursor_y + required_height,
    )
    allocated_height = _bbox_height(bbox)
    height_slack = allocated_height - required_height
    block_flags = list(prepared.flags)
    if getattr(block, "_formula_reflow_clustered", False):
        block_flags.extend(["formula_reflow_clustered", "formula_like_compacted"])
    if fragment_count > 1:
        block_flags.append("reflow_split")
        if fragment_index > 1:
            block_flags.append("reflow_continued")
    if height_slack < -_LAYOUT_EPSILON_PT:
        block_flags.append("overflow_clipped")
    render_block_id = (
        block.block_id if fragment_count == 1 else f"{block.block_id}__reflow_{fragment_index:02d}"
    )
    quality_flags = _unique_flags(block_flags)
    render_block = RenderBlock(
        block_id=render_block_id,
        role=block.role,
        bbox=bbox,
        text=fragment,
        style_seed=block.style_seed,
        font_size_pt=style.font_size_pt,
        font_scale=style.font_size_pt / block.style_seed.font_size
        if block.style_seed.font_size
        else 1.0,
        html=fragment_html,
        render_intent=prepared.render_intent,
        text_align=style.alignment,
        font_weight=700 if style.bold else 400,
        font_style="italic" if style.italic else "normal",
        first_line_indent_em=style.first_line_indent_em if fragment_index == 1 else 0.0,
        line_height=style.line_height,
        font_stack=style.font_stack,
        formula_number=prepared.formula_number if fragment_index == 1 else None,
        figure_group_id=figure_group_id,
        caption_for_asset_id=caption_for_asset_id,
        source_block_id=block.block_id,
        layout_signature=layout_signature,
        quality_flags=quality_flags,
    )
    trace = {
        "source_block_id": block.block_id,
        "render_block_id": render_block_id,
        "layout_signature": layout_signature,
        "source_page_id": block.page_id,
        "output_page_id": f"r{page_index:04d}",
        "role": block.role.value,
        "translated_chars": len(fragment),
        "estimated_lines": _estimated_formula_aware_line_count(
            fragment,
            content_width,
            style.font_size_pt,
            style.line_height,
            document=document,
            block=block,
        ),
        "bbox": bbox.model_dump(),
        "allocated_height_pt": round(allocated_height, 4),
        "required_height_pt": round(required_height, 4),
        "height_slack_pt": round(height_slack, 4),
        "fragment_index": fragment_index,
        "fragment_count": fragment_count,
        "column_count": column_count,
        "column_index": column_index,
        "span": span,
        "quality_flags": quality_flags,
    }
    if figure_group_id:
        trace["figure_group_id"] = figure_group_id
    if caption_for_asset_id:
        trace["caption_for_asset_id"] = caption_for_asset_id
    return render_block, trace, bbox.y1 + style.space_after_pt


def _is_formula_like_text(text: str) -> bool:
    stripped = _normalized_text(text)
    if not stripped or len(stripped) > 128:
        return False
    if _FORMULA_REF_PATTERN.fullmatch(stripped):
        return True
    without_refs = _FORMULA_REF_PATTERN.sub(" ", stripped)
    without_refs = re.sub(r"\s+", " ", without_refs).strip()
    if not without_refs:
        return True
    alpha_words = re.findall(r"[A-Za-z]{3,}", without_refs)
    if len(alpha_words) > 3:
        return False
    compact = without_refs.replace(" ", "")
    return (
        bool(compact)
        and all(char.isalnum() or char in "=+-−×·,.:;()[]{}" for char in compact)
        and any(marker in compact for marker in "=+-−×·()[]{}")
    )


def _is_formula_like_reflow_block(
    block: DocumentBlock,
    text: str,
    plan: Any,
) -> bool:
    if block.role == BlockRole.FORMULA:
        return True
    if plan is not None and "formula_like_repaired" in getattr(plan, "quality_flags", []):
        return True
    return _is_formula_like_text(text)


def _is_formula_like_block_for_estimation(block: DocumentBlock, text: str) -> bool:
    return block.role == BlockRole.FORMULA or _is_formula_like_text(text)


def _formula_like_style(style: RoleStyleDefaults) -> RoleStyleDefaults:
    return style.model_copy(
        update={
            "space_before_pt": min(style.space_before_pt, _FORMULA_LIKE_SPACE_BEFORE_PT),
            "space_after_pt": min(style.space_after_pt, _FORMULA_LIKE_SPACE_AFTER_PT),
            "first_line_indent_em": 0.0,
        }
    )


def _merge_formula_like_ordered_items(
    ordered_items: list[tuple[str, DocumentBlock | Asset | _ReflowFigureGroup]],
    document: DocumentIR,
    translations: dict[str, Any],
) -> list[tuple[str, DocumentBlock | Asset | _ReflowFigureGroup]]:
    merged: list[tuple[str, DocumentBlock | Asset | _ReflowFigureGroup]] = []
    index = 0
    while index < len(ordered_items):
        kind, item = ordered_items[index]
        if kind != "block" or not isinstance(item, DocumentBlock):
            merged.append((kind, item))
            index += 1
            continue
        plan = translations.get(item.block_id)
        seed_text = (
            plan.translated_text
            if plan is not None and isinstance(getattr(plan, "translated_text", None), str)
            else (item.text_for_translation or item.source_text)
        )
        if not _is_formula_like_reflow_block(item, seed_text, plan):
            merged.append((kind, item))
            index += 1
            continue
        cluster = [item]
        cluster_plans = [plan]
        next_index = index + 1
        while next_index < len(ordered_items):
            next_kind, next_item = ordered_items[next_index]
            if next_kind != "block" or not isinstance(next_item, DocumentBlock):
                break
            next_plan = translations.get(next_item.block_id)
            next_text = (
                next_plan.translated_text
                if next_plan is not None
                and isinstance(getattr(next_plan, "translated_text", None), str)
                else (next_item.text_for_translation or next_item.source_text)
            )
            if not _is_formula_like_reflow_block(next_item, next_text, next_plan):
                break
            gap = next_item.bbox.y0 - cluster[-1].bbox.y1
            if gap > _FORMULA_REFLOW_CLUSTER_MAX_VERTICAL_GAP_PT:
                break
            cluster.append(next_item)
            cluster_plans.append(next_plan)
            next_index += 1
        if len(cluster) == 1:
            merged.append((kind, item))
            index += 1
            continue
        primary = cluster[0]
        merged_text = " ".join(
            (
                translations.get(block.block_id).translated_text
                if translations.get(block.block_id)
                else (block.text_for_translation or block.source_text)
            ).strip()
            for block in cluster
        ).strip()
        merged_bbox = BoundingBox(
            x0=min(block.bbox.x0 for block in cluster),
            y0=min(block.bbox.y0 for block in cluster),
            x1=max(block.bbox.x1 for block in cluster),
            y1=max(block.bbox.y1 for block in cluster),
        )
        merged_flags = _unique_flags(
            [
                flag
                for block in cluster
                for flag in (
                    ["formula_reflow_clustered"] + block.formulas[0].quality_flags
                    if block.formulas
                    else ["formula_reflow_clustered"]
                )
            ]
        )
        merged_block = primary.model_copy(
            update={
                "bbox": merged_bbox,
                "text_for_translation": merged_text,
                "source_text": merged_text,
                "formula_id": None,
                "formulas": [formula for block in cluster for formula in block.formulas],
            },
            deep=True,
        )
        merged_block = merged_block.model_copy(
            update={
                "text_for_translation": merged_text,
                "source_text": merged_text,
            },
            deep=True,
        )
        setattr(merged_block, "_merged_translated_text", merged_text)
        setattr(
            merged_block,
            "_merged_quality_flags",
            _unique_flags(
                [
                    *[
                        flag
                        for cluster_plan in cluster_plans
                        if cluster_plan is not None
                        for flag in getattr(cluster_plan, "quality_flags", [])
                    ],
                    "formula_reflow_clustered",
                    "formula_like_compacted",
                ]
            ),
        )
        setattr(merged_block, "_formula_reflow_clustered", True)
        merged.append(("block", merged_block))
        index = next_index
    return merged


def _formula_html_for_text(
    text: str,
    document: DocumentIR,
    block: DocumentBlock,
) -> tuple[str | None, list[str]]:
    if _FORMULA_PLACEHOLDER_PATTERN.search(text):
        return _formula_placeholder_html_for_text(text, block.formulas)
    html, flags = _formula_ir_html_for_text(text, document, role=block.role)
    if html is not None:
        return html, flags
    script_html, script_flags = _non_formula_text_html(text)
    if script_flags:
        return script_html, script_flags
    return None, []


def _formula_placeholder_html_for_text(
    text: str,
    formulas: list[Formula],
) -> tuple[str, list[str]]:
    formulas_by_placeholder = {formula.placeholder: formula for formula in formulas}
    flags: list[str] = []
    parts: list[str] = []
    cursor = 0
    for match in _FORMULA_PLACEHOLDER_PATTERN.finditer(text):
        raw_html, raw_flags = _non_formula_text_html(text[cursor : match.start()])
        parts.append(raw_html)
        flags.extend(raw_flags)
        placeholder = match.group(0)
        formula = formulas_by_placeholder.get(placeholder)
        if formula is None:
            parts.append(
                _unresolved_formula_html(
                    _legacy_formula_id_from_placeholder(placeholder),
                    display=False,
                )
            )
            flags.append("unresolved_formula_placeholder")
        else:
            html, formula_flags = _formula_span(formula)
            parts.append(html)
            flags.append("formula_placeholder_resolved")
            flags.extend(formula_flags)
        cursor = match.end()
    raw_html, raw_flags = _non_formula_text_html(text[cursor:])
    parts.append(raw_html)
    flags.extend(raw_flags)
    return "".join(parts), _unique_flags(flags)


def _formula_span(formula: Formula) -> tuple[str, list[str]]:
    display = "true" if formula.kind == "display" else "false"
    css_kind = "display" if formula.kind == "display" else "inline"
    latex = formula.latex.strip() or formula.source_text.strip()
    if formula.kind == "display":
        latex, _tag_number = _strip_latex_equation_tag(latex)
    flags: list[str] = []
    rendered = None
    if latex and _latex_looks_renderable(latex):
        rendered = _katex_html(latex, display=formula.kind == "display")
    if rendered is None:
        rendered = _formula_plaintext_fallback_html(
            _formula_plaintext_fallback_text(
                formula.source_text,
                latex,
                formula_id=formula.formula_id,
            )
        )
        flags.append("formula_plaintext_fallback")
    html = (
        f'<span class="formula formula-{css_kind}" '
        f'data-formula-id="{escape(formula.formula_id, quote=True)}" '
        f'data-display="{display}" '
        f'data-latex="{escape(latex, quote=True)}">'
        f"{rendered}</span>"
    )
    return html, flags


def _legacy_formula_id_from_placeholder(placeholder: str) -> str:
    prefix = "@@FORMULA_"
    suffix = "@@"
    if placeholder.startswith(prefix) and placeholder.endswith(suffix):
        return placeholder[len(prefix) : -len(suffix)]
    return placeholder


def _style_for_role(defaults: RenderDefaults, role: BlockRole) -> RoleStyleDefaults:
    return getattr(defaults.role_styles, role.value, defaults.role_styles.unknown)


def _asset_reading_order(blocks: list[DocumentBlock], asset: Asset) -> float:
    before = [block.reading_order for block in blocks if block.bbox.y0 <= asset.bbox.y0]
    if before:
        return max(before) + 0.5
    return -0.5


def _reflow_asset_order(
    blocks: list[DocumentBlock],
    asset: Asset,
) -> tuple[float, float, float, int]:
    nearby_blocks = [
        block
        for block in blocks
        if _bbox_vertical_distance(block.bbox, asset.bbox) <= _FIGURE_CAPTION_MAX_DISTANCE_PT
    ]
    column = min((block.column for block in nearby_blocks), default=0)
    return (
        _asset_reading_order(blocks, asset),
        asset.bbox.y0,
        asset.bbox.x0,
        column,
    )


def _bbox_vertical_distance(a: BoundingBox, b: BoundingBox) -> float:
    if a.y1 < b.y0:
        return b.y0 - a.y1
    if b.y1 < a.y0:
        return a.y0 - b.y1
    return 0.0


def _asset_is_captionable(asset: Asset) -> bool:
    return asset.kind in {"image", "figure", "table"}


_STRICT_FIGURE_CAPTION_PATTERN = re.compile(
    r"^\s*(?:"
    r"(?:fig\.|figure|table)\s*[A-Z]?\d+(?:[.\-][A-Za-z0-9]+)*\s*[.:：)]|"
    r"(?:图|表)\s*[A-Z]?\d+(?:[.\-][A-Za-z0-9]+)*\s*[.:：、)]"
    r")",
    re.IGNORECASE,
)


def _block_is_figure_caption(block: DocumentBlock) -> bool:
    text = (block.source_text or block.text_for_translation or "").strip().lower()
    if not _STRICT_FIGURE_CAPTION_PATTERN.match(text):
        return False
    return block.role in {BlockRole.CAPTION, BlockRole.FIGURE} or bool(text)


def _caption_distance_score(asset: Asset, caption: DocumentBlock) -> tuple[float, float, int]:
    vertical_distance = _bbox_vertical_distance(asset.bbox, caption.bbox)
    below_penalty = 0.0 if caption.bbox.y0 >= asset.bbox.y0 else 30.0
    horizontal_distance = abs(
        ((asset.bbox.x0 + asset.bbox.x1) / 2) - ((caption.bbox.x0 + caption.bbox.x1) / 2)
    )
    return (
        vertical_distance + below_penalty + horizontal_distance * 0.05,
        vertical_distance,
        caption.reading_order,
    )


def _build_reflow_figure_groups(
    blocks: list[DocumentBlock],
    assets: list[Asset],
) -> list[_ReflowFigureGroup]:
    caption_candidates = [block for block in blocks if _block_is_figure_caption(block)]
    used_caption_ids: set[str] = set()
    groups: list[_ReflowFigureGroup] = []
    for asset in sorted(
        [asset for asset in assets if _asset_is_captionable(asset)],
        key=lambda item: (item.bbox.y0, item.bbox.x0, item.asset_id),
    ):
        candidates = [
            caption
            for caption in caption_candidates
            if caption.block_id not in used_caption_ids
            and _bbox_vertical_distance(asset.bbox, caption.bbox) <= _FIGURE_CAPTION_MAX_DISTANCE_PT
        ]
        caption = (
            min(candidates, key=lambda item: _caption_distance_score(asset, item))
            if candidates
            else None
        )
        if caption is None:
            continue
        used_caption_ids.add(caption.block_id)
        asset_order = _reflow_asset_order(blocks, asset)
        order = (
            min(asset_order[0], float(caption.reading_order) - 0.25),
            min(asset.bbox.y0, caption.bbox.y0),
            min(asset.bbox.x0, caption.bbox.x0),
            min(asset_order[3], caption.column),
        )
        groups.append(_ReflowFigureGroup(asset=asset, caption=caption, order=order))
    return groups


def _reflow_asset_suppression_reason(asset: Asset) -> str | None:
    if asset.kind == "formula":
        return "formula_rendered_from_text"
    if asset.kind == "figure" and not asset.path:
        return "vector_asset_not_rasterized"
    return None


def _reflow_suppressed_asset_quality_flags(asset: Asset, reason: str) -> list[str]:
    if asset.kind == "formula":
        return ["formula_asset_suppressed"]
    if reason == "vector_asset_not_rasterized":
        return ["vector_asset_not_rasterized"]
    return ["asset_suppressed_placeholder"]


def _append_reflow_asset(
    *,
    asset: Asset,
    page_size: PageSize,
    content_x0: float,
    content_y0: float,
    content_width: float,
    content_bottom: float,
    cursor_y: float,
    current_assets: list[RenderAsset],
    current_page_id: Any,
    asset_traces: list[dict[str, Any]],
    quality_flags: list[str] | None = None,
    figure_group_id: str | None = None,
    caption_block_id: str | None = None,
    space_before: float = 8.0,
    space_after: float = 8.0,
    dimensions: tuple[float, float] | None = None,
    scale: float = 1.0,
    column_count: int = 1,
    column_index: int | None = None,
    span: str = "full_width",
) -> float:
    width, height = dimensions or _reflow_asset_dimensions(asset, page_size, content_width)
    flags = _unique_flags(quality_flags or ["reflow_asset"])
    cursor_y += space_before
    x0 = content_x0 + max(0.0, (content_width - width) / 2)
    bbox = BoundingBox(
        x0=x0,
        y0=cursor_y,
        x1=x0 + width,
        y1=cursor_y + height,
    )
    current_assets.append(
        RenderAsset(
            asset_id=asset.asset_id,
            kind=asset.kind,
            bbox=bbox,
            path=asset.path,
            alt_text=asset.alt_text,
            figure_group_id=figure_group_id,
            caption_block_id=caption_block_id,
            quality_flags=flags,
        )
    )
    trace = {
        "asset_id": asset.asset_id,
        "source_page_id": asset.page_id,
        "output_page_id": current_page_id(),
        "kind": asset.kind,
        "bbox": bbox.model_dump(),
        "allocated_height_pt": round(_bbox_height(bbox), 4),
        "required_height_pt": round(height, 4),
        "height_slack_pt": round(_bbox_height(bbox) - height, 4),
        "scale": round(scale, 4),
        "column_count": column_count,
        "column_index": column_index,
        "span": span,
        "quality_flags": flags,
    }
    if figure_group_id:
        trace["figure_group_id"] = figure_group_id
    if caption_block_id:
        trace["caption_block_id"] = caption_block_id
    asset_traces.append(trace)
    return bbox.y1 + space_after


def _reflow_asset_dimensions(
    asset: Asset,
    page_size: PageSize,
    content_width: float,
) -> tuple[float, float]:
    source_width = max(1.0, _bbox_width(asset.bbox))
    source_height = max(1.0, _bbox_height(asset.bbox))
    max_height = max(72.0, page_size.height * 0.42)
    scale = min(1.0, content_width / source_width, max_height / source_height)
    return max(24.0, source_width * scale), max(24.0, source_height * scale)


def _estimated_reflow_height(
    text: str,
    width_pt: float,
    style: RoleStyleDefaults,
    *,
    document: DocumentIR | None = None,
    source_block: DocumentBlock | None = None,
) -> float:
    text_width = max(1.0, width_pt - style.first_line_indent_em * style.font_size_pt)
    return max(
        style.font_size_pt * style.line_height,
        _estimated_formula_aware_height(
            text,
            text_width,
            style.font_size_pt,
            style.line_height,
            document=document,
            block=source_block,
        ),
    )


def _split_reflow_text(
    text: str,
    width_pt: float,
    max_height_pt: float,
    style: RoleStyleDefaults,
    *,
    first_max_height_pt: float | None = None,
    document: DocumentIR | None = None,
    source_block: DocumentBlock | None = None,
) -> list[str]:
    text = _normalized_text(text)
    if not text:
        return []
    initial_height_pt = first_max_height_pt if first_max_height_pt else max_height_pt
    max_lines = max(1, int(initial_height_pt / (style.font_size_pt * style.line_height)))
    max_chars = max(1, _estimated_chars_for_lines(width_pt, style.font_size_pt, max_lines))
    if len(text) <= max_chars:
        return [text]

    fragments: list[str] = []
    remaining = text
    while remaining:
        height_for_fragment = (
            first_max_height_pt if not fragments and first_max_height_pt else max_height_pt
        )
        lines_for_fragment = max(
            1,
            int(height_for_fragment / (style.font_size_pt * style.line_height)),
        )
        chars_for_fragment = max(
            1,
            _estimated_chars_for_lines(width_pt, style.font_size_pt, lines_for_fragment),
        )
        chars_for_fragment = min(
            chars_for_fragment,
            _max_reflow_chars_to_fit(
                remaining,
                width_pt,
                style,
                height_for_fragment,
                document=document,
                source_block=source_block,
            ),
        )
        if len(remaining) <= chars_for_fragment:
            fragments.append(remaining)
            break
        split_index = _best_reflow_split_index(remaining, chars_for_fragment)
        fragment = remaining[:split_index].strip()
        rest = remaining[split_index:].strip()
        if rest and (
            len(rest) < _MIN_FINAL_FRAGMENT_CHARS
            or _estimated_line_count(rest, width_pt, style.font_size_pt) < _MIN_FINAL_FRAGMENT_LINES
        ):
            rebalance_at = _best_reflow_split_index(fragment, max(1, int(len(fragment) * 0.75)))
            rest = f"{fragment[rebalance_at:].strip()} {rest}".strip()
            fragment = fragment[:rebalance_at].strip()
        if not fragment:
            fragment = remaining[:chars_for_fragment].strip()
            rest = remaining[chars_for_fragment:].strip()
        fragments.append(fragment)
        remaining = rest
    return [fragment for fragment in fragments if fragment]


def _contains_formula_tokens(text: str) -> bool:
    return bool(_FORMULA_REF_PATTERN.search(text) or _FORMULA_PLACEHOLDER_PATTERN.search(text))


def _contains_text_script_markers(text: str) -> bool:
    return bool(_TEXT_SCRIPT_MARKER_PATTERN.search(text))


def _should_split_inline_markup_reflow_text(
    text: str,
    block: DocumentBlock,
) -> bool:
    return block.role != BlockRole.FORMULA and (
        _contains_formula_tokens(text) or _contains_text_script_markers(text)
    )


def _should_safe_split_formula_reflow_text(
    text: str,
    block: DocumentBlock,
) -> bool:
    return block.role != BlockRole.FORMULA and _contains_formula_tokens(text)


def _best_formula_reflow_split_index(text: str, max_chars: int) -> int:
    max_chars = min(max_chars, len(text))
    if max_chars <= 0:
        return 1

    cursor = 0
    best = 0
    for match in _FORMULA_REF_PATTERN.finditer(text):
        if match.start() > cursor:
            segment = text[cursor : match.start()]
            if match.start() > max_chars:
                available = max_chars - cursor
                if available <= 0:
                    return max(1, best)
                local_split = _best_reflow_split_index(segment, available)
                if segment[:local_split].strip():
                    return cursor + local_split
                return max(1, best)
            best = match.start()
        if match.end() > max_chars:
            return max(1, best) if best > 0 else match.end()
        best = match.end()
        cursor = match.end()

    legacy_cursor = 0
    legacy_best = 0
    legacy_text = text
    for match in _FORMULA_PLACEHOLDER_PATTERN.finditer(legacy_text):
        if match.start() > legacy_cursor:
            segment = legacy_text[legacy_cursor : match.start()]
            if match.start() > max_chars:
                available = max_chars - legacy_cursor
                if available <= 0:
                    return max(1, legacy_best)
                local_split = _best_reflow_split_index(segment, available)
                if segment[:local_split].strip():
                    return legacy_cursor + local_split
                return max(1, legacy_best)
            legacy_best = match.start()
        if match.end() > max_chars:
            return max(1, legacy_best) if legacy_best > 0 else match.end()
        legacy_best = match.end()
        legacy_cursor = match.end()

    split_pattern = re.compile(
        f"{_FORMULA_REF_PATTERN.pattern}|{_FORMULA_PLACEHOLDER_PATTERN.pattern}"
    )
    cursor = 0
    best = 0
    for match in split_pattern.finditer(text):
        if match.start() > cursor:
            segment = text[cursor : match.start()]
            if match.start() > max_chars:
                available = max_chars - cursor
                if available <= 0:
                    return max(1, best)
                local_split = _best_reflow_split_index(segment, available)
                if segment[:local_split].strip():
                    return cursor + local_split
                return max(1, best)
            best = match.start()
        if match.end() > max_chars:
            return max(1, best) if best > 0 else match.end()
        best = match.end()
        cursor = match.end()

    tail = text[cursor:]
    if tail:
        available = max_chars - cursor
        if len(text) <= max_chars:
            return len(text)
        if available <= 0:
            return max(1, best)
        local_split = _best_reflow_split_index(tail, available)
        if tail[:local_split].strip():
            return cursor + local_split
    return max(1, min(max_chars, len(text)))


def _split_formula_reflow_text(
    text: str,
    width_pt: float,
    max_height_pt: float,
    style: RoleStyleDefaults,
    *,
    first_max_height_pt: float | None = None,
    document: DocumentIR | None = None,
    source_block: DocumentBlock | None = None,
) -> list[str]:
    text = _normalized_text(text)
    if not text:
        return []
    fragments: list[str] = []
    remaining = text
    while remaining:
        height_for_fragment = (
            first_max_height_pt if not fragments and first_max_height_pt else max_height_pt
        )
        lines_for_fragment = max(
            1,
            int(height_for_fragment / (style.font_size_pt * style.line_height)),
        )
        chars_for_fragment = max(
            1,
            _estimated_chars_for_lines(width_pt, style.font_size_pt, lines_for_fragment),
        )
        chars_for_fragment = min(
            chars_for_fragment,
            _max_reflow_chars_to_fit(
                remaining,
                width_pt,
                style,
                height_for_fragment,
                document=document,
                source_block=source_block,
            ),
        )
        if len(remaining) <= chars_for_fragment:
            fragments.append(remaining)
            break
        split_index = _best_formula_reflow_split_index(remaining, chars_for_fragment)
        fragment = remaining[:split_index].strip()
        rest = remaining[split_index:].strip()
        if rest and (
            len(rest) < _MIN_FINAL_FRAGMENT_CHARS
            or _estimated_line_count(rest, width_pt, style.font_size_pt) < _MIN_FINAL_FRAGMENT_LINES
        ):
            rebalance_at = _best_formula_reflow_split_index(
                fragment,
                max(1, int(len(fragment) * 0.75)),
            )
            if 0 < rebalance_at < len(fragment):
                rest = f"{fragment[rebalance_at:].strip()} {rest}".strip()
                fragment = fragment[:rebalance_at].strip()
        if not fragment:
            fragment = remaining[:chars_for_fragment].strip()
            rest = remaining[chars_for_fragment:].strip()
        fragments.append(fragment)
        remaining = rest
    return [fragment for fragment in fragments if fragment]


def _split_reflow_text_for_measured_height(
    text: str,
    width_pt: float,
    max_height_pt: float,
    available_height_pt: float,
    measured_height_pt: float,
    style: RoleStyleDefaults,
    *,
    document: DocumentIR | None = None,
    source_block: DocumentBlock | None = None,
) -> list[str]:
    fragments = _split_reflow_text(
        text,
        width_pt,
        max_height_pt,
        style,
        first_max_height_pt=available_height_pt,
        document=document,
        source_block=source_block,
    )
    normalized_text = _normalized_text(text)
    if (
        len(fragments) > 1
        and fragments[0].strip()
        and fragments[0].strip() != normalized_text.strip()
    ):
        return fragments
    if measured_height_pt <= available_height_pt + _LAYOUT_EPSILON_PT:
        return fragments
    if len(normalized_text) < 2:
        return fragments

    split_ratio = max(
        0.2,
        min(
            0.9,
            available_height_pt / max(measured_height_pt, _LAYOUT_EPSILON_PT),
        ),
    )
    max_chars = max(1, min(len(normalized_text) - 1, int(len(normalized_text) * split_ratio)))
    split_index = _best_reflow_split_index(normalized_text, max_chars)
    if split_index <= 0 or split_index >= len(normalized_text):
        split_index = _best_reflow_split_index(
            normalized_text,
            max(1, min(len(normalized_text) - 1, max_chars // 2)),
        )
    if split_index <= 0 or split_index >= len(normalized_text):
        return fragments

    first = normalized_text[:split_index].strip()
    rest = normalized_text[split_index:].strip()
    if not first or not rest:
        return fragments
    rest_fragments = _split_reflow_text(
        rest,
        width_pt,
        max_height_pt,
        style,
        document=document,
        source_block=source_block,
    )
    return [first, *rest_fragments]


def _split_formula_reflow_text_for_measured_height(
    text: str,
    width_pt: float,
    max_height_pt: float,
    available_height_pt: float,
    measured_height_pt: float,
    style: RoleStyleDefaults,
    *,
    document: DocumentIR | None = None,
    source_block: DocumentBlock | None = None,
) -> list[str]:
    fragments = _split_formula_reflow_text(
        text,
        width_pt,
        max_height_pt,
        style,
        first_max_height_pt=available_height_pt,
        document=document,
        source_block=source_block,
    )
    normalized_text = _normalized_text(text)
    if (
        len(fragments) > 1
        and fragments[0].strip()
        and fragments[0].strip() != normalized_text.strip()
    ):
        return fragments
    if measured_height_pt <= available_height_pt + _LAYOUT_EPSILON_PT:
        return fragments
    if len(normalized_text) < 2:
        return fragments

    split_ratio = max(
        0.2,
        min(
            0.9,
            available_height_pt / max(measured_height_pt, _LAYOUT_EPSILON_PT),
        ),
    )
    max_chars = max(1, min(len(normalized_text) - 1, int(len(normalized_text) * split_ratio)))
    split_index = _best_formula_reflow_split_index(normalized_text, max_chars)
    if split_index <= 0 or split_index >= len(normalized_text):
        split_index = _best_formula_reflow_split_index(
            normalized_text,
            max(1, min(len(normalized_text) - 1, max_chars // 2)),
        )
    if split_index <= 0 or split_index >= len(normalized_text):
        return fragments

    first = normalized_text[:split_index].strip()
    rest = normalized_text[split_index:].strip()
    if not first or not rest:
        return fragments
    rest_fragments = _split_formula_reflow_text(
        rest,
        width_pt,
        max_height_pt,
        style,
        document=document,
        source_block=source_block,
    )
    return [first, *rest_fragments]


def _estimated_chars_for_lines(width_pt: float, font_size_pt: float, lines: int) -> int:
    return max(1, int((width_pt / max(font_size_pt, 1.0)) * lines * 1.75))


def _max_reflow_chars_to_fit(
    text: str,
    width_pt: float,
    style: RoleStyleDefaults,
    height_pt: float,
    *,
    document: DocumentIR | None = None,
    source_block: DocumentBlock | None = None,
) -> int:
    low = 1
    high = max(1, len(text))
    best = 1
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].strip()
        if (
            _estimated_reflow_height(
                candidate,
                width_pt,
                style,
                document=document,
                source_block=source_block,
            )
            + _reflow_height_safety(candidate, style, source_block)
            <= height_pt
        ):
            best = max(1, mid)
            low = mid + 1
        else:
            high = mid - 1
    return best


def _best_reflow_split_index(text: str, max_chars: int) -> int:
    max_chars = min(max_chars, len(text))
    candidates = [
        text.rfind(marker, 0, max_chars)
        for marker in ("。", "！", "？", ". ", "! ", "? ", "；", "; ", "，", ", ", " ")
    ]
    split_index = max(candidates)
    if split_index >= max(1, int(max_chars * 0.55)):
        char = text[split_index]
        return split_index + (0 if char.isspace() else 1)
    return max_chars


def _should_suppress_reflow_block(block: DocumentBlock) -> bool:
    text = block.source_text.strip()
    if not text:
        return True
    width = _bbox_width(block.bbox)
    height = _bbox_height(block.bbox)
    if re.match(
        r"^©\s*(?:(?:the\s+)?author\(s\)|authors?|作者)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if "doi:" in text.lower() and re.search(r"\bj\.\s+[A-Za-z]", text):
        return True
    if width <= 8 and height >= 40 and re.search(r"\d{4}", text):
        return True
    if block.role == BlockRole.FOOTNOTE and re.search(r"\bdoi:\s*\S+", text.lower()):
        return True
    if text.lower().startswith(("view online", "export citation", "crossmark")):
        return True
    return False


def _is_underfilled_reflow_page(
    *,
    combined_utilization: float,
    bottom_whitespace_ratio: float,
) -> bool:
    low_area_underfill = (
        combined_utilization < _LOW_UTILIZATION_THRESHOLD
        and bottom_whitespace_ratio >= _UNDERFILLED_BOTTOM_WHITESPACE_RATIO
    )
    large_bottom_underfill = (
        combined_utilization <= _UNDERFILLED_LARGE_BOTTOM_MAX_UTILIZATION
        and bottom_whitespace_ratio >= _UNDERFILLED_LARGE_BOTTOM_WHITESPACE_RATIO
    )
    return low_area_underfill or large_bottom_underfill


def _column_flow_issues(layout_trace: dict[str, Any]) -> list[dict[str, Any]]:
    if layout_trace.get("layout_mode") != "continuous_reflow":
        return []
    column_layout = layout_trace.get("column_layout")
    if not isinstance(column_layout, dict) or column_layout.get("column_count") != 2:
        return []

    render_defaults = layout_trace.get("render_defaults")
    page_layout = render_defaults.get("page_layout") if isinstance(render_defaults, dict) else None
    if not isinstance(page_layout, dict):
        page_layout = {}
    content_y0 = _trace_float(page_layout.get("margin_top_pt"), 0.0)
    page_height = _trace_float(page_layout.get("height_pt"), 0.0)
    margin_bottom = _trace_float(page_layout.get("margin_bottom_pt"), 0.0)
    content_bottom = page_height - margin_bottom if page_height > margin_bottom else 0.0
    content_height = max(1.0, content_bottom - content_y0)

    traces_by_page: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    blocks = layout_trace.get("blocks")
    if not isinstance(blocks, list):
        return []
    for index, trace in enumerate(blocks):
        if not isinstance(trace, dict):
            continue
        if trace.get("span") != "column":
            continue
        if trace.get("column_index") not in {0, 1}:
            continue
        page_id = trace.get("output_page_id")
        if not isinstance(page_id, str):
            continue
        traces_by_page.setdefault(page_id, []).append((index, trace))

    issues: list[dict[str, Any]] = []
    for page_id, indexed_traces in traces_by_page.items():
        first_index, first_trace = min(
            indexed_traces,
            key=lambda item: (
                _trace_bbox_float(item[1], "y0"),
                _trace_bbox_float(item[1], "x0"),
                item[0],
            ),
        )
        if first_trace.get("column_index") == 1:
            issues.append(
                {
                    "kind": "right_column_page_start",
                    "page_id": page_id,
                    "render_block_id": first_trace.get("render_block_id"),
                    "source_block_id": first_trace.get("source_block_id"),
                    "trace_index": first_index,
                }
            )

        left_traces = [trace for _index, trace in indexed_traces if trace.get("column_index") == 0]
        right_traces = [trace for _index, trace in indexed_traces if trace.get("column_index") == 1]
        if not right_traces:
            continue
        right_top = min(_trace_bbox_float(trace, "y0") for trace in right_traces)
        right_bottom = max(_trace_bbox_float(trace, "y1") for trace in right_traces)
        left_bottom = (
            max(_trace_bbox_float(trace, "y1") for trace in left_traces)
            if left_traces
            else content_y0
        )
        left_whitespace_ratio = max(0.0, content_bottom - left_bottom) / content_height
        right_starts_near_top = right_top <= (
            content_y0 + content_height * _RIGHT_COLUMN_START_TOP_RATIO
        )
        if (
            right_starts_near_top
            and left_whitespace_ratio >= _LEFT_COLUMN_UNDERFILLED_WHITESPACE_RATIO
            and right_bottom > left_bottom + 24.0
        ):
            issues.append(
                {
                    "kind": "left_column_underfilled_before_right_column",
                    "page_id": page_id,
                    "left_bottom_pt": round(left_bottom, 4),
                    "right_top_pt": round(right_top, 4),
                    "left_whitespace_ratio": round(left_whitespace_ratio, 4),
                }
            )
    return issues


def _trace_bbox_float(trace: dict[str, Any], key: str) -> float:
    bbox = trace.get("bbox")
    if not isinstance(bbox, dict):
        return 0.0
    return _trace_float(bbox.get(key), 0.0)


def _trace_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _page_utilization(pages: list[RenderPage]) -> list[dict[str, Any]]:
    utilization: list[dict[str, Any]] = []
    for page in pages:
        area = page.size.width * page.size.height
        text_area = sum(_bbox_area(block.bbox) for block in page.blocks)
        asset_area = sum(_bbox_area(asset.bbox) for asset in page.assets)
        bottom_y = max(
            [
                *[block.bbox.y1 for block in page.blocks],
                *[asset.bbox.y1 for asset in page.assets],
                0.0,
            ]
        )
        bottom_whitespace = max(0.0, page.size.height - bottom_y)
        utilization.append(
            {
                "page_id": page.page_id,
                "text_area_ratio": round(text_area / area, 4) if area > 0 else 0.0,
                "asset_area_ratio": round(asset_area / area, 4) if area > 0 else 0.0,
                "combined_area_ratio": round((text_area + asset_area) / area, 4)
                if area > 0
                else 0.0,
                "bottom_whitespace_pt": round(bottom_whitespace, 4),
                "bottom_whitespace_ratio": round(bottom_whitespace / page.size.height, 4)
                if page.size.height > 0
                else 0.0,
                "block_count": len(page.blocks),
                "asset_count": len(page.assets),
            }
        )
    return utilization


def _build_reflow_trace(
    *,
    document: DocumentIR,
    pages: list[RenderPage],
    defaults: RenderDefaults,
    source_page_ids: set[str],
    rendered_source_ids: set[str],
    block_traces: list[dict[str, Any]],
    asset_traces: list[dict[str, Any]],
    figure_group_traces: list[dict[str, Any]],
    suppressed_artifacts: list[dict[str, Any]],
    layout_intent_plan: LayoutIntentPlan | None,
) -> dict[str, Any]:
    source_block_count = sum(len(page.blocks) for page in document.pages)
    return {
        "kind": "layout_trace",
        "layout_mode": _enum_value(defaults.layout_mode),
        "standard": "gb_t_7713_1_2025",
        "render_defaults": defaults.model_dump(mode="json"),
        "column_layout": defaults.column_layout.model_dump(mode="json"),
        "source": {
            "page_count": len(source_page_ids),
            "block_count": source_block_count,
        },
        "output": {
            "page_count": len(pages),
            "block_count": sum(len(page.blocks) for page in pages),
        },
        "rendered_source_block_count": len(rendered_source_ids),
        "suppressed_artifacts": suppressed_artifacts,
        "intent_requirements": _intent_requirement_diagnostics(layout_intent_plan),
        "page_utilization": _page_utilization(pages),
        "blocks": block_traces,
        "assets": asset_traces,
        "figure_groups": figure_group_traces,
    }


def _build_source_bbox_trace(
    document: DocumentIR,
    pages: list[RenderPage],
    defaults: RenderDefaults,
    layout_intent_plan: LayoutIntentPlan | None,
) -> dict[str, Any]:
    return {
        "kind": "layout_trace",
        "layout_mode": _enum_value(defaults.layout_mode),
        "standard": "none",
        "render_defaults": defaults.model_dump(mode="json"),
        "column_layout": defaults.column_layout.model_dump(mode="json"),
        "source": {
            "page_count": len(document.pages),
            "block_count": sum(len(page.blocks) for page in document.pages),
        },
        "output": {
            "page_count": len(pages),
            "block_count": sum(len(page.blocks) for page in pages),
        },
        "suppressed_artifacts": [],
        "intent_requirements": _intent_requirement_diagnostics(layout_intent_plan),
        "page_utilization": _page_utilization(pages),
        "blocks": [
            {
                "source_block_id": block.block_id.split("__cont_", 1)[0],
                "render_block_id": block.block_id,
                "layout_signature": block.layout_signature,
                "output_page_id": page.page_id,
                "role": block.role.value,
                "translated_chars": len(block.text),
                "estimated_lines": _estimated_line_count(
                    block.text,
                    _bbox_width(block.bbox),
                    block.font_size_pt,
                ),
                "bbox": block.bbox.model_dump(),
                "quality_flags": block.quality_flags,
            }
            for page in pages
            for block in page.blocks
        ],
    }


def _intent_requirement_diagnostics(
    layout_intent_plan: LayoutIntentPlan | None,
) -> list[dict[str, Any]]:
    if layout_intent_plan is None:
        return []
    diagnostics: list[dict[str, Any]] = []
    for requirement in layout_intent_plan.requirements:
        flags = list(requirement.quality_flags)
        status = "recognized"
        if "requirement_satisfied" in flags:
            status = "satisfied"
        elif "requirement_diagnostic" in flags:
            status = "diagnostic"
        diagnostics.append(
            {
                "requirement_id": requirement.requirement_id,
                "label": requirement.label,
                "category": requirement.category,
                "required": requirement.required,
                "section_kinds": [
                    _enum_value(section_kind) for section_kind in requirement.section_kinds
                ],
                "status": status,
                "quality_flags": flags,
                "evidence": list(requirement.evidence),
            }
        )
    return diagnostics


def _make_continuation_blocks(
    source_block: DocumentBlock,
    text: str,
    page_size: PageSize,
    document: DocumentIR,
    font_size_pt: float,
    font_scale: float,
    render_intent: str,
    text_align: str,
    font_weight: int,
    font_style: str,
    line_height: float,
) -> list[RenderBlock]:
    bbox = _continuation_bbox(page_size)
    blocks: list[RenderBlock] = []
    remaining = text
    index = 1
    while remaining:
        visible_text, overflow_text = _split_text_to_fit(
            remaining,
            bbox,
            font_size_pt,
            line_height,
        )
        if not visible_text:
            visible_text = remaining
            overflow_text = ""
        flags = ["continuation_page", "continued_from_overflow"]
        if overflow_text:
            flags.append("continued_on_next_page")
        blocks.append(
            RenderBlock(
                block_id=f"{source_block.block_id}__cont_{index:02d}",
                role=source_block.role,
                bbox=bbox,
                text=visible_text,
                html=_formula_html_for_text(visible_text, document, source_block)[0],
                style_seed=source_block.style_seed,
                font_size_pt=font_size_pt,
                font_scale=font_scale,
                render_intent=render_intent,
                text_align=text_align,
                font_weight=font_weight,
                font_style=font_style,
                first_line_indent_em=0.0,
                line_height=line_height,
                source_block_id=source_block.block_id,
                layout_signature=_layout_signature(source_block.block_id, visible_text, index),
                quality_flags=flags,
            )
        )
        remaining = overflow_text
        index += 1
    return blocks
