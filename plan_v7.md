## Internal Reasons
1. Text clipping is caused by estimator/browser mismatch.
The renderer creates absolutely positioned blocks with fixed height and overflow: hidden in [document.html.j2 (line 39)](/Users/leotetic/app/trans-typesetting/packages/renderer/pdf_renderer/templates/document.html.j2:39). Heights are estimated in Python, mostly by character-width heuristics and line_count * font_size * line_height in [models.py (line 684)](/Users/leotetic/app/trans-typesetting/packages/renderer/pdf_renderer/models.py:684).
But Playwright’s actual layout measured 17 real block overflows in pdf-export-diagnostics.json. The renderer diagnostics reported no layout_issues, because the pre-browser model thought the blocks fit. So the general problem is: the final browser layout is the source of truth, but current pagination trusts a heuristic that is slightly too tight for CJK text, superscripts/subscripts, inline formula spans, font fallback, browser rounding, and descenders.
Your page-3 symptom matches this class: output page r0003 has block overflow where actual scrollHeight > clientHeight, so the lower part of the final line gets clipped.
2. Blank space is caused by rigid inline figure-group pagination.
The previous fix correctly groups figure and caption, but now each figure+caption is treated as a hard indivisible chunk. In [models.py (line 1966)](/Users/leotetic/app/trans-typesetting/packages/renderer/pdf_renderer/models.py:1966), the renderer computes a full group_required_height; if it does not fit at the current cursor, it immediately calls finish_page() at [models.py (line 1979)](/Users/leotetic/app/trans-typesetting/packages/renderer/pdf_renderer/models.py:1979). There is no float queue, lookahead, backfill, or adaptive shrink step.
So the general problem is: figures are preserved as inline blocks in strict source order. When a large figure group barely misses the remaining page height, the previous page is left sparse instead of continuing with later paragraph text and placing the figure at a better top/bottom position.
## Concrete Fix Plan
1. Add a browser-layout validation pass as a first-class renderer gate.
Use the existing Playwright measurement from pdf-export-diagnostics.json.page.block_overflows. Any nonzero block overflow should produce overflow_clipped or a new browser_overflow flag, and render-evaluation.json should reject it. Current evaluation only checks renderer-side flags in [workflow.py (line 381)](/Users/leotetic/app/trans-typesetting/services/api/app/pipeline/workflow.py:381).

2. Make text height allocation intentionally browser-safe.
Increase non-formula reflow safety from the current tiny 0.6pt in [models.py (line 2369)](/Users/leotetic/app/trans-typesetting/packages/renderer/pdf_renderer/models.py:2369), likely to at least one descender/rounding buffer, for example max(2pt, 0.18em) plus extra for sup/sub, inline formula spans, and CJK fallback. Then rerun browser diagnostics to tune it against real scrollHeight.

3. Replace “estimate only” with “measure and repaginate” for final output.
Longer-term, render candidate pages in a hidden browser context, measure each block’s actual scrollHeight, then adjust block heights/page breaks before writing final preview.html/PDF. That makes Chromium, not the heuristic, the final layout oracle.

4. Change figure pagination from inline blocks to float placement.
Introduce a figure queue with policies: try current position, then page top, then page bottom, then shrink within a safe min scale, then defer while text backfills the remaining space. Keep figure+caption together, but do not force an immediate page break just because the current cursor cannot fit the whole group.

5. Improve diagnostics and tests.
Add renderer tests for CJK paragraph + superscript descender clipping, a browser-overflow integration fixture, and a figure/backfill fixture. Also change utilization diagnostics to include asset area and bottom whitespace, because current text_area_ratio under-reports pages dominated by figures.