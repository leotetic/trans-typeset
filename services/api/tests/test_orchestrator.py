import asyncio
from pathlib import Path

import pytest
from app import runtime_config
from app.config import Settings
from app.models import JobState
from app.pipeline import orchestrator
from app.pipeline.workflow import coerce_user_intent
from app.pipeline.parser import UnsupportedPdfError
from app.storage import Storage
from pdf_renderer import RenderDocument
from pdf_translator_schema import (
    Asset,
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
from pdf_translator_schema.models import (
    DocumentBlock,
    FormulaRecognitionResult,
    OCRRecognitionResult,
    RenderDefaults,
)


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


def test_coerce_user_intent_detects_gbt_7713_standard() -> None:
    intent = coerce_user_intent(
        "zh-CN",
        output_kind="typeset_document",
        instruction="按照 GB/T 7713.1 标准进行排版",
    )

    assert intent.typesetting_standard == "gb_t_7713_1_2025"


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
            "translation_chunk_max_chars": 1800,
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
        captured["chunk_max_chars"] = kwargs["max_chars"]
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
    formula_recognition = storage.read_output_json("doc_1", "formula-recognition.json")
    formula_diagnostics = storage.read_output_json("doc_1", "formula-diagnostics.json")

    assert status.status == JobState.COMPLETED
    assert [chunk.status for chunk in status.chunks] == ["completed", "completed"]
    assert progress[0]["chunk_id"] == "chunk_1"
    assert progress[1]["quality_flags"] == ["repaired_layout_plan"]
    assert storage.output_pdf_path("doc_1").read_bytes().startswith(b"%PDF-")
    assert captured["chunk_render_defaults"].font_stack == ["Configured Sans", "serif"]
    assert captured["chunk_max_chars"] == 1800
    assert captured["renderer_render_defaults"].line_height == 1.58
    assert captured["renderer_render_defaults"].overflow_policy.min_font_scale == 0.77
    assert formula_recognition == []
    assert formula_diagnostics["kind"] == "formula_diagnostics"
    assert formula_diagnostics["recognizer_type"] == "deterministic"
    assert formula_diagnostics["visual_formula_recognition_enabled"] is True
    parser_diagnostics = storage.read_output_json("doc_1", "parser-diagnostics.json")
    assert "pdf_parse_ms" in parser_diagnostics
    assert parser_diagnostics["formula_recognizer_type"] == "deterministic"


def test_process_document_job_gbt_intent_uses_gbt_render_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    render_defaults = RenderDefaults(
        target_lang="zh-CN",
        font_stack=[
            "Noto Sans CJK SC",
            "Source Han Sans SC",
            "Arial Unicode MS",
            "sans-serif",
        ],
    ).model_dump()
    storage.write_runtime_config(
        {
            "translation_concurrency": 1,
            "translator_max_attempts": 1,
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
                    )
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
        )
    ]

    class FakeTranslator:
        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id="b1",
                        translated_text="阿尔法",
                        role=BlockRole.PARAGRAPH,
                    )
                ],
            )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    captured: dict[str, object] = {}

    def fake_build_chunks(*_args, **kwargs):
        captured["chunk_render_defaults"] = kwargs["render_defaults"]
        captured["chunk_max_chars"] = kwargs["max_chars"]
        return chunks

    def fake_from_ir_and_plans(*_args, **kwargs):
        captured["renderer_render_defaults"] = kwargs["render_defaults"]
        return RenderDocument(doc_id="doc_1", target_lang="zh-CN", pages=[])

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
            coerce_user_intent(
                "zh-CN",
                output_kind="typeset_document",
                instruction="按照 GB/T 7713.1 标准排版",
            ),
        )
    )

    assert captured["chunk_render_defaults"].layout_mode == "continuous_reflow"
    assert captured["renderer_render_defaults"].layout_mode == "continuous_reflow"
    assert captured["chunk_render_defaults"].font_stack == [
        "Times New Roman",
        "SimSun",
        "Songti SC",
        "Noto Serif CJK SC",
        "Source Han Serif SC",
        "serif",
    ]


