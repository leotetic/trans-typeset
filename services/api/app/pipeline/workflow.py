from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from pdf_translator_schema import (
    AcademicRequirement,
    Asset,
    AssetIR,
    BibliographyPlan,
    BibliographyPreference,
    BlockRole,
    BlockSectionMapping,
    BoundingBox,
    CitationStyle,
    ColumnLayoutDefaults,
    DocumentIR,
    DocumentPage,
    DocumentKind,
    DocumentProfile,
    DocumentStructureCandidate,
    DocumentStructurePlan,
    DocumentStructureSection,
    EditScope,
    EditScopeMode,
    InlineItem,
    InputKind,
    InputSource,
    LayoutIntentAsset,
    LayoutIntentBlock,
    LayoutIntentPlan,
    NumberingPlan,
    NumberingRule,
    NumberingStyle,
    OutputKind,
    OutputFormat,
    OutputTarget,
    PageSetup,
    PageSize,
    SectionKind,
    SemanticAssetSignal,
    SemanticBlockSignal,
    SemanticLayoutAnalysis,
    StyleSystem,
    StyleIntent,
    TemplateProfile,
    TemplateSource,
    TypesettingStandard,
    TocGenerationPlan,
    TaskIntent,
    TranslationBlockPlan,
    TranslationChunk,
    TranslationLayoutPlan,
    UserIntent,
    WorkflowMode,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepName,
    WorkflowStepStatus,
    validate_layout_intent_plan,
    validate_layout_plan,
)
from pdf_translator_schema.models import DocumentBlock, StyleSeed, UserConstraints
from pdf_translator_schema.validation import LayoutIntentPlanValidationError

from .image_ocr import ImageOCRResult
from ..storage import Storage

_DEFAULT_PAGE_WIDTH = 612.0
_DEFAULT_PAGE_HEIGHT = 792.0
_PAGE_MARGIN = 54.0
_TEXT_WIDTH = _DEFAULT_PAGE_WIDTH - _PAGE_MARGIN * 2
_LINE_HEIGHT_PT = 15.0
_PARAGRAPH_GAP_PT = 10.0


def coerce_user_intent(
    target_lang: str,
    output_kind: str | None = None,
    style_intent: str | None = None,
    instruction: str | None = None,
    constraints: UserConstraints | dict[str, Any] | None = None,
    workflow_mode: str | None = None,
) -> UserIntent:
    normalized_instruction = (instruction or "").strip()
    coerced_constraints = _coerce_user_constraints(constraints)
    coerced_mode = _coerce_workflow_mode(workflow_mode, output_kind)
    coerced_output_kind = _output_kind_for_workflow_mode(
        coerced_mode,
        _coerce_output_kind(output_kind),
    )
    typesetting_standard = _typesetting_standard_for_instruction(normalized_instruction)
    task_intent = _task_intent_for_instruction(target_lang, normalized_instruction)
    return UserIntent(
        target_lang=target_lang,
        workflow_mode=coerced_mode,
        output_kind=coerced_output_kind,
        style_intent=_coerce_style_intent(style_intent),
        typesetting_standard=typesetting_standard,
        instruction=normalized_instruction,
        constraints=coerced_constraints,
        column_layout=_column_layout_for_instruction(normalized_instruction),
        task_intent=task_intent,
        output_targets=_output_targets_for_instruction(normalized_instruction),
        template_profile=_template_profile_for_instruction(
            normalized_instruction,
            typesetting_standard,
        ),
        bibliography_preference=_bibliography_preference_for_instruction(normalized_instruction),
        requirements=_requirements_for_instruction(
            normalized_instruction,
            task_intent,
        ),
    )


def build_input_source(
    *,
    source_id: str,
    input_type: InputKind,
    source_role: str = "content",
    filename: str | None,
    mime_type: str | None,
    path: Path | None,
    size_bytes: int = 0,
    quality_flags: list[str] | None = None,
) -> InputSource:
    digest = _sha256(path) if path and path.exists() else None
    return InputSource(
        source_id=source_id,
        input_type=input_type,
        source_role=source_role,
        filename=filename,
        mime_type=mime_type,
        size_bytes=path.stat().st_size if path and path.exists() else size_bytes,
        sha256=digest,
        artifact_path=str(path) if path else None,
        quality_flags=quality_flags or [],
    )


def build_text_document(doc_id: str, text: str, intent: UserIntent) -> DocumentIR:
    normalized_blocks = _split_text_blocks(text)
    if not normalized_blocks:
        raise ValueError("Text input is empty")
    return _blocks_to_virtual_document(doc_id, normalized_blocks, intent)


def build_image_document(
    *,
    doc_id: str,
    image_path: Path,
    storage: Storage,
    intent: UserIntent,
    filename: str,
    mime_type: str | None,
    ocr_result: ImageOCRResult | None = None,
) -> tuple[DocumentIR, list[AssetIR]]:
    suffix = image_path.suffix.lower() or ".png"
    asset_id = f"{doc_id}_asset_0001"
    asset_target = storage.asset_dir(doc_id) / f"{asset_id}{suffix}"
    shutil.copyfile(image_path, asset_target)
    asset_url = f"/api/documents/{doc_id}/assets/{asset_target.name}"
    ocr_blocks = ocr_result.blocks if ocr_result is not None else []
    source_texts = [block.text for block in ocr_blocks if block.text.strip()]
    if not source_texts:
        source_texts = [_deterministic_image_summary(filename, intent)]
    source_block_ids = [f"{doc_id}_image_text_{index:04d}" for index in range(1, len(source_texts) + 1)]
    text_blocks = [
        DocumentBlock(
            block_id=block_id,
            page_id="p1",
            role=_block_role_for_image_ocr(
                ocr_blocks[index - 1].role if index <= len(ocr_blocks) else "paragraph"
            ),
            bbox=BoundingBox(
                x0=_PAGE_MARGIN,
                y0=530 + (index - 1) * 48,
                x1=_DEFAULT_PAGE_WIDTH - _PAGE_MARGIN,
                y1=570 + (index - 1) * 48,
            ),
            reading_order=index - 1,
            source_text=text,
            style_seed=StyleSeed(font_size=11),
        )
        for index, (block_id, text) in enumerate(zip(source_block_ids, source_texts), start=1)
    ]
    quality_flags = (
        list(ocr_result.quality_flags)
        if ocr_result is not None
        else ["deterministic_ocr_mock", "ocr_uncertain"]
    )
    document = DocumentIR(
        doc_id=doc_id,
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=_DEFAULT_PAGE_WIDTH, height=_DEFAULT_PAGE_HEIGHT),
                blocks=text_blocks,
                assets=[
                    Asset(
                        asset_id=asset_id,
                        page_id="p1",
                        kind="image",
                        bbox=BoundingBox(
                            x0=_PAGE_MARGIN,
                            y0=_PAGE_MARGIN,
                            x1=_DEFAULT_PAGE_WIDTH - _PAGE_MARGIN,
                            y1=500,
                        ),
                        path=asset_url,
                        alt_text=f"Image input: {filename}",
                    )
                ],
            )
        ],
    )
    asset_ir = AssetIR(
        asset_id=asset_id,
        source_id="source_1",
        kind="image",
        mime_type=mime_type,
        path=asset_url,
        ocr_text="\n\n".join(source_texts),
        alt_text=f"Image input: {filename}",
        source_block_ids=source_block_ids,
        confidence=0.7 if ocr_result is not None and ocr_result.provider != "deterministic" else 0.35,
        quality_flags=quality_flags,
    )
    return document, [asset_ir]


def _block_role_for_image_ocr(role: str) -> BlockRole:
    try:
        return BlockRole(role)
    except ValueError:
        return BlockRole.PARAGRAPH


def normalized_input_payload(
    *,
    input_sources: list[InputSource],
    document: DocumentIR,
    assets: list[AssetIR] | None = None,
    input_text: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "normalized_input",
        "input_sources": [source.model_dump() for source in input_sources],
        "document_ir_ref": "document-ir",
        "asset_count": len(assets or []),
        "block_count": sum(len(page.blocks) for page in document.pages),
        "text_excerpt": _compact_text(input_text or _document_text(document), 600),
        "quality_flags": _collect_source_flags(input_sources) + _collect_asset_flags(assets or []),
    }


def build_initial_workflow_run(
    *,
    job_id: str,
    doc_id: str,
    input_sources: list[InputSource],
    intent: UserIntent,
) -> WorkflowRun:
    return WorkflowRun(
        workflow_id=f"workflow_{job_id}",
        job_id=job_id,
        doc_id=doc_id,
        status=WorkflowStatus.RUNNING,
        current_step=WorkflowStepName.READ_INPUT,
        progress=0,
        input_sources=input_sources,
        user_intent=intent,
        steps=[],
    )


def make_workflow_step(
    name: WorkflowStepName,
    status: WorkflowStepStatus,
    *,
    progress: float,
    attempt: int = 1,
    message: str = "",
    input_artifacts: list[str] | None = None,
    output_artifacts: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
    error: str | None = None,
) -> WorkflowStep:
    return WorkflowStep(
        step_id=f"{len(str(name.value))}_{name.value}_{attempt}",
        name=name,
        status=status,
        progress=progress,
        attempt=attempt,
        message=message,
        input_artifacts=input_artifacts or [],
        output_artifacts=output_artifacts or [],
        diagnostics=diagnostics or {},
        error=error,
    )


def append_workflow_step(
    run: WorkflowRun,
    step: WorkflowStep,
    *,
    status: WorkflowStatus | None = None,
) -> WorkflowRun:
    return run.model_copy(
        update={
            "current_step": step.name,
            "progress": step.progress,
            "status": status or run.status,
            "steps": [*run.steps, step],
        },
        deep=True,
    )


