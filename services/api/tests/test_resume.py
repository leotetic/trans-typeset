import asyncio
from pathlib import Path

from app.models import JobState, JobStatus
from app.pipeline import resume
from app.storage import Storage


def test_resume_incomplete_jobs_schedules_existing_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    (storage.uploads / "doc_1.pdf").write_bytes(b"%PDF-1.7\n%%EOF")
    storage.save_status(
        JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="paper.pdf",
            target_lang="zh-CN",
            status=JobState.TRANSLATING,
            progress=0.5,
            message="Translating",
        )
    )
    scheduled: list[tuple] = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append(("task",))
        return object()

    async def fake_process(*args):
        return None

    monkeypatch.setattr(resume.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(resume, "process_document_job", fake_process)

    resumed = asyncio.run(resume.resume_incomplete_jobs(storage))

    assert resumed == 1
    assert scheduled == [("task",)]


def test_resume_incomplete_jobs_marks_missing_upload_failed(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    storage.save_status(
        JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="paper.pdf",
            target_lang="zh-CN",
            status=JobState.QUEUED,
            progress=0,
            message="Queued",
        )
    )

    resumed = asyncio.run(resume.resume_incomplete_jobs(storage))

    status = storage.load_status("job_1")
    assert resumed == 0
    assert status.status == JobState.FAILED
    assert status.error == "Original upload not found during resume"