def test_process_document_job_fails_with_unrecoverable_translation_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    storage.write_runtime_config(
        {
            "translation_concurrency": 1,
            "translator_max_attempts": 2,
            "agent_max_repair_attempts": 0,
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
                        source_text="Beta [1]",
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
                SourceBlock(
                    block_id="b2",
                    role=BlockRole.PARAGRAPH,
                    source_text="Beta [1]",
                    preserve_tokens=["[1]"],
                )
            ],
        ),
    ]

    class FakeTranslator:
        def __init__(self) -> None:
            self._diagnostics: list[dict[str, object]] = []

        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            block = chunk.source_blocks[0]
            if chunk.chunk_id == "chunk_2":
                self._diagnostics.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "attempt": 2,
                        "error_type": "UnparseableTranslationResponseError",
                        "content_type": "str",
                        "content_length": 42,
                        "response_preview_length": 24,
                        "sanitized_response_preview": "plain text without JSON",
                        "quality_flags": [
                            "translator_response_unparseable",
                            "translator_unrecoverable_response",
                        ],
                    }
                )
                raise RuntimeError("translator_unrecoverable_response")
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id=block.block_id,
                        translated_text="translated Alpha",
                        role=block.role,
                    )
                ],
            )

        def drain_diagnostics(self) -> list[dict[str, object]]:
            diagnostics = list(self._diagnostics)
            self._diagnostics.clear()
            return diagnostics

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda _path, _doc_id, _asset_dir: document)
    monkeypatch.setattr(orchestrator, "build_chunks", lambda *_args, **_kwargs: chunks)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: FakeTranslator())

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
    diagnostics = storage.read_output_json("doc_1", "translation-diagnostics.json")

    assert status.status == JobState.FAILED
    assert "translation chunk(s) failed" in status.error
    assert [chunk.status for chunk in status.chunks] == ["completed", "failed"]
    assert progress[1]["status"] == "failed"
    assert progress[1]["error"] == "translator_unrecoverable_response"
    assert progress[1]["quality_flags"] == [
        "translator_response_unparseable",
        "translator_unrecoverable_response",
    ]
    assert diagnostics == [
        {
            "chunk_id": "chunk_2",
            "attempt": 2,
            "error_type": "UnparseableTranslationResponseError",
            "content_type": "str",
            "content_length": 42,
            "response_preview_length": 24,
            "sanitized_response_preview": "plain text without JSON",
            "quality_flags": [
                "translator_response_unparseable",
                "translator_unrecoverable_response",
            ],
        }
    ]


def test_process_document_job_persists_pdf_export_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
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
    ]

    class FakeTranslator:
        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id="b1",
                        translated_text="translated Alpha",
                        role=BlockRole.PARAGRAPH,
                    )
                ],
            )

    async def fake_render_to_pdf(
        html: str,
        output_path: Path,
        *,
        diagnostics_path: Path | None = None,
        asset_base_path: Path | None = None,
    ) -> Path:
        assert "translated Alpha" in html
        assert asset_base_path == storage.asset_dir("doc_1")
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        if diagnostics_path is not None:
            diagnostics_path.write_text(
                '{"kind":"pdf_export","status":"completed","output_bytes":14}',
                encoding="utf-8",
            )
        return output_path

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda _path, _doc_id, _asset_dir: document)
    monkeypatch.setattr(orchestrator, "build_chunks", lambda *_args, **_kwargs: chunks)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: FakeTranslator())
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
    diagnostics = storage.read_output_json("doc_1", "pdf-export-diagnostics.json")
    workflow = storage.read_output_json("doc_1", "workflow-run.json")

    assert status.status == JobState.COMPLETED
    assert diagnostics["status"] == "completed"
    complete_steps = [step for step in workflow["steps"] if step["name"] == "complete"]
    assert "pdf-export-diagnostics" in complete_steps[-1]["output_artifacts"]


def test_formula_enrichment_does_not_build_openai_formula_provider_unless_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    storage.write_runtime_config(
        {
            "openai_base_url": "https://models.example.test/v1",
            "openai_api_key": "secret-key",
            "openai_model": "paper-model",
            "vision_analyzer_model": "vision-model",
            "agent_enable_vision_analysis": False,
            "ocr_provider_order": ["deterministic"],
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
                        block_id="formula_1",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="E = mc^2",
                    )
                ],
            )
        ],
    )

    class ForbiddenRecognizer:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("vision recognizer should not be constructed")

    monkeypatch.setattr(orchestrator, "OpenAIFormulaRecognizer", ForbiddenRecognizer)

    result = asyncio.run(orchestrator._enrich_document_formulas(document, doc_id="doc_1"))

    assert result.diagnostics["recognizer_type"] == "deterministic"
    assert result.diagnostics["visual_formula_recognition_enabled"] is False
    assert "visual_formula_recognition_disabled" in result.diagnostics["quality_flags"]