def build_layout_intent_plan(
    document: DocumentIR,
    intent: UserIntent,
    *,
    attempt: int = 1,
    diagnostics: dict[str, Any] | None = None,
    semantic_analysis: SemanticLayoutAnalysis | None = None,
) -> LayoutIntentPlan:
    diagnostics = diagnostics or {}
    repair_block_ids = _diagnostic_block_ids(diagnostics)
    instruction = intent.instruction.lower()
    fallback_structure_sections = _document_structure_sections(document, intent)
    structure_sections = (
        _structure_sections_from_semantic_analysis(
            document,
            intent,
            semantic_analysis,
            fallback_structure_sections,
        )
        if semantic_analysis is not None
        else fallback_structure_sections
    )
    semantic_signals = _semantic_signal_by_block(semantic_analysis)
    semantic_asset_signals = _semantic_signal_by_asset(semantic_analysis)
    blocks: list[LayoutIntentBlock] = []
    for page in document.pages:
        for block in sorted(page.blocks, key=lambda item: item.reading_order):
            signal = semantic_signals.get(block.block_id)
            role = _semantic_role_for_block(block.role, signal)
            render_intent = _render_intent_for_block(role, intent, instruction)
            quality_flags: list[str] = (
                _semantic_quality_flags_for_block(block, signal)
                if semantic_analysis is not None
                else []
            )
            if block.block_id in repair_block_ids and render_intent == "normal":
                render_intent = "compact"
                quality_flags.append("repair_compact_intent")
            if role == BlockRole.UNKNOWN:
                quality_flags.append("role_uncertain")
            if not block.source_text.strip():
                quality_flags.append("empty_source_block")
            blocks.append(
                LayoutIntentBlock(
                    source_block_id=block.block_id,
                    role=role,
                    priority=_priority_for_role(role),
                    render_intent=render_intent,
                    asset_refs=_asset_refs_for_block(block, document),
                    quality_flags=quality_flags,
                )
            )
    assets = [
        LayoutIntentAsset(
            asset_id=asset.asset_id,
            usage=_semantic_asset_usage(
                asset,
                intent,
                semantic_asset_signals.get(asset.asset_id),
            ),
            quality_flags=_semantic_asset_quality_flags(
                asset,
                intent,
                semantic_asset_signals.get(asset.asset_id),
            ),
        )
        for page in document.pages
        for asset in page.assets
    ]
    plan = LayoutIntentPlan(
        plan_id=f"{document.doc_id}_layout_intent_{attempt:02d}",
        doc_id=document.doc_id,
        target_lang=intent.target_lang,
        workflow_mode=intent.workflow_mode,
        output_kind=intent.output_kind,
        style_intent=intent.style_intent,
        column_layout=intent.column_layout,
        document_profile=DocumentProfile(
            document_kind=intent.task_intent.document_kind,
            target_lang=intent.target_lang,
            style_intent=intent.style_intent,
            template_profile=intent.template_profile,
            citation_style=intent.bibliography_preference.citation_style,
        ),
        structure_plan=DocumentStructurePlan(
            sections=structure_sections,
            missing_sections=_missing_sections_for_intent(intent, structure_sections),
            uncertain_sections=_uncertain_sections(structure_sections),
        ),
        page_setup=_page_setup_for_intent(intent),
        style_system=_style_system_for_intent(intent),
        numbering_plan=_numbering_plan_for_intent(intent, structure_sections),
        bibliography_plan=_bibliography_plan_for_intent(intent, structure_sections),
        requirements=_requirements_with_status(intent.requirements, structure_sections),
        blocks=blocks,
        assets=assets,
        quality_flags=_layout_plan_flags(intent, diagnostics, attempt),
    )
    if semantic_analysis is not None:
        plan = plan.model_copy(
            update={
                "quality_flags": _unique([*plan.quality_flags, "semantic_analysis_considered"])
            },
            deep=True,
        )
    return validate_layout_intent_plan(document, plan)


def build_semantic_layout_analysis(
    document: DocumentIR,
    intent: UserIntent,
    *,
    input_kind: InputKind,
) -> SemanticLayoutAnalysis:
    block_signals: list[SemanticBlockSignal] = []
    section_hints: list[str] = []
    structure_sections = _document_structure_sections(document, intent)
    section_by_block = {
        block_id: section for section in structure_sections for block_id in section.source_block_ids
    }
    for page in document.pages:
        for block in sorted(page.blocks, key=lambda item: item.reading_order):
            quality_flags: list[str] = []
            role_candidates = [block.role]
            if block.role == BlockRole.UNKNOWN:
                quality_flags.append("role_uncertain")
            if not block.source_text.strip():
                quality_flags.append("empty_source_block")
            if block.role in {BlockRole.TITLE, BlockRole.HEADING, BlockRole.ABSTRACT}:
                section_hints.append(_compact_text(block.source_text, 120))
            section = section_by_block.get(block.block_id)
            if section is not None and section.title:
                section_hints.append(section.title)
            block_signals.append(
                SemanticBlockSignal(
                    source_block_id=block.block_id,
                    role_candidates=role_candidates,
                    section_hint=_section_hint_for_block(block),
                    confidence=0.85 if not quality_flags else 0.45,
                    quality_flags=quality_flags,
                )
            )
    asset_signals = [
        SemanticAssetSignal(
            asset_id=asset.asset_id,
            usage_hint=_layout_asset_usage(asset, intent),
            text_hint=asset.alt_text or "",
            confidence=0.75 if _layout_asset_usage(asset, intent) == "preserve" else 0.35,
            quality_flags=_layout_asset_quality_flags(asset, intent),
        )
        for page in document.pages
        for asset in page.assets
    ]
    quality_flags = ["deterministic_semantic_analysis"]
    if input_kind == InputKind.IMAGE:
        quality_flags.append("vision_analysis_disabled")
    if intent.instruction.strip():
        quality_flags.append("user_instruction_considered")
    if intent.typesetting_standard == TypesettingStandard.GB_T_7713_1_2025:
        quality_flags.append("gb_t_7713_1_requested")
    confidence_values = [signal.confidence for signal in block_signals] + [
        signal.confidence for signal in asset_signals
    ]
    confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.5
    return SemanticLayoutAnalysis(
        analysis_id=f"{document.doc_id}_semantic_analysis_01",
        doc_id=document.doc_id,
        target_lang=intent.target_lang,
        block_signals=block_signals,
        asset_signals=asset_signals,
        section_hints=_unique([hint for hint in section_hints if hint]),
        structure_candidates=[
            DocumentStructureCandidate(
                section_id=section.section_id,
                kind=section.kind,
                title=section.title,
                level=section.level,
                source_block_ids=section.source_block_ids,
                required=section.required,
                confidence=0.84 if not section.quality_flags else 0.52,
                quality_flags=section.quality_flags,
            )
            for section in structure_sections
        ],
        block_section_mappings=[
            BlockSectionMapping(
                source_block_id=block_id,
                section_id=section.section_id,
                section_kind=section.kind,
                confidence=0.84 if not section.quality_flags else 0.52,
                quality_flags=section.quality_flags,
            )
            for section in structure_sections
            for block_id in section.source_block_ids
        ],
        recognized_requirements=_requirements_with_status(
            intent.requirements,
            structure_sections,
        ),
        missing_sections=_missing_sections_for_intent(intent, structure_sections),
        uncertain_sections=_uncertain_sections(structure_sections),
        confidence=round(confidence, 4),
        quality_flags=quality_flags,
    )


def _semantic_signal_by_block(
    analysis: SemanticLayoutAnalysis | None,
) -> dict[str, SemanticBlockSignal]:
    if analysis is None:
        return {}
    return {signal.source_block_id: signal for signal in analysis.block_signals}


def _semantic_signal_by_asset(
    analysis: SemanticLayoutAnalysis | None,
) -> dict[str, SemanticAssetSignal]:
    if analysis is None:
        return {}
    return {signal.asset_id: signal for signal in analysis.asset_signals}


def _semantic_role_for_block(
    source_role: BlockRole,
    signal: SemanticBlockSignal | None,
) -> BlockRole:
    if signal is None or not signal.role_candidates:
        return source_role
    candidate = signal.role_candidates[0]
    if (
        source_role == BlockRole.UNKNOWN
        and candidate != BlockRole.UNKNOWN
        and signal.confidence >= 0.6
    ):
        return candidate
    return source_role


def _semantic_quality_flags_for_block(
    block: DocumentBlock,
    signal: SemanticBlockSignal | None,
) -> list[str]:
    if signal is None:
        return ["semantic_signal_missing"]

    flags = list(signal.quality_flags)
    if signal.confidence < 0.6:
        flags.append("semantic_signal_low_confidence")
    if signal.role_candidates:
        candidate = signal.role_candidates[0]
        if (
            block.role == BlockRole.UNKNOWN
            and candidate != BlockRole.UNKNOWN
            and signal.confidence >= 0.6
        ):
            flags.append(f"semantic_role_inferred_{candidate.value}")
        elif candidate != block.role and candidate != BlockRole.UNKNOWN:
            flags.append(f"semantic_role_candidate_{candidate.value}")
    return _unique(flags)


def _asset_refs_for_block(block: DocumentBlock, document: DocumentIR) -> list[str]:
    refs: list[str] = []
    for formula in document.formulas:
        if formula.asset_id and (
            formula.source_block_id == block.block_id
            or formula.anchor_block_id == block.block_id
            or block.formula_id == formula.formula_id
        ):
            refs.append(formula.asset_id)
    return _unique(refs)


def _semantic_asset_usage(
    asset: Asset,
    intent: UserIntent,
    signal: SemanticAssetSignal | None,
) -> str:
    if not intent.constraints.preserve_images:
        return "ignore"
    if signal is None or signal.confidence < 0.6 or signal.usage_hint == "unknown":
        return _layout_asset_usage(asset, intent)
    return signal.usage_hint


def _semantic_asset_quality_flags(
    asset: Asset,
    intent: UserIntent,
    signal: SemanticAssetSignal | None,
) -> list[str]:
    flags = _layout_asset_quality_flags(asset, intent)
    if signal is None:
        return flags
    flags.extend(signal.quality_flags)
    if signal.confidence < 0.6:
        flags.append("semantic_asset_signal_low_confidence")
    elif signal.usage_hint != "unknown":
        flags.append(f"semantic_asset_usage_{signal.usage_hint}")
    return _unique(flags)


