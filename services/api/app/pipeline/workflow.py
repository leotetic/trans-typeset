from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from pdf_translator_schema import (
    Asset,
    AssetIR,
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    InputKind,
    InputSource,
    LayoutIntentAsset,
    LayoutIntentBlock,
    LayoutIntentPlan,
    OutputKind,
    PageSize,
    SemanticAssetSignal,
    SemanticBlockSignal,
    SemanticLayoutAnalysis,
    StyleIntent,
    TypesettingStandard,
    UserIntent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepName,
    WorkflowStepStatus,
    validate_layout_intent_plan,
)
from pdf_translator_schema.models import DocumentBlock, StyleSeed, UserConstraints
from pdf_translator_schema.validation import LayoutIntentPlanValidationError

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
) -> UserIntent:
    normalized_instruction = (instruction or "").strip()
    return UserIntent(
        target_lang=target_lang,
        output_kind=_coerce_output_kind(output_kind),
        style_intent=_coerce_style_intent(style_intent),
        typesetting_standard=_typesetting_standard_for_instruction(normalized_instruction),
        instruction=normalized_instruction,
        constraints=_coerce_user_constraints(constraints),
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
) -> tuple[DocumentIR, list[AssetIR]]:
    suffix = image_path.suffix.lower() or ".png"
    asset_id = f"{doc_id}_asset_0001"
    asset_target = storage.asset_dir(doc_id) / f"{asset_id}{suffix}"
    shutil.copyfile(image_path, asset_target)
    asset_url = f"/api/documents/{doc_id}/assets/{asset_target.name}"
    summary = _deterministic_image_summary(filename, intent)
    document = DocumentIR(
        doc_id=doc_id,
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=_DEFAULT_PAGE_WIDTH, height=_DEFAULT_PAGE_HEIGHT),
                blocks=[
                    DocumentBlock(
                        block_id=f"{doc_id}_image_summary_0001",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(
                            x0=_PAGE_MARGIN,
                            y0=530,
                            x1=_DEFAULT_PAGE_WIDTH - _PAGE_MARGIN,
                            y1=690,
                        ),
                        reading_order=0,
                        source_text=summary,
                        style_seed=StyleSeed(font_size=11),
                    )
                ],
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
        ocr_text=summary,
        alt_text=f"Image input: {filename}",
        source_block_ids=[f"{doc_id}_image_summary_0001"],
        confidence=0.35,
        quality_flags=["deterministic_ocr_mock", "ocr_uncertain"],
    )
    return document, [asset_ir]


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
    blocks: list[LayoutIntentBlock] = []
    for page in document.pages:
        for block in sorted(page.blocks, key=lambda item: item.reading_order):
            role = block.role
            render_intent = _render_intent_for_block(role, intent, instruction)
            quality_flags: list[str] = []
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
                    quality_flags=quality_flags,
                )
            )
    assets = [
        LayoutIntentAsset(
            asset_id=asset.asset_id,
            usage=_layout_asset_usage(asset, intent),
            quality_flags=_layout_asset_quality_flags(asset, intent),
        )
        for page in document.pages
        for asset in page.assets
    ]
    plan = LayoutIntentPlan(
        plan_id=f"{document.doc_id}_layout_intent_{attempt:02d}",
        doc_id=document.doc_id,
        target_lang=intent.target_lang,
        output_kind=intent.output_kind,
        style_intent=intent.style_intent,
        blocks=blocks,
        assets=assets,
        quality_flags=_layout_plan_flags(intent, diagnostics, attempt),
    )
    if semantic_analysis is not None:
        plan = plan.model_copy(
            update={
                "quality_flags": _unique(
                    [*plan.quality_flags, "semantic_analysis_considered"]
                )
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
            confidence=0.75
            if _layout_asset_usage(asset, intent) == "preserve"
            else 0.35,
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
    confidence_values = [
        signal.confidence for signal in block_signals
    ] + [signal.confidence for signal in asset_signals]
    confidence = (
        sum(confidence_values) / len(confidence_values)
        if confidence_values
        else 0.5
    )
    return SemanticLayoutAnalysis(
        analysis_id=f"{document.doc_id}_semantic_analysis_01",
        doc_id=document.doc_id,
        target_lang=intent.target_lang,
        block_signals=block_signals,
        asset_signals=asset_signals,
        section_hints=_unique([hint for hint in section_hints if hint]),
        confidence=round(confidence, 4),
        quality_flags=quality_flags,
    )


def render_evaluation_summary(renderer_diagnostics: dict[str, Any]) -> dict[str, Any]:
    quality_counts = renderer_diagnostics.get("quality_flag_counts")
    layout_issues = renderer_diagnostics.get("layout_issues")
    if not isinstance(quality_counts, dict):
        quality_counts = {}
    if not isinstance(layout_issues, list):
        layout_issues = []
    blocking_flags = {
        flag: count
        for flag, count in quality_counts.items()
        if flag in {"overflow_clipped", "asset_missing_path", "missing_translation"}
    }
    return {
        "kind": "render_evaluation",
        "accepted": not blocking_flags and not layout_issues,
        "quality_flag_counts": quality_counts,
        "layout_issue_count": len(layout_issues),
        "blocking_flags": blocking_flags,
        "repair_recommended": bool(blocking_flags or layout_issues),
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


def _coerce_output_kind(value: str | None) -> OutputKind:
    if not value:
        return OutputKind.TRANSLATION
    try:
        return OutputKind(value)
    except ValueError:
        return OutputKind.TRANSLATION


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
    if "gb/t 7713.1" in intent.instruction.lower() or "gb-gb/t 7713.1" in intent.instruction.lower():
        flags.append("gb_t_7713_1_requested")
    if attempt > 1:
        flags.append("repair_attempt")
    if diagnostics:
        flags.append("renderer_diagnostics_considered")
    return flags


def _diagnostic_block_ids(diagnostics: dict[str, Any]) -> set[str]:
    block_ids: set[str] = set()
    layout_issues = diagnostics.get("layout_issues")
    if isinstance(layout_issues, list):
        for issue in layout_issues:
            if isinstance(issue, dict) and isinstance(issue.get("item_id"), str):
                block_ids.add(issue["item_id"])
    pages = diagnostics.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            flagged_blocks = page.get("flagged_blocks")
            if not isinstance(flagged_blocks, list):
                continue
            for block in flagged_blocks:
                if isinstance(block, dict) and isinstance(block.get("block_id"), str):
                    block_ids.add(block["block_id"])
    return block_ids


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result