def test_formula_enrichment_openai_formula_ocr_is_decoupled_from_agent_vision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
    storage.write_runtime_config(
        {
            "openai_base_url": "https://models.example.test/v1",
            "openai_api_key": "secret-key",
            "openai_model": "paper-model",
            "vision_analyzer_model": "vision-model",
            "agent_enable_vision_analysis": False,
            "ocr_provider_order": ["openai_vision", "deterministic"],
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
                        block_id="formula_1",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text="E = mc^2",
                    )
                ],
            )
        ],
    )
    constructed: list[dict] = []

    class FakeRecognizer:
        def __init__(self, **kwargs) -> None:
            constructed.append(kwargs)

        async def recognize(self, candidate):
            return FormulaRecognitionResult(
                latex="E = mc^2",
                display_mode="display",
                confidence=0.91,
                quality_flags=[],
            )

    monkeypatch.setattr(orchestrator, "OpenAIFormulaRecognizer", FakeRecognizer)

    result = asyncio.run(orchestrator._enrich_document_formulas(document, doc_id="doc_1"))

    assert constructed
    assert result.diagnostics["recognizer_type"] == "deterministic"
    assert result.diagnostics["visual_formula_recognition_enabled"] is True


def test_formula_enrichment_reports_progress_and_falls_back_from_pix2text(
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
            status=JobState.PARSING,
            progress=0.17,
            message="Recognizing formulas",
        )
    )
    storage.write_runtime_config(
        {
            "ocr_provider_order": ["pix2text", "deterministic"],
            "ocr_provider_timeout_seconds": 1,
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
                        block_id="formula_1",
                        page_id="p1",
                        role=BlockRole.FORMULA,
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        reading_order=0,
                        source_text=r"\partial f_s / \partial t = \sum_n (4)",
                    )
                ],
                assets=[
                    Asset(
                        asset_id="formula_asset",
                        page_id="p1",
                        kind="formula",
                        bbox=BoundingBox(x0=10, y0=10, x1=120, y1=40),
                        path="/api/documents/doc_1/assets/formula_asset.png",
                    )
                ],
            )
        ],
    )
    storage.asset_dir("doc_1").mkdir(parents=True, exist_ok=True)
    (storage.asset_dir("doc_1") / "formula_asset.png").write_bytes(b"fake-image")

    class EmptyPix2TextProvider:
        name = "pix2text"

        def __init__(self, **_kwargs) -> None:
            pass

        async def recognize_formula(self, candidate, *, image_path=None):
            return OCRRecognitionResult(
                region_kind="formula",
                provider="pix2text",
                confidence=0,
                quality_flags=["pix2text_formula_ocr_empty"],
            )

    monkeypatch.setattr(orchestrator, "Pix2TextOCRProvider", EmptyPix2TextProvider)

    result = asyncio.run(
        orchestrator._enrich_document_formulas(
            document,
            doc_id="doc_1",
            job_id="job_1",
            filename="paper.pdf",
            target_lang="zh-CN",
        )
    )

    status = storage.load_status("job_1")
    assert status.message == "Recognizing formulas 1/1"
    assert result.formulas[0].ocr_provider == "deterministic"
    ocr_records = storage.read_output_json("doc_1", "ocr-recognition.json")
    assert any(record["provider"] == "pix2text" for record in ocr_records)