def _structure_sections_from_semantic_analysis(
    document: DocumentIR,
    intent: UserIntent,
    analysis: SemanticLayoutAnalysis,
    fallback_sections: list[DocumentStructureSection],
) -> list[DocumentStructureSection]:
    if not analysis.structure_candidates:
        return fallback_sections

    expected_block_ids = set(document.blocks_by_id())
    block_order = _document_block_order(document)
    sections: list[DocumentStructureSection] = []
    seen_section_ids: set[str] = set()
    covered_block_ids: set[str] = set()
    assigned_body_block_ids: set[str] = set()

    for candidate in sorted(
        analysis.structure_candidates,
        key=lambda item: _first_block_order(item.source_block_ids, block_order),
    ):
        source_block_ids = [
            block_id for block_id in candidate.source_block_ids if block_id in expected_block_ids
        ]
        if not source_block_ids:
            continue
        if candidate.kind not in {SectionKind.FIGURE, SectionKind.TABLE, SectionKind.FORMULA}:
            source_block_ids = [
                block_id for block_id in source_block_ids if block_id not in assigned_body_block_ids
            ]
        if not source_block_ids:
            continue

        section_id = _unique_section_id(candidate.section_id, seen_section_ids)
        seen_section_ids.add(section_id)
        covered_block_ids.update(source_block_ids)
        if candidate.kind not in {SectionKind.FIGURE, SectionKind.TABLE, SectionKind.FORMULA}:
            assigned_body_block_ids.update(source_block_ids)

        quality_flags = [
            *candidate.quality_flags,
            "semantic_structure_candidate",
        ]
        if candidate.confidence < 0.6:
            quality_flags.append("semantic_structure_low_confidence")
        if len(source_block_ids) != len(candidate.source_block_ids):
            quality_flags.append("semantic_structure_unknown_block_ignored")

        sections.append(
            DocumentStructureSection(
                section_id=section_id,
                kind=candidate.kind,
                title=candidate.title,
                level=candidate.level,
                source_block_ids=source_block_ids,
                required=candidate.required or _section_required_for_intent(intent, candidate.kind),
                quality_flags=_unique(quality_flags),
            )
        )

    for fallback in fallback_sections:
        missing_block_ids = [
            block_id for block_id in fallback.source_block_ids if block_id not in covered_block_ids
        ]
        if not missing_block_ids:
            continue
        section_id = _unique_section_id(fallback.section_id, seen_section_ids)
        seen_section_ids.add(section_id)
        sections.append(
            fallback.model_copy(
                update={
                    "section_id": section_id,
                    "source_block_ids": missing_block_ids,
                    "quality_flags": _unique(
                        [*fallback.quality_flags, "semantic_structure_fallback"]
                    ),
                },
                deep=True,
            )
        )

    if not sections:
        return fallback_sections
    return sorted(
        sections,
        key=lambda item: _first_block_order(item.source_block_ids, block_order),
    )


def _document_block_order(document: DocumentIR) -> dict[str, tuple[int, int]]:
    order: dict[str, tuple[int, int]] = {}
    for page_index, page in enumerate(document.pages):
        for block in page.blocks:
            order[block.block_id] = (page_index, block.reading_order)
    return order


def _first_block_order(
    source_block_ids: list[str],
    block_order: dict[str, tuple[int, int]],
) -> tuple[int, int]:
    orders = [block_order[block_id] for block_id in source_block_ids if block_id in block_order]
    return min(orders) if orders else (10**9, 10**9)


def render_evaluation_summary(renderer_diagnostics: dict[str, Any]) -> dict[str, Any]:
    quality_counts = renderer_diagnostics.get("quality_flag_counts")
    layout_issues = renderer_diagnostics.get("layout_issues")
    page_utilization = renderer_diagnostics.get("page_utilization")
    underfilled_reflow_pages = renderer_diagnostics.get("underfilled_reflow_pages")
    right_column_start_pages = renderer_diagnostics.get("right_column_start_pages")
    left_column_underfilled_pages = renderer_diagnostics.get("left_column_underfilled_pages")
    browser_overflow_count = int(renderer_diagnostics.get("browser_block_overflow_count") or 0)
    browser_figure_group_issue_count = int(
        renderer_diagnostics.get("browser_figure_group_issue_count") or 0
    )
    browser_unavailable = bool(renderer_diagnostics.get("browser_validation_unavailable"))
    if not isinstance(quality_counts, dict):
        quality_counts = {}
    if not isinstance(layout_issues, list):
        layout_issues = []
    if not isinstance(page_utilization, list):
        page_utilization = []
    if not isinstance(underfilled_reflow_pages, list):
        underfilled_reflow_pages = []
    if not isinstance(right_column_start_pages, list):
        right_column_start_pages = []
    if not isinstance(left_column_underfilled_pages, list):
        left_column_underfilled_pages = []
    blocking_flags = {
        flag: count
        for flag, count in quality_counts.items()
        if flag
        in {
            "overflow_clipped",
            "asset_missing_path",
            "missing_translation",
            "underfilled_reflow_page",
            "right_column_page_start",
            "left_column_underfilled_before_right_column",
        }
    }
    if underfilled_reflow_pages and "underfilled_reflow_page" not in blocking_flags:
        blocking_flags["underfilled_reflow_page"] = len(underfilled_reflow_pages)
    if right_column_start_pages and "right_column_page_start" not in blocking_flags:
        blocking_flags["right_column_page_start"] = len(right_column_start_pages)
    if (
        left_column_underfilled_pages
        and "left_column_underfilled_before_right_column" not in blocking_flags
    ):
        blocking_flags["left_column_underfilled_before_right_column"] = len(
            left_column_underfilled_pages
        )
    if browser_overflow_count:
        blocking_flags["browser_overflow"] = browser_overflow_count
    if browser_figure_group_issue_count:
        blocking_flags["browser_figure_group_issue"] = browser_figure_group_issue_count
    repair_recommended = bool(blocking_flags or layout_issues) and not browser_unavailable
    return {
        "kind": "render_evaluation",
        "accepted": not blocking_flags and not layout_issues and not browser_unavailable,
        "quality_flag_counts": quality_counts,
        "layout_issue_count": len(layout_issues),
        "browser_block_overflow_count": browser_overflow_count,
        "browser_figure_group_issue_count": browser_figure_group_issue_count,
        "browser_validation_unavailable": browser_unavailable,
        "page_utilization": page_utilization,
        "underfilled_reflow_pages": underfilled_reflow_pages,
        "right_column_start_pages": right_column_start_pages,
        "left_column_underfilled_pages": left_column_underfilled_pages,
        "blocking_flags": blocking_flags,
        "repair_recommended": repair_recommended,
        "manual_action_required": browser_unavailable,
    }


