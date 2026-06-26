from __future__ import annotations

from pdf_translator_schema import UserIntent

from ..jobs import schedule_job
from ..models import JobState
from ..runtime_config import effective_runtime_config
from ..storage import Storage
from .orchestrator import process_document_continuation_job, process_document_job

RESUMABLE_STATES = {
    JobState.QUEUED,
    JobState.PARSING,
    JobState.TRANSLATING,
    JobState.RENDERING,
}


async def resume_incomplete_jobs(storage: Storage, limit: int = 100) -> int:
    resumed = 0
    job_max_concurrency = max(
        1,
        int(effective_runtime_config(storage)["translation_concurrency"]),
    )
    for status in storage.list_statuses(limit):
        if status.status not in RESUMABLE_STATES or not status.doc_id:
            continue
        pdf_path = storage.find_upload(status.doc_id, role="content")
        document_ir_path = storage.documents / f"{status.doc_id}.json"
        can_continue_from_artifacts = (
            status.status in {JobState.TRANSLATING, JobState.RENDERING}
            and document_ir_path.exists()
            and storage.output_json_path(status.doc_id, "translation-chunks.json").exists()
        )
        if pdf_path is None and not can_continue_from_artifacts:
            status.status = JobState.FAILED
            status.progress = 1
            status.message = "Failed"
            status.error = "Original upload not found during resume"
            storage.save_status(status)
            continue
        target_lang = status.target_lang or "zh-CN"
        layout_pdf_path = storage.find_upload(status.doc_id, role="layout")
        user_intent = _load_user_intent(storage, status.doc_id)
        if can_continue_from_artifacts:
            schedule_job(
                process_document_continuation_job,
                status.job_id,
                status.doc_id,
                status.filename,
                pdf_path,
                target_lang,
                user_intent,
                layout_pdf_path,
                max_concurrency=job_max_concurrency,
                tracked_job_id=status.job_id,
            )
            resumed += 1
            continue
        schedule_job(
            process_document_job,
            status.job_id,
            status.doc_id,
            status.filename,
            pdf_path,
            target_lang,
            user_intent,
            layout_pdf_path,
            max_concurrency=job_max_concurrency,
            tracked_job_id=status.job_id,
        )
        resumed += 1
    return resumed


def _load_user_intent(storage: Storage, doc_id: str) -> UserIntent | None:
    try:
        return UserIntent.model_validate(storage.read_output_json(doc_id, "user-intent.json"))
    except Exception:
        return None
