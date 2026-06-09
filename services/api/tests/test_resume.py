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
    storage.write_runtime_config({"translation_concurrency": 4})
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

    async def fake_process(*args):
        return None

    def fake_schedule_job(func, *args, **kwargs):
        scheduled.append((func, args, kwargs))

    monkeypatch.setattr(resume, "schedule_job", fake_schedule_job)
    monkeypatch.setattr(resume, "process_document_job", fake_process)

    resumed = asyncio.run(resume.resume_incomplete_jobs(storage))

    assert resumed == 1
    assert scheduled[0][0] is fake_process
    assert scheduled[0][1] == (
        "job_1",
        "doc_1",
        "paper.pdf",
        storage.uploads / "doc_1.pdf",
        "zh-CN",
    )
    assert scheduled[0][2]["max_concurrency"] == 4


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
