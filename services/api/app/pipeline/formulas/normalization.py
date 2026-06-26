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
_FORMULA_PROSE_FRAGMENT = re.compile(
    r"\b(?:defined\s+as|equation|into\s+the|where|while|with|holds?|shown|represents?)\b",
    re.IGNORECASE,
)
_EQUATION_NUMBER_SUFFIX = re.compile(
    r"(?:[,;:]\s*)?(\([A-Za-z]?\d+(?:[.\-]\d+)*[a-z]?\))\s*$"
)
_EQUATION_NUMBER_WITH_SHORT_TAIL = re.compile(
    r"(?:[,;:]\s*)?(\([A-Za-z]?\d+(?:[.\-]\d+)*[a-z]?\))"
    r"(?P<tail>\s+[A-Za-z0-9α-ωΑ-Ω_{}^\\\\'+\-*/=:.,\s]{1,24})$"
)
_TRAILING_SCRIPT_OPERATOR = re.compile(r"(?:[_^]\s*)+$")
_HYPHEN_SPLIT_WORD = re.compile(r"\b([A-Za-z]{2,})-\s+([A-Za-z]{2,})\b")
_PDF_LIGATURE_REPLACEMENTS = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
}
_PDF_LIGATURE_PATTERN = re.compile("[" + "".join(_PDF_LIGATURE_REPLACEMENTS) + "]")
_UNDERBRACE_ARTIFACT_PATTERN = re.compile(
    r"\|\s*(?P<filler>(?:(?:ffl|ffi|ff|fi|fl)|[\s\ufb00-\ufb04])*)"
    r"\{\s*z(?P<label>(?:(?:ffl|ffi|ff|fi|fl)|[\s\ufb00-\ufb04A-Za-z0-9+\-−])*)\}"
)
_RAW_CORRUPTION_MARKER = re.compile(r"[\x01-\x04¼þðÞÐ\ufb00-\ufb04]|@[A-Za-z]")
_PARTIAL_SLASH_GLYPH_PATTERN = re.compile(
    r"@[A-Za-z][A-Za-z0-9_']*\s*=\s*@[A-Za-z][A-Za-z0-9_']*"
)
_COMPACT_SLASH_GLYPH_PATTERN = re.compile(
    r"\b[a-z][a-z0-9_']{1,5}\s*=\s*[a-z][a-z0-9_']{1,5}\b"
)
_PRIME_GLYPH_PATTERN = re.compile(r"\b[fqmn]\s*0\s*[sn]\b|\b[fqmn]0[sn]\b")
_UNREPAIRED_CORRUPTION_PATTERN = re.compile(
    r"(?:\\partial\s+[A-Za-z]{2,}\s*=\s*\\partial|\b[fqmn]\s*0\s*[sn]\b|"
    r"\b[fqmn]0[sn]\b|\b[fqmn]_?[sn]\s*=\s*k(?:\^?\d|\{\d\})\b|"
    r"\b[fqmn][sn]=k\d\b)"
)

PDF_TEXT_REPLACEMENTS = {
    "\x01": " × ",
    "\x03": "-",
    "\x04": " · ",
    "¼": "=",
    "þ": "+",
    "ð": "∫",
    "\u00ad": "",
    "\uf0b7": " · ",
    "Ð": "∫",
    "Þ": "∂",
    "ℏ": r" \hbar ",
    "ℝ": r" \mathbb{R} ",
}

