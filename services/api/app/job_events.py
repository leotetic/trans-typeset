from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from .models import ChunkProgress, JobLogEvent, JobLogResponse, JobState, JobStatus
from .storage import Storage

ArtifactSpecs = Mapping[str, tuple[str, str]]

_WORKFLOW_TITLES = {
    "read_input": "Read input",
    "analyze_intent": "Analyze intent",
    "semantic_recognize": "Recognize document semantics",
    "build_plan": "Build layout plan",
    "validate_plan": "Validate layout plan",
    "translate": "Translate chunks",
    "render": "Render preview",
    "evaluate_render": "Evaluate rendered layout",
    "repair": "Repair layout plan",
    "export_pdf": "Export PDF",
    "complete": "Complete workflow",
    "fail": "Workflow failed",
}

_JOB_TITLES = {
    JobState.QUEUED: "Job queued",
    JobState.PARSING: "Parsing input",
    JobState.TRANSLATING: "Translating content",
    JobState.RENDERING: "Rendering output",
    JobState.COMPLETED: "Job completed",
    JobState.FAILED: "Job failed",
    JobState.CANCELED: "Job canceled",
}


def build_job_log_response(
    status: JobStatus,
    storage: Storage,
    artifact_specs: ArtifactSpecs,
    *,
    limit: int = 80,
) -> JobLogResponse:
    events: list[JobLogEvent] = [_job_event(status)]
    workflow_payload, stale_workflow = _load_matching_workflow(storage, status)
    if workflow_payload is not None:
        events.extend(_workflow_events(status.job_id, workflow_payload))

    chunk_progress = status.chunks or (
        [] if stale_workflow else _load_chunk_progress(storage, status.doc_id)
    )
    translate_sequence = _first_workflow_sequence(events, "translate")
    events.extend(_chunk_events(status.job_id, chunk_progress, translate_sequence))
    if status.doc_id and not stale_workflow:
        artifact_event = _artifact_event(status, storage, artifact_specs)
        if artifact_event is not None:
            events.append(artifact_event)

    events = _dedupe_events(events)
    events.sort(key=lambda event: (event.sequence, event.id))
    if limit > 0:
        events = events[-limit:]
    return JobLogResponse(
        job_id=status.job_id,
        doc_id=status.doc_id,
        status=status.status,
        progress=status.progress,
        message=status.error or status.message,
        events=events,
    )


def _job_event(status: JobStatus) -> JobLogEvent:
    return JobLogEvent(
        id=_event_id(
            status.job_id,
            "job",
            status.status.value,
            f"{status.progress:.4f}",
            status.message,
            status.error or "",
        ),
        sequence=10,
        source="job",
        level=_job_level(status.status),
        phase=status.status.value,
        title=_JOB_TITLES.get(status.status, "Job status"),
        message=status.error or status.message,
        progress=status.progress,
        details=_compact_details(
            [
                f"File: {status.filename}",
                f"Target: {status.target_lang}" if status.target_lang else "",
            ]
        ),
    )


def _workflow_events(job_id: str, payload: dict[str, Any]) -> list[JobLogEvent]:
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return []
    events: list[JobLogEvent] = []
    for index, item in enumerate(steps):
        if not isinstance(item, dict):
            continue
        phase = _as_text(item.get("name")) or "workflow"
        status = _as_text(item.get("status")) or "running"
        message = _as_text(item.get("message")) or _WORKFLOW_TITLES.get(phase, phase)
        error = _as_text(item.get("error"))
        progress = _progress_value(item.get("progress"))
        sequence = 100 + index * 100
        events.append(
            JobLogEvent(
                id=_event_id(
                    job_id,
                    "workflow",
                    _as_text(item.get("step_id")) or str(index),
                    phase,
                    status,
                    message,
                    error or "",
                ),
                sequence=sequence,
                source="workflow",
                level=_workflow_level(status, error),
                phase=phase,
                title=_workflow_title(phase, status),
                message=error or message,
                progress=progress,
                details=_workflow_details(item),
            )
        )
    return events


def _chunk_events(
    job_id: str,
    chunks: list[ChunkProgress],
    translate_sequence: int | None,
) -> list[JobLogEvent]:
    base_sequence = (translate_sequence or 600) + 1
    events: list[JobLogEvent] = []
    for chunk in chunks:
        status = chunk.status or "queued"
        message = chunk.error or chunk.message
        events.append(
            JobLogEvent(
                id=_event_id(
                    job_id,
                    "chunk",
                    chunk.chunk_id,
                    status,
                    f"{chunk.progress:.4f}",
                    message,
                ),
                sequence=base_sequence + chunk.index,
                source="chunk",
                level=_chunk_level(status, chunk.error),
                phase="translate",
                title=f"Chunk {chunk.index}/{chunk.total}: {status}",
                message=message,
                progress=chunk.progress,
                details=_compact_details(chunk.quality_flags),
            )
        )
    return events


