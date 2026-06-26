from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .models import (
    DocumentBlock,
    DocumentIR,
    LayoutIntentPlan,
    TranslationChunk,
    TranslationLayoutPlan,
)


class LayoutPlanValidationError(ValueError):
    pass


class LayoutIntentPlanValidationError(ValueError):
    pass


class FormulaRefValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FormulaRefIssue:
    code: str
    location: str
    formula_id: str
    message: str


_FORMULA_REF_PATTERN = re.compile(r"\{\{formula:([A-Za-z0-9_.:-]+)\}\}")
_LEGACY_FORMULA_PLACEHOLDER_PATTERN = re.compile(r"@@FORMULA_[A-Za-z0-9_]+@@")
_LEGACY_FORMULA_ID_PATTERN = re.compile(r"F[A-Za-z0-9_]+")


def extract_formula_refs(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(dict.fromkeys(match.group(1) for match in _FORMULA_REF_PATTERN.finditer(text)))


def collect_formula_ref_issues(
    document: DocumentIR,
    *,
    chunk: TranslationChunk | None = None,
    plan: TranslationLayoutPlan | None = None,
    fallback_formula_ids: Iterable[str] | None = None,
    flag_legacy_formula_ids: bool = False,
    check_anchor_consistency: bool = False,
) -> list[FormulaRefIssue]:
    known_formula_ids = set(document.formulas_by_id())
    if fallback_formula_ids:
        known_formula_ids.update(fallback_formula_ids)

    issues: list[FormulaRefIssue] = []
    if flag_legacy_formula_ids:
        issues.extend(_legacy_document_formula_id_issues(document))

    for location, refs in _iter_document_formula_refs(document):
        issues.extend(
            _unknown_formula_ref_issues(
                location,
                refs,
                known_formula_ids,
                flag_legacy_formula_ids=flag_legacy_formula_ids,
            )
        )

    if chunk is not None:
        for location, refs in _iter_chunk_formula_refs(chunk):
            issues.extend(
                _unknown_formula_ref_issues(
                    location,
                    refs,
                    known_formula_ids,
                    flag_legacy_formula_ids=flag_legacy_formula_ids,
                )
            )

    if plan is not None:
        for location, refs in _iter_plan_formula_refs(plan):
            issues.extend(
                _unknown_formula_ref_issues(
                    location,
                    refs,
                    known_formula_ids,
                    flag_legacy_formula_ids=flag_legacy_formula_ids,
                )
            )
        issues.extend(_formula_inline_item_kind_issues(plan))

    if check_anchor_consistency:
        issues.extend(_document_formula_anchor_issues(document))

    return issues


def validate_formula_refs(
    document: DocumentIR,
    *,
    chunk: TranslationChunk | None = None,
    plan: TranslationLayoutPlan | None = None,
    fallback_formula_ids: Iterable[str] | None = None,
    flag_legacy_formula_ids: bool = False,
    check_anchor_consistency: bool = False,
) -> DocumentIR:
    issues = collect_formula_ref_issues(
        document,
        chunk=chunk,
        plan=plan,
        fallback_formula_ids=fallback_formula_ids,
        flag_legacy_formula_ids=flag_legacy_formula_ids,
        check_anchor_consistency=check_anchor_consistency,
    )
    if issues:
        raise FormulaRefValidationError(_format_formula_ref_issues(issues))
    return document


def validate_layout_plan(
    chunk: TranslationChunk,
    plan: TranslationLayoutPlan,
    *,
    document: DocumentIR | None = None,
    fallback_formula_ids: Iterable[str] | None = None,
    flag_legacy_formula_ids: bool = False,
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

    inline_item_issues = _formula_inline_item_kind_issues(plan)
    if inline_item_issues:
        raise LayoutPlanValidationError(_format_formula_ref_issues(inline_item_issues))

    fallback_formula_id_set = set(fallback_formula_ids or [])
    formula_refs_by_block = _formula_refs_by_source_block(chunk)
    invented_formula_issues = _plan_formula_refs_not_in_chunk(
        plan,
        formula_refs_by_block,
        fallback_formula_id_set,
    )
    if invented_formula_issues:
        raise LayoutPlanValidationError(_format_formula_ref_issues(invented_formula_issues))

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
            formula_tokens = {
                token for token in required_tokens if _FORMULA_REF_PATTERN.fullmatch(token)
            }
            missing_formula_tokens = {
                token for token in formula_tokens if token not in planned_text
            }
            missing_tokens = {
                token
                for token in required_tokens
                if token not in formula_tokens
                and token not in planned_text
                and token not in planned_tokens
            }
            missing_tokens.update(missing_formula_tokens)
            if missing_tokens:
                raise LayoutPlanValidationError(
                    f"block {block_plan.source_block_id} is missing preserve tokens: "
                    + ", ".join(sorted(missing_tokens))
                )

    if document is not None:
        try:
            validate_formula_refs(
                document,
                chunk=chunk,
                plan=plan,
                fallback_formula_ids=fallback_formula_ids,
                flag_legacy_formula_ids=flag_legacy_formula_ids,
            )
        except FormulaRefValidationError as exc:
            raise LayoutPlanValidationError(str(exc)) from exc

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


def _iter_document_formula_refs(document: DocumentIR) -> Iterable[tuple[str, tuple[str, ...]]]:
    for page in document.pages:
        for block in page.blocks:
            block_location = f"DocumentIR.pages[{page.page_id}].blocks[{block.block_id}]"
            yield f"{block_location}.source_text", extract_formula_refs(block.source_text)
            yield (
                f"{block_location}.text_for_translation",
                extract_formula_refs(block.text_for_translation),
            )


def _iter_chunk_formula_refs(chunk: TranslationChunk) -> Iterable[tuple[str, tuple[str, ...]]]:
    for block in chunk.source_blocks:
        block_location = f"TranslationChunk.source_blocks[{block.block_id}]"
        yield f"{block_location}.source_text", extract_formula_refs(block.source_text)
        for index, token in enumerate(block.preserve_tokens):
            yield f"{block_location}.preserve_tokens[{index}]", extract_formula_refs(token)


def _iter_plan_formula_refs(
    plan: TranslationLayoutPlan,
) -> Iterable[tuple[str, tuple[str, ...]]]:
    for block in plan.blocks:
        block_location = f"TranslationLayoutPlan.blocks[{block.source_block_id}]"
        yield f"{block_location}.translated_text", extract_formula_refs(block.translated_text)
        for index, item in enumerate(block.inline_items):
            item_location = f"{block_location}.inline_items[{index}]"
            yield f"{item_location}.text", extract_formula_refs(item.text)
            yield f"{item_location}.source_token", extract_formula_refs(item.source_token)


def _formula_refs_by_source_block(chunk: TranslationChunk) -> dict[str, set[str]]:
    refs_by_block: dict[str, set[str]] = {}
    for block in chunk.source_blocks:
        refs = set(extract_formula_refs(block.source_text))
        for token in block.preserve_tokens:
            refs.update(extract_formula_refs(token))
        refs_by_block[block.block_id] = refs
    return refs_by_block


def _plan_formula_refs_not_in_chunk(
    plan: TranslationLayoutPlan,
    refs_by_block: dict[str, set[str]],
    fallback_formula_ids: set[str],
) -> list[FormulaRefIssue]:
    issues: list[FormulaRefIssue] = []
    for block in plan.blocks:
        allowed_refs = refs_by_block.get(block.source_block_id, set()) | fallback_formula_ids
        block_location = f"TranslationLayoutPlan.blocks[{block.source_block_id}]"
        locations_and_refs = [
            (f"{block_location}.translated_text", extract_formula_refs(block.translated_text))
        ]
        for index, item in enumerate(block.inline_items):
            item_location = f"{block_location}.inline_items[{index}]"
            locations_and_refs.append((f"{item_location}.text", extract_formula_refs(item.text)))
            locations_and_refs.append(
                (f"{item_location}.source_token", extract_formula_refs(item.source_token))
            )
        for location, refs in locations_and_refs:
            for formula_id in refs:
                if formula_id in allowed_refs:
                    continue
                issues.append(
                    FormulaRefIssue(
                        code="formula_ref_not_in_source_block",
                        location=location,
                        formula_id=formula_id,
                        message=(
                            f"{location} references formula {formula_id!r}, "
                            "which is not present in the source block preserve tokens"
                        ),
                    )
                )
    return issues


def _unknown_formula_ref_issues(
    location: str,
    formula_refs: Iterable[str],
    known_formula_ids: set[str],
    *,
    flag_legacy_formula_ids: bool = False,
) -> list[FormulaRefIssue]:
    issues: list[FormulaRefIssue] = []
    for formula_id in formula_refs:
        is_legacy = _LEGACY_FORMULA_ID_PATTERN.fullmatch(formula_id) is not None
        if formula_id in known_formula_ids:
            if flag_legacy_formula_ids and is_legacy:
                issues.append(
                    FormulaRefIssue(
                        code="legacy_formula_ref",
                        location=location,
                        formula_id=formula_id,
                        message=(
                            f"{location} references legacy formula {formula_id!r}; "
                            "prefer a canonical DocumentIR.formulas id "
                            "or an explicit fallback alias"
                        ),
                    )
                )
            continue
        code = "stale_legacy_formula_ref" if is_legacy else "unknown_formula_ref"
        legacy_note = " stale legacy" if is_legacy else ""
        issues.append(
            FormulaRefIssue(
                code=code,
                location=location,
                formula_id=formula_id,
                message=(
                    f"{location} references{legacy_note} formula {formula_id!r}, "
                    "which is not in DocumentIR.formulas"
                ),
            )
        )
    return issues


def _legacy_document_formula_id_issues(document: DocumentIR) -> list[FormulaRefIssue]:
    issues: list[FormulaRefIssue] = []
    for formula in document.formulas:
        if _LEGACY_FORMULA_ID_PATTERN.fullmatch(formula.formula_id) is None:
            continue
        issues.append(
            FormulaRefIssue(
                code="legacy_formula_id",
                location=f"DocumentIR.formulas[{formula.formula_id}].formula_id",
                formula_id=formula.formula_id,
                message=(
                    f"DocumentIR.formulas contains legacy formula id {formula.formula_id!r}; "
                    "prefer a canonical formula id or mark it as an explicit fallback alias"
                ),
            )
        )
    return issues


def _formula_inline_item_kind_issues(plan: TranslationLayoutPlan) -> list[FormulaRefIssue]:
    issues: list[FormulaRefIssue] = []
    for block in plan.blocks:
        block_location = f"TranslationLayoutPlan.blocks[{block.source_block_id}]"
        for index, item in enumerate(block.inline_items):
            refs = _combined_formula_refs(item.text, item.source_token)
            if not refs or item.kind == "formula":
                continue
            location = f"{block_location}.inline_items[{index}]"
            for formula_id in refs:
                issues.append(
                    FormulaRefIssue(
                        code="formula_inline_item_kind_mismatch",
                        location=location,
                        formula_id=formula_id,
                        message=(
                            f"{location} carries formula ref {formula_id!r} "
                            "but inline item kind must be 'formula'"
                        ),
                    )
                )
    return issues


def _combined_formula_refs(*texts: str | None) -> tuple[str, ...]:
    formula_ids: dict[str, None] = {}
    for text in texts:
        for formula_id in extract_formula_refs(text):
            formula_ids[formula_id] = None
    return tuple(formula_ids)


def _document_formula_anchor_issues(document: DocumentIR) -> list[FormulaRefIssue]:
    block_by_id = document.blocks_by_id()
    asset_pages = {
        asset.asset_id: asset.page_id
        for page in document.pages
        for asset in page.assets
    }

    issues: list[FormulaRefIssue] = []
    for formula in document.formulas:
        for field_name, block_id in (
            ("source_block_id", formula.source_block_id),
            ("anchor_block_id", formula.anchor_block_id),
        ):
            if block_id is None:
                continue
            block = block_by_id.get(block_id)
            if block is not None and block.page_id != formula.page_id:
                issues.append(
                    FormulaRefIssue(
                        code="formula_anchor_page_mismatch",
                        location=f"DocumentIR.formulas[{formula.formula_id}].{field_name}",
                        formula_id=formula.formula_id,
                        message=(
                            f"formula {formula.formula_id!r} has page_id "
                            f"{formula.page_id!r} but {field_name} {block_id!r} "
                            f"is on page {block.page_id!r}"
                        ),
                    )
                )

        if formula.asset_id is not None:
            asset_page_id = asset_pages.get(formula.asset_id)
            if asset_page_id is not None and asset_page_id != formula.page_id:
                issues.append(
                    FormulaRefIssue(
                        code="formula_anchor_page_mismatch",
                        location=f"DocumentIR.formulas[{formula.formula_id}].asset_id",
                        formula_id=formula.formula_id,
                        message=(
                            f"formula {formula.formula_id!r} has page_id "
                            f"{formula.page_id!r} but asset_id {formula.asset_id!r} "
                            f"is on page {asset_page_id!r}"
                        ),
                    )
                )

        anchor_block = _formula_anchor_block(
            formula.source_block_id,
            formula.anchor_block_id,
            block_by_id,
        )
        if anchor_block is not None:
            issues.extend(_formula_span_ref_issues(formula, anchor_block))
            issues.extend(_formula_source_text_range_issues(formula, anchor_block))

    return issues


def _formula_anchor_block(
    source_block_id: str | None,
    anchor_block_id: str | None,
    block_by_id: dict[str, DocumentBlock],
) -> DocumentBlock | None:
    for block_id in (source_block_id, anchor_block_id):
        if block_id is None:
            continue
        block = block_by_id.get(block_id)
        if block is not None:
            return block
    return None


def _formula_span_ref_issues(formula, block: DocumentBlock) -> list[FormulaRefIssue]:
    if not formula.span_ids:
        return []
    known_span_ids = {span.span_id for span in block.spans}
    if not known_span_ids:
        return []
    unknown_span_ids = sorted(set(formula.span_ids) - known_span_ids)
    if not unknown_span_ids:
        return []
    return [
        FormulaRefIssue(
            code="formula_unknown_span_ref",
            location=f"DocumentIR.formulas[{formula.formula_id}].span_ids",
            formula_id=formula.formula_id,
            message=(
                f"formula {formula.formula_id!r} references unknown span_ids in block "
                f"{block.block_id!r}: " + ", ".join(unknown_span_ids)
            ),
        )
    ]


def _formula_source_text_range_issues(formula, block: DocumentBlock) -> list[FormulaRefIssue]:
    if formula.source_text_range is None or not formula.source_text:
        return []
    block_text = block.source_text
    if not block_text or "{{formula:" in block_text:
        return []

    start, end = formula.source_text_range
    location = f"DocumentIR.formulas[{formula.formula_id}].source_text_range"
    if end > len(block_text):
        return [
            FormulaRefIssue(
                code="formula_source_text_range_out_of_bounds",
                location=location,
                formula_id=formula.formula_id,
                message=(
                    f"formula {formula.formula_id!r} source_text_range "
                    f"{formula.source_text_range!r} "
                    f"exceeds source block {block.block_id!r} text length {len(block_text)}"
                ),
            )
        ]

    source_slice = block_text[start:end]
    if _compact_formula_text(source_slice) == _compact_formula_text(formula.source_text):
        return []

    return [
        FormulaRefIssue(
            code="formula_source_text_range_mismatch",
            location=location,
            formula_id=formula.formula_id,
            message=(
                f"formula {formula.formula_id!r} source_text_range does not match "
                f"source_text in block {block.block_id!r}"
            ),
        )
    ]


def _compact_formula_text(text: str) -> str:
    return "".join(text.split())


def _format_formula_ref_issues(issues: Iterable[FormulaRefIssue]) -> str:
    return "; ".join(issue.message for issue in issues)


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

    section_ids = {section.section_id for section in plan.structure_plan.sections}
    unknown_section_block_ids = _unknown_structure_block_ids(plan, expected_ids)
    if unknown_section_block_ids:
        raise LayoutIntentPlanValidationError(
            "layout intent plan structure references unknown source_block_id values: "
            + ", ".join(sorted(unknown_section_block_ids))
        )

    duplicate_body_block_ids = _duplicate_body_section_block_ids(plan)
    if duplicate_body_block_ids:
        raise LayoutIntentPlanValidationError(
            "layout intent plan structure assigns source blocks to multiple body sections: "
            + ", ".join(sorted(duplicate_body_block_ids))
        )

    unknown_rule_section_ids = _unknown_rule_section_ids(plan, section_ids)
    if unknown_rule_section_ids:
        raise LayoutIntentPlanValidationError(
            "layout intent plan references unknown section_id values: "
            + ", ".join(sorted(unknown_rule_section_ids))
        )

    return plan


def _unknown_structure_block_ids(
    plan: LayoutIntentPlan,
    expected_ids: set[str],
) -> set[str]:
    unknown_ids: set[str] = set()
    for section in plan.structure_plan.sections:
        for block_id in section.source_block_ids:
            if block_id not in expected_ids:
                unknown_ids.add(block_id)
    return unknown_ids


def _duplicate_body_section_block_ids(plan: LayoutIntentPlan) -> set[str]:
    assigned: dict[str, str] = {}
    duplicate_ids: set[str] = set()
    for section in plan.structure_plan.sections:
        if not section.source_block_ids:
            continue
        if section.kind in {"figure", "table", "formula"}:
            continue
        for block_id in section.source_block_ids:
            previous = assigned.get(block_id)
            if previous is not None and previous != section.section_id:
                duplicate_ids.add(block_id)
            assigned[block_id] = section.section_id
    return duplicate_ids


def _unknown_rule_section_ids(
    plan: LayoutIntentPlan,
    section_ids: set[str],
) -> set[str]:
    referenced: set[str] = set()
    numbering = plan.numbering_plan
    for rule in (
        numbering.heading_numbering,
        numbering.figure_numbering,
        numbering.table_numbering,
        numbering.formula_numbering,
        numbering.reference_numbering,
    ):
        referenced.update(rule.section_ids)
    referenced.update(numbering.toc_generation.section_ids)
    referenced.update(plan.bibliography_plan.section_ids)
    return {section_id for section_id in referenced if section_id not in section_ids}
