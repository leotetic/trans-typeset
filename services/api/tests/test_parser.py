from app.pipeline.parser import _reading_sort_key, _stable_block_id, classify_role
from pdf_translator_schema import BlockRole


def test_classify_role_is_testable_for_common_pdf_blocks() -> None:
    assert (
        classify_role("Paper Title", page_index=0, block_index=0, font_size=18)
        == BlockRole.TITLE
    )
    assert (
        classify_role("Abstract", page_index=0, block_index=1, font_size=11)
        == BlockRole.ABSTRACT
    )
    assert (
        classify_role("1 Introduction", page_index=0, block_index=2, font_size=12)
        == BlockRole.HEADING
    )
    assert (
        classify_role("Fig. 2. Result overview.", page_index=1, block_index=0, font_size=9)
        == BlockRole.CAPTION
    )
    assert (
        classify_role("[1] Smith, A. 2024.", page_index=5, block_index=0, font_size=9)
        == BlockRole.REFERENCE
    )
    assert (
        classify_role("x = y + 1", page_index=1, block_index=2, font_size=10)
        == BlockRole.FORMULA
    )


def test_stable_block_id_is_independent_of_extraction_index() -> None:
    first = _stable_block_id("p0001", "Alpha beta", (10.0, 20.0, 100.0, 40.0))
    second = _stable_block_id("p0001", "Alpha   beta", (10.02, 20.01, 100.02, 40.01))

    assert first == second


def test_reading_sort_key_is_top_to_bottom_then_left_to_right() -> None:
    blocks = [
        {"bbox": (200, 20, 250, 40)},
        {"bbox": (10, 80, 150, 100)},
        {"bbox": (10, 20, 150, 40)},
    ]

    ordered = sorted(blocks, key=_reading_sort_key)

    assert ordered == [blocks[2], blocks[0], blocks[1]]
