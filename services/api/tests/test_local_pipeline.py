import asyncio
from pathlib import Path

from app.config import Settings
from app.models import JobState
from app.pipeline import orchestrator
from app import runtime_config
from app.storage import Storage


def _write_digital_pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page(width=360, height=420)
    page.insert_textbox(
        fitz.Rect(36, 36, 324, 70),
        "A Digital Paper Fixture",
        fontsize=16,
    )
    page.insert_textbox(
        fitz.Rect(36, 88, 324, 126),
        "Abstract This paper studies local PDF translation [1].",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(36, 144, 324, 194),
        "1 Introduction The method preserves Eq. 1 and references Smith et al. 2024.",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(36, 212, 324, 246),
        "E = mc^2",
        fontsize=10,
    )
    document.save(path)
    document.close()


def test_local_pipeline_runs_digital_pdf_to_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    no_provider_config = Settings(openai_api_key="", openai_api_key_from_env=False)
    monkeypatch.setattr(runtime_config, "settings", no_provider_config)
    storage.write_runtime_config(
        {
            "openai_api_key": "",
            "translation_concurrency": 2,
            "translator_max_attempts": 2,
        }
    )
    pdf_path = storage.uploads / "doc_1.pdf"
    _write_digital_pdf(pdf_path)

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "【译】" in html
        output_path.write_bytes(b"%PDF-1.7\n% local pipeline fixture\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "fixture.pdf",
            pdf_path,
            "zh-CN",
        )
    )

    status = storage.load_status("job_1")
    document_ir = storage.load_document_ir("doc_1")
    chunks = storage.read_output_json("doc_1", "translation-chunks.json")
    plans = storage.read_output_json("doc_1", "translation-plans.json")
    progress = storage.read_output_json("doc_1", "translation-progress.json")
    parser_diagnostics = storage.read_output_json("doc_1", "parser-diagnostics.json")
    renderer_diagnostics = storage.read_output_json("doc_1", "renderer-diagnostics.json")
    html = storage.preview_html_path("doc_1").read_text(encoding="utf-8")

    source_block_ids = {
        block.block_id
        for page in document_ir.pages
        for block in page.blocks
        if block.source_text.strip()
    }
    planned_block_ids = {
        block["source_block_id"]
        for plan in plans
        for block in plan["blocks"]
    }
    preserve_tokens = {
        token
        for chunk in chunks
        for block in chunk["source_blocks"]
        for token in block["preserve_tokens"]
    }

    assert status.status == JobState.COMPLETED
    assert status.progress == 1
    assert status.chunks
    assert all(chunk.status == "completed" for chunk in status.chunks)
    assert document_ir.doc_id == "doc_1"
    assert len(source_block_ids) >= 2
    assert source_block_ids == planned_block_ids
    assert "[1]" in preserve_tokens
    assert any("mock_translation" in block["quality_flags"] for plan in plans for block in plan["blocks"])
    assert parser_diagnostics["kind"] == "parser_diagnostics"
    assert parser_diagnostics["text_block_count"] == len(source_block_ids)
    assert renderer_diagnostics["doc_id"] == "doc_1"
    assert renderer_diagnostics["page_count"] >= 1
    assert len(progress) == len(chunks)
    assert "【译】" in html
    assert storage.output_pdf_path("doc_1").read_bytes().startswith(b"%PDF-")
