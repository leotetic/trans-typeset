import asyncio
from pathlib import Path

from app.models import JobState, JobStatus
from app.pipeline import resume
from app.storage import Storage
from pdf_translator_schema import (
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    SourceBlock,
    TranslationChunk,
)
from pdf_translator_schema.models import DocumentBlock


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
    assert scheduled[0][1][:5] == (
        "job_1",
        "doc_1",
        "paper.pdf",
        storage.uploads / "doc_1.pdf",
        "zh-CN",
    )
    assert scheduled[0][1][5] is None
    assert scheduled[0][1][6] is None
    assert scheduled[0][2]["max_concurrency"] == 4


def test_resume_incomplete_jobs_schedules_layout_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    storage.write_runtime_config({"translation_concurrency": 4})
    content_path = storage.uploads / "doc_1.content.pdf"
    layout_path = storage.uploads / "doc_1.layout.pdf"
    content_path.write_bytes(b"%PDF-1.7\n%%EOF")
    layout_path.write_bytes(b"%PDF-1.7\n%%EOF")
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
    assert scheduled[0][1][3] == content_path
    assert scheduled[0][1][6] == layout_path
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


def test_resume_translating_job_uses_continuation_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    storage.write_runtime_config({"translation_concurrency": 4})
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=300, height=400),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="Alpha",
                    )
                ],
            )
        ],
    )
    storage.save_document_ir(document)
    storage.write_json(
        "doc_1",
        "translation-chunks.json",
        [
            TranslationChunk(
                chunk_id="chunk_1",
                source_blocks=[
                    SourceBlock(
                        block_id="b1",
                        role=BlockRole.PARAGRAPH,
                        source_text="Alpha",
                    )
                ],
            ).model_dump()
        ],
    )
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

    async def fake_continue(*args):
        return None

    def fake_schedule_job(func, *args, **kwargs):
        scheduled.append((func, args, kwargs))

    monkeypatch.setattr(resume, "schedule_job", fake_schedule_job)
    monkeypatch.setattr(resume, "process_document_continuation_job", fake_continue)

    resumed = asyncio.run(resume.resume_incomplete_jobs(storage))

    assert resumed == 1
    assert scheduled[0][0] is fake_continue
    assert scheduled[0][1][:5] == (
        "job_1",
        "doc_1",
        "paper.pdf",
        None,
        "zh-CN",
    )
    assert scheduled[0][2]["max_concurrency"] == 4
    assert scheduled[0][2]["tracked_job_id"] == "job_1"
