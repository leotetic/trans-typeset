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

from .formula_processing import formula_placeholders_for_block, is_formula_only_block

TOKEN_PATTERN = re.compile(
    r"("
    r"\{\{formula:[A-Za-z0-9_.:-]+\}\}|"
    r"@@FORMULA_[A-Za-z0-9_]+@@|"
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


def _preserve_tokens_for_block(block: DocumentBlock, source_text: str) -> list[str]:
    tokens = extract_preserve_tokens(source_text)
    for placeholder in formula_placeholders_for_block(block):
        if placeholder not in tokens:
            tokens.append(placeholder)
    return sorted(
        tokens,
        key=lambda token: source_text.find(token) if token in source_text else 10**9,
    )


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
    context: str,
    glossary: dict[str, str],
) -> TranslationChunk:
    return TranslationChunk(
        chunk_id=f"{document.doc_id}_chunk_{index:04d}",
        target_lang=target_lang,
        source_blocks=blocks,
        context=context,
        glossary=glossary,
        render_defaults=render_defaults,
        constraints=TranslationConstraints(),
    )


def _summarize_blocks(blocks: list[SourceBlock], limit: int = 220) -> str:
    text = " ".join(block.source_text for block in blocks).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _chunk_context(
    document: DocumentIR,
    chunk_index: int,
    blocks: list[SourceBlock],
    previous_blocks: list[SourceBlock],
) -> str:
    title = next(
        (
            block.source_text
            for page in document.pages
            for block in sorted(page.blocks, key=lambda item: item.reading_order)
            if block.role == BlockRole.TITLE and block.source_text.strip()
        ),
        "",
    )
    nearby_titles = []
    for block in blocks:
        for title_text in block.nearby_titles:
            if title_text not in nearby_titles:
                nearby_titles.append(title_text)
    parts = [
        "Academic paper translation chunk.",
        f"Chunk index: {chunk_index}.",
    ]
    if title:
        parts.append(f"Document title: {title}.")
    if nearby_titles:
        parts.append("Nearby titles: " + " > ".join(nearby_titles[-3:]) + ".")
    if previous_blocks:
        parts.append("Previous chunk tail: " + _summarize_blocks(previous_blocks[-2:]) + ".")
    formulas = document.formulas_by_id()
    formula_context: list[str] = []
    for block in blocks:
        for token in block.preserve_tokens:
            formula_match = re.fullmatch(r"\{\{formula:([A-Za-z0-9_.:-]+)\}\}", token)
            if formula_match:
                formula = formulas.get(formula_match.group(1))
                if formula is not None:
                    formula_context.append(
                        f"{token} => {formula.display_mode} LaTeX: {formula.latex}"
                    )
                continue
            for document_page in document.pages:
                for document_block in document_page.blocks:
                    for formula in document_block.formulas:
                        if formula.placeholder == token:
                            formula_context.append(
                                f"{token} => {formula.kind} LaTeX: {formula.latex}"
                            )
                            break
    if formula_context:
        parts.append("Formula refs: " + " | ".join(formula_context) + ".")
    parts.append("Current chunk summary: " + _summarize_blocks(blocks) + ".")
    return "\n".join(parts)


def build_chunks(
    document: DocumentIR,
    target_lang: str,
    max_chars: int = 6000,
    glossary: dict[str, str] | None = None,
    render_defaults: RenderDefaults | None = None,
) -> list[TranslationChunk]:
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than 0")

    render_defaults = (
        render_defaults.model_copy(update={"target_lang": target_lang}, deep=True)
        if render_defaults is not None
        else RenderDefaults(target_lang=target_lang)
    )
    glossary = glossary or {}
    chunks: list[TranslationChunk] = []
    current_blocks: list[SourceBlock] = []
    previous_chunk_blocks: list[SourceBlock] = []
    current_chars = 0
    ordered_document_blocks = [
        block
        for page in document.pages
        for block in sorted(page.blocks, key=lambda item: item.reading_order)
    ]

    for page in document.pages:
        page_blocks = sorted(page.blocks, key=lambda block: block.reading_order)
        for block in page_blocks:
            chunk_text = block.text_for_translation.strip() or block.source_text.strip()
            if not chunk_text:
                continue
            source_block = SourceBlock(
                block_id=block.block_id,
                role=block.role,
                source_text=chunk_text,
                nearby_titles=find_nearby_titles(block, ordered_document_blocks),
                preserve_tokens=_preserve_tokens_for_block(block, chunk_text),
                requires_translation=not is_formula_only_block(block),
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
                        _chunk_context(
                            document,
                            len(chunks) + 1,
                            current_blocks,
                            previous_chunk_blocks,
                        ),
                        glossary,
                    )
                )
                previous_chunk_blocks = current_blocks
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
                _chunk_context(
                    document,
                    len(chunks) + 1,
                    current_blocks,
                    previous_chunk_blocks,
                ),
                glossary,
            )
        )

    if not chunks:
        raise ValueError("Document has no translatable text blocks")
    return chunks
