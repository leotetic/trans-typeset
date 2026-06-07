DEFAULT_TARGET_LANG = "zh-CN"

DEFAULT_RENDER_DEFAULTS = {
    "target_lang": DEFAULT_TARGET_LANG,
    "font_stack": ["Noto Sans CJK SC", "Source Han Sans SC", "Arial Unicode MS", "sans-serif"],
    "line_height": 1.35,
    "paragraph_spacing_em": 0.45,
    "alignment": {
        "title": "center",
        "abstract": "justify",
        "heading": "left",
        "paragraph": "justify",
        "caption": "center",
        "formula": "center",
        "table": "center",
        "figure": "center",
        "footnote": "justify",
        "reference": "left",
        "unknown": "left",
    },
    "overflow_policy": {
        "strategy": "scale_then_expand_then_continue",
        "min_font_scale": 0.86,
        "max_font_scale": 1.0,
        "allow_box_expansion": True,
        "allow_continuation_page": True,
    },
    "preserve_policy": {
        "formulas": "preserve",
        "citations": "preserve",
        "reference_markers": "preserve",
        "figure_table_assets": "preserve",
        "whitespace": "allow_reflow",
        "line_breaks": "allow_reflow",
    },
}