def build_repair_record(
    *,
    attempt: int,
    before: LayoutIntentPlan,
    after: LayoutIntentPlan,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    changed_blocks = []
    before_by_id = {block.source_block_id: block for block in before.blocks}
    for after_block in after.blocks:
        before_block = before_by_id.get(after_block.source_block_id)
        if before_block is None or before_block.render_intent != after_block.render_intent:
            changed_blocks.append(
                {
                    "source_block_id": after_block.source_block_id,
                    "before_render_intent": before_block.render_intent if before_block else None,
                    "after_render_intent": after_block.render_intent,
                    "quality_flags": after_block.quality_flags,
                }
            )
    return {
        "attempt": attempt,
        "reason": "renderer diagnostics requested semantic repair",
        "changed_blocks": changed_blocks,
        "diagnostic_block_ids": sorted(_diagnostic_block_ids(diagnostics)),
        "accepted": True,
    }


def safe_validate_layout_intent_plan(
    document: DocumentIR,
    plan: LayoutIntentPlan,
) -> tuple[LayoutIntentPlan, dict[str, Any]]:
    try:
        return validate_layout_intent_plan(document, plan), {"status": "valid"}
    except LayoutIntentPlanValidationError as exc:
        repaired = build_layout_intent_plan(
            document,
            UserIntent(target_lang=plan.target_lang),
            attempt=2,
            diagnostics={"repair_reason": str(exc)},
        )
        return repaired, {"status": "repaired", "error": str(exc)}


def selected_block_ids_for_scope(document: DocumentIR, scope: EditScope | None) -> set[str]:
    scope = scope or EditScope()
    if scope.mode == EditScopeMode.ALL:
        return set(document.blocks_by_id())
    if scope.mode == EditScopeMode.PAGES:
        unknown_pages = [
            page_number
            for page_number in scope.page_numbers
            if page_number < 1 or page_number > len(document.pages)
        ]
        if unknown_pages:
            raise ValueError(
                "edit scope references unknown page number(s): "
                + ", ".join(str(page_number) for page_number in unknown_pages)
            )
        return {
            block.block_id
            for page_number in scope.page_numbers
            for block in document.pages[page_number - 1].blocks
        }
    expected_block_ids = set(document.blocks_by_id())
    unknown_block_ids = sorted(set(scope.block_ids) - expected_block_ids)
    if unknown_block_ids:
        raise ValueError(
            "edit scope references unknown block id(s): " + ", ".join(unknown_block_ids)
        )
    return set(scope.block_ids)


def build_source_preserving_layout_plans(
    *,
    document: DocumentIR,
    chunks: list[TranslationChunk],
    layout_plan: LayoutIntentPlan | None = None,
    edit_scope: EditScope | None = None,
    existing_plans: list[TranslationLayoutPlan] | None = None,
) -> list[TranslationLayoutPlan]:
    selected_block_ids = selected_block_ids_for_scope(document, edit_scope)
    layout_blocks = {
        block.source_block_id: block
        for block in (layout_plan.blocks if layout_plan is not None else [])
    }
    existing_blocks = {
        block.source_block_id: block for plan in (existing_plans or []) for block in plan.blocks
    }
    plans: list[TranslationLayoutPlan] = []
    for chunk in chunks:
        plan = _source_preserving_plan_for_chunk(
            chunk,
            layout_blocks,
            existing_blocks,
            selected_block_ids,
        )
        try:
            plans.append(validate_layout_plan(chunk, plan))
        except Exception:
            fallback = _source_preserving_plan_for_chunk(
                chunk,
                layout_blocks,
                {},
                {block.block_id for block in chunk.source_blocks},
            )
            plans.append(validate_layout_plan(chunk, fallback))
    return plans


def source_preserving_summary(
    *,
    document: DocumentIR,
    edit_scope: EditScope | None,
    chunks: list[TranslationChunk],
    plans: list[TranslationLayoutPlan],
    reused_existing_plans: bool,
) -> dict[str, Any]:
    selected_block_ids = selected_block_ids_for_scope(document, edit_scope)
    return {
        "kind": "source_preserving_typeset",
        "scope": (edit_scope or EditScope()).model_dump(mode="json"),
        "selected_block_count": len(selected_block_ids),
        "chunk_count": len(chunks),
        "plan_count": len(plans),
        "reused_existing_plans": reused_existing_plans,
        "quality_flags": ["translation_skipped", "source_text_preserved"],
    }


def _source_preserving_plan_for_chunk(
    chunk: TranslationChunk,
    layout_blocks: dict[str, LayoutIntentBlock],
    existing_blocks: dict[str, TranslationBlockPlan],
    selected_block_ids: set[str],
) -> TranslationLayoutPlan:
    blocks: list[TranslationBlockPlan] = []
    for source in chunk.source_blocks:
        existing = existing_blocks.get(source.block_id)
        if source.block_id not in selected_block_ids and existing is not None:
            blocks.append(
                existing.model_copy(
                    update={
                        "quality_flags": _unique(
                            [
                                *existing.quality_flags,
                                "retypeset_reused_existing_plan",
                            ]
                        )
                    },
                    deep=True,
                )
            )
            continue
        layout_block = layout_blocks.get(source.block_id)
        render_intent = layout_block.render_intent if layout_block is not None else "normal"
        if source.role in {BlockRole.FIGURE, BlockRole.TABLE} and render_intent == "normal":
            render_intent = "preserve_asset"
        blocks.append(
            TranslationBlockPlan(
                source_block_id=source.block_id,
                translated_text=source.source_text,
                inline_items=[
                    _inline_item_for_preserved_token(token) for token in source.preserve_tokens
                ],
                role=source.role,
                render_intent=render_intent,
                quality_flags=_unique(
                    [
                        "translation_skipped",
                        "source_text_preserved",
                        *(layout_block.quality_flags if layout_block is not None else []),
                    ]
                ),
            )
        )
    return TranslationLayoutPlan(
        chunk_id=chunk.chunk_id,
        target_lang=chunk.target_lang,
        blocks=blocks,
    )


def _inline_item_for_preserved_token(token: str) -> InlineItem:
    if re.fullmatch(r"\{\{formula:([A-Za-z0-9_.:-]+)\}\}", token):
        return InlineItem(
            kind="formula",
            text=token,
            source_token=token,
            asset_id=token.removeprefix("{{formula:").removesuffix("}}"),
        )
    if re.fullmatch(r"\[[0-9,\-\s;]+\]", token):
        return InlineItem(kind="reference_marker", text=token, source_token=token)
    if re.search(r"\b\d{4}[a-z]?\b", token):
        return InlineItem(kind="citation", text=token, source_token=token)
    if any(symbol in token for symbol in ("=", "+", "-", "*", "/", "^", "_", "≤", "≥")):
        return InlineItem(kind="formula", text=token, source_token=token)
    return InlineItem(kind="citation", text=token, source_token=token)


def _coerce_output_kind(value: str | None) -> OutputKind:
    if not value:
        return OutputKind.TRANSLATION
    try:
        return OutputKind(value)
    except ValueError:
        return OutputKind.TRANSLATION


def _coerce_workflow_mode(
    value: str | None,
    output_kind: str | None = None,
) -> WorkflowMode:
    if value:
        try:
            return WorkflowMode(value)
        except ValueError:
            pass
    if output_kind == OutputKind.TYPESET_DOCUMENT.value:
        return WorkflowMode.TYPESET_ONLY
    return WorkflowMode.TRANSLATE_AND_TYPESET


def _output_kind_for_workflow_mode(
    workflow_mode: WorkflowMode,
    output_kind: OutputKind,
) -> OutputKind:
    if workflow_mode == WorkflowMode.TYPESET_ONLY:
        return OutputKind.TYPESET_DOCUMENT
    if workflow_mode in {
        WorkflowMode.TRANSLATE_ONLY,
        WorkflowMode.TRANSLATE_AND_TYPESET,
    }:
        return OutputKind.TRANSLATION
    return output_kind


def _coerce_style_intent(value: str | None) -> StyleIntent:
    if not value:
        return StyleIntent.ACADEMIC
    try:
        return StyleIntent(value)
    except ValueError:
        return StyleIntent.ACADEMIC


def _coerce_user_constraints(
    value: UserConstraints | dict[str, Any] | None,
) -> UserConstraints:
    if value is None:
        return UserConstraints()
    if isinstance(value, UserConstraints):
        return value
    return UserConstraints.model_validate(value)


def _typesetting_standard_for_instruction(instruction: str) -> TypesettingStandard:
    normalized = instruction.lower()
    if "gb/t 7713.1" in normalized or "gb-gb/t 7713.1" in normalized:
        return TypesettingStandard.GB_T_7713_1_2025
    return TypesettingStandard.NONE


def _task_intent_for_instruction(target_lang: str, instruction: str) -> TaskIntent:
    normalized = instruction.lower()
    candidates: tuple[tuple[re.Pattern[str], DocumentKind, str], ...] = (
        (
            re.compile(
                r"本科论文|毕业论文|学位论文|undergraduate\s+thesis|\bthesis\b",
                re.IGNORECASE,
            ),
            DocumentKind.UNDERGRADUATE_THESIS,
            "undergraduate_thesis_keyword",
        ),
        (
            re.compile(
                r"social\s+practice\s+report|社会实践报告",
                re.IGNORECASE,
            ),
            DocumentKind.SOCIAL_PRACTICE_REPORT,
            "social_practice_report_keyword",
        ),
        (
            re.compile(
                r"group\s+course\s+assignment|group\s+assignment|小组作业|团队作业",
                re.IGNORECASE,
            ),
            DocumentKind.GROUP_ASSIGNMENT,
            "group_assignment_keyword",
        ),
        (
            re.compile(r"book\s+report|读书报告|读后感", re.IGNORECASE),
            DocumentKind.BOOK_REPORT,
            "book_report_keyword",
        ),
        (
            re.compile(
                r"实验报告|lab(?:oratory)?\s+report|experiment\s+report",
                re.IGNORECASE,
            ),
            DocumentKind.LAB_REPORT,
            "lab_report_keyword",
        ),
        (
            re.compile(
                r"开题报告|proposal\s+report|research\s+proposal|graduation\s+project\s+proposal",
                re.IGNORECASE,
            ),
            DocumentKind.PROPOSAL_REPORT,
            "proposal_report_keyword",
        ),
        (
            re.compile(r"课程论文|course\s+paper|term\s+paper", re.IGNORECASE),
            DocumentKind.COURSE_PAPER,
            "course_paper_keyword",
        ),
        (
            re.compile(r"普通作业|作业|homework|assignment", re.IGNORECASE),
            DocumentKind.HOMEWORK,
            "homework_keyword",
        ),
    )
    for pattern, document_kind, evidence in candidates:
        if pattern.search(normalized):
            return TaskIntent(
                document_kind=document_kind,
                language=target_lang,
                confidence=0.82,
                evidence=[evidence],
            )
    return TaskIntent(
        document_kind=DocumentKind.GENERIC_ACADEMIC,
        language=target_lang,
        confidence=0.45 if instruction else 0.35,
        evidence=["default_generic_academic"],
    )


def _requirements_for_instruction(
    instruction: str,
    task_intent: TaskIntent,
) -> list[AcademicRequirement]:
    normalized = instruction.lower()
    requirements: dict[str, AcademicRequirement] = {}

    def add(
        requirement_id: str,
        label: str,
        category: str = "structure",
        section_kinds: list[SectionKind] | None = None,
        evidence: str | None = None,
    ) -> None:
        existing = requirements.get(requirement_id)
        new_evidence = [evidence] if evidence else []
        if existing is None:
            requirements[requirement_id] = AcademicRequirement(
                requirement_id=requirement_id,
                label=label,
                category=category,  # type: ignore[arg-type]
                section_kinds=section_kinds or [],
                evidence=new_evidence,
            )
            return
        requirements[requirement_id] = existing.model_copy(
            update={
                "section_kinds": _unique_section_kinds(
                    [*existing.section_kinds, *(section_kinds or [])]
                ),
                "evidence": _unique([*existing.evidence, *new_evidence]),
            },
            deep=True,
        )

    document_kind = task_intent.document_kind
    if document_kind == DocumentKind.PROPOSAL_REPORT:
        add("formal_academic_style", "Formal academic style", "style", evidence="proposal_report")
        add(
            "supervisor_submission",
            "Suitable for academic supervisor submission",
            "tone",
            evidence="proposal_report",
        )
        add("clear_title", "Clear title", "structure", [SectionKind.TITLE], "proposal_report")
        add("main_text", "Main text", "structure", [SectionKind.BODY], "proposal_report")
    elif document_kind == DocumentKind.UNDERGRADUATE_THESIS:
        add(
            "clear_title",
            "Clear thesis title",
            "structure",
            [SectionKind.TITLE],
            "undergraduate_thesis",
        )
        add("main_text", "Main text", "structure", [SectionKind.BODY], "undergraduate_thesis")
        add(
            "references",
            "References",
            "bibliography",
            [SectionKind.REFERENCES],
            "undergraduate_thesis",
        )
    elif document_kind == DocumentKind.COURSE_PAPER:
        add("academic_style", "Standard academic style", "style", evidence="course_paper")
        add("clear_title", "Clear title", "structure", [SectionKind.TITLE], "course_paper")
        add("readable_body_text", "Readable body text", "style", [SectionKind.BODY], "course_paper")
    elif document_kind == DocumentKind.LAB_REPORT:
        for requirement_id, label, section_kind in (
            ("lab_objective", "Objective section", SectionKind.EXPERIMENT_PURPOSE),
            ("lab_principles", "Principles section", SectionKind.EXPERIMENT_THEORY),
            ("lab_procedure", "Procedure section", SectionKind.EXPERIMENT_STEPS),
            ("lab_results", "Results section", SectionKind.EXPERIMENT_RESULTS),
            ("lab_analysis", "Analysis section", SectionKind.EXPERIMENT_ANALYSIS),
            ("lab_conclusion", "Conclusion section", SectionKind.CONCLUSION),
        ):
            add(requirement_id, label, "structure", [section_kind], "lab_report")
    elif document_kind == DocumentKind.BOOK_REPORT:
        add("clear_headings", "Clear headings", "structure", [SectionKind.HEADING], "book_report")
        add(
            "highlight_important_content",
            "Highlight important content",
            "style",
            evidence="book_report",
        )
        add(
            "non_promotional_tone",
            "Avoid commercial or promotional style",
            "tone",
            evidence="book_report",
        )
    elif document_kind == DocumentKind.SOCIAL_PRACTICE_REPORT:
        add(
            "target_length_5000_words",
            "Approximately 5,000 words",
            "length",
            evidence="social_practice_report",
        )
        add("cover_page", "Cover page", "structure", [SectionKind.COVER], "social_practice_report")
        add(
            "table_of_contents",
            "Table of contents",
            "structure",
            [SectionKind.TOC],
            "social_practice_report",
        )
        add(
            "section_headings",
            "Section headings",
            "structure",
            [SectionKind.HEADING],
            "social_practice_report",
        )
        add("headers", "Page headers", "structure", evidence="social_practice_report")
        add("page_numbers", "Page numbers", "numbering", evidence="social_practice_report")
    elif document_kind == DocumentKind.GROUP_ASSIGNMENT:
        add("cover_page", "Cover page", "structure", [SectionKind.COVER], "group_assignment")
        add("course_name", "Course name", "metadata", [SectionKind.COURSE_INFO], "group_assignment")
        add("project_title", "Project title", "metadata", [SectionKind.TITLE], "group_assignment")
        add(
            "group_members",
            "Group member information",
            "metadata",
            [SectionKind.AUTHOR_INFO],
            "group_assignment",
        )
    elif document_kind == DocumentKind.HOMEWORK:
        add(
            "formal_assignment_style", "Clean formal assignment style", "style", evidence="homework"
        )

    if re.search(r"cover\s+page|封面", normalized):
        add("cover_page", "Cover page", "structure", [SectionKind.COVER], "cover_page_keyword")
    if re.search(r"\babstract\b|摘要", normalized):
        add("abstract", "Abstract", "structure", [SectionKind.ABSTRACT], "abstract_keyword")
    if re.search(r"\bkeywords?\b|关键词|关键字", normalized):
        add("keywords", "Keywords", "structure", [SectionKind.KEYWORDS], "keywords_keyword")
    if re.search(r"table\s+of\s+contents|\btoc\b|目录", normalized):
        add("table_of_contents", "Table of contents", "structure", [SectionKind.TOC], "toc_keyword")
    if re.search(r"main\s+text|正文", normalized):
        add("main_text", "Main text", "structure", [SectionKind.BODY], "main_text_keyword")
    if re.search(r"\breferences?\b|bibliography|参考文献", normalized):
        add(
            "references",
            "References",
            "bibliography",
            [SectionKind.REFERENCES],
            "references_keyword",
        )
    if re.search(r"acknowledg(?:e)?ments?|致谢", normalized):
        add(
            "acknowledgements",
            "Acknowledgments",
            "structure",
            [SectionKind.ACKNOWLEDGEMENTS],
            "acknowledgements_keyword",
        )
    if re.search(
        r"citations?\s+and\s+references?|consistent\s+citations?|引用.*参考文献", normalized
    ):
        add(
            "consistent_citations_references",
            "Consistent citations and references",
            "bibliography",
            [SectionKind.REFERENCES],
            "citation_consistency_keyword",
        )
    if re.search(r"figures?.*tables?.*equations?|figures?.*tables?|图.*表.*公式", normalized):
        add(
            "asset_alignment",
            "Figure, table, and equation alignment",
            "asset",
            [SectionKind.FIGURE, SectionKind.TABLE, SectionKind.FORMULA],
            "asset_alignment_keyword",
        )
        add(
            "figure_numbering",
            "Figure numbering",
            "numbering",
            [SectionKind.FIGURE],
            "figure_numbering_keyword",
        )
        add(
            "table_numbering",
            "Table numbering",
            "numbering",
            [SectionKind.TABLE],
            "table_numbering_keyword",
        )
        add(
            "formula_numbering",
            "Equation numbering",
            "numbering",
            [SectionKind.FORMULA],
            "formula_numbering_keyword",
        )
    if re.search(r"lists?\s+of\s+figures?\s+and\s+tables?|图目录|表目录", normalized):
        add(
            "list_of_figures",
            "List of figures",
            "structure",
            [SectionKind.LIST_OF_FIGURES],
            "list_of_figures_keyword",
        )
        add(
            "list_of_tables",
            "List of tables",
            "structure",
            [SectionKind.LIST_OF_TABLES],
            "list_of_tables_keyword",
        )
    if re.search(r"12[- ]?point\s+simsun|12\s*pt\s+simsun|12\s*号\s*宋体|小四|simsun", normalized):
        add(
            "main_text_12pt_simsun",
            "12-point SimSun main text",
            "style",
            [SectionKind.BODY],
            "main_text_font_keyword",
        )
    if re.search(r"1\.5\s+line\s+spacing|1\.5\s*倍行距|line\s+spacing", normalized):
        add(
            "line_spacing_1_5",
            "1.5 line spacing",
            "style",
            [SectionKind.BODY],
            "line_spacing_keyword",
        )
    if re.search(r"16[- ]?point\s+simhei|16\s*pt\s+simhei|16\s*号\s*黑体|simhei", normalized):
        add(
            "level1_heading_16pt_simhei",
            "16-point SimHei level 1 headings",
            "style",
            [SectionKind.HEADING],
            "heading_font_keyword",
        )
    if re.search(r"page\s+numbers?.*main\s+text|页码.*正文|正文.*页码", normalized):
        add(
            "main_text_page_numbers",
            "Page numbers start from main text",
            "numbering",
            [SectionKind.BODY],
            "main_text_page_number_keyword",
        )
    if re.search(r"course\s+name|课程名称|课程名", normalized):
        add(
            "course_name",
            "Course name",
            "metadata",
            [SectionKind.COURSE_INFO],
            "course_name_keyword",
        )
    if re.search(r"student\s+name|学生姓名|姓名", normalized):
        add(
            "student_name",
            "Student name",
            "metadata",
            [SectionKind.AUTHOR_INFO],
            "student_name_keyword",
        )
    if re.search(r"student\s+id|student\s+number|学号", normalized):
        add("student_id", "Student ID", "metadata", [SectionKind.AUTHOR_INFO], "student_id_keyword")
    if re.search(r"submission\s+date|提交日期|日期", normalized):
        add(
            "submission_date",
            "Submission date",
            "metadata",
            [SectionKind.COURSE_INFO],
            "submission_date_keyword",
        )
    if re.search(r"project\s+title|项目标题|课题名称", normalized):
        add(
            "project_title",
            "Project title",
            "metadata",
            [SectionKind.TITLE],
            "project_title_keyword",
        )
    if re.search(r"group\s+members?|all\s+group\s+members?|小组成员|团队成员", normalized):
        add(
            "group_members",
            "Group member information",
            "metadata",
            [SectionKind.AUTHOR_INFO],
            "group_members_keyword",
        )
    if re.search(r"headers?|页眉", normalized):
        add("headers", "Page headers", "structure", evidence="headers_keyword")
    if re.search(r"page\s+numbers?|页码", normalized):
        add("page_numbers", "Page numbers", "numbering", evidence="page_numbers_keyword")
    if re.search(r"approximately\s+5,?000\s+words|约\s*5000\s*字|5,?000\s+words", normalized):
        add(
            "target_length_5000_words",
            "Approximately 5,000 words",
            "length",
            evidence="length_keyword",
        )
    if re.search(r"highlight\s+important\s+content|突出重点|highlight", normalized):
        add(
            "highlight_important_content",
            "Highlight important content",
            "style",
            evidence="highlight_keyword",
        )
    if re.search(r"commercial|promotional|商业|宣传", normalized):
        add(
            "non_promotional_tone",
            "Avoid commercial or promotional style",
            "tone",
            evidence="non_promotional_keyword",
        )
    if re.search(r"formal|professional|正式|规范|academic\s+style", normalized):
        add(
            "formal_academic_style",
            "Formal academic style",
            "style",
            evidence="formal_style_keyword",
        )

    return list(requirements.values())


def _requirements_with_status(
    requirements: list[AcademicRequirement],
    sections: list[DocumentStructureSection],
) -> list[AcademicRequirement]:
    present = {section.kind for section in sections}
    result: list[AcademicRequirement] = []
    for requirement in requirements:
        flags = list(requirement.quality_flags)
        if requirement.section_kinds:
            missing = [
                section_kind
                for section_kind in requirement.section_kinds
                if section_kind not in present
            ]
            if missing:
                flags.extend(f"missing_section_{section_kind.value}" for section_kind in missing)
                flags.append("requirement_diagnostic")
            else:
                flags.append("requirement_satisfied")
        else:
            flags.append("requirement_recognized")
        result.append(
            requirement.model_copy(
                update={"quality_flags": _unique(flags)},
                deep=True,
            )
        )
    return result


def _unique_section_kinds(values: list[SectionKind]) -> list[SectionKind]:
    result: list[SectionKind] = []
    seen: set[SectionKind] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _has_requirement(intent: UserIntent, requirement_id: str) -> bool:
    return any(requirement.requirement_id == requirement_id for requirement in intent.requirements)


def _page_setup_for_intent(intent: UserIntent) -> PageSetup:
    setup = PageSetup()
    if _has_requirement(intent, "headers"):
        setup = setup.model_copy(
            update={
                "header_footer": setup.header_footer.model_copy(
                    update={
                        "enabled": True,
                        "header_text": _default_header_text_for_intent(intent),
                    },
                    deep=True,
                )
            },
            deep=True,
        )
    if _has_requirement(intent, "page_numbers") or _has_requirement(
        intent,
        "main_text_page_numbers",
    ):
        setup = setup.model_copy(
            update={
                "page_numbering": setup.page_numbering.model_copy(
                    update={
                        "enabled": True,
                        "style": NumberingStyle.ARABIC,
                        "start_at": 1,
                    },
                    deep=True,
                )
            },
            deep=True,
        )
    return setup


def _default_header_text_for_intent(intent: UserIntent) -> str:
    if intent.task_intent.document_kind == DocumentKind.SOCIAL_PRACTICE_REPORT:
        return "Social Practice Report"
    if intent.task_intent.document_kind == DocumentKind.UNDERGRADUATE_THESIS:
        return "Thesis"
    if intent.task_intent.document_kind == DocumentKind.COURSE_PAPER:
        return "Course Paper"
    return "Academic Document"


def _style_system_for_intent(intent: UserIntent) -> StyleSystem:
    named_styles: dict[str, dict[str, object]] = {}
    if _has_requirement(intent, "main_text_12pt_simsun") or _has_requirement(
        intent,
        "line_spacing_1_5",
    ):
        named_styles["main_text"] = {
            "font_size_pt": 12.0,
            "bold": False,
            "alignment": "justify",
            "line_height": 1.5,
            "first_line_indent_em": 2.0,
            "font_stack": [
                "Times New Roman",
                "SimSun",
                "Songti SC",
                "Noto Serif CJK SC",
                "serif",
            ],
        }
    if _has_requirement(intent, "level1_heading_16pt_simhei"):
        named_styles["level1_heading"] = {
            "font_size_pt": 16.0,
            "bold": True,
            "alignment": "left",
            "line_height": 1.5,
            "font_stack": [
                "Times New Roman",
                "SimHei",
                "Heiti SC",
                "Noto Sans CJK SC",
                "sans-serif",
            ],
        }
    if _has_requirement(intent, "highlight_important_content"):
        named_styles["important_content"] = {
            "font_size_pt": 12.0,
            "bold": True,
            "alignment": "left",
            "line_height": 1.5,
        }
    return StyleSystem(named_styles=named_styles)


def _output_targets_for_instruction(instruction: str) -> list[OutputTarget]:
    normalized = instruction.lower()
    targets = [
        OutputTarget(format=OutputFormat.HTML_PREVIEW, artifact_name="preview.html"),
        OutputTarget(format=OutputFormat.PDF, artifact_name="translated.pdf"),
    ]
    if re.search(r"\bdocx\b|\bword\b|\.docx|word文档|word 格式", normalized):
        targets.append(
            OutputTarget(
                format=OutputFormat.DOCX,
                artifact_name="translated.docx",
            )
        )
    return targets


def _template_profile_for_instruction(
    instruction: str,
    typesetting_standard: TypesettingStandard,
) -> TemplateProfile:
    normalized = instruction.lower()
    source = TemplateSource.DEFAULT_ACADEMIC
    fallback_used = True
    if re.search(r"学校模板|院校模板|school\s+template|university\s+template", normalized):
        source = TemplateSource.SCHOOL_TEMPLATE
        fallback_used = False
    elif re.search(r"课程要求|课程模板|course\s+requirement|course\s+template", normalized):
        source = TemplateSource.COURSE_REQUIREMENT
        fallback_used = False
    elif re.search(r"自定义|用户指定|user\s+specified|custom", normalized):
        source = TemplateSource.USER_SPECIFIED
        fallback_used = False
    return TemplateProfile(
        source=source,
        standard=typesetting_standard.value,
        fallback_used=fallback_used,
    )


def _bibliography_preference_for_instruction(instruction: str) -> BibliographyPreference:
    citation_style = _citation_style_for_instruction(instruction)
    default_reason = (
        "Explicit citation style keyword detected."
        if citation_style != CitationStyle.AUTO
        else "No explicit citation style was requested."
    )
    return BibliographyPreference(
        citation_style=citation_style,
        default_reason=default_reason,
    )


def _citation_style_for_instruction(instruction: str) -> CitationStyle:
    normalized = instruction.lower()
    if re.search(r"gb/t\s*7714|gbt\s*7714|国标\s*7714|参考文献国标", normalized):
        return CitationStyle.GB_T_7714
    if re.search(r"\bapa\b", normalized):
        return CitationStyle.APA
    if re.search(r"\bmla\b", normalized):
        return CitationStyle.MLA
    if re.search(r"\bieee\b", normalized):
        return CitationStyle.IEEE
    if re.search(r"\bchicago\b", normalized):
        return CitationStyle.CHICAGO
    return CitationStyle.AUTO


_COLUMN_INTENT_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (
        re.compile(
            r"双栏|两栏|双列|两列|"
            r"(?:double|two|2)\s*[- ]?\s*col(?:umns?|umes?|ums?)",
            re.IGNORECASE,
        ),
        2,
    ),
    (
        re.compile(
            r"单栏|一栏|单列|"
            r"(?:single|one|1)\s*[- ]?\s*col(?:umns?|umes?|ums?)",
            re.IGNORECASE,
        ),
        1,
    ),
)
_COLUMN_BODY_SCOPE_PATTERN = re.compile(
    r"正文(?:部分)?(?:双栏|两栏|双列|两列)|body\s+only|main\s+text\s+only",
    re.IGNORECASE,
)
_COLUMN_DOCUMENT_SCOPE_PATTERN = re.compile(
    r"全文(?:双栏|两栏|双列|两列)|整篇(?:双栏|两栏|双列|两列)|whole\s+document|"
    r"entire\s+document|including\s+(?:title|abstract|headings?)",
    re.IGNORECASE,
)
_COLUMN_BALANCE_PATTERN = re.compile(
    r"平衡(?:双栏|两栏|列)?|均衡(?:双栏|两栏|列)?|balance(?:d)?\s+columns?",
    re.IGNORECASE,
)
_COLUMN_UNBALANCE_PATTERN = re.compile(
    r"不(?:要)?平衡(?:双栏|两栏|列)?|无需平衡|do\s+not\s+balance|unbalanced\s+columns?",
    re.IGNORECASE,
)
_COLUMN_GAP_PATTERN = re.compile(
    r"(?:栏距|列距|column\s+gap)\s*[:：=]?\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>pt|mm|cm|毫米|厘米)?",
    re.IGNORECASE,
)


