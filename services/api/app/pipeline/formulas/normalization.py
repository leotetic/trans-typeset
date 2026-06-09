from __future__ import annotations

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NATURAL_LANGUAGE_BOUNDARY = re.compile(
    r"\s+(?:"
    r"and|are|as|defined\s+as|due\s+to|equation|for|from|holds?|in|into\s+the|"
    r"is|notably|preventing|ratio\s+of|represents?|resulting|scaled|shown|"
    r"the|then|thus|to|unlike|where|which|while|with"
    r")\b",
    re.IGNORECASE,
)
_LONG_WORD_SEQUENCE = re.compile(r"\b[A-Za-z]{4,}\b(?:[\s,-]+\b[A-Za-z]{4,}\b){2,}")

PDF_TEXT_REPLACEMENTS = {
    "\x01": " × ",
    "\x03": "-",
    "\x04": " · ",
    "¼": "=",
    "þ": "+",
    "ð": "∫",
    "\u00ad": "",
    "\uf0b7": " · ",
}

GREEK_TO_LATEX = {
    "α": r"\alpha",
    "β": r"\beta",
    "γ": r"\gamma",
    "δ": r"\delta",
    "ε": r"\epsilon",
    "θ": r"\theta",
    "λ": r"\lambda",
    "μ": r"\mu",
    "ν": r"\nu",
    "ρ": r"\rho",
    "σ": r"\sigma",
    "τ": r"\tau",
    "φ": r"\phi",
    "ω": r"\omega",
    "Ω": r"\Omega",
    "Δ": r"\Delta",
}

SYMBOL_TO_LATEX = {
    "≤": r"\le",
    "≥": r"\ge",
    "≠": r"\ne",
    "≈": r"\approx",
    "∑": r"\sum",
    "∫": r"\int",
    "√": r"\sqrt{}",
    "∞": r"\infty",
    "∂": r"\partial",
    "∇": r"\nabla",
    "×": r"\times",
    "÷": r"\div",
    "·": r"\cdot",
    "−": "-",
}


def normalize_pdf_text(text: str) -> str:
    normalized = normalize_pdf_text_fragment(text)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_pdf_text_fragment(text: str) -> str:
    normalized = text
    for source, replacement in PDF_TEXT_REPLACEMENTS.items():
        normalized = normalized.replace(source, replacement)
    return _CONTROL_CHARS.sub(" ", normalized)


def is_noise_text(text: str) -> bool:
    normalized = normalize_pdf_text(text)
    if not normalized:
        return True
    lowered = normalized.lower()
    if lowered in {"dx", "n", "vn", "ω"}:
        return True
    if normalized in {"∫", "\\int", "Ω", "× -"}:
        return True
    if re.fullmatch(r"(?:[×+\-*/=·\s]+)", normalized):
        return True
    if re.fullmatch(r"(?:@|\\partial)?\s*[=+\-*/·]\s*(?:@|\\partial)?", normalized):
        return True
    if re.fullmatch(r"coll\s*=\s*X", normalized, re.IGNORECASE):
        return True
    if re.fullmatch(r"dB\s*-?\s*\d+", normalized, re.IGNORECASE):
        return True
    if not re.search(r"[A-Za-z0-9α-ωΑ-Ω∂∇∫∑=+\-*/^_]", normalized):
        return True
    return False


def truncate_at_language_boundary(text: str) -> tuple[str, bool]:
    candidate = normalize_pdf_text(text).strip(" \t\n\r,;。；")
    if not candidate:
        return "", candidate != text
    boundary = _NATURAL_LANGUAGE_BOUNDARY.search(candidate)
    if boundary:
        candidate = candidate[: boundary.start()]
    sentence_mark = re.search(r"[。；;]\s*", candidate)
    if sentence_mark:
        candidate = candidate[: sentence_mark.start()]
    if "," in candidate:
        prefix, suffix = candidate.split(",", 1)
        if _NATURAL_LANGUAGE_BOUNDARY.search(" " + suffix.strip()):
            candidate = prefix
    candidate = candidate.strip(" \t\n\r,;。；")
    if candidate.endswith(".") and not re.search(r"\d\.$", candidate):
        candidate = candidate[:-1].rstrip()
    if re.search(r"\.\d{1,3}$", candidate) and re.search(r"[A-Za-z]\d*\.\d{1,3}$", candidate):
        candidate = re.sub(r"\.\d{1,3}$", "", candidate)
    return candidate, candidate != normalize_pdf_text(text).strip(" \t\n\r,;。；")


def truncate_raw_at_language_boundary(text: str) -> tuple[str, bool]:
    candidate = re.sub(r"\s+", " ", text.strip(" \t\n\r,;。；"))
    if not candidate:
        return "", candidate != text
    boundary = _NATURAL_LANGUAGE_BOUNDARY.search(candidate)
    if boundary:
        candidate = candidate[: boundary.start()]
    sentence_mark = re.search(r"[。；;]\s*", candidate)
    if sentence_mark:
        candidate = candidate[: sentence_mark.start()]
    if "," in candidate:
        prefix, suffix = candidate.split(",", 1)
        if _NATURAL_LANGUAGE_BOUNDARY.search(" " + suffix.strip()):
            candidate = prefix
    candidate = candidate.strip(" \t\n\r,;。；")
    if candidate.endswith(".") and not re.search(r"\d\.$", candidate):
        candidate = candidate[:-1].rstrip()
    if re.search(r"\.\d{1,3}$", candidate) and re.search(r"[A-Za-z]\d*\.\d{1,3}$", candidate):
        candidate = re.sub(r"\.\d{1,3}$", "", candidate)
    return candidate, candidate != re.sub(r"\s+", " ", text.strip(" \t\n\r,;。；"))


def contains_natural_language(text: str) -> bool:
    normalized = normalize_pdf_text(text)
    if _NATURAL_LANGUAGE_BOUNDARY.search(normalized):
        return True
    if _LONG_WORD_SEQUENCE.search(normalized):
        return True
    return False


def latex_from_pdf_text(text: str) -> tuple[str, list[str]]:
    normalized, truncated = truncate_at_language_boundary(text)
    flags: list[str] = ["formula_latex_normalized"] if normalize_pdf_text(text) != text else []
    if truncated:
        flags.append("formula_text_truncated")
    if not normalized or is_noise_text(normalized):
        return "", _unique([*flags, "formula_low_confidence"])

    latex = normalized
    for source, target in {**GREEK_TO_LATEX, **SYMBOL_TO_LATEX}.items():
        latex = latex.replace(source, f" {target} ")
    latex = re.sub(r"@([A-Za-z][A-Za-z0-9_]*)", r"\\partial \1", latex)
    latex = re.sub(r"\s+", " ", latex).strip()
    latex, balance_flags = balance_latex_delimiters(latex)
    return latex, _unique([*flags, *balance_flags])


def balance_latex_delimiters(text: str) -> tuple[str, list[str]]:
    flags: list[str] = []
    balanced = text
    for left, right in (("(", ")"), ("[", "]"), ("{", "}")):
        open_count = balanced.count(left)
        close_count = balanced.count(right)
        if close_count > open_count:
            remove_count = close_count - open_count
            chars: list[str] = []
            for char in reversed(balanced):
                if char == right and remove_count > 0:
                    remove_count -= 1
                    continue
                chars.append(char)
            balanced = "".join(reversed(chars))
            flags.append("formula_delimiter_repaired")
        elif open_count > close_count:
            balanced = balanced + (right * (open_count - close_count))
            flags.append("formula_delimiter_repaired")
    return balanced, flags


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result
