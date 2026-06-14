# Browser Layout Validation

The renderer treats Chromium layout as the final validation pass for preview and
PDF output. Python layout estimates still create the first candidate
`RenderDocument`, but the browser measurement pass checks every rendered block
for real DOM overflow before `render-evaluation.json` is accepted.

## Flow

1. Build candidate pages from `DocumentIR + TranslationLayoutPlan + RenderDefaults`.
2. Render candidate HTML.
3. Measure `.block` elements in Chromium with `scrollHeight/clientHeight` and
   `scrollWidth/clientWidth`.
4. Convert height overflow from CSS pixels to points and add a small safety
   buffer.
5. Rebuild continuous reflow pages with measured minimum heights for the
   affected block signatures.
6. Repeat up to three iterations, then write final preview HTML, layout trace,
   renderer diagnostics, and render evaluation.

The browser pass is renderer-owned. It does not add coordinates, page IDs, or
bounding boxes to LLM contracts, and model output still cannot override page
placement.

## Diagnostics

`renderer-diagnostics.json` includes:

- `browser_validation.status`: `passed`, `failed`, or `unavailable`.
- `browser_block_overflow_count` and `browser_overflows`.
- `browser_figure_group_issue_count` and `browser_figure_group_issues`.
- `browser_validation_unavailable` when Playwright or Chromium cannot run.
- `layout_iterations`, one record per measure-and-reflow attempt.
- Page utilization ratios for text, assets, combined area, and bottom whitespace.

`render-evaluation.json` rejects browser overflow with `browser_overflow`. If
browser validation is unavailable, artifacts are still produced, but
`accepted=false` and `manual_action_required=true` make the degraded result
visible instead of silently passing.

## Figure Placement

Continuous reflow keeps figure and caption groups together when possible. If a
figure group barely misses the remaining page height, it is deferred while later
text can backfill the current page. Deferred figures are placed at a later page
top or bottom, and large figure assets may be scaled within the renderer before
the caption is considered for continuation flags.
