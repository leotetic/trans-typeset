from __future__ import annotations

import re

from pdf_translator_schema import (
    BlockRole,
    DocumentBlock,
    DocumentIR,
    RenderDefaults,
    SourceBlock,
    TranslationChunk,
    TranslationConstraints,
)

TOKEN_PATTERN = re.compile(
    r"("
    r"\[[0-9,\-\s;]+\]|"
    r"\([A-Z][A-Za-z'’\-]+(?:\s+(?:and|&)\s+[A-Z][A-Za-z'’\-]+|\s+et al\.?)?,?\s+\d{4}[a-z]?\)|"
    r"\b[A-Z][A-Za-z'’\-]+(?:\s+(?:and|&)\s+[A-Z][A-Za-z'’\-]+|\s+et al\.?)?,?\s+\d{4}[a-z]?\b|"
    r"\b[A-Z][A-Za-z'’\-]+\s+et al\.?,?\s+\(\d{4}[a-z]?\)|"
    r"\b[A-Z][A-Za-z'’\-]+\s+\(\d{4}[a-z]?\)|"
    r"\b(?:Eq|Equation|Fig|Figure|Table|Sec|Section|Appendix)\.?\s*[A-Z]?\d+(?:[.\-][A-Za-z0-9]+)*|"
    r"\([A-Za-z0-9+\-*/=<>≤≥∑∫^_.,\s]+\)|"
    r"\b[A-Za-z][A-Za-z0-9_]*\s*=\s*[A-Za-z0-9+\-*/^_().]+"
    r")"
)
TITLE_ROLES = {BlockRole.TITLE, BlockRole.HEADING}


def extract_preserve_tokens(text: str) -> list[str]:
    tokens = {match.group(0).strip() for match in TOKEN_PATTERN.finditer(text)}
    return sorted(tokens, key=lambda token: text.find(token))


def find_nearby_titles(block: DocumentBlock, all_blocks: list[DocumentBlock]) -> list[str]:
    titles: list[str] = []
    ordered_blocks = all_blocks
    current_index = next(
        (
            index
            for index, candidate in enumerate(ordered_blocks)
            if candidate.block_id == block.block_id
        ),
        0,
    )
    for previous in reversed(ordered_blocks[:current_index]):
        if previous.role in TITLE_ROLES and previous.source_text:
            titles.append(previous.source_text)
        if len(titles) >= 2:
            break
    return list(reversed(titles))


def _chunk_char_count(blocks: list[SourceBlock]) -> int:
    # Count a small separator between blocks because the prompt serializes them
    # as distinct records, not a raw concatenation.
    return sum(len(block.source_text) for block in blocks) + max(len(blocks) - 1, 0)


def _make_chunk(
    document: DocumentIR,
    index: int,
    target_lang: str,
    blocks: list[SourceBlock],
    render_defaults: RenderDefaults,
) -> TranslationChunk:
    return TranslationChunk(
        chunk_id=f"{document.doc_id}_chunk_{index:04d}",
        target_lang=target_lang,
        source_blocks=blocks,
        context="Academic paper translation chunk.",
        render_defaults=render_defaults,
        constraints=TranslationConstraints(),
    )


def build_chunks(
    document: DocumentIR,
    target_lang: str,
    max_chars: int = 6000,
) -> list[TranslationChunk]:
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than 0")

    render_defaults = RenderDefaults(target_lang=target_lang)
    chunks: list[TranslationChunk] = []
    current_blocks: list[SourceBlock] = []
    current_chars = 0
    ordered_document_blocks = [
        block
        for page in document.pages
        for block in sorted(page.blocks, key=lambda item: item.reading_order)
    ]

    for page in document.pages:
        page_blocks = sorted(page.blocks, key=lambda block: block.reading_order)
        for block in page_blocks:
            if not block.source_text.strip():
                continue
            source_block = SourceBlock(
                block_id=block.block_id,
                role=block.role,
                source_text=block.source_text,
                nearby_titles=find_nearby_titles(block, ordered_document_blocks),
                preserve_tokens=extract_preserve_tokens(block.source_text),
            )
            block_chars = len(source_block.source_text)
            next_chars = current_chars + block_chars + (1 if current_blocks else 0)
            if current_blocks and next_chars > max_chars:
                chunks.append(
                    _make_chunk(
                        document,
                        len(chunks) + 1,
                        target_lang,
                        current_blocks,
                        render_defaults,
                    )
                )
                current_blocks = []
                current_chars = 0
            current_blocks.append(source_block)
            current_chars = _chunk_char_count(current_blocks)

    if current_blocks:
        chunks.append(
            _make_chunk(
                document,
                len(chunks) + 1,
                target_lang,
                current_blocks,
                render_defaults,
            )
        )

    if not chunks:
        raise ValueError("Document has no translatable text blocks")
    return chunks
