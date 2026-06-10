from __future__ import annotations

import re

from .models import DocumentIR, LayoutIntentPlan, TranslationChunk, TranslationLayoutPlan


class LayoutPlanValidationError(ValueError):
    pass


class LayoutIntentPlanValidationError(ValueError):
    pass


_FORMULA_REF_PATTERN = re.compile(r"\{\{formula:([A-Za-z0-9_.:-]+)\}\}")
_LEGACY_FORMULA_PLACEHOLDER_PATTERN = re.compile(r"@@FORMULA_[A-Za-z0-9_]+@@")


def validate_layout_plan(
    chunk: TranslationChunk, plan: TranslationLayoutPlan
) -> TranslationLayoutPlan:
    if plan.chunk_id != chunk.chunk_id:
        raise LayoutPlanValidationError(
            f"plan chunk_id {plan.chunk_id!r} does not match {chunk.chunk_id!r}"
        )

    if plan.target_lang != chunk.target_lang:
        raise LayoutPlanValidationError(
            f"plan target_lang {plan.target_lang!r} does not match {chunk.target_lang!r}"
        )

    expected_ids = chunk.source_block_ids()
    returned_ids = {block.source_block_id for block in plan.blocks}

    unknown_ids = returned_ids - expected_ids
    if unknown_ids:
        raise LayoutPlanValidationError(
            "plan contains unknown source_block_id values: " + ", ".join(sorted(unknown_ids))
        )

    missing_ids = expected_ids - returned_ids
    if chunk.constraints.require_all_blocks and missing_ids:
        raise LayoutPlanValidationError(
            "plan is missing source_block_id values: " + ", ".join(sorted(missing_ids))
        )

    if chunk.constraints.preserve_tokens:
        _reject_legacy_formula_tokens(chunk)
        tokens_by_block = {
            block.block_id: set(block.preserve_tokens)
            for block in chunk.source_blocks
            if block.preserve_tokens
        }
        for block_plan in plan.blocks:
            required_tokens = tokens_by_block.get(block_plan.source_block_id, set())
            if not required_tokens:
                continue
            planned_text = block_plan.translated_text
            planned_tokens = {
                item.source_token or item.text
                for item in block_plan.inline_items
                if item.kind != "text"
            }
            _reject_legacy_formula_tokens_from_plan(block_plan.translated_text, planned_tokens)
            missing_tokens = {
                token
                for token in required_tokens
                if token not in planned_text and token not in planned_tokens
            }
            if missing_tokens:
                raise LayoutPlanValidationError(
                    f"block {block_plan.source_block_id} is missing preserve tokens: "
                    + ", ".join(sorted(missing_tokens))
                )

    return plan


def _reject_legacy_formula_tokens(chunk: TranslationChunk) -> None:
    for source_block in chunk.source_blocks:
        for token in source_block.preserve_tokens:
            if _LEGACY_FORMULA_PLACEHOLDER_PATTERN.fullmatch(token):
                raise LayoutPlanValidationError(
                    "legacy formula placeholders are not allowed in preserve_tokens: "
                    f"{token}"
                )


def _reject_legacy_formula_tokens_from_plan(
    translated_text: str,
    planned_tokens: set[str],
) -> None:
    if _LEGACY_FORMULA_PLACEHOLDER_PATTERN.search(translated_text):
        raise LayoutPlanValidationError(
            "translated_text contains legacy formula placeholder; use {{formula:id}}"
        )
    for token in planned_tokens:
        if token and _LEGACY_FORMULA_PLACEHOLDER_PATTERN.fullmatch(token):
            raise LayoutPlanValidationError(
                "inline_items contain legacy formula placeholder; use {{formula:id}}"
            )


def validate_layout_intent_plan(
    document: DocumentIR, plan: LayoutIntentPlan
) -> LayoutIntentPlan:
    if plan.doc_id != document.doc_id:
        raise LayoutIntentPlanValidationError(
            f"plan doc_id {plan.doc_id!r} does not match {document.doc_id!r}"
        )

    expected_ids = set(document.blocks_by_id())
    returned_ids = {block.source_block_id for block in plan.blocks}

    unknown_ids = returned_ids - expected_ids
    if unknown_ids:
        raise LayoutIntentPlanValidationError(
            "layout intent plan contains unknown source_block_id values: "
            + ", ".join(sorted(unknown_ids))
        )

    missing_ids = expected_ids - returned_ids
    if missing_ids:
        raise LayoutIntentPlanValidationError(
            "layout intent plan is missing source_block_id values: "
            + ", ".join(sorted(missing_ids))
        )

    expected_asset_ids = {
        asset.asset_id for page in document.pages for asset in page.assets
    }
    returned_asset_ids = {asset.asset_id for asset in plan.assets}
    unknown_asset_ids = returned_asset_ids - expected_asset_ids
    if unknown_asset_ids:
        raise LayoutIntentPlanValidationError(
            "layout intent plan contains unknown asset_id values: "
            + ", ".join(sorted(unknown_asset_ids))
        )

    return plan