def _column_layout_for_instruction(instruction: str) -> ColumnLayoutDefaults:
    last_match: tuple[int, int] | None = None
    for pattern, column_count in _COLUMN_INTENT_PATTERNS:
        for match in pattern.finditer(instruction):
            if last_match is None or match.start() >= last_match[0]:
                last_match = (match.start(), column_count)
    if last_match is None:
        return ColumnLayoutDefaults()
    return ColumnLayoutDefaults(
        column_count=last_match[1],
        column_gap_pt=_column_gap_for_instruction(instruction),
        scope=_column_scope_for_instruction(instruction),
        balance_columns=_column_balance_for_instruction(instruction),
    )


def _column_scope_for_instruction(instruction: str) -> str:
    document_match = _last_pattern_match(_COLUMN_DOCUMENT_SCOPE_PATTERN, instruction)
    body_match = _last_pattern_match(_COLUMN_BODY_SCOPE_PATTERN, instruction)
    if document_match is not None and (
        body_match is None or document_match.start() >= body_match.start()
    ):
        return "document"
    return "body"


def _column_balance_for_instruction(instruction: str) -> bool:
    balanced = _last_pattern_match(_COLUMN_BALANCE_PATTERN, instruction)
    unbalanced = _last_pattern_match(_COLUMN_UNBALANCE_PATTERN, instruction)
    if balanced is None:
        return ColumnLayoutDefaults().balance_columns
    if unbalanced is None:
        return True
    if unbalanced.start() <= balanced.start() < unbalanced.end():
        return False
    return balanced.start() > unbalanced.start()