def test_process_document_job_rerenders_preview_after_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setattr(orchestrator, "storage", storage)
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
    ]

    class FakeTranslator:
        async def translate(self, chunk: TranslationChunk) -> TranslationLayoutPlan:
            return TranslationLayoutPlan(
                chunk_id=chunk.chunk_id,
                blocks=[
                    TranslationBlockPlan(
                        source_block_id="b1",
                        translated_text="translated Alpha",
                        role=BlockRole.PARAGRAPH,
                    )
                ],
            )

    render_calls = 0
    real_render_to_html = orchestrator.render_to_html

    def fake_render_to_html(render_document):
        nonlocal render_calls
        render_calls += 1
        suffix = "repaired" if render_calls == 2 else "initial"
        return f"{real_render_to_html(render_document)}<!-- {suffix} -->"

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        assert "<!-- repaired -->" in html
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda _path, _doc_id, _asset_dir: document)
    monkeypatch.setattr(orchestrator, "build_chunks", lambda *_args, **_kwargs: chunks)
    monkeypatch.setattr(orchestrator, "build_translator", lambda *_args: FakeTranslator())
    monkeypatch.setattr(orchestrator, "render_to_html", fake_render_to_html)
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)
    monkeypatch.setattr(
        orchestrator,
        "render_evaluation_summary",
        lambda diagnostics: {
            "kind": "render_evaluation",
            "accepted": render_calls >= 2,
            "quality_flag_counts": diagnostics.get("quality_flag_counts", {}),
            "layout_issue_count": len(diagnostics.get("layout_issues", [])),
            "blocking_flags": {} if render_calls >= 2 else {"overflow_clipped": 1},
            "repair_recommended": render_calls < 2,
        },
    )

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "paper.pdf",
            tmp_path / "paper.pdf",
            "zh-CN",
        )
    )

    assert render_calls == 2
    assert "<!-- repaired -->" in storage.preview_html_path("doc_1").read_text(encoding="utf-8")
    workflow = storage.read_output_json("doc_1", "workflow-run.json")
    repair_step = [step for step in workflow["steps"] if step["name"] == "repair"][-1]
    assert "preview" in repair_step["output_artifacts"]


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
    semantic = storage.read_output_json("doc_1", "semantic-analysis.json")
    layout_plan = storage.read_output_json("doc_1", "layout-intent-plan.json")

    assert status.status == JobState.COMPLETED
    assert workflow["status"] == "completed"
    assert "semantic_recognize" in {step["name"] for step in workflow["steps"]}
    assert "export_pdf" in {step["name"] for step in workflow["steps"]}
    assert normalized["input_sources"][0]["input_type"] == "text"
    assert semantic["quality_flags"]
    assert layout_plan["blocks"]
    assert "semantic_analysis_considered" in layout_plan["quality_flags"]
    assert "planner_fallback" in layout_plan["quality_flags"]
    assert storage.output_pdf_path("doc_1").read_bytes().startswith(b"%PDF-")
    assert (tmp_path / "checkpoints" / "langgraph.sqlite").exists()


def test_pdf_workflow_records_content_and_layout_sources(
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
    content_pdf = tmp_path / "content.pdf"
    layout_pdf = tmp_path / "layout.pdf"
    content_pdf.write_bytes(b"%PDF-1.7\n%%EOF")
    layout_pdf.write_bytes(b"%PDF-1.7\n%%EOF")
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
                ],
            )
        ],
    )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda *_args: document)
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "content.pdf",
            content_pdf,
            "zh-CN",
            layout_pdf_path=layout_pdf,
            layout_filename="layout.pdf",
        )
    )

    status = storage.load_status("job_1")
    normalized = storage.read_output_json("doc_1", "normalized-input.json")
    workflow = storage.read_output_json("doc_1", "workflow-run.json")
    semantic = storage.read_output_json("doc_1", "semantic-analysis.json")

    assert status.status == JobState.COMPLETED
    assert [source["source_role"] for source in normalized["input_sources"]] == [
        "content",
        "layout_reference",
    ]
    assert normalized["layout_reference"]["filename"] == "layout.pdf"
    assert "layout_reference_source_available" in semantic["quality_flags"]
    assert [source["source_role"] for source in workflow["input_sources"]] == [
        "content",
        "layout_reference",
    ]


def test_pdf_workflow_marks_layout_source_fallback(
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
    content_pdf = tmp_path / "content.pdf"
    content_pdf.write_bytes(b"%PDF-1.7\n%%EOF")
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
                ],
            )
        ],
    )

    async def fake_render_to_pdf(html: str, output_path: Path) -> Path:
        output_path.write_bytes(b"%PDF-1.7\n%%EOF")
        return output_path

    monkeypatch.setattr(orchestrator, "parse_pdf", lambda *_args: document)
    monkeypatch.setattr(orchestrator, "render_to_pdf", fake_render_to_pdf)

    asyncio.run(
        orchestrator.process_document_job(
            "job_1",
            "doc_1",
            "content.pdf",
            content_pdf,
            "zh-CN",
        )
    )

    normalized = storage.read_output_json("doc_1", "normalized-input.json")
    semantic = storage.read_output_json("doc_1", "semantic-analysis.json")

    assert normalized["input_sources"][1]["source_role"] == "layout_reference"
    assert "layout_source_fallback_to_content" in normalized["quality_flags"]
    assert "layout_source_fallback_to_content" in semantic["quality_flags"]


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
    semantic = storage.read_output_json("doc_1", "semantic-analysis.json")
    diagnostics = storage.read_output_json("doc_1", "parser-diagnostics.json")
    html = storage.preview_html_path("doc_1").read_text(encoding="utf-8")

    assert status.status == JobState.COMPLETED
    assert asset_ir[0]["quality_flags"] == ["deterministic_ocr_mock", "ocr_uncertain"]
    assert "vision_analysis_disabled" in semantic["quality_flags"]
    assert diagnostics["kind"] == "image_adapter_diagnostics"
    assert 'data-asset-id="doc_1_asset_0001"' in html