def _artifact_event(
    status: JobStatus,
    storage: Storage,
    artifact_specs: ArtifactSpecs,
) -> JobLogEvent | None:
    if not status.doc_id:
        return None
    available: list[str] = []
    for name, (filename, _kind) in artifact_specs.items():
        if name == "document-ir":
            exists = (storage.documents / f"{status.doc_id}.json").exists()
        else:
            exists = storage.output_json_path(status.doc_id, filename).exists()
        if exists:
            available.append(name)
    if not available:
        return None
    level = "success" if status.status == JobState.COMPLETED else "info"
    return JobLogEvent(
        id=_event_id(status.job_id, "artifact", *available),
        sequence=9000,
        source="artifact",
        level=level,
        phase="artifacts",
        title="Artifacts updated",
        message=f"{len(available)} debug artifact(s) available",
        progress=status.progress,
        details=available[:12],
    )


def _load_matching_workflow(
    storage: Storage,
    status: JobStatus,
) -> tuple[dict[str, Any] | None, bool]:
    if not status.doc_id:
        return None, False
    try:
        payload = storage.read_output_json(status.doc_id, "workflow-run.json")
    except Exception:
        return None, False
    if not isinstance(payload, dict):
        return None, False
    workflow_job_id = payload.get("job_id")
    if workflow_job_id and workflow_job_id != status.job_id:
        return None, True
    return payload, False


def _load_chunk_progress(storage: Storage, doc_id: str | None) -> list[ChunkProgress]:
    if not doc_id:
        return []
    try:
        payload = storage.read_output_json(doc_id, "translation-progress.json")
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    chunks: list[ChunkProgress] = []
    for item in payload:
        try:
            chunks.append(ChunkProgress.model_validate(item))
        except Exception:
            continue
    return chunks


def _workflow_details(item: dict[str, Any]) -> list[str]:
    diagnostics = item.get("diagnostics")
    details: list[str] = []
    input_artifacts = _string_list(item.get("input_artifacts"))
    output_artifacts = _string_list(item.get("output_artifacts"))
    if input_artifacts:
        details.append(f"Inputs: {', '.join(input_artifacts[:6])}")
    if output_artifacts:
        details.append(f"Outputs: {', '.join(output_artifacts[:6])}")
    if isinstance(diagnostics, dict):
        flags = _string_list(diagnostics.get("quality_flags"))
        if flags:
            details.append(f"Flags: {', '.join(flags[:6])}")
        if diagnostics.get("repair_recommended") is True:
            details.append("Repair recommended")
        if diagnostics.get("accepted") is False:
            details.append("Render QA needs review")
        if diagnostics.get("model_used") is False:
            details.append("Using deterministic fallback")
        if diagnostics.get("planner_fallback") is True:
            details.append("Planner fallback used")
    return _compact_details(details)


def _workflow_title(phase: str, status: str) -> str:
    title = _WORKFLOW_TITLES.get(phase, phase.replace("_", " ").title())
    if status == "running":
        return f"{title} started"
    if status == "completed":
        return f"{title} completed"
    if status == "repaired":
        return f"{title} repaired"
    if status == "failed":
        return f"{title} failed"
    if status == "skipped":
        return f"{title} skipped"
    return title


def _job_level(status: JobState) -> str:
    if status == JobState.COMPLETED:
        return "success"
    if status == JobState.FAILED:
        return "error"
    if status == JobState.CANCELED:
        return "warning"
    return "info"


def _workflow_level(status: str, error: str | None) -> str:
    if error or status == "failed":
        return "error"
    if status == "completed":
        return "success"
    if status in {"skipped", "repaired"}:
        return "warning"
    return "info"


def _chunk_level(status: str, error: str | None) -> str:
    if error or status == "failed":
        return "error"
    if status == "completed":
        return "success"
    if status == "canceled":
        return "warning"
    return "info"


def _first_workflow_sequence(
    events: list[JobLogEvent],
    phase: str,
) -> int | None:
    for event in events:
        if event.source == "workflow" and event.phase == phase:
            return event.sequence
    return None


def _dedupe_events(events: list[JobLogEvent]) -> list[JobLogEvent]:
    seen: set[str] = set()
    result: list[JobLogEvent] = []
    for event in events:
        if event.id in seen:
            continue
        seen.add(event.id)
        result.append(event)
    return result


def _event_id(job_id: str, *parts: str) -> str:
    raw = "\x1f".join([job_id, *parts])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{job_id}:{digest}"


def _progress_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(1, max(0, float(value)))


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _compact_details(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        compact = " ".join(value.split())
        if compact:
            result.append(compact[:240])
    return result
