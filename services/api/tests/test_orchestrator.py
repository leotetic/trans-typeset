import asyncio
from pathlib import Path

import pytest
from app.models import JobState
from app.pipeline import orchestrator
from app.storage import Storage


def test_process_document_job_persists_frontend_visible_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)

    def fail_parse(pdf_path: Path, doc_id: str):
        raise ValueError("parse failed clearly")

    monkeypatch.setattr(orchestrator, "parse_pdf", fail_parse)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            tmp_path / "paper.pdf",
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    assert status.status == JobState.FAILED
    assert status.error == "parse failed clearly"
    assert status.message == "Failed"
