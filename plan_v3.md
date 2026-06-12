## What I verified in the live preview

I measured every formula block in Chromium:

| Formula | Box height | Real content height | Clipped | KaTeX `\tag` | Extra number span |
|---|---|---|---|---|---|
| (1) | 27.1px | 53px | **25.9px (half is invisible)** | "(1)" at y=16 | "(1)" at y=3, same x |
| (2) | 45.4px | 65px | 19.6px | "(2)" at y=16 | "(2)" at y=12, same x |
| (3) | 45.4px | 65px | 19.6px | none (number is inside the LaTeX body) | "(3)" |
| (4) | 88.6px | 89px | ~0 | none (number inside body) | "(4)" |

## Problem 1 — formulas in paragraphs are not rendered

There are three separate causes.

**1a. Many math fragments are never detected.** The parser ([parser.py:290](services/api/app/pipeline/parser.py:290), `_format_line_text`) converts small sub/superscript spans into TeX-style markers, so paragraph text contains `n_{e}`, `x_{1}/x_{k}`, `m_{s}`, and citation superscripts like `50^{–54}`. Only the spans that the detector wraps as formulas get KaTeX rendering. Everything else passes through the translator and renderer **as literal text**. There is no fallback that turns leftover `_{...}` / `^{...}` markers into `<sub>/<sup>`.

**1b. One "formula" is actually English prose with a broken boundary.** Formula `Fe26c1119d120` has latex `"s = (e, i) rep- resents either electrons or ions, q_"` and falls back to plaintext (`formula-plaintext-fallback`). The chain of failures, all in [formula_processing.py](services/api/app/pipeline/formula_processing.py):
- `_INLINE_PATTERNS[2]` ([line 68](services/api/app/pipeline/formula_processing.py:68)) is `[A-Za-zα-ωΑ-Ω][A-Za-z0-9_]*\s*=\s*[A-Za-z0-9...\s]+` — its character class includes `\s , . ( ) -`, so after `s =` it greedily swallows the whole English clause and only stops at `{` of `q_{s}`. That is exactly why `q_` is inside the formula and `{s}` is left behind in the paragraph ("`[FORMULA]{s} 和 m_{s} 分别是...`").
- The trim step `truncate_raw_at_language_boundary` should cut at words like "represents", but the word is hyphen-split across a line break ("rep- resents"), so `_NATURAL_LANGUAGE_BOUNDARY` does not match.
- `_looks_like_formula` ([line 742](services/api/app/pipeline/formula_processing.py:742)) detects natural language but still accepts the text because it counts 2 math signals (`=` and the `_` in `q_`).

**1c. The span-run detector cuts tokens in the middle.** Inline formula `p0001_formula_9e9f7d96dc97` has latex `\alpha [w] = \alpha [E_]` and the paragraph still shows the leftover `{e}] = α[u_{e}] = 0`. In [detector.py:645](services/api/app/pipeline/formulas/detector.py:645), a run of "math-looking" spans ends when a span fails `_span_looks_math` — a plain subscript span like "e" fails it, so the run ends at `E_`. Then `balance_latex_delimiters` ([normalization.py:284](services/api/app/pipeline/formulas/normalization.py:284)) "repairs" the cut by appending `]`, producing the nonsense `E_]`.

## Problem 2 — equation numbers repeat and sit on top of each other

Two number mechanisms run at the same time and do not know about each other:

