Diagnosis: 3 problems in doc_f5b59615ea064d9c8880241b407e9b94

Since the server is closed, I read the saved run artifacts in data/outputs/doc_f5b59615ea064d9c8880241b407e9b94/ (preview.html, formula-candidates.json, formula-recognition.json, ocr-recognition.json, translation-plans.json, renderer-diagnostics.json, etc.) plus the source code.

A quick page-id note: PDF page p0005 → rendered page r0009, and p0010 → r0016. So "page 9" and "page 16" in the preview map to PDF pages 5 and 10.

---
Problem 1 — Bottom of the last line before the first formula is clipped (Renderer)

What I confirmed. The first display formula is equation (1) G(x₁,t₁)=k^{α[G]}G(xₖ,tₖ) on page 3. The text block just before it gets a fixed height (--h-pt) that is slightly too small, and the .block CSS uses overflow: hidden, so the descenders of the last line are cut off.

Root cause — your two uncommitted edits in packages/renderer/pdf_renderer/models.py. git diff shows the last-turn "optimize formula render" change made formula height estimates smaller in two places:

1. _formula_rendered_or_heuristic_height (line 808): changed
return max(rendered_height, _formula_latex_heuristic_height(...))
→ return rendered_height + font_size_pt * 0.15.
This removed the heuristic safety net (the max(...)).
2. _height_from_katex_html (lines 812–820): removed the depth_em term
(max_em = max(max_em, height_em + depth_em) → max_em = max(max_em, height_em)).
depth_em was the descender depth (the negative vertical-align of KaTeX struts, ~0.25em). Dropping it makes every formula height ~0.25em too short.

These smaller heights flow into the continuous-reflow height estimate for the block, so the block is given less vertical space than its text needs, and overflow: hidden clips the last line. The committed version (379992b) still had depth_em; your WIP removed it.

Fix plan.
- In _height_from_katex_html, restore the depth_em calculation so descenders are counted again.
- In _formula_rendered_or_heuristic_height, do not just stack +0.15*font on the now-correct height — that risks over-counting. Either restore max(rendered_height, heuristic), or keep the heuristic as the floor and drop the extra +0.15 padding. Pick one descender source, not both.
- Re-render page 3 and confirm the last line before equation (1) is whole. The existing renderer tests in tests/test_renderer.py (also WIP-modified) should be extended with a "no clipping of text line before a display formula" case.

---
Problem 2 — Page 9 keeps English (no translation) (Translator / validation)

What I confirmed. On page 9, two body blocks are still English: p0005_b0914fac3f211 ("… of the oscillations. Furthermore, by considering the criterion…") and p0005_badeda54384ef ("occur when the critical wavelength is smaller than the discharge gap length…"). In translation-plans.json both blocks have 0 Chinese characters and empty quality_flags — they passed through completely silently.

Root cause — there is no target-language check anywhere in the validate/repair path.
- translator.py:263-283 _validate_or_repair_content: on line 273 it calls validate_layout_plan(...) and returns immediately if it passes. _repair_payload (which is the only place that adds a missing_translation fallback flag) runs only inside the except (line 274), i.e. only when validation fails.
- validate_layout_plan (packages/schema/pdf_translator_schema/validation.py:20) only checks structure: block IDs present, all expected blocks covered, preserve_tokens kept. It does not check that the text is in the target language.
- So an English answer that keeps the block IDs and formula tokens is "valid", _repair_payload is never called, no flag is set, and the renderer prints the English as-is. This also violates the project rule in AGENTS.md ("missing translation may fall back to source text but must emit a quality flag").

Contributing cause. Both untranslated blocks start with raw inline math that was never turned into a formula token (e.g. = ν_{iz} - Te μ_{eB2} k^{2} w …). A block that begins with math noise makes the model treat the whole block as non-translatable and echo it. This is the same weak text-layer formula handling behind Problem 3.

Fix plan.
- Add a target-language check before line 273 in _validate_or_repair_content (or inside validate_layout_plan). For requires_translation=true blocks where target_lang starts with zh, require at least some Han characters (Unicode 0x4E00–0x9FFF). Use a generous threshold (e.g. flag only if Han ratio is near zero) so legitimate Chinese-with-English-citations is not flagged. Skip blocks whose source is itself non-prose (citation/date/figure), like p0005_be88672aba9d8 = "25 April 2025…".
- When a block fails the check: first retry the translation (the translator already has retry attempts); if it still comes back untranslated, fall back to source with a missing_translation quality flag so the UI can show it. Today it falls back to nothing and flags nothing.
- Secondary: improve inline-formula tokenization (Problem 3 fix) so these blocks reach the model as "prose + formula tokens", which removes the trigger that makes the model echo English.

