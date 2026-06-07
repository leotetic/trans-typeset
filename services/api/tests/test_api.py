from pathlib import Path

from app.config import Settings
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


def test_unsupported_target_language_returns_400(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.post(
        "/api/documents",
        data={"target_lang": "fr-FR"},
        files={"file": ("paper.pdf", b"%PDF-1.7\n%%EOF", "application/pdf")},
    )

    assert response.status_code == 400
    assert "Unsupported target language" in response.json()["detail"]


def test_oversized_pdf_upload_returns_413(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    monkeypatch.setattr(
        documents_route,
        "settings",
        Settings(storage_dir=tmp_path, max_upload_bytes=12),
    )
    client = TestClient(app)

    response = client.post(
        "/api/documents",
        files={"file": ("paper.pdf", b"%PDF-1.7\nlarge content", "application/pdf")},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "PDF upload is too large"


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


def test_preview_not_found_returns_404(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.get("/api/documents/doc_missing/preview")

    assert response.status_code == 404
    assert response.json()["detail"] == "Preview not found"


def test_preview_head_not_found_returns_404(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.head("/api/documents/doc_missing/preview")

    assert response.status_code == 404


def test_preview_returns_html(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    storage.save_preview_html("doc_1", "<html><body>preview</body></html>")
    client = TestClient(app)

    response = client.get("/api/documents/doc_1/preview")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "preview" in response.text


def test_preview_head_returns_html_metadata(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    storage.save_preview_html("doc_1", "<html><body>preview</body></html>")
    client = TestClient(app)

    response = client.head("/api/documents/doc_1/preview")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_download_not_found_returns_404(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.get("/api/documents/doc_missing/download")

    assert response.status_code == 404
    assert response.json()["detail"] == "Translated PDF not found"


def test_download_head_not_found_returns_404(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.head("/api/documents/doc_missing/download")

    assert response.status_code == 404


def test_download_returns_pdf(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    storage.output_pdf_path("doc_1").write_bytes(b"%PDF-1.7\n%%EOF")
    client = TestClient(app)

    response = client.get("/api/documents/doc_1/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")


def test_download_head_returns_pdf_metadata(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    storage.output_pdf_path("doc_1").write_bytes(b"%PDF-1.7\n%%EOF")
    client = TestClient(app)

    response = client.head("/api/documents/doc_1/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