GREEK_TO_LATEX = {
    "α": r"\alpha",
    "β": r"\beta",
    "γ": r"\gamma",
    "δ": r"\delta",
    "ε": r"\epsilon",
    "ϵ": r"\epsilon",
    "η": r"\eta",
    "ζ": r"\zeta",
    "θ": r"\theta",
    "κ": r"\kappa",
    "λ": r"\lambda",
    "μ": r"\mu",
    "ν": r"\nu",
    "ξ": r"\xi",
    "π": r"\pi",
    "ρ": r"\rho",
    "σ": r"\sigma",
    "τ": r"\tau",
    "φ": r"\phi",
    "ψ": r"\psi",
    "χ": r"\chi",
    "ω": r"\omega",
    "ϕ": r"\varphi",
    "ϱ": r"\varrho",
    "Ω": r"\Omega",
    "Δ": r"\Delta",
    "Γ": r"\Gamma",
    "Θ": r"\Theta",
    "Λ": r"\Lambda",
    "Ξ": r"\Xi",
    "Π": r"\Pi",
    "Ψ": r"\Psi",
    "Σ": r"\Sigma",
    "Φ": r"\Phi",
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
    "→": r"\to",
    "↦": r"\mapsto",
    "←": r"\leftarrow",
    "±": r"\pm",
    "∓": r"\mp",
    "∈": r"\in",
    "∉": r"\notin",
    "⊂": r"\subset",
    "⊆": r"\subseteq",
    "△": r"\Delta",
    "∆": r"\Delta",
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
    normalized = re.sub(r"\b([A-Za-z])\u0304", r" \\bar{\1} ", normalized)
    normalized = re.sub(r"¯\s*([A-Za-z])\b", r" \\bar{\1} ", normalized)
    normalized = re.sub(r"\b([A-Za-z])\s*¯", r" \\bar{\1} ", normalized)
    normalized = re.sub(r"\u02d9\s*([A-Za-z])\b", r" \\dot{\1} ", normalized)
    normalized = re.sub(r"\b([A-Za-z])\u0307", r" \\dot{\1} ", normalized)
    return _CONTROL_CHARS.sub(" ", normalized)


def alpha_word_tokens(
    text: str,
    *,
    min_len: int = 3,
    ignore_latex_commands: bool = True,
) -> list[str]:
    normalized = normalize_pdf_text(text)
    if min_len < 1:
        raise ValueError("min_len must be positive")
    if ignore_latex_commands:
        pattern = rf"(?<!\\)\b[A-Za-z]{{{min_len},}}\b"
    else:
        pattern = rf"\b[A-Za-z]{{{min_len},}}\b"
    return re.findall(pattern, normalized)


def formula_corruption_flags(
    text: str,
    *,
    normalized_latex: str = "",
) -> list[str]:
    flags: list[str] = []
    has_raw_marker = _RAW_CORRUPTION_MARKER.search(text or "") is not None
    if has_raw_marker:
        flags.append("formula_text_layer_corrupt")
    if _PDF_LIGATURE_PATTERN.search(text or "") or _PDF_LIGATURE_PATTERN.search(
        normalized_latex or ""
    ):
        flags.append("formula_pdf_ligature_corrupt")
    raw_text = text or ""
    if (
        has_raw_marker
        and (
            _PARTIAL_SLASH_GLYPH_PATTERN.search(raw_text)
            or _COMPACT_SLASH_GLYPH_PATTERN.search(raw_text)
        )
        or _UNREPAIRED_CORRUPTION_PATTERN.search(normalized_latex or "")
    ):
        flags.append("formula_slash_glyph_suspect")
    if (
        has_raw_marker
        and _PRIME_GLYPH_PATTERN.search(raw_text)
        or _PRIME_GLYPH_PATTERN.search(normalized_latex or "")
    ):
        flags.append("formula_prime_glyph_suspect")
    return _unique(flags)


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
    candidate = _strip_hard_noise_sequences(candidate)
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
    candidate = _strip_sentence_period(candidate)
    if re.search(r"\.\d{1,3}$", candidate) and re.search(r"[A-Za-z]\d*\.\d{1,3}$", candidate):
        candidate = re.sub(r"\.\d{1,3}$", "", candidate)
    candidate = _trim_formula_tail(candidate)
    return candidate, candidate != normalize_pdf_text(text).strip(" \t\n\r,;。；")


def truncate_raw_at_language_boundary(text: str) -> tuple[str, bool]:
    candidate = re.sub(r"\s+", " ", text.strip(" \t\n\r,;。；"))
    if not candidate:
        return "", candidate != text
    original = candidate
    candidate = _join_hyphen_split_words(candidate)
    candidate = _strip_hard_noise_sequences(candidate)
    boundary = _NATURAL_LANGUAGE_BOUNDARY.search(candidate)
    if boundary:
        candidate = candidate[: boundary.start()]
    long_words = _LONG_WORD_SEQUENCE.search(candidate)
    if long_words:
        candidate = candidate[: long_words.start()]
    sentence_mark = re.search(r"[。；;]\s*", candidate)
    if sentence_mark:
        candidate = candidate[: sentence_mark.start()]
    if "," in candidate:
        prefix, suffix = candidate.split(",", 1)
        if _NATURAL_LANGUAGE_BOUNDARY.search(" " + suffix.strip()):
            candidate = prefix
    candidate = candidate.strip(" \t\n\r,;。；")
    candidate = _strip_sentence_period(candidate)
    if re.search(r"\.\d{1,3}$", candidate) and re.search(r"[A-Za-z]\d*\.\d{1,3}$", candidate):
        candidate = re.sub(r"\.\d{1,3}$", "", candidate)
    candidate = _trim_formula_tail(candidate)
    return candidate, candidate != original


def contains_natural_language(text: str) -> bool:
    normalized = normalize_pdf_text(text)
    if _NATURAL_LANGUAGE_BOUNDARY.search(normalized):
        return True
    if _LONG_WORD_SEQUENCE.search(normalized):
        return True
    return False


def latex_from_pdf_text(text: str) -> tuple[str, list[str]]:
    corruption_flags = formula_corruption_flags(text)
    repaired_text, ligature_flags = repair_pdf_ligatures(text)
    repaired_text, underbrace_flags = repair_underbrace_artifacts(repaired_text)
    repaired_text, slash_flags = repair_corrupt_formula_slash_glyphs(
        repaired_text,
        source_text=text,
    )
    normalized, truncated = truncate_at_language_boundary(repaired_text)
    flags: list[str] = (
        ["formula_latex_normalized"] if normalize_pdf_text(repaired_text) != text else []
    )
    flags.extend([*corruption_flags, *ligature_flags, *underbrace_flags, *slash_flags])
    if truncated:
        flags.append("formula_text_truncated")
    if not normalized or is_noise_text(normalized):
        return "", _unique([*flags, "formula_low_confidence"])

    latex = normalized
    equation_number = _extract_equation_number(latex)
    if equation_number is not None:
        latex = _strip_equation_number(latex, equation_number)
        flags.append("formula_equation_number_preserved")
    latex, common_flags = repair_common_pdf_math_degradation(latex)
    flags.extend(common_flags)
    for source, target in {**GREEK_TO_LATEX, **SYMBOL_TO_LATEX}.items():
        latex = latex.replace(source, f" {target} ")
    latex = re.sub(r"@([A-Za-z][A-Za-z0-9_]*)", r"\\partial \1", latex)
    latex = re.sub(r"\b([A-Za-z])\s+([0-9])\b", r"\1\2", latex)
    latex = re.sub(r"\b([0-9])\s+([A-Za-z])\b", r"\1\2", latex)
    latex, partial_flags = repair_compact_partial_derivatives(latex)
    flags.extend(partial_flags)
    latex, variable_flags = format_compact_formula_variables(latex)
    flags.extend(variable_flags)
    latex = re.sub(r"\s+", " ", latex).strip()
    latex, balance_flags = balance_latex_delimiters(latex)
    if equation_number is not None:
        latex = f"{latex} \\tag{{{equation_number.strip('()')}}}".strip()
    return latex, _unique([*flags, *balance_flags])


def repair_pdf_ligatures(text: str) -> tuple[str, list[str]]:
    repaired = text
    for source, replacement in _PDF_LIGATURE_REPLACEMENTS.items():
        repaired = repaired.replace(source, replacement)
    return repaired, ["formula_pdf_ligature_repaired"] if repaired != text else []


def repair_underbrace_artifacts(text: str) -> tuple[str, list[str]]:
    if not _UNDERBRACE_ARTIFACT_PATTERN.search(text) and "|{" not in text:
        return text, []

    repaired = _UNDERBRACE_ARTIFACT_PATTERN.sub(_underbrace_artifact_replacement, text)
    repaired = re.sub(r"\|\s*\{\s*z\s*[^}]*\}", "", repaired)
    repaired = re.sub(r"\|\s*(?:ffl|ffi|ff|fi|fl|\s)+\{\s*z(?:\s*(?:ffl|ffi|ff|fi|fl))*\s*\}", "", repaired)
    if repaired == text:
        return text, []
    return re.sub(r"\s+", " ", repaired).strip(), ["formula_underbrace_artifact_repaired"]


def _underbrace_artifact_replacement(match: re.Match[str]) -> str:
    label = match.group("label") or ""
    for source, replacement in _PDF_LIGATURE_REPLACEMENTS.items():
        label = label.replace(source, replacement)
    label = re.sub(r"(?:ffl|ffi|ff|fi|fl|\s)+", "", label)
    label = label.strip()
    return f"_{{{label}}}" if label else ""


def repair_corrupt_formula_slash_glyphs(
    text: str,
    *,
    source_text: str = "",
) -> tuple[str, list[str]]:
    flags: list[str] = []
    candidate = text
    marker_text = f"{source_text} {text}"
    if not _RAW_CORRUPTION_MARKER.search(marker_text) or "=" not in candidate:
        return candidate, flags

    repaired = re.sub(
        r"@([A-Za-z][A-Za-z0-9_']*)\s*=\s*@([A-Za-z][A-Za-z0-9_']*)",
        r"@\1 / @\2",
        candidate,
    )
    repaired = re.sub(
        r"\b([fqmn]\s*0\s*[sn])\s*=\s*([A-Za-z][A-Za-z0-9_']{0,5})\b",
        r"\1 / \2",
        repaired,
    )

    def replace_compact_slash(match: re.Match[str]) -> str:
        left, right = re.split(r"\s*=\s*", match.group(0), maxsplit=1)
        return f"{left} / {right}"

    repaired = re.sub(
        r"\b[a-z][a-z0-9_']{1,5}\s*=\s*[a-z][a-z0-9_']{1,5}\b",
        replace_compact_slash,
        repaired,
    )
    if repaired != candidate:
        flags.append("formula_slash_glyph_repaired")
    return repaired, flags


def repair_common_pdf_math_degradation(text: str) -> tuple[str, list[str]]:
    repaired = text
    flags: list[str] = []

    def replace_integral_domain(match: re.Match[str]) -> str:
        exponent = match.group("exponent")
        if exponent:
            return rf"\int_{{\mathbb{{R}}^{{{exponent}}}}}"
        return r"\int_{\mathbb{R}}"

    repaired = re.sub(
        r"\bZ\s+I\s*R(?:[_^]\{?(?P<exponent>[A-Za-z0-9+\-/]+)\}?)?",
        replace_integral_domain,
        repaired,
    )

    def replace_real_space(match: re.Match[str]) -> str:
        exponent = match.group("exponent")
        if exponent:
            return rf"\mathbb{{R}}^{{{exponent}}}"
        return r"\mathbb{R}"

    repaired = re.sub(
        r"\bI\s*R(?:\s*(?:\^|_)\s*\{?(?P<exponent>[A-Za-z0-9+\-/]+)\}?)?",
        replace_real_space,
        repaired,
    )
    repaired = re.sub(
        r"\bZ\s+(?=(?:\\?[A-Za-zα-ωΑ-Ω])[^=]{0,64}(?:\(|d[A-Za-z]|\\,d))",
        r"\\int ",
        repaired,
    )
    if repaired != text:
        flags.append("formula_pdf_math_degradation_repaired")
    return repaired, flags


def repair_compact_partial_derivatives(text: str) -> tuple[str, list[str]]:
    repaired = text
    flags: list[str] = []

    def replace_plain(match: re.Match[str]) -> str:
        variable = match.group("variable")
        target = match.group("target")
        return rf"\partial_{{{variable}}} {target}"

    repaired = re.sub(
        r"\\partial\s+(?P<variable>[txyvz])(?P<target>[A-Za-z])\b",
        replace_plain,
        repaired,
    )

    def replace_command(match: re.Match[str]) -> str:
        variable = match.group("variable")
        target = match.group("target")
        return rf"\partial_{{\{variable}}} {target}"

    repaired = re.sub(
        r"\\partial\s+\\(?P<variable>alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|kappa|lambda|mu|nu|xi|pi|rho|varrho|sigma|tau|phi|varphi|chi|psi|omega)\s*(?P<target>[A-Za-z])\b",
        replace_command,
        repaired,
    )
    if repaired != text:
        flags.append("formula_compact_partial_repaired")
    return repaired, flags


def format_compact_formula_variables(text: str) -> tuple[str, list[str]]:
    formatted = text
    formatted = re.sub(r"\b([fqmn])\s*0\s*([sn])\b", r"\1'_\2", formatted)
    formatted = re.sub(r"\b([fqmn])0([sn])\b", r"\1'_\2", formatted)
    formatted = re.sub(r"\b([fqmn])([sn])\b", r"\1_\2", formatted)
    formatted = re.sub(r"\bk([23])\b", r"k^\1", formatted)
    flags = ["formula_compact_variable_repaired"] if formatted != text else []
    return formatted, flags


def balance_latex_delimiters(text: str) -> tuple[str, list[str]]:
    flags: list[str] = []
    balanced, trimmed_dangling_script = _trim_dangling_script_operator(text)
    if trimmed_dangling_script:
        flags.append("formula_dangling_script_trimmed")
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


def _trim_formula_tail(text: str) -> str:
    trimmed = text.strip(" \t\n\r,;。；")
    prose_match = _FORMULA_PROSE_FRAGMENT.search(trimmed)
    if prose_match:
        trimmed = trimmed[: prose_match.start()].rstrip(" \t\n\r,;。；")
    return trimmed


def _strip_hard_noise_sequences(text: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f]+", " ", text)
    cleaned = re.sub(r"\b(?:coll|dx|vn|n)\b(?=\s*[,:;)]|$)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _join_hyphen_split_words(text: str) -> str:
    previous = None
    joined = text
    while previous != joined:
        previous = joined
        joined = _HYPHEN_SPLIT_WORD.sub(r"\1\2", joined)
    return joined


def _strip_sentence_period(text: str) -> str:
    candidate = text.rstrip()
    if not candidate.endswith("."):
        return text
    if re.search(
        r"(?:=|≤|≥|<|>|≠|≈|\\(?:le|leq|ge|geq|ne|neq|approx|sim))\s*"
        r"[A-Za-z0-9α-ωΑ-Ω]+[)\]}]*\.$",
        candidate,
    ):
        return candidate[:-1].rstrip()
    if not re.search(r"\d\.$", candidate):
        return candidate[:-1].rstrip()
    return text


def _extract_equation_number(text: str) -> str | None:
    match = _EQUATION_NUMBER_SUFFIX.search(text)
    if match is not None:
        return match.group(1)
    match = _EQUATION_NUMBER_WITH_SHORT_TAIL.search(text)
    if match is None:
        return None
    tail = match.group("tail").strip()
    if re.search(r"\b(?:and|as|for|from|is|represents?|the|where|with)\b", tail, re.IGNORECASE):
        return None
    if len(re.findall(r"[A-Za-z]{3,}", tail)) > 1:
        return None
    return match.group(1)


def _strip_equation_number(text: str, equation_number: str) -> str:
    stripped = _EQUATION_NUMBER_SUFFIX.sub("", text).rstrip(" ,;:")
    if stripped != text:
        return stripped
    pattern = re.compile(r"(?:[,;:]\s*)?" + re.escape(equation_number))
    match = None
    for candidate in pattern.finditer(text):
        tail = text[candidate.end() :].strip()
        if not tail or (
            len(tail) <= 24
            and re.fullmatch(r"[A-Za-z0-9α-ωΑ-Ω_{}^\\\\'+\-*/=:.,\s]+", tail)
            and not re.search(
                r"\b(?:and|as|for|from|is|represents?|the|where|with)\b",
                tail,
                re.IGNORECASE,
            )
        ):
            match = candidate
    if match is None:
        return text
    return (text[: match.start()] + text[match.end() :]).rstrip(" ,;:")


def _trim_dangling_script_operator(text: str) -> tuple[str, bool]:
    stripped = _TRAILING_SCRIPT_OPERATOR.sub("", text.rstrip())
    return stripped, stripped != text.rstrip()