---
Problem 3 — ﬄﬄﬄﬄ…{z…} garbage in equation (11) (Formula OCR)

What I confirmed. Equation (11) on page 16 should be a row of \underbrace{X}_{n}. It renders as J·E |{z} 1 = … |ﬄﬄ{zﬄﬄ} 3 - … |ﬄﬄﬄﬄﬄﬄﬄ{zﬄﬄﬄﬄﬄﬄﬄ} 4. The ﬄ is U+FB04 (the "ffl" ligature), and the count grows with brace width (2 → 7). This is the classic copy-paste artifact of \underbrace: the PDF text layer encodes the brace's down-stem as |, the gap as {z}, and the extensible brace fill as glyphs that the font's ToUnicode maps to U+FB04. The ﬄ are already present in the candidate's source_text (formula-candidates.json, p0010_formula_de88eb1bbb0d, source_kind: text_layer), so they come straight from the parser's text extraction.

There are two independent root causes (this is the part the automated agents only got half of — I traced both):

Cause 3A — Visual OCR (pix2text) is turned off, so every formula uses raw text-layer passthrough.
- ocr-recognition.json: all 12 display formulas were recognized by provider: deterministic with the flag ocr_text_layer_passthrough (added in ocr/providers.py:199). pix2text had 0 attempts.
- Reason: the persisted config data/config/runtime-config.json sets ocr_provider_order = ["deterministic"] — pix2text is explicitly disabled. The orchestrator wires pix2text (orchestrator.py:341,376) only if it is in that order, so it is never added.
- The diagnostics line "ocr_provider": {"name":"pix2text","status":"available"} is misleading: _formula_ocr_provider_status() (formula_processing.py:816-821) only checks that the pix2text package can be imported, not that it is actually used.
- Good news: I tested it — pix2text does initialize on your Python 3.14.5 and exposes recognize_formula. (runtime_config.py:184 only warns that 3.11/3.12 is recommended; it does not disable it.) So re-enabling it is viable.

Cause 3B — Even on the deterministic path, normalization does not clean the ligatures or the underbrace artifact. In formulas/normalization.py:
- PDF_TEXT_REPLACEMENTS (lines 39–50) has no entry for ﬄ/ﬁ/ﬂ/ﬀ, and there is no NFKC pass — so the ligatures survive untouched.
- _RAW_CORRUPTION_MARKER (line 25) does not include these ligatures, so formula_corruption_flags() returns nothing, the formula keeps a high confidence (0.92), and it is never marked for escalation.
- latex_from_pdf_text (lines 229–261) has no rule to detect the |{z} / |ﬄﬄ{zﬄﬄ} underbrace pattern and rebuild \underbrace{...}_{...}.

Fix plan.
- 3A (enable real OCR): set ocr_provider_order back to ["pix2text","deterministic"] (edit data/config/runtime-config.json, or via the config API/UI). Then re-run and confirm ocr-recognition.json shows pix2text attempts and clean LaTeX for the underbrace formulas. Also fix the misleading diagnostic: _formula_ocr_provider_status() should report the active provider order, not just "can import".
- 3B (robust cleanup, works even if pix2text is off): in normalization.py:
  - decompose/strip the ligatures (ﬀ→ff, ﬁ→fi, ﬂ→fl, ﬃ→ffi, ﬄ→ffl, or strip), applied only inside latex_from_pdf_text (formula context, not body text);
  - add a pattern that detects | … {z…} (with any number of ligature/space fillers) and rebuilds it as \underbrace{body}_{label} (or at minimum removes the artifact);
  - add the ligatures to the corruption markers so such formulas get formula_low_confidence and escalate to pix2text when it is available.
- This keeps the system safe in the deterministic-only mode (your current config) while letting pix2text give the real fix when enabled.

---
Shared insight and suggested order

Problems 2 and 3 share one upstream weakness: the deterministic text-layer formula path is fragile — it leaves raw math in prose blocks (feeds Problem 2) and copies PDF artifacts into LaTeX (Problem 3).

Recommended order:
1. Problem 1 (renderer height): smallest, self-contained, fixes a visible clip. Revert/repair your two WIP edits in models.py.
2. Problem 3 (formula): re-enable pix2text in runtime-config.json and add the normalization cleanup + fix the misleading provider status.
3. Problem 2 (translation): add target-language validation + retry/fallback-with-flag; it also benefits from the Problem 3 tokenization improvement.
