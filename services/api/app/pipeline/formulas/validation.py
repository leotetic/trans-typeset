from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .normalization import contains_natural_language, formula_corruption_flags, normalize_pdf_text

FORMULA_REF_PATTERN = re.compile(r"\{\{formula:([A-Za-z0-9_.:-]+)\}\}")
LEGACY_FORMULA_PLACEHOLDER_PATTERN = re.compile(r"@@FORMULA_([A-Za-z0-9_]+)@@")
_MATH_SIGNAL_PATTERN = re.compile(
    r"(?:[=≤≥∑∫√∞≈≠∂∇^_+\-*/]|\\(?:partial|nabla|frac|sum|int|sqrt|"
    r"alpha|beta|gamma|delta|epsilon|theta|lambda|mu|nu|pi|rho|sigma|"
    r"phi|omega|Delta|Omega|cdot|times)|\b[A-Za-zα-ωΑ-Ω]\s*[+\-*/^_]\s*"
    r"[A-Za-z0-9α-ωΑ-Ω])"
)


@dataclass(frozen=True)
class FormulaLatexValidation:
    accepted: bool
    status: str
    math_signal_count: int
    acceptance_level: str = "fallback_required"
    quality_flags: tuple[str, ...] = ()
    fallback_reason: str | None = None


def formula_ref(formula_id: str) -> str:
    return f"{{{{formula:{formula_id}}}}}"


def extract_formula_refs(text: str) -> list[str]:
    return [match.group(0) for match in FORMULA_REF_PATTERN.finditer(text or "")]


def legacy_formula_placeholder(formula_id: str) -> str:
    return f"@@FORMULA_{formula_id}@@"


def formula_id_from_legacy_placeholder(token: str) -> str | None:
    match = LEGACY_FORMULA_PLACEHOLDER_PATTERN.fullmatch(token or "")
    return match.group(1) if match else None


def validate_formula_latex(
    latex: str,
    *,
    source_text: str = "",
    display_mode: str | None = None,
) -> FormulaLatexValidation:
    normalized = normalize_pdf_text(latex).strip()
    corruption_flags = formula_corruption_flags(
        source_text or latex,
        normalized_latex=normalized,
    )
    if not normalized:
        return FormulaLatexValidation(
            accepted=False,
            status="empty",
            acceptance_level="fallback_required",
            quality_flags=tuple(_unique(["formula_latex_empty", *corruption_flags])),
            math_signal_count=0,
            fallback_reason="source_text_plaintext",
        )

    math_signal_count = len(_MATH_SIGNAL_PATTERN.findall(normalized))
    severe_corruption_flags = _severe_corruption_flags(corruption_flags, normalized)
    if display_mode == "display" and severe_corruption_flags:
        return FormulaLatexValidation(
            accepted=False,
            status="corrupt_text_layer",
            acceptance_level="fallback_required",
            quality_flags=tuple(
                _unique(
                    [
                        "formula_corrupt_text_rejected",
                        "formula_low_confidence",
                        *corruption_flags,
                    ]
                )
            ),
            math_signal_count=math_signal_count,
            fallback_reason="formula_asset_image",
        )

    prose_like = _looks_like_prose(normalized)
    if prose_like:
        return FormulaLatexValidation(
            accepted=False,
            status="prose_like",
            acceptance_level="fallback_required",
            quality_flags=tuple(_unique(["formula_prose_like", *corruption_flags])),
            math_signal_count=math_signal_count,
            fallback_reason="source_text_plaintext",
        )

    if math_signal_count < _minimum_math_signals(normalized):
        return FormulaLatexValidation(
            accepted=False,
            status="not_math",
            acceptance_level="fallback_required",
            quality_flags=tuple(_unique(["formula_not_math", *corruption_flags])),
            math_signal_count=math_signal_count,
            fallback_reason="source_text_plaintext",
        )

    katex_error = _katex_render_error(normalized)
    if katex_error is not None:
        return FormulaLatexValidation(
            accepted=False,
            status="katex_error",
            acceptance_level="fallback_required",
            quality_flags=tuple(_unique(["formula_katex_render_failed", *corruption_flags])),
            math_signal_count=math_signal_count,
            fallback_reason="formula_asset_image"
            if source_text.strip() != normalized
            else "source_text_plaintext",
        )

    acceptance_level = (
        "accepted_low_confidence"
        if _looks_low_confidence(normalized, math_signal_count) or corruption_flags
        else "accepted_structured"
    )
    return FormulaLatexValidation(
        accepted=True,
        status="accepted",
        acceptance_level=acceptance_level,
        quality_flags=tuple(
            _unique(
                [
                    *(["formula_low_confidence"] if acceptance_level == "accepted_low_confidence" else []),
                    *corruption_flags,
                ]
            )
        )
        if acceptance_level == "accepted_low_confidence"
        else (),
        math_signal_count=math_signal_count,
        fallback_reason=None,
    )


