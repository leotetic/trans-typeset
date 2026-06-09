from __future__ import annotations

from ..jobs import schedule_job
from ..models import JobState
from ..runtime_config import effective_runtime_config
from ..storage import Storage
from .orchestrator import process_document_job

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
        if pdf_path is None:
            status.status = JobState.FAILED
            status.progress = 1
            status.message = "Failed"
            status.error = "Original upload not found during resume"
            storage.save_status(status)
            continue
        target_lang = status.target_lang or "zh-CN"
        layout_pdf_path = storage.find_upload(status.doc_id, role="layout")
        schedule_job(
            process_document_job,
            status.job_id,
            status.doc_id,
            status.filename,
            pdf_path,
            target_lang,
            None,
            layout_pdf_path,
            max_concurrency=job_max_concurrency,
        )
        resumed += 1
    return resumed