- The pipeline extracts a trailing "(1)" and puts it **inside the LaTeX** as `\tag{1}` ([normalization.py:220-234](services/api/app/pipeline/formulas/normalization.py:220)). KaTeX renders this tag right-aligned.
- The renderer's GB/T numbering ([models.py:1579-1588](packages/renderer/pdf_renderer/models.py:1579)) checks `_SOURCE_EQUATION_NUMBER_PATTERN.search(text)` to see if the source already has a number. But for formula blocks, `text` is just the placeholder `{{formula:...}}` — the number lives in the formula's LaTeX, not in the text. So the check never matches, the counter assigns a new "(1)", and the template ([document.html.j2:338](packages/renderer/pdf_renderer/templates/document.html.j2:338)) appends a second `.formula-equation-number` span at `right:0` — the same corner as the KaTeX tag. Result: two "(1)" stacked together.
- For (3) and (4) the duplication path is different: the source number sits **mid-LaTeX** (e.g. `... d \Omega , (3) v_{n}`) because the formula-fragment cluster merge appended trailing tokens (the integral's `v_{n}` limit) after the number. `_EQUATION_NUMBER_SUFFIX` ([normalization.py:19](services/api/app/pipeline/formulas/normalization.py:19)) is end-anchored, so it cannot extract it. The number stays in the body and GB/T adds another one.

## Problem 3 — part of formula (1) disappeared

Two stacked causes:

- **KaTeX's own CSS is not overridden.** The renderer inlines `katex.min.css`, which contains `.katex-display{margin:1em 0}`. The template re-declares `display/width/text-align` for `.katex-display` ([document.html.j2:164-168](packages/renderer/pdf_renderer/templates/document.html.j2:164)) but **not margin**. So every display formula is pushed down 16px inside a fixed-height block with `overflow: hidden` ([document.html.j2:45](packages/renderer/pdf_renderer/templates/document.html.j2:45)). Measured `margin-top: 16px` on all four formulas. This alone explains almost all of the clipping for (2) and (3) (19.6px clipped ≈ 16px margin).
- **The height estimate is too small.** `_estimated_formula_aware_height` ([models.py:604](packages/renderer/pdf_renderer/models.py:604)) gives formula (1) exactly `1.15 lines × 12pt × 1.35 + 12pt × 0.14em = 20.31pt` (the constants at [models.py:37-45](packages/renderer/pdf_renderer/models.py:37)). The real KaTeX content is ~40pt: tall constructs (`\frac`, `\int`) produce struts up to 2.74em, and the estimate has no idea about that. The block even gets flagged `formula_height_risk` — the renderer knows it is risky but does not expand the box.

---

## Fix plan (ordered by impact, smallest risk first)

**Step 1 — stop the clipping (renderer only, fixes existing documents on re-render).**
In [document.html.j2](packages/renderer/pdf_renderer/templates/document.html.j2), add `margin: 0.18em 0;` (or `0`) to the `.katex-display` override block. This recovers 2em of dead space per formula.

**Step 2 — make box heights honest (renderer).**
In [models.py](packages/renderer/pdf_renderer/models.py), improve `_estimated_formula_aware_height`: scan the formula LaTeX for tall constructs (`\frac`, `\int`, `\sum`, nested `^{}/_{}`) and use a per-construct line factor (e.g. base 1.45em, fraction/integral ≈ 2.9em) instead of the flat `_FORMULA_LIKE_MIN_LINES = 1.15`. Safer long-term option: since `_katex_html` already renders the real KaTeX HTML at layout time, parse the max strut `height`/`vertical-align` values out of that HTML and compute the true height — no guessing. Also: when `formula_height_risk` is set, grow the box instead of only flagging it.

**Step 3 — one source of truth for equation numbers (renderer).**
In [models.py:1579](packages/renderer/pdf_renderer/models.py:1579), before assigning a GB/T number, check the referenced formulas, not just the block text: skip numbering if the formula LaTeX contains `\tag{` (helper already exists in [validation.py:255](services/api/app/pipeline/formulas/validation.py:255)), or carries the `formula_equation_number_preserved` flag, or its latex/source_text contains a parenthesized number `(\d+)` anywhere. Decide one renderer: either keep `\tag` and drop the `.formula-equation-number` span for that block, or strip `\tag` and always use the span. The span is the better single mechanism because it also covers (3)/(4)-style cases and keeps GB/T styling consistent.

**Step 4 — fix mid-LaTeX numbers for (3)/(4) (pipeline).**
In [normalization.py](services/api/app/pipeline/formulas/normalization.py), extend `_extract_equation_number` to also find `(N)` that is followed only by short trailing junk tokens (like `v_{n}`), and strip it from the body. Separately, fix the fragment-cluster merge order in [formula_processing.py](services/api/app/pipeline/formula_processing.py) so integral-limit tokens are not appended after the equation number.

**Step 5 — fix inline detection boundaries (pipeline).**
- [formula_processing.py:68](services/api/app/pipeline/formula_processing.py:68): tighten `_INLINE_PATTERNS[2]` — do not allow unlimited `\s` and `,` runs; cap the number of plain English words; add `{}` to the right-side class so `q_{s}` is captured whole, never split at `{`.
- `_looks_like_formula`: reject candidates where `_LONG_WORD_SEQUENCE` matches (3+ real words) regardless of signal count, or raise the signal threshold for such text.
- `truncate_raw_at_language_boundary`: also cut at `_LONG_WORD_SEQUENCE`, and de-hyphenate line-broken words ("rep- resents" → "represents") before testing the boundary regex.
- [detector.py:645](services/api/app/pipeline/formulas/detector.py:645): never end a span run on a dangling `_` or `^` — extend the run while the run text ends with `_`/`^` or the next character is a `{...}` group. In normalization, trim a trailing `_`/`^` instead of letting `balance_latex_delimiters` fabricate `E_]`.

**Step 6 — safety net for leftover markers (renderer).**
Add a post-pass for non-formula text: convert remaining literal `X_{...}` / `X^{...}` markers into `<sub>/<sup>` HTML (citation superscripts like `50^{–54}` become a plain superscript). This guarantees readers never see raw markers, even when detection misses something.

**Verification.**
Re-run the pipeline for this document, then re-run my Playwright measurement script and assert: `scrollHeight <= clientHeight + 1` for every formula block; exactly one "(N)" per display formula; zero literal `_{`/`^{` outside formula spans; no `formula-plaintext-fallback` containing 3+ English words. Add matching unit tests in [test_renderer.py](packages/renderer/tests/test_renderer.py) and [test_formulas.py](services/api/tests/test_formulas.py) using the exact strings from this document (`q_{s}` split, `\alpha [E_]`, `\tag{1}` + GB/T).

One note: Steps 1–3 fix existing documents on re-render. Steps 4–5 only affect new pipeline runs, so this document needs reprocessing to see those fixes.