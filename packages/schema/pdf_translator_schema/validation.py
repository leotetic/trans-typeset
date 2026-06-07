from __future__ import annotations

from .models import TranslationChunk, TranslationLayoutPlan


class LayoutPlanValidationError(ValueError):
    pass


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

