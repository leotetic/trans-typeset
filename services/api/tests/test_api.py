from pathlib import Path

import pytest
from app import config as config_module
from app.provider_config import ProviderConfigError
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
from app.pipeline.workflow import coerce_user_intent
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
        translation_chunk_max_chars=3200,
        agent_max_repair_attempts=3,
        agent_enable_vision_analysis=True,
        layout_planner_model="layout-model",
        vision_analyzer_model="vision-model",
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
    assert payload["translation_chunk_max_chars"] == 3200
    assert payload["agent_max_repair_attempts"] == 3
    assert payload["agent_enable_vision_analysis"] is True
    assert payload["layout_planner_model"] == "layout-model"
    assert payload["vision_analyzer_model"] == "vision-model"
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


def test_load_settings_rejects_invalid_openai_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "_dotenv_values",
        lambda: {"OPENAI_BASE_URL": "10.194.160.128:8080/v1"},
    )
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    with pytest.raises(ProviderConfigError, match="Base URL must start"):
        config_module.load_settings()


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


def test_config_defaults_chunk_size_by_translator_provider(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(documents_route, "storage", storage)
    monkeypatch.setattr(runtime_config, "settings", config)

    deterministic = runtime_config.runtime_config_response(storage)

    assert deterministic.translation_chunk_max_chars == 6000

    storage.write_runtime_config(
        {
            "openai_base_url": "https://persisted.example.test/v1",
            "openai_api_key": "persisted-secret",
            "openai_model": "persisted-model",
            "translation_concurrency": 16,
        }
    )

    model_config = runtime_config.runtime_config_response(storage)

    assert model_config.translation_chunk_max_chars == 3500
    assert model_config.translation_concurrency == 4


def test_config_migrates_legacy_default_ocr_order(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    storage.write_runtime_config(
        {"ocr_provider_order": ["pix2text", "openai_vision", "deterministic"]}
    )
    config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(runtime_config, "settings", config)

    migrated = runtime_config.runtime_config_response(storage)

    assert migrated.ocr_provider_order == ["pix2text", "deterministic"]


def test_config_preserves_explicit_pix2text_ocr_order(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    storage.write_runtime_config({"ocr_provider_order": ["pix2text", "deterministic"]})
    config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(runtime_config, "settings", config)

    configured = runtime_config.runtime_config_response(storage)

    assert configured.ocr_provider_order == ["pix2text", "deterministic"]


def test_config_returns_complete_render_defaults(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    storage.write_runtime_config(
        {
            "default_target_lang": "zh-CN",
            "render_defaults": {
                "target_lang": "en-US",
                "font_stack": ["Configured Sans", "serif"],
                "line_height": 1.42,
                "layout_mode": "source_bbox",
                "page_layout": {"width_pt": 595.28, "height_pt": 841.89},
                "role_styles": {"paragraph": {"font_size_pt": 12}},
            },
        }
    )
    config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(documents_route, "storage", storage)
    monkeypatch.setattr(runtime_config, "settings", config)
    client = TestClient(app)

    response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["render_defaults"]["target_lang"] == "zh-CN"
    assert payload["render_defaults"]["font_stack"] == ["Configured Sans", "serif"]
    assert payload["render_defaults"]["line_height"] == 1.42
    assert payload["render_defaults"]["layout_mode"] == "source_bbox"
    assert payload["render_defaults"]["page_layout"]["width_pt"] == 595.28
    assert payload["render_defaults"]["role_styles"]["paragraph"]["font_size_pt"] == 12


def test_gbt_intent_upgrades_legacy_sans_font_stack(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    storage.write_runtime_config(
        {
            "default_target_lang": "zh-CN",
            "render_defaults": {
                "target_lang": "zh-CN",
                "font_stack": [
                    "Noto Sans CJK SC",
                    "Source Han Sans SC",
                    "Arial Unicode MS",
                    "sans-serif",
                ],
                "line_height": 1.35,
            },
        }
    )
    config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(runtime_config, "settings", config)

    defaults = runtime_config.render_defaults_for_intent(
        storage,
        "zh-CN",
        coerce_user_intent(
            "zh-CN",
            output_kind="typeset_document",
            instruction="按照 GB/T 7713.1 标准排版",
        ),
    )

    assert defaults.layout_mode == "continuous_reflow"
    assert defaults.font_stack == [
        "Times New Roman",
        "SimSun",
        "Songti SC",
        "Noto Serif CJK SC",
        "Source Han Serif SC",
        "serif",
    ]
    assert defaults.formula_numbering == "parenthesized"


def test_non_gbt_intent_keeps_formula_numbering_disabled(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(runtime_config, "settings", config)

    defaults = runtime_config.render_defaults_for_intent(
        storage,
        "zh-CN",
        coerce_user_intent("zh-CN", output_kind="translation", instruction=""),
    )

    assert defaults.formula_numbering == "none"


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
            "translation_chunk_max_chars": 4200,
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
    assert payload["translation_chunk_max_chars"] == 4200
    assert payload["render_defaults"]["font_stack"] == ["Noto Sans CJK SC", "serif"]
    assert payload["render_defaults"]["line_height"] == 1.5
    assert payload["render_defaults"]["overflow_policy"]["min_font_scale"] == 0.72
    assert "secret-key" not in response.text
    assert storage.read_runtime_config()["openai_api_key"] == "secret-key"
    assert storage.read_runtime_config()["render_defaults"]["line_height"] == 1.5


@pytest.mark.parametrize(
    ("base_url", "detail"),
    [
        ("10.194.160.128:8080/v1", "Base URL must start with http:// or https://"),
        ("ftp://example.test/v1", "Base URL must start with http:// or https://"),
        (
            "https://example.test/chat/completions",
            "Base URL must point to an OpenAI-compatible /v1 API root, "
            "for example https://api.example.com/v1",
        ),
    ],
)
def test_update_config_rejects_invalid_openai_base_url(
    base_url: str,
    detail: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(documents_route, "storage", storage)
    monkeypatch.setattr(runtime_config, "settings", config)
    client = TestClient(app)

    response = client.put("/api/config", json={"openai_base_url": base_url})

    assert response.status_code == 400
    assert response.json()["detail"] == detail
    assert storage.read_runtime_config() == {}


def test_update_config_allows_private_http_openai_base_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(documents_route, "storage", storage)
    monkeypatch.setattr(runtime_config, "settings", config)
    client = TestClient(app)

    response = client.put(
        "/api/config",
        json={"openai_base_url": "http://10.194.160.128:8080/v1/"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["openai_base_url"] == "http://10.194.160.128:8080/v1"
    assert storage.read_runtime_config()["openai_base_url"] == "http://10.194.160.128:8080/v1"


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
    content_path = storage.uploads / "doc_1.content.pdf"
    layout_path = storage.uploads / "doc_1.layout.pdf"
    content_path.write_bytes(b"%PDF-1.7\n%%EOF")
    layout_path.write_bytes(b"%PDF-1.7\n%%EOF")
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

    scheduled: list[tuple] = []

    def fake_schedule_job(func, *args, **kwargs):
        scheduled.append((func, args, kwargs))

    monkeypatch.setattr(documents_route, "schedule_job", fake_schedule_job)
    client = TestClient(app)

    response = client.post("/api/jobs/job_1/retry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["doc_id"] == "doc_1"
    retry_status = storage.load_status(payload["job_id"])
    assert retry_status.status == documents_route.JobState.QUEUED
    assert retry_status.message == "Queued retry"
    assert scheduled[0][0] is documents_route.process_document_job
    assert scheduled[0][1][0] == payload["job_id"]
    assert scheduled[0][1][3] == content_path
    assert scheduled[0][1][6] == layout_path


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
    scheduled: list[tuple] = []

    def fake_schedule_job(func, *args, **kwargs):
        scheduled.append((func, args, kwargs))

    monkeypatch.setattr(documents_route, "schedule_job", fake_schedule_job)
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
    assert storage.find_upload(payload["doc_id"], role="content") is not None
    assert scheduled[0][0] is documents_route.process_document_job
    assert scheduled[0][1][0] == payload["job_id"]


def test_pdf_upload_accepts_content_and_layout_sources(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    scheduled: list[tuple] = []

    def fake_schedule_job(func, *args, **kwargs):
        scheduled.append((func, args, kwargs))

    monkeypatch.setattr(documents_route, "schedule_job", fake_schedule_job)
    client = TestClient(app)

    response = client.post(
        "/api/documents",
        data={
            "target_lang": "zh-CN",
            "output_kind": "typeset_document",
            "style_intent": "academic",
            "instruction": "按照 GB/T 7713.1 标准排版",
            "page_width_pt": "595.28",
            "page_height_pt": "841.89",
            "target_font_size_pt": "12",
            "allow_continuation": "true",
            "preserve_images": "false",
        },
        files={
            "content_file": ("paper.pdf", b"%PDF-1.7\n%%EOF", "application/pdf"),
            "layout_file": ("layout.pdf", b"%PDF-1.7\n%%EOF", "application/pdf"),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    content_path = storage.find_upload(payload["doc_id"], role="content")
    layout_path = storage.find_upload(payload["doc_id"], role="layout")
    assert content_path is not None
    assert layout_path is not None
    assert content_path.name.endswith(".content.pdf")
    assert layout_path.name.endswith(".layout.pdf")
    assert scheduled[0][0] is documents_route.process_document_job
    assert scheduled[0][1][3] == content_path
    assert scheduled[0][1][6] == layout_path
    intent = scheduled[0][1][5]
    assert intent.constraints.page_width_pt == 595.28
    assert intent.constraints.preserve_images is False


def test_pdf_upload_response_does_not_wait_for_scheduled_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    scheduled: list[tuple] = []

    async def never_run(*_args, **_kwargs) -> None:
        raise AssertionError("scheduled jobs should not run inside the request")

    def fake_schedule_job(func, *args, **kwargs):
        scheduled.append((func, args, kwargs))

    monkeypatch.setattr(documents_route, "process_document_job", never_run)
    monkeypatch.setattr(documents_route, "schedule_job", fake_schedule_job)
    client = TestClient(app)

    response = client.post(
        "/api/documents",
        files={"file": ("paper.pdf", b"%PDF-1.7\n%%EOF", "application/pdf")},
    )

    assert response.status_code == 200
    assert storage.load_status(response.json()["job_id"]).status == "queued"
    assert scheduled[0][0] is never_run


def test_text_workflow_queues_job_with_user_intent(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)

    captured = {}

    def fake_schedule_job(func, *args, **kwargs):
        captured["func"] = func
        captured["args"] = args

    monkeypatch.setattr(documents_route, "schedule_job", fake_schedule_job)
    client = TestClient(app)

    response = client.post(
        "/api/workflows/text",
        data={
            "text": "Title\n\nA paragraph.",
            "target_lang": "zh-CN",
            "output_kind": "typeset_document",
            "style_intent": "academic",
            "instruction": "按照gb-GB/T 7713.1 进行排版",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    status = storage.load_status(payload["job_id"])
    assert status.status == "queued"
    assert status.filename == "text-input.txt"
    assert captured["func"] is documents_route.process_text_document_job
    assert captured["args"][5].instruction == "按照gb-GB/T 7713.1 进行排版"


def test_image_workflow_queues_job(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    scheduled: list[tuple] = []

    def fake_schedule_job(func, *args, **kwargs):
        scheduled.append((func, args, kwargs))

    monkeypatch.setattr(documents_route, "schedule_job", fake_schedule_job)
    client = TestClient(app)

    response = client.post(
        "/api/workflows/image",
        data={
            "target_lang": "zh-CN",
            "instruction": "按照gb-GB/T 7713.1 进行排版",
        },
        files={"file": ("layout.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert storage.load_status(payload["job_id"]).filename == "layout.png"
    assert storage.find_upload(payload["doc_id"]) is not None
    assert scheduled[0][0] is documents_route.process_image_document_job
    assert scheduled[0][1][0] == payload["job_id"]


def test_batch_pdf_upload_queues_multiple_jobs(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    storage.write_runtime_config({"translation_concurrency": 4})
    monkeypatch.setattr(documents_route, "storage", storage)
    scheduled: list[tuple] = []

    def fake_schedule_job(func, *args, **kwargs):
        scheduled.append((func, args, kwargs))

    monkeypatch.setattr(documents_route, "schedule_job", fake_schedule_job)
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
    assert [item[0] for item in scheduled] == [
        documents_route.process_document_job,
        documents_route.process_document_job,
    ]
    assert [item[2]["max_concurrency"] for item in scheduled] == [4, 4]


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
    storage.write_json(
        "doc_1",
        "translation-diagnostics.json",
        [{"chunk_id": "chunk_1", "error_type": "UnparseableTranslationResponseError"}],
    )
    storage.write_json("doc_1", "layout-trace.json", {"kind": "layout_trace"})
    storage.write_json("doc_1", "parser-diagnostics.json", {"kind": "parser_diagnostics"})
    storage.write_json(
        "doc_1",
        "formula-recognition.json",
        [{"formula_id": "formula_1", "latex": "x = y"}],
    )
    storage.write_json(
        "doc_1",
        "formula-diagnostics.json",
        {"kind": "formula_diagnostics"},
    )
    client = TestClient(app)

    summary = client.get("/api/documents/doc_1/artifacts")

    assert summary.status_code == 200
    artifacts = {item["name"]: item for item in summary.json()["artifacts"]}
    assert artifacts["normalized-input"]["available"] is False
    assert artifacts["workflow-run"]["available"] is False
    assert artifacts["semantic-analysis"]["available"] is False
    assert artifacts["document-ir"]["available"] is True
    assert artifacts["translation-chunks"]["available"] is True
    assert artifacts["translation-diagnostics"]["available"] is True
    assert artifacts["layout-trace"]["available"] is True
    assert artifacts["formula-recognition"]["available"] is True
    assert artifacts["formula-diagnostics"]["available"] is True
    assert artifacts["translation-plans"]["available"] is False
    assert artifacts["parser-diagnostics"]["available"] is True

    document_response = client.get("/api/documents/doc_1/artifacts/document-ir")
    chunks_response = client.get("/api/documents/doc_1/artifacts/translation-chunks")
    translation_diagnostics_response = client.get(
        "/api/documents/doc_1/artifacts/translation-diagnostics"
    )
    parser_response = client.get("/api/documents/doc_1/artifacts/parser-diagnostics")
    formula_response = client.get("/api/documents/doc_1/artifacts/formula-recognition")
    formula_diagnostics_response = client.get(
        "/api/documents/doc_1/artifacts/formula-diagnostics"
    )
    trace_response = client.get("/api/documents/doc_1/artifacts/layout-trace")

    assert document_response.status_code == 200
    assert document_response.json()["doc_id"] == "doc_1"
    assert chunks_response.status_code == 200
    assert chunks_response.json() == [{"chunk_id": "chunk_1"}]
    assert translation_diagnostics_response.status_code == 200
    assert translation_diagnostics_response.json() == [
        {"chunk_id": "chunk_1", "error_type": "UnparseableTranslationResponseError"}
    ]
    assert parser_response.status_code == 200
    assert parser_response.json() == {"kind": "parser_diagnostics"}
    assert formula_response.status_code == 200
    assert formula_response.json() == [{"formula_id": "formula_1", "latex": "x = y"}]
    assert formula_diagnostics_response.status_code == 200
    assert formula_diagnostics_response.json() == {"kind": "formula_diagnostics"}
    assert trace_response.status_code == 200
    assert trace_response.json() == {"kind": "layout_trace"}


def test_missing_artifact_returns_404(tmp_path: Path, monkeypatch) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(documents_route, "storage", storage)
    client = TestClient(app)

    response = client.get("/api/documents/doc_missing/artifacts/translation-plans")

    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found"
