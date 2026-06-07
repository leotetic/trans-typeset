from pathlib import Path

from app.main import app
from app.routes import documents as documents_route
from app.storage import Storage
from fastapi.testclient import TestClient


def test_api_health() -> None:
    client = TestClient(app)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_job_not_found(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.get("/api/jobs/job_missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


def test_non_pdf_upload_returns_400(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.post(
        "/api/documents",
        files={"file": ("notes.txt", b"not a pdf", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only PDF uploads are supported"


def test_pdf_extension_with_non_pdf_content_returns_400(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.post(
        "/api/documents",
        files={"file": ("paper.pdf", b"plain text", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only PDF uploads are supported"


def test_pdf_upload_queues_job(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)

    async def noop_process(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(documents_route, "process_document_job", noop_process)
    client = TestClient(app)

    response = client.post(
        "/api/documents",
        files={"file": ("paper.pdf", b"%PDF-1.7\n%%EOF", "application/pdf")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"].startswith("job_")
    assert payload["doc_id"].startswith("doc_")
    status = storage.load_status(payload["job_id"])
    assert status.status == "queued"
    assert status.error is None