def _minimum_math_signals(text: str) -> int:
    return 1 if len(text) <= 24 else 2


def _looks_like_prose(text: str) -> bool:
    words = re.findall(r"[A-Za-z]{3,}", text)
    math_signals = len(_MATH_SIGNAL_PATTERN.findall(text))
    if contains_natural_language(text) and len(words) >= 2:
        return True
    if re.search(r"[,.;]\s+[A-Za-z]{3,}", text) and len(words) >= 4:
        return True
    if len(text) > 80 and len(words) >= 6 and math_signals < 3:
        return True
    if len(words) >= 10 and math_signals <= max(1, len(words) // 8):
        return True
    return False


def _looks_low_confidence(text: str, math_signal_count: int) -> bool:
    if len(text) <= 8:
        return True
    if math_signal_count <= 1 and len(re.findall(r"[A-Za-z]{1,2}", text)) >= 4:
        return True
    if re.search(r"\\tag\{\d+\}$", text):
        return False
    if text.count("=") >= 3 and math_signal_count < 3:
        return True
    return False


def _severe_corruption_flags(flags: list[str], latex: str) -> list[str]:
    if _has_unrepaired_text_layer_corruption(latex) and set(flags).intersection(
        {
            "formula_text_layer_corrupt",
            "formula_slash_glyph_suspect",
            "formula_prime_glyph_suspect",
        }
    ):
        return flags
    if _has_structured_visual_math(latex):
        return []
    if "formula_text_layer_corrupt" in flags:
        return flags
    if "formula_prime_glyph_suspect" in flags:
        return flags
    if "formula_slash_glyph_suspect" not in flags:
        return []
    if _has_unrepaired_text_layer_corruption(latex):
        return flags
    return []


def _has_unrepaired_text_layer_corruption(latex: str) -> bool:
    return bool(
        re.search(
            r"(?:\\partial\s+[A-Za-z]{2,}|"
            r"[A-Za-z]_?[A-Za-z]\s*=\s*k(?:\^?\d|\{\d\})|"
            r"[A-Za-z]\s*0\s*[A-Za-z])",
            latex,
        )
    )


def _has_structured_visual_math(latex: str) -> bool:
    return bool(
        re.search(
            r"\\(?:frac|dfrac|tfrac|sum|int|partial|nabla|sqrt)\b|\\begin\{",
            latex,
        )
    )


@lru_cache(maxsize=512)
def _katex_render_error(latex: str) -> str | None:
    display_mode = "true" if _requires_display_mode_for_validation(latex) else "false"
    script = (
        "let katex;try{katex=require('katex')}catch(error){process.exit(3);}"
        "const latex=Buffer.from(process.argv[1],'base64').toString('utf8');"
        "try{katex.renderToString(latex,{displayMode:"
        + display_mode
        + ",throwOnError:true,"
        "strict:'ignore',trust:false});}"
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
            cwd=_project_root(),
        )
    except Exception:
        return None
    if completed.returncode in {0, 3}:
        return None
    stderr = completed.stderr or ""
    return stderr.strip() or "katex_render_failed"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _requires_display_mode_for_validation(latex: str) -> bool:
    return "\\tag{" in latex or "\\begin{" in latex


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique
