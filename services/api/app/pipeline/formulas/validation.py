from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .normalization import contains_natural_language, normalize_pdf_text

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
) -> FormulaLatexValidation:
    normalized = normalize_pdf_text(latex).strip()
    if not normalized:
        return FormulaLatexValidation(
            accepted=False,
            status="empty",
            quality_flags=("formula_latex_empty",),
            math_signal_count=0,
            fallback_reason="source_text_plaintext",
        )

    math_signal_count = len(_MATH_SIGNAL_PATTERN.findall(normalized))
    prose_like = _looks_like_prose(normalized)
    if prose_like:
        return FormulaLatexValidation(
            accepted=False,
            status="prose_like",
            quality_flags=("formula_prose_like",),
            math_signal_count=math_signal_count,
            fallback_reason="source_text_plaintext",
        )

    if math_signal_count < _minimum_math_signals(normalized):
        return FormulaLatexValidation(
            accepted=False,
            status="not_math",
            quality_flags=("formula_not_math",),
            math_signal_count=math_signal_count,
            fallback_reason="source_text_plaintext",
        )

    katex_error = _katex_render_error(normalized)
    if katex_error is not None:
        return FormulaLatexValidation(
            accepted=False,
            status="katex_error",
            quality_flags=("formula_katex_render_failed",),
            math_signal_count=math_signal_count,
            fallback_reason="formula_asset_image"
            if source_text.strip() != normalized
            else "source_text_plaintext",
        )

    return FormulaLatexValidation(
        accepted=True,
        status="accepted",
        quality_flags=(),
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


@lru_cache(maxsize=512)
def _katex_render_error(latex: str) -> str | None:
    script = (
        "let katex;try{katex=require('katex')}catch(error){process.exit(3);}"
        "const latex=Buffer.from(process.argv[1],'base64').toString('utf8');"
        "try{katex.renderToString(latex,{displayMode:false,throwOnError:true,"
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
            timeout=3,
            cwd=_project_root(),
        )
    except Exception:
        return None
    if completed.returncode in {0, 3}:
        return None
    return completed.stderr.strip() or "katex_render_failed"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[5]
