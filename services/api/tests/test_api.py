from pathlib import Path

import pytest
from app import config as config_module
from app.config import Settings
from app.main import app
from app import runtime_config
from app.routes import documents as documents_route
from app.storage import Storage
from fastapi.testclient import TestClient
from pdf_translator_schema import (
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    RenderDefaults,
)
from pdf_translator_schema.models import DocumentBlock


def test_api_health() -> None:
    client = TestClient(app)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_returns_runtime_settings_without_api_key(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    config = Settings(
        openai_base_url="https://models.example.test/v1",
        openai_api_key="secret-value",
        openai_api_key_from_env=True,
        openai_model="paper-model",
        default_target_lang="ja-JP",
        allowed_target_langs=("ja-JP", "zh-CN"),
        max_upload_bytes=1234,
        translation_concurrency=3,
        translator_max_attempts=4,
        render_font_stack=("Example Sans", "serif"),
        render_line_height=1.44,
        render_paragraph_spacing_em=0.3,
        render_min_font_scale=0.81,
    )
    monkeypatch.setattr(documents_route, "storage", storage)
    monkeypatch.setattr(documents_route, "settings", config)
    monkeypatch.setattr(runtime_config, "settings", config)
    client = TestClient(app)

    response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_target_lang"] == "ja-JP"
    assert payload["allowed_target_langs"] == ["ja-JP", "zh-CN"]
    assert payload["max_upload_bytes"] == 1234
    assert payload["translator_provider"] == "openai-compatible"
    assert payload["openai_base_url"] == "https://models.example.test/v1"
    assert payload["openai_model"] == "paper-model"
    assert payload["openai_api_key_configured"] is True
    assert payload["translation_concurrency"] == 3
    assert payload["translator_max_attempts"] == 4
    assert payload["render_defaults"]["target_lang"] == "ja-JP"
    assert payload["render_defaults"]["font_stack"] == ["Example Sans", "serif"]
    assert payload["render_defaults"]["line_height"] == 1.44
    assert payload["render_defaults"]["paragraph_spacing_em"] == 0.3
    assert payload["render_defaults"]["overflow_policy"]["min_font_scale"] == 0.81
    assert "secret-value" not in response.text


def test_load_settings_prefers_dotenv_provider_values_over_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "_dotenv_values",
        lambda: {
            "OPENAI_BASE_URL": "https://dotenv.example.test/v1",
            "OPENAI_API_KEY": "dotenv-secret",
            "OPENAI_MODEL": "dotenv-model",
        },
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://shell.example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "shell-secret")
    monkeypatch.setenv("OPENAI_MODEL", "shell-model")

    loaded = config_module.load_settings()

    assert loaded.openai_base_url == "https://dotenv.example.test/v1"
    assert loaded.openai_api_key == "dotenv-secret"
    assert loaded.openai_api_key_from_env is True
    assert loaded.openai_model == "dotenv-model"


def test_config_env_provider_overrides_stale_persisted_key(
    tmp_path: Path, monkeypatch
) -> None:
    storage = Storage(tmp_path)
    storage.write_runtime_config(
        {
            "openai_base_url": "https://stale.example.test/v1",
            "openai_api_key": "stale-secret",
            "openai_model": "stale-model",
        }
    )
    config = Settings(
        openai_base_url="https://env.example.test/v1",
        openai_api_key="env-secret",
        openai_api_key_from_env=True,
        openai_model="env-model",
    )
    monkeypatch.setattr(documents_route, "storage", storage)
    monkeypatch.setattr(runtime_config, "settings", config)
    client = TestClient(app)

    response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["translator_provider"] == "openai-compatible"
    assert payload["openai_base_url"] == "https://env.example.test/v1"
    assert payload["openai_model"] == "env-model"
    assert payload["openai_api_key_configured"] is True
    assert "env-secret" not in response.text
    assert "stale-secret" not in response.text


def test_config_uses_persisted_provider_when_env_has_no_key(
    tmp_path: Path, monkeypatch
) -> None:
    storage = Storage(tmp_path)
    storage.write_runtime_config(
        {
            "openai_base_url": "https://persisted.example.test/v1",
            "openai_api_key": "persisted-secret",
            "openai_model": "persisted-model",
        }
    )
    config = Settings(
        openai_base_url="https://env.example.test/v1",
        openai_api_key="",
        openai_api_key_from_env=False,
        openai_model="env-model",
    )
    monkeypatch.setattr(documents_route, "storage", storage)
    monkeypatch.setattr(runtime_config, "settings", config)
    client = TestClient(app)

    response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["translator_provider"] == "openai-compatible"
    assert payload["openai_base_url"] == "https://persisted.example.test/v1"
    assert payload["openai_model"] == "persisted-model"
    assert payload["openai_api_key_configured"] is True
    assert "persisted-secret" not in response.text


def test_update_config_persists_runtime_settings_without_leaking_key(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(documents_route, "storage", storage)
    monkeypatch.setattr(runtime_config, "settings", config)
    render_defaults = RenderDefaults(
        target_lang="zh-CN",
        font_stack=["Noto Sans CJK SC", "serif"],
        line_height=1.5,
        paragraph_spacing_em=0.2,
    ).model_dump()
    render_defaults["overflow_policy"]["strategy"] = "scale_then_continue"
    render_defaults["overflow_policy"]["min_font_scale"] = 0.72
    render_defaults["overflow_policy"]["allow_box_expansion"] = False
    client = TestClient(app)

    response = client.put(
        "/api/config",
        json={
            "default_target_lang": "zh-CN",
            "openai_base_url": "https://models.example.test/v1/",
            "openai_model": "paper-model",
            "openai_api_key": "secret-key",
            "translation_concurrency": 4,
            "translator_max_attempts": 3,
            "render_defaults": render_defaults,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["translator_provider"] == "openai-compatible"
    assert payload["openai_base_url"] == "https://models.example.test/v1"
    assert payload["openai_model"] == "paper-model"
    assert payload["openai_api_key_configured"] is True
    assert payload["translation_concurrency"] == 4
    assert payload["translator_max_attempts"] == 3
    assert payload["render_defaults"]["font_stack"] == ["Noto Sans CJK SC", "serif"]
    assert payload["render_defaults"]["line_height"] == 1.5
    assert payload["render_defaults"]["overflow_policy"]["min_font_scale"] == 0.72
    assert "secret-key" not in response.text
    assert storage.read_runtime_config()["openai_api_key"] == "secret-key"
    assert storage.read_runtime_config()["render_defaults"]["line_height"] == 1.5


def test_update_config_rejects_unsupported_default_language(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.put("/api/config", json={"default_target_lang": "fr-FR"})

    assert response.status_code == 400
    assert "Unsupported target language" in response.json()["detail"]


def test_job_not_found(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.get("/api/jobs/job_missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


def test_list_jobs_returns_recent_statuses(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    storage.save_status(
        documents_route.JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="one.pdf",
            status=documents_route.JobState.COMPLETED,
            progress=1,
            message="Completed",
        )
    )
    storage.save_status(
        documents_route.JobStatus(
            job_id="job_2",
            doc_id="doc_2",
            filename="two.pdf",
            status=documents_route.JobState.FAILED,
            progress=1,
            message="Failed",
            error="render failed",
        )
    )
    client = TestClient(app)

    response = client.get("/api/jobs?limit=1")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["job_id"] in {"job_1", "job_2"}


def test_cancel_running_job_marks_status_canceled(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    storage.save_status(
        documents_route.JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="paper.pdf",
            target_lang="zh-CN",
            status=documents_route.JobState.TRANSLATING,
            progress=0.4,
            message="Translating",
        )
    )
    client = TestClient(app)

    response = client.post("/api/jobs/job_1/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    assert response.json()["message"] == "Canceled"
    assert storage.load_status("job_1").status == documents_route.JobState.CANCELED


def test_cancel_completed_job_is_noop(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    storage.save_status(
        documents_route.JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="paper.pdf",
            status=documents_route.JobState.COMPLETED,
            progress=1,
            message="Completed",
        )
    )
    client = TestClient(app)

    response = client.post("/api/jobs/job_1/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_retry_job_requeues_existing_upload(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    (storage.uploads / "doc_1.pdf").write_bytes(b"%PDF-1.7\n%%EOF")
    storage.save_status(
        documents_route.JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="paper.pdf",
            target_lang="zh-CN",
            status=documents_route.JobState.FAILED,
            progress=1,
            message="Failed",
            error="previous failure",
        )
    )

    async def noop_process(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(documents_route, "process_document_job", noop_process)
    client = TestClient(app)

    response = client.post("/api/jobs/job_1/retry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["doc_id"] == "doc_1"
    retry_status = storage.load_status(payload["job_id"])
    assert retry_status.status == documents_route.JobState.QUEUED
    assert retry_status.message == "Queued retry"


def test_retry_job_without_upload_returns_404(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    storage.save_status(
        documents_route.JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="paper.pdf",
            status=documents_route.JobState.FAILED,
            progress=1,
            message="Failed",
        )
    )
    client = TestClient(app)

    response = client.post("/api/jobs/job_1/retry")

    assert response.status_code == 404
    assert response.json()["detail"] == "Original upload not found"


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


def test_batch_pdf_upload_queues_multiple_jobs(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)

    async def noop_process(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(documents_route, "process_document_job", noop_process)
    client = TestClient(app)

    response = client.post(
        "/api/documents/batch",
        files=[
            ("files", ("one.pdf", b"%PDF-1.7\n%%EOF", "application/pdf")),
            ("files", ("two.pdf", b"%PDF-1.7\n%%EOF", "application/pdf")),
        ],
        data={"target_lang": "zh-CN"},
    )

    assert response.status_code == 200
    jobs = response.json()["jobs"]
    assert len(jobs) == 2
    assert {storage.load_status(job["job_id"]).filename for job in jobs} == {
        "one.pdf",
        "two.pdf",
    }


def test_batch_upload_rejects_non_pdf(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.post(
        "/api/documents/batch",
        files=[
            ("files", ("one.pdf", b"%PDF-1.7\n%%EOF", "application/pdf")),
            ("files", ("bad.txt", b"plain text", "text/plain")),
        ],
        data={"target_lang": "zh-CN"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only PDF uploads are supported"


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


def test_asset_endpoint_returns_extracted_image(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    asset_path = storage.asset_dir("doc_1") / "asset_1.png"
    asset_path.write_bytes(b"png-bytes")
    client = TestClient(app)

    response = client.get("/api/documents/doc_1/assets/asset_1.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"png-bytes"


def test_asset_endpoint_rejects_unknown_asset(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.get("/api/documents/doc_1/assets/missing.png")

    assert response.status_code == 404
    assert response.json()["detail"] == "Asset not found"


def test_artifacts_summary_and_document_ir_endpoint(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p1",
                size=PageSize(width=100, height=120),
                blocks=[
                    DocumentBlock(
                        block_id="b1",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=1, y0=2, x1=40, y1=50),
                        reading_order=0,
                        source_text="Text",
                    )
                ],
            )
        ],
    )
    storage.save_document_ir(document)
    storage.write_json("doc_1", "translation-chunks.json", [{"chunk_id": "chunk_1"}])
    storage.write_json("doc_1", "parser-diagnostics.json", {"kind": "parser_diagnostics"})
    client = TestClient(app)

    summary = client.get("/api/documents/doc_1/artifacts")

    assert summary.status_code == 200
    artifacts = {item["name"]: item for item in summary.json()["artifacts"]}
    assert artifacts["document-ir"]["available"] is True
    assert artifacts["translation-chunks"]["available"] is True
    assert artifacts["translation-plans"]["available"] is False
    assert artifacts["parser-diagnostics"]["available"] is True

    document_response = client.get("/api/documents/doc_1/artifacts/document-ir")
    chunks_response = client.get("/api/documents/doc_1/artifacts/translation-chunks")
    parser_response = client.get("/api/documents/doc_1/artifacts/parser-diagnostics")

    assert document_response.status_code == 200
    assert document_response.json()["doc_id"] == "doc_1"
    assert chunks_response.status_code == 200
    assert chunks_response.json() == [{"chunk_id": "chunk_1"}]
    assert parser_response.status_code == 200
    assert parser_response.json() == {"kind": "parser_diagnostics"}


def test_missing_artifact_returns_404(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.get("/api/documents/doc_missing/artifacts/translation-plans")

    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found"
