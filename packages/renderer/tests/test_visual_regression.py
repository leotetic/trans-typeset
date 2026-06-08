from __future__ import annotations

from pdf_renderer import RenderDocument, render_to_html

from fixtures.visual_regression import build_visual_regression_fixture


def _normalized_layout_issues(
    issues: list[dict[str, object]],
) -> list[dict[str, object]]:
    return sorted(
        issues,
        key=lambda issue: (
            str(issue["kind"]),
            str(issue["page_id"]),
            str(issue["item_id"]),
            str(issue.get("other_id", "")),
        ),
    )


def test_visual_regression_fixture_detects_layout_risks() -> None:
    fixture = build_visual_regression_fixture()

    render_document = RenderDocument.from_ir_and_plans(
        fixture.document,
        fixture.plans,
        "zh-CN",
    )
    diagnostics = render_document.diagnostics()
    html = render_to_html(render_document)

    rendered_original_ids = {
        block.block_id
        for page in render_document.pages
        for block in page.blocks
        if "__cont_" not in block.block_id
    }

    assert fixture.source_block_ids <= rendered_original_ids
    assert diagnostics["doc_id"] == "visual_regression"
    assert diagnostics["target_lang"] == "zh-CN"
    assert diagnostics["page_count"] == fixture.expected_page_count
    assert diagnostics["quality_flag_counts"] == fixture.expected_quality_flag_counts
    assert _normalized_layout_issues(diagnostics["layout_issues"]) == (
        fixture.expected_layout_issues
    )

    for token in fixture.expected_html_tokens:
        assert token in html
