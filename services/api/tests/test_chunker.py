from app.pipeline.chunker import build_chunks, extract_preserve_tokens
from pdf_translator_schema import BlockRole, BoundingBox, DocumentIR, DocumentPage, PageSize
from pdf_translator_schema.models import DocumentBlock


def make_block(
    block_id: str,
    text: str,
    role: BlockRole = BlockRole.PARAGRAPH,
    reading_order: int = 0,
) -> DocumentBlock:
    return DocumentBlock(
        block_id=block_id,
        page_id="p1",
        role=role,
        bbox=BoundingBox(x0=0, y0=reading_order * 20, x1=100, y1=reading_order * 20 + 10),
        reading_order=reading_order,
        source_text=text,
    )


def make_document(blocks: list[DocumentBlock]) -> DocumentIR:
    return DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=blocks,
            )
        ],
    )


def test_extract_preserve_tokens() -> None:
    tokens = extract_preserve_tokens("See Fig. 2 and Smith et al., 2024 in [1, 2].")
    assert "Fig. 2" in tokens
    assert "Smith et al., 2024" in tokens
    assert "[1, 2]" in tokens


def test_extract_preserve_tokens_keeps_citation_formula_and_reference_markers() -> None:
    text = "As shown in Eq. 2, y = f(x) and (Smith, 2024) support Fig. 3 [7; 9]."
    tokens = extract_preserve_tokens(text)

    assert tokens == ["Eq. 2", "y = f(x)", "(Smith, 2024)", "Fig. 3", "[7; 9]"]


def test_build_chunks_keeps_block_ids_and_tokens() -> None:
    block = make_block("p1_b1", "This is a paragraph with [3].")
    document = make_document([block])

    chunks = build_chunks(document, target_lang="zh-CN")

    assert chunks[0].source_blocks[0].block_id == "p1_b1"
    assert chunks[0].source_blocks[0].preserve_tokens == ["[3]"]


def test_build_chunks_respects_max_chars_between_blocks() -> None:
    document = make_document(
        [
            make_block("p1_b1", "a" * 6, reading_order=0),
            make_block("p1_b2", "b" * 6, reading_order=1),
            make_block("p1_b3", "c" * 6, reading_order=2),
        ]
    )

    chunks = build_chunks(document, target_lang="zh-CN", max_chars=13)

    assert [[block.block_id for block in chunk.source_blocks] for chunk in chunks] == [
        ["p1_b1", "p1_b2"],
        ["p1_b3"],
    ]


def test_build_chunks_nearby_titles_uses_previous_titles_in_reading_order() -> None:
    document = make_document(
        [
            make_block("title", "Main Title", BlockRole.TITLE, reading_order=0),
            make_block("intro", "1 Introduction", BlockRole.HEADING, reading_order=1),
            make_block("body", "Body with [1].", BlockRole.PARAGRAPH, reading_order=2),
        ]
    )

    chunks = build_chunks(document, target_lang="zh-CN")

    body = chunks[0].source_blocks[2]
    assert body.nearby_titles == ["Main Title", "1 Introduction"]
