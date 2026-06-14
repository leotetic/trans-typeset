Internal Reasons
The page 3 clipping is a renderer allocation bug. In layout-trace, output page r0003 block p0002_bf2e9125f85a2 is the second paragraph. It has estimated_lines: 4, but its bbox height is only 43.74pt. With compact paragraph style, one line is 10.8pt * 1.35 = 14.58pt, so 4 lines need 58.32pt. The renderer allocated room for only 3 lines, then the template clips it because .block uses fixed height and overflow: hidden in [document.html.j2 (line 39)](/Users/leotetic/app/trans-typesetting/packages/renderer/pdf_renderer/templates/document.html.j2:39).

The figure issue is not missing image files. pdf-export-diagnostics.json says images: 18 and incomplete_images: 0. The images load; they are placed wrongly.

Fig. 1 is separated from its caption. Source image p0004_aab59413d98f2 renders on output page r0006, but its caption block p0004_bc83d6dc01240 renders on r0005. Then Fig. 2 caption p0005_b1b6edbb21285 also appears on r0006, while the real Fig. 2 image p0005_af68942091b23 moves to r0007.

The root cause is the continuous reflow asset ordering. In [models.py (line 1771)](/Users/leotetic/app/trans-typesetting/packages/renderer/pdf_renderer/models.py:1771), the renderer flattens text blocks and assets into one stream. Assets get a guessed order from _asset_reading_order, then _append_reflow_asset centers them at the current cursor in [models.py (line 2385)](/Users/leotetic/app/trans-typesetting/packages/renderer/pdf_renderer/models.py:2385). There is no figure-group concept, so image + caption can be split across pages and interleaved with the next figure.

Diagnostics are too weak. Current PDF diagnostics only count pages/blocks/assets/images in [renderer.py (line 243)](/Users/leotetic/app/trans-typesetting/packages/renderer/pdf_renderer/renderer.py:243). They do not measure scrollHeight > clientHeight, so clipped text can pass with layout_issues: [].

Concrete Fix Plan
Add renderer regression tests first: one synthetic page with the page-3 “4 lines allocated into 3 lines” case, and one two-column Fig. 1/Fig. 2 case where image/caption must stay grouped.

Fix text height allocation in continuous reflow: compute required_height_pt from the same line-count model used in trace, add descender/sup/sub safety slack, and never silently clamp bbox.y1 with min(content_bottom, ...) unless a block is explicitly split or flagged.

Add a deterministic clipped-block diagnostic: record allocated_height_pt, required_height_pt, height_slack_pt, and emit overflow_clipped when allocated height is too small.

Build figure groups before reflow: associate raster image assets with nearest caption/FIG. block on the same source page, preserve image-before-caption order, and paginate the group as one unit when it fits.

Improve asset ordering: stop relying only on _asset_reading_order(page.blocks, asset). Use source page, column, caption proximity, and group order.

Handle vector-only figures separately: large kind="figure" assets with path: null should be rasterized/cropped by the parser or clearly reported as vector_asset_not_rasterized, not silently treated like preserved figures.

Extend diagnostics/export QA to detect figure_group_separated, asset_caption_mismatch, and browser-measured block overflow during PDF export.