def _column_gap_for_instruction(instruction: str) -> float:
    match = _last_pattern_match(_COLUMN_GAP_PATTERN, instruction)
    if match is None:
        return ColumnLayoutDefaults().column_gap_pt
    value = float(match.group("value"))
    unit = (match.group("unit") or "pt").lower()
    if unit in {"mm", "毫米"}:
        return round(value * 72.0 / 25.4, 2)
    if unit in {"cm", "厘米"}:
        return round(value * 72.0 / 2.54, 2)
    return value


def _last_pattern_match(
    pattern: re.Pattern[str],
    text: str,
) -> re.Match[str] | None:
    last_match: re.Match[str] | None = None
    for match in pattern.finditer(text):
        last_match = match
    return last_match


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_text_blocks(text: str) -> list[tuple[BlockRole, str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = [
        re.sub(r"\s+", " ", block).strip()
        for block in re.split(r"\n\s*\n+", normalized)
        if block.strip()
    ]
    blocks: list[tuple[BlockRole, str]] = []
    for index, paragraph in enumerate(paragraphs):
        role = _role_for_text_block(paragraph, index)
        blocks.append((role, paragraph))
    return blocks


def _role_for_text_block(text: str, index: int) -> BlockRole:
    if index == 0 and len(text) <= 120:
        return BlockRole.TITLE
    if re.match(r"^(abstract|摘要)\b", text, flags=re.IGNORECASE):
        return BlockRole.ABSTRACT
    if len(text) <= 90 and not text.endswith((".", "。", "!", "！", "?", "？")):
        return BlockRole.HEADING
    return BlockRole.PARAGRAPH


def _blocks_to_virtual_document(
    doc_id: str,
    blocks: list[tuple[BlockRole, str]],
    intent: UserIntent,
) -> DocumentIR:
    pages: list[DocumentPage] = []
    current_blocks: list[DocumentBlock] = []
    page_index = 1
    reading_order = 0
    cursor_y = _PAGE_MARGIN
    page_height = intent.constraints.page_height_pt
    page_width = intent.constraints.page_width_pt
    font_size = intent.constraints.target_font_size_pt
    for role, text in blocks:
        height = _estimated_block_height(text, role, font_size)
        if current_blocks and cursor_y + height > page_height - _PAGE_MARGIN:
            pages.append(
                DocumentPage(
                    page_id=f"p{page_index}",
                    size=PageSize(width=page_width, height=page_height),
                    blocks=current_blocks,
                )
            )
            page_index += 1
            current_blocks = []
            cursor_y = _PAGE_MARGIN
        block_id = f"{doc_id}_text_{reading_order + 1:04d}"
        style_seed = StyleSeed(
            font_size=_font_size_for_role(role, font_size),
            bold=role in {BlockRole.TITLE, BlockRole.HEADING},
        )
        current_blocks.append(
            DocumentBlock(
                block_id=block_id,
                page_id=f"p{page_index}",
                role=role,
                bbox=BoundingBox(
                    x0=_PAGE_MARGIN,
                    y0=cursor_y,
                    x1=page_width - _PAGE_MARGIN,
                    y1=min(page_height - _PAGE_MARGIN, cursor_y + height),
                ),
                reading_order=reading_order,
                source_text=text,
                style_seed=style_seed,
            )
        )
        cursor_y += height + _PARAGRAPH_GAP_PT
        reading_order += 1
    if current_blocks:
        pages.append(
            DocumentPage(
                page_id=f"p{page_index}",
                size=PageSize(width=page_width, height=page_height),
                blocks=current_blocks,
            )
        )
    return DocumentIR(doc_id=doc_id, pages=pages)


def _estimated_block_height(text: str, role: BlockRole, font_size: float) -> float:
    line_units = max(1, int(_TEXT_WIDTH / max(font_size, 1)))
    estimated_lines = max(1, len(text) // line_units + 1)
    multiplier = 1.4 if role in {BlockRole.TITLE, BlockRole.HEADING} else 1.25
    return max(24.0, estimated_lines * font_size * multiplier)


def _font_size_for_role(role: BlockRole, base_font_size: float) -> float:
    if role == BlockRole.TITLE:
        return base_font_size + 5
    if role == BlockRole.HEADING:
        return base_font_size + 2
    return base_font_size


def _deterministic_image_summary(filename: str, intent: UserIntent) -> str:
    instruction = intent.instruction.strip()
    parts = [
        f"Image input {filename} was preserved as a visual asset.",
        "OCR provider is not configured, so this deterministic summary is used for local workflow validation.",
    ]
    if instruction:
        parts.append(f"User instruction: {instruction}")
    return " ".join(parts)


def _compact_text(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _document_text(document: DocumentIR) -> str:
    return " ".join(
        block.source_text
        for page in document.pages
        for block in sorted(page.blocks, key=lambda item: item.reading_order)
    )


def _section_hint_for_block(block: DocumentBlock) -> str | None:
    text = _compact_text(block.source_text, 80)
    if block.role == BlockRole.TITLE:
        return f"title: {text}" if text else "title"
    if block.role == BlockRole.HEADING:
        return f"heading: {text}" if text else "heading"
    if block.role == BlockRole.ABSTRACT:
        return "abstract"
    return None


def _document_structure_sections(
    document: DocumentIR,
    intent: UserIntent,
) -> list[DocumentStructureSection]:
    sections: list[DocumentStructureSection] = []
    seen_section_ids: set[str] = set()
    for page in document.pages:
        for block in sorted(page.blocks, key=lambda item: item.reading_order):
            section_kind = _section_kind_for_block(block)
            title = _section_title_for_block(block, section_kind)
            section_id = _unique_section_id(
                f"{section_kind.value}_{len(sections) + 1:02d}",
                seen_section_ids,
            )
            seen_section_ids.add(section_id)
            sections.append(
                DocumentStructureSection(
                    section_id=section_id,
                    kind=section_kind,
                    title=title,
                    level=_section_level_for_block(block, section_kind),
                    source_block_ids=[block.block_id],
                    required=_section_required_for_intent(intent, section_kind),
                    quality_flags=_section_quality_flags(block, section_kind),
                )
            )
    return sections


def _section_kind_for_block(block: DocumentBlock) -> SectionKind:
    text = _compact_text(block.source_text, 140)
    normalized = text.lower()
    list_of_figures_pattern = re.compile(
        r"^(list\s+of\s+figures|figures\s+list|图目录|插图目录)\b",
        re.I,
    )
    list_of_tables_pattern = re.compile(
        r"^(list\s+of\s+tables|tables\s+list|表目录|表格目录)\b",
        re.I,
    )
    if re.match(r"^(cover\s+page|封面)\b", text, re.I):
        return SectionKind.COVER
    if block.role == BlockRole.TITLE:
        return SectionKind.TITLE
    if block.role == BlockRole.ABSTRACT or re.match(r"^(abstract|摘要)\b", text, re.I):
        return SectionKind.ABSTRACT
    if re.match(r"^(keywords?|关键词|关键字)\b", text, re.I):
        return SectionKind.KEYWORDS
    if re.match(r"^(contents?|table\s+of\s+contents|目录)\b", text, re.I):
        return SectionKind.TOC
    if list_of_figures_pattern.match(text):
        return SectionKind.LIST_OF_FIGURES
    if list_of_tables_pattern.match(text):
        return SectionKind.LIST_OF_TABLES
    if re.match(r"^(references?|bibliography|参考文献)\b", text, re.I):
        return SectionKind.REFERENCES
    if re.match(r"^(appendix|附录)\b", text, re.I):
        return SectionKind.APPENDIX
    if re.match(r"^(acknowledg(e)?ments?|致谢)\b", text, re.I):
        return SectionKind.ACKNOWLEDGEMENTS
    if re.search(r"course\s+name|课程名称|课程名|submission\s+date|提交日期", normalized):
        return SectionKind.COURSE_INFO
    if re.search(
        r"student\s+name|student\s+id|student\s+number|group\s+members?|姓名|学号|小组成员|团队成员",
        normalized,
    ):
        return SectionKind.AUTHOR_INFO
    if re.search(r"实验目的|experimental\s+purpose|objective", normalized):
        return SectionKind.EXPERIMENT_PURPOSE
    if re.search(r"实验原理|theory|principles?", normalized):
        return SectionKind.EXPERIMENT_THEORY
    if re.search(r"实验步骤|procedure|method", normalized):
        return SectionKind.EXPERIMENT_STEPS
    if re.match(r"^(results?|结果)\b", text, re.I):
        return SectionKind.EXPERIMENT_RESULTS
    if re.match(r"^(analysis|分析)\b", text, re.I):
        return SectionKind.EXPERIMENT_ANALYSIS
    if re.search(r"结果分析|results?\s+and\s+discussion|analysis", normalized):
        return SectionKind.RESULT_ANALYSIS
    if re.match(r"^(conclusion|结论)\b", text, re.I):
        return SectionKind.CONCLUSION
    if block.role == BlockRole.HEADING:
        return SectionKind.HEADING
    if block.role == BlockRole.FIGURE:
        return SectionKind.FIGURE
    if block.role == BlockRole.TABLE:
        return SectionKind.TABLE
    if block.role == BlockRole.FORMULA:
        return SectionKind.FORMULA
    if block.role == BlockRole.REFERENCE:
        return SectionKind.REFERENCES
    return SectionKind.BODY


def _section_title_for_block(block: DocumentBlock, section_kind: SectionKind) -> str:
    text = _compact_text(block.source_text, 100)
    if section_kind == SectionKind.BODY:
        return ""
    return text or section_kind.value


def _section_level_for_block(block: DocumentBlock, section_kind: SectionKind) -> int:
    if section_kind in {
        SectionKind.TITLE,
        SectionKind.COVER,
        SectionKind.ABSTRACT,
        SectionKind.KEYWORDS,
        SectionKind.TOC,
        SectionKind.LIST_OF_FIGURES,
        SectionKind.LIST_OF_TABLES,
        SectionKind.REFERENCES,
        SectionKind.APPENDIX,
        SectionKind.ACKNOWLEDGEMENTS,
    }:
        return 1
    if block.role == BlockRole.HEADING:
        return 1
    return 2


def _section_required_for_intent(intent: UserIntent, section_kind: SectionKind) -> bool:
    if intent.task_intent.document_kind in {
        DocumentKind.UNDERGRADUATE_THESIS,
        DocumentKind.COURSE_PAPER,
        DocumentKind.PROPOSAL_REPORT,
    }:
        return section_kind in {
            SectionKind.TITLE,
            SectionKind.ABSTRACT,
            SectionKind.BODY,
            SectionKind.REFERENCES,
        }
    if intent.task_intent.document_kind == DocumentKind.LAB_REPORT:
        return section_kind in {
            SectionKind.TITLE,
            SectionKind.EXPERIMENT_PURPOSE,
            SectionKind.EXPERIMENT_THEORY,
            SectionKind.EXPERIMENT_STEPS,
            SectionKind.EXPERIMENT_RESULTS,
            SectionKind.EXPERIMENT_ANALYSIS,
            SectionKind.RESULT_ANALYSIS,
            SectionKind.CONCLUSION,
        }
    return section_kind in {SectionKind.TITLE, SectionKind.BODY}


def _section_quality_flags(
    block: DocumentBlock,
    section_kind: SectionKind,
) -> list[str]:
    flags: list[str] = []
    if section_kind == SectionKind.BODY and block.role == BlockRole.UNKNOWN:
        flags.append("section_kind_uncertain")
    if not block.source_text.strip():
        flags.append("empty_section_source")
    return flags


def _unique_section_id(candidate: str, seen: set[str]) -> str:
    if candidate not in seen:
        return candidate
    index = 2
    while f"{candidate}_{index}" in seen:
        index += 1
    return f"{candidate}_{index}"


def _missing_sections_for_intent(
    intent: UserIntent,
    sections: list[DocumentStructureSection],
) -> list[SectionKind]:
    present = {section.kind for section in sections}
    required_by_kind = {
        DocumentKind.UNDERGRADUATE_THESIS: {
            SectionKind.COVER,
            SectionKind.TITLE,
            SectionKind.ABSTRACT,
            SectionKind.KEYWORDS,
            SectionKind.TOC,
            SectionKind.BODY,
            SectionKind.REFERENCES,
            SectionKind.ACKNOWLEDGEMENTS,
        },
        DocumentKind.COURSE_PAPER: {
            SectionKind.TITLE,
            SectionKind.BODY,
            SectionKind.REFERENCES,
        },
        DocumentKind.PROPOSAL_REPORT: {
            SectionKind.TITLE,
            SectionKind.BODY,
            SectionKind.REFERENCES,
        },
        DocumentKind.LAB_REPORT: {
            SectionKind.TITLE,
            SectionKind.EXPERIMENT_PURPOSE,
            SectionKind.EXPERIMENT_THEORY,
            SectionKind.EXPERIMENT_STEPS,
            SectionKind.EXPERIMENT_RESULTS,
            SectionKind.EXPERIMENT_ANALYSIS,
            SectionKind.RESULT_ANALYSIS,
            SectionKind.CONCLUSION,
        },
        DocumentKind.BOOK_REPORT: {
            SectionKind.TITLE,
            SectionKind.HEADING,
            SectionKind.BODY,
        },
        DocumentKind.SOCIAL_PRACTICE_REPORT: {
            SectionKind.COVER,
            SectionKind.TITLE,
            SectionKind.TOC,
            SectionKind.HEADING,
            SectionKind.BODY,
        },
        DocumentKind.GROUP_ASSIGNMENT: {
            SectionKind.COVER,
            SectionKind.TITLE,
            SectionKind.COURSE_INFO,
            SectionKind.AUTHOR_INFO,
        },
        DocumentKind.HOMEWORK: {SectionKind.TITLE, SectionKind.BODY},
        DocumentKind.GENERIC_ACADEMIC: {SectionKind.TITLE, SectionKind.BODY},
    }
    required = set(required_by_kind.get(intent.task_intent.document_kind, set()))
    for requirement in intent.requirements:
        required.update(requirement.section_kinds)
    return sorted(required - present, key=lambda item: item.value)


def _uncertain_sections(sections: list[DocumentStructureSection]) -> list[str]:
    return [section.section_id for section in sections if section.quality_flags]


def _numbering_plan_for_intent(
    intent: UserIntent,
    sections: list[DocumentStructureSection],
) -> NumberingPlan:
    heading_section_ids = [
        section.section_id
        for section in sections
        if section.kind
        in {
            SectionKind.HEADING,
            SectionKind.EXPERIMENT_PURPOSE,
            SectionKind.EXPERIMENT_THEORY,
            SectionKind.EXPERIMENT_STEPS,
            SectionKind.EXPERIMENT_RESULTS,
            SectionKind.EXPERIMENT_ANALYSIS,
            SectionKind.RESULT_ANALYSIS,
            SectionKind.CONCLUSION,
        }
    ]
    figure_section_ids = [
        section.section_id for section in sections if section.kind == SectionKind.FIGURE
    ]
    table_section_ids = [
        section.section_id for section in sections if section.kind == SectionKind.TABLE
    ]
    formula_section_ids = [
        section.section_id for section in sections if section.kind == SectionKind.FORMULA
    ]
    reference_section_ids = [
        section.section_id for section in sections if section.kind == SectionKind.REFERENCES
    ]
    numbered = intent.typesetting_standard == TypesettingStandard.GB_T_7713_1_2025
    return NumberingPlan(
        heading_numbering=NumberingRule(
            enabled=numbered,
            style=NumberingStyle.ARABIC if numbered else NumberingStyle.NONE,
            section_ids=heading_section_ids,
        ),
        figure_numbering=NumberingRule(
            enabled=bool(figure_section_ids),
            style=NumberingStyle.ARABIC if figure_section_ids else NumberingStyle.NONE,
            section_ids=figure_section_ids,
        ),
        table_numbering=NumberingRule(
            enabled=bool(table_section_ids),
            style=NumberingStyle.ARABIC if table_section_ids else NumberingStyle.NONE,
            section_ids=table_section_ids,
        ),
        formula_numbering=NumberingRule(
            enabled=bool(formula_section_ids) and numbered,
            style=NumberingStyle.PARENTHESIZED
            if formula_section_ids and numbered
            else NumberingStyle.NONE,
            section_ids=formula_section_ids,
        ),
        reference_numbering=NumberingRule(
            enabled=bool(reference_section_ids),
            style=NumberingStyle.ARABIC if reference_section_ids else NumberingStyle.NONE,
            section_ids=reference_section_ids,
        ),
        toc_generation=TocGenerationPlan(
            enabled=any(section.kind == SectionKind.TOC for section in sections),
            max_level=3,
            section_ids=heading_section_ids,
        ),
    )


def _bibliography_plan_for_intent(
    intent: UserIntent,
    sections: list[DocumentStructureSection],
) -> BibliographyPlan:
    reference_section_ids = [
        section.section_id for section in sections if section.kind == SectionKind.REFERENCES
    ]
    return BibliographyPlan(
        citation_style=intent.bibliography_preference.citation_style,
        default_reason=intent.bibliography_preference.default_reason,
        section_ids=reference_section_ids,
    )


def _collect_source_flags(input_sources: list[InputSource]) -> list[str]:
    flags: list[str] = []
    for source in input_sources:
        flags.extend(source.quality_flags)
    return _unique(flags)


def _collect_asset_flags(assets: list[AssetIR]) -> list[str]:
    flags: list[str] = []
    for asset in assets:
        flags.extend(asset.quality_flags)
    return _unique(flags)


def _render_intent_for_block(
    role: BlockRole,
    intent: UserIntent,
    instruction: str,
) -> str:
    if role in {BlockRole.FIGURE, BlockRole.TABLE}:
        return "preserve_asset"
    if "gb/t 7713.1" in instruction or "gb-gb/t 7713.1" in instruction:
        if role in {BlockRole.TITLE, BlockRole.HEADING}:
            return "emphasis"
        if role in {BlockRole.REFERENCE, BlockRole.FOOTNOTE}:
            return "compact"
    if intent.style_intent == StyleIntent.SLIDE_LIKE and role in {
        BlockRole.TITLE,
        BlockRole.HEADING,
    }:
        return "callout"
    if intent.style_intent == StyleIntent.HANDOUT:
        return "compact"
    return "normal"


def _layout_asset_usage(asset: Asset, intent: UserIntent) -> str:
    if not intent.constraints.preserve_images:
        return "ignore"
    if not asset.path and asset.kind == "figure":
        return "ignore"
    return "preserve"


def _layout_asset_quality_flags(asset: Asset, intent: UserIntent) -> list[str]:
    flags: list[str] = []
    if not asset.path:
        flags.append("asset_missing_path")
    if not intent.constraints.preserve_images:
        flags.append("asset_suppressed_by_user_constraint")
    elif asset.kind == "figure" and not asset.path:
        flags.append("asset_suppressed_placeholder")
    return flags


def _priority_for_role(role: BlockRole) -> int:
    if role == BlockRole.TITLE:
        return 5
    if role in {BlockRole.HEADING, BlockRole.ABSTRACT}:
        return 4
    if role in {BlockRole.FOOTNOTE, BlockRole.REFERENCE}:
        return 2
    return 3


def _layout_plan_flags(
    intent: UserIntent,
    diagnostics: dict[str, Any],
    attempt: int,
) -> list[str]:
    flags: list[str] = []
    if intent.instruction.strip():
        flags.append("user_instruction_applied")
    if (
        "gb/t 7713.1" in intent.instruction.lower()
        or "gb-gb/t 7713.1" in intent.instruction.lower()
    ):
        flags.append("gb_t_7713_1_requested")
    if attempt > 1:
        flags.append("repair_attempt")
    if diagnostics:
        flags.append("renderer_diagnostics_considered")
    return flags


_BLOCKING_RENDERER_BLOCK_FLAGS = {
    "overflow_clipped",
    "missing_translation",
    "empty_translation",
    "asset_missing_path",
}


def _diagnostic_block_ids(diagnostics: dict[str, Any]) -> set[str]:
    block_ids: set[str] = set()
    layout_issues = diagnostics.get("layout_issues")
    if isinstance(layout_issues, list):
        for issue in layout_issues:
            if isinstance(issue, dict) and isinstance(issue.get("item_id"), str):
                block_ids.add(_source_block_id_from_render_block_id(issue["item_id"]))
    browser_overflows = diagnostics.get("browser_overflows")
    if isinstance(browser_overflows, list):
        for overflow in browser_overflows:
            if not isinstance(overflow, dict):
                continue
            source_block_id = overflow.get("source_block_id")
            block_id = overflow.get("block_id")
            if isinstance(source_block_id, str) and source_block_id:
                block_ids.add(source_block_id)
            elif isinstance(block_id, str) and block_id:
                block_ids.add(_source_block_id_from_render_block_id(block_id))
    pages = diagnostics.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            flagged_blocks = page.get("flagged_blocks")
            if not isinstance(flagged_blocks, list):
                continue
            for block in flagged_blocks:
                if not isinstance(block, dict):
                    continue
                quality_flags = block.get("quality_flags")
                if not isinstance(quality_flags, list) or not (
                    set(str(flag) for flag in quality_flags) & _BLOCKING_RENDERER_BLOCK_FLAGS
                ):
                    continue
                block_id = block.get("block_id")
                if isinstance(block_id, str):
                    block_ids.add(_source_block_id_from_render_block_id(block_id))
    return block_ids


def _source_block_id_from_render_block_id(block_id: str) -> str:
    return block_id.split("__reflow_", 1)[0].split("__cont_", 1)[0]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result
