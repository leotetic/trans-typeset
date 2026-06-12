Problem 1 — too much blank around formulas
Measured dead space inside each formula box (content sits at top, rest is empty): 11px, 23px, 16px, and 48px. Plus a ~110px empty band at the bottom of page 1. Four causes, all in models.py:

The strut parser double-counts depth. _height_from_katex_html (line 785) returns height + |vertical-align|. But in KaTeX output, the strut's height already includes the part below the baseline; vertical-align only shifts it. I verified this: formula (2) has strut height:2.0574em, and its real rendered height is exactly 2.0574em (32.9px) — the code computes 2.74em.
The heuristic overrides the real measurement. _formula_rendered_or_heuristic_height (line 776) returns max(rendered, heuristic). So even when the true KaTeX height is known, the heuristic floors win: formula (1) is really 1.44em tall, but script_count >= 3 forces 1.9em (line 802) → 8.5pt of blank.
LaTeX length is treated like prose lines. display_lines = max(min_lines, estimated_formula_lines) wraps the raw LaTeX character count into text lines. Formula (4) has ~230 chars of LaTeX → "4 lines" → a 66.48pt box for a one-line, ~30pt formula. That is the 48px hole. KaTeX never wraps a display formula, so character count says nothing about height.
Formula-bearing paragraphs can never split. At line 1849, fragments = [text] if html is not None else _split_reflow_text(...). The paragraph after formula (3) holds 5 formula spans → html path → unsplittable. It is 126pt tall, only ~83pt remained on page 1, so the whole block jumped to page 2, leaving the bottom of page 1 empty. The next paragraph is 360pt tall with 8 formulas — same risk, bigger hole.
Side effect: because the box is too tall and content sits at the top, the .formula-equation-number span (at top:50%) now floats visibly below the formula line.

Problem 2 — last formula numbered "(1)"
The document shows (1), (2), (3), (1). Two bugs combine:

Formulas 1–3 take the new "source number preserved" branch. Formula 4 does not: _extract_source_equation_number (line 368) finds (4) mid-LaTeX, but rejects it because the tail after it — = X f ' s k^{2} ... — contains = and ', which _SHORT_EQUATION_TAIL_PATTERN (line 62) does not allow. I reproduced this exactly with the real LaTeX string.
So the GB/T fallback runs, and formula_counter (line 1810) is blind to the preserved numbers — it starts at 0 and assigns "(1)".
Bonus bug: since (4) was not extracted, it is still inside the LaTeX, so KaTeX renders "(4)" inside formula 4 while the span shows "(1)" at the right — the number is both wrong and doubled.
Root cause behind all of it: the fragment-cluster merge still builds formula 4's LaTeX with the equation number mid-body and the second half of the equation after it.
Problem 3 — leftover ^{3} and ^{50–54} in paragraphs
The new _non_formula_text_html converter (line 281) works — f_{e} → f<sub>e</sub> is in the output. But _TEXT_SCRIPT_MARKER_PATTERN (line 63) requires a Latin/Greek/digit base character immediately before ^{/_{. Three cases fail:

^{3} sits at the start of a text segment, right after an inline formula span (the base d of d^{3}v was swallowed into the formula span — the old boundary-split bug again). No base in the segment → skipped.
Citation superscripts ^{50–54}, ^{55,56}, ^{63}, ^{71} follow CJK characters or 。, which are not in the base class → skipped.
The old E_ / {e}] split is also still visible in block 1 — the detector boundary fix did not cover this case.
Fix plan
Step 1 — trust the real KaTeX height (kills the blank space).
In models.py: (a) fix _height_from_katex_html to use max(strut height) only, without adding |vertical-align|; (b) in _formula_rendered_or_heuristic_height, when a rendered height exists, return it plus a small padding (~0.15em) — use the heuristic only when KaTeX is unavailable; (c) in _estimated_formula_aware_height, for formula-only blocks, make the visual height the authoritative value (a cap, not just a floor) — drop the prose-line estimate of LaTeX length there. Expected result from my measurements: boxes within ~2px of content.

Step 2 — fix the numbering.
(a) Widen _SHORT_EQUATION_TAIL_PATTERN to accept =, ', : so the mid-body (4) is recognized — and when accepted, also strip it from the rendered LaTeX (today only \tag is stripped, so the inline "(4)" would remain). (b) Make formula_counter sequence-aware: track the numeric values of preserved numbers and continue from the maximum (after (1),(2),(3) the next GB/T number is (4)). (c) Pipeline side, fix the same tail-allowlist in _EQUATION_NUMBER_WITH_SHORT_TAIL in formula_processing.py so the number is extracted at normalization time; longer term, fix the cluster merge so the equation number token always lands at the end.

Step 3 — finish the script-marker converter.
Make the base group optional in _TEXT_SCRIPT_MARKER_PATTERN: a bare ^{...}/_{...} with no base becomes a plain <sup>/<sub>. This fixes both the CJK-preceded citation superscripts and the segment-start ^{3}. Separately, in the detector, never end an inline formula span between a base character and its following ^{/_{ group (covers d + ^{3} and E_ + {e}]).

Step 4 — let formula-bearing paragraphs split.
Implement splitting for the html path: cut the text at {{formula:...}} boundaries (segments between refs are plain text and safe to split), render html per fragment. This removes the page-bottom holes and protects against the 360pt mega-paragraph.

Verification.
Re-run the pipeline, then re-run my Playwright script asserting: dead space below formula content ≤ ~6px per block; number sequence equals (1),(2),(3),(4) with no digit-in-parentheses inside the KaTeX output when the span number exists; zero literal ^{/_{ in visible text; trailing blank on each non-final page < 90pt.

Suggested order: Step 1 and Step 3 are small, renderer-only, and fix the most visible issues on a simple re-render. Step 2 needs renderer + pipeline. Step 4 is the largest change and can go last.