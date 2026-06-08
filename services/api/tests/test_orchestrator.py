import asyncio
from pathlib import Path

import pytest
from app.config import Settings
from app.models import JobState
from app import runtime_config
from app.pipeline import orchestrator
from app.pipeline.parser import UnsupportedPdfError
from app.storage import Storage
from pdf_renderer import RenderDocument
from pdf_translator_schema import (
    BlockRole,
    BoundingBox,
    DocumentIR,
    DocumentPage,
    PageSize,
    SourceBlock,
    TranslationBlockPlan,
    TranslationChunk,
    TranslationLayoutPlan,
)
from pdf_translator_schema.models import DocumentBlock, RenderDefaults


def test_process_document_job_persists_frontend_visible_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)

    def fail_parse(pdf_path: Path, doc_id: str, asset_output_dir: Path | None = None):
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


def test_process_document_job_honors_existing_cancel_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    storage.save_status(
        orchestrator.JobStatus(
            job_id="job_1",
            doc_id="doc_1",
            filename="paper.pdf",
            target_lang="zh-CN",
            status=JobState.CANCELED,
            progress=1,
            message="Canceled",
        )
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("parse_pdf should not run after cancellation")

    monkeypatch.setattr(orchestrator, "parse_pdf", fail_if_called)

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
    assert status.status == JobState.CANCELED
    assert status.message == "Canceled"


def test_process_document_job_persists_chunk_progress_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    render_defaults = RenderDefaults(
        target_lang="zh-CN",
        font_stack=["Configured Sans", "serif"],
        line_height=1.58,
    ).model_dump()
    render_defaults["overflow_policy"]["min_font_scale"] = 0.77
    storage.write_runtime_config(
        {
            "translation_concurrency": 2,
            "translator_max_attempts": 3,
            "render_defaults": render_defaults,
        }
    )
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
                    ),
                    DocumentBlock(
                        block_id="b2",
                        page_id="p1",
                        role=BlockRole.PARAGRAPH,
                        bbox=BoundingBox(x0=10, y0=50, x1=120, y1=80),
                        reading_order=1,
                        source_text="Beta",
                    ),
                ],
            )
        ],
    )
    chunks = [
        TranslationChunk(
            chunk_id="chunk_1",
            source_blocks=[
                SourceBlock(block_id="b1", role=BlockRole.PARAGRAPH, source_text="Alpha")
            ],
        ),
        TranslationChunk(
            chunk_id="chunk_2",
            source_blocks=[
                SourceBlock(block_id="b2", role=BlockRole.PARAGRAPH, source_text="Beta")
            ],
        ),
    ]

    class FakeTranslator:
        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            block = chunk.source_blocks[0]
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id=block.block_id,
                        translated_text=f"translated {block.source_text}",
                        role=block.role,
                        quality_flags=["repaired_layout_plan"]
                        if chunk.chunk_id == "chunk_2"
                        else [],
                    )
                ],
            )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    captured: dict[str, object] = {}

    def fake_build_chunks(*_args, **kwargs):
        captured["chunk_render_defaults"] = kwargs["render_defaults"]
        return chunks

    real_from_ir_and_plans = RenderDocument.from_ir_and_plans

    def fake_from_ir_and_plans(*args, **kwargs):
        captured["renderer_render_defaults"] = kwargs["render_defaults"]
        return real_from_ir_and_plans(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda _path, _doc_id, _asset_dir: document)
    monkeypatch.setattr(orchestrator, "build_chunks", fake_build_chunks)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: FakeTranslator())
    monkeypatch.setattr(orchestrator.RenderDocument, "from_ir_and_plans", fake_from_ir_and_plans)
    monkeypatch.setattr(orchestrator, "render_to_html", lambda _document: "<html></html>")
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

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
    progress = storage.read_output_json("doc_1", "translation-progress.json")

    assert status.status == JobState.COMPLETED
    assert [chunk.status for chunk in status.chunks] == ["completed", "completed"]
    assert progress[0]["chunk_id"] == "chunk_1"
    assert progress[1]["quality_flags"] == ["repaired_layout_plan"]
    assert storage.output_pdf_path("doc_1").read_bytes().startswith(b"%PDF-")
    assert captured["chunk_render_defaults"].font_stack == ["Configured Sans", "serif"]
    assert captured["renderer_render_defaults"].line_height == 1.58
    assert captured["renderer_render_defaults"].overflow_policy.min_font_scale == 0.77


def test_process_document_job_persists_scanned_pdf_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)

    def fail_with_ocr_required(
        pdf_path: Path,
        doc_id: str,
        asset_output_dir: Path | None = None,
    ) -> DocumentIR:
        raise UnsupportedPdfError(
            "Scanned or image-only PDF requires OCR, which is not implemented yet",
            {
                "kind": "unsupported_scanned_pdf",
                "reason": "ocr_required",
                "text_block_count": 0,
                "recoverable": True,
            },
        )

    monkeypatch.setattr(orchestrator, "parse_pdf", fail_with_ocr_required)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "scan.pdf",
            tmp_path / "scan.pdf",
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    diagnostics = storage.read_output_json("doc_1", "parser-diagnostics.json")

    assert status.status == JobState.FAILED
    assert "requires OCR" in (status.error or "")
    assert diagnostics["kind"] == "unsupported_scanned_pdf"
    assert diagnostics["recoverable"] is True


def test_text_workflow_runs_to_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(openai_api_key="", openai_api_key_from_env=False),
    )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "【译】" in html
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_text_document_job(
            "job_1",
            "doc_1",
            "text-input.txt",
            "Paper Title\n\nAbstract This is a text workflow [1].",
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    workflow = storage.read_output_json("doc_1", "workflow-run.json")
    normalized = storage.read_output_json("doc_1", "normalized-input.json")
    layout_plan = storage.read_output_json("doc_1", "layout-intent-plan.json")

    assert status.status == JobState.COMPLETED
    assert workflow["status"] == "completed"
    assert normalized["input_sources"][0]["input_type"] == "text"
    assert layout_plan["blocks"]
    assert storage.output_pdf_path("doc_1").read_bytes().startswith(b"%PDF-")


def test_image_workflow_uses_deterministic_ocr_mock_and_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(
        runtime_config,
        "settings",
        Settings(openai_api_key="", openai_api_key_from_env=False),
    )
    image_path = tmp_path / "layout.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "deterministic summary" in html
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_image_document_job(
            "job_1",
            "doc_1",
            "layout.png",
            image_path,
            "zh-CN",
            "image/png",
        )
    )

    status = storage.load_status("job_1")
    asset_ir = storage.read_output_json("doc_1", "asset-ir.json")
    diagnostics = storage.read_output_json("doc_1", "parser-diagnostics.json")
    html = storage.preview_html_path("doc_1").read_text(encoding="utf-8")

    assert status.status == JobState.COMPLETED
    assert asset_ir[0]["quality_flags"] == ["deterministic_ocr_mock", "ocr_uncertain"]
    assert diagnostics["kind"] == "image_adapter_diagnostics"
    assert 'data-asset-id="doc_1_asset_0001"' in html
