from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pdf_translator_schema import (
    BlockRole,
    DocumentIR,
    DocumentPage,
    PageSize,
)

import app.pipeline.mineru_adapter as mineru_adapter
from app.pipeline import orchestrator
from app.pipeline.mineru_adapter import (
    MinerUAdapterError,
    MinerUParseResult,
    parse_pdf_with_mineru,
)
from app.storage import Storage


def test_mineru_adapter_uses_absolute_paths_and_venv_executable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    python_exe = bin_dir / "python"
    mineru_exe = bin_dir / "mineru"
    python_exe.write_text("# python", encoding="utf-8")
    mineru_exe.write_text("# mineru", encoding="utf-8")
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        output_dir = Path(kwargs["cwd"])
        middle = {
            "_version_name": "3.4.0",
            "pdf_info": [{"page_idx": 0, "page_size": [600, 800], "para_blocks": []}],
        }
        (output_dir / "sample_middle.json").write_text(
            json.dumps(middle),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(mineru_adapter.sys, "executable", str(python_exe))
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = parse_pdf_with_mineru(
        pdf_path,
        "doc_1",
        asset_output_dir=tmp_path / "assets",
        output_dir=tmp_path / "mineru",
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == str(mineru_exe.resolve())
    assert Path(command[2]).is_absolute()
    assert Path(command[4]).is_absolute()
    assert Path(captured["cwd"]).is_absolute()
    assert result.diagnostics["invocation"]["mineru_executable"] == str(mineru_exe.resolve())
    assert result.diagnostics["invocation"]["input_path_exists"] is True


def test_mineru_adapter_error_carries_invocation_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="bad path")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MinerUAdapterError) as exc_info:
        parse_pdf_with_mineru(
            pdf_path,
            "doc_1",
            asset_output_dir=tmp_path / "assets",
            output_dir=tmp_path / "mineru",
        )

    diagnostics = exc_info.value.diagnostics
    assert diagnostics["invocation"]["input_path"] == str(pdf_path.resolve())
    assert diagnostics["invocation"]["input_path_exists"] is True
    assert diagnostics["returncode"] == 2


def test_mineru_adapter_maps_middle_json_to_document_ir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        output_dir = Path(kwargs["cwd"])
        image_dir = output_dir / "images"
        image_dir.mkdir(parents=True)
        for name in ["formula.png", "table.png", "figure.png"]:
            (image_dir / name).write_bytes(b"png")
        middle = {
            "_version_name": "3.4.0",
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [600, 800],
                    "para_blocks": [
                        {
                            "type": "title",
                            "bbox": [50, 60, 550, 90],
                            "lines": [
                                {
                                    "bbox": [50, 60, 550, 90],
                                    "spans": [
                                        {
                                            "type": "text",
                                            "content": "A MinerU Paper",
                                            "bbox": [50, 60, 550, 90],
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "type": "text",
                            "bbox": [50, 110, 550, 145],
                            "lines": [
                                {
                                    "bbox": [50, 110, 550, 145],
                                    "spans": [
                                        {
                                            "type": "text",
                                            "content": "Energy",
                                            "bbox": [50, 110, 110, 130],
                                        },
                                        {
                                            "type": "inline_equation",
                                            "content": "$E=mc^2$",
                                            "bbox": [120, 110, 210, 130],
                                        },
                                    ],
                                }
                            ],
                        },
                        {
                            "type": "interline_equation",
                            "bbox": [100, 170, 500, 210],
                            "text": "$$a=b$$",
                            "img_path": "images/formula.png",
                        },
                        {
                            "type": "table",
                            "bbox": [50, 240, 550, 340],
                            "table_body": "<table><tr><td>Site</td></tr></table>",
                            "img_path": "images/table.png",
                        },
                        {
                            "type": "image",
                            "bbox": [50, 380, 550, 620],
                            "img_path": "images/figure.png",
                        },
                        {
                            "type": "header",
                            "bbox": [0, 0, 100, 20],
                            "text": "header",
                        },
                    ],
                }
            ],
        }
        (output_dir / "sample_middle.json").write_text(
            json.dumps(middle),
            encoding="utf-8",
        )
        (output_dir / "sample_content_list.json").write_text("[]", encoding="utf-8")
        (output_dir / "sample_content_list_v2.json").write_text("[[]]", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = parse_pdf_with_mineru(
        tmp_path / "paper.pdf",
        "doc_1",
        asset_output_dir=tmp_path / "assets",
        output_dir=tmp_path / "mineru",
    )

    document = result.document
    blocks = document.pages[0].blocks
    assert document.extraction_backend == "mineru"
    assert document.extraction_version == "3.4.0"
    assert [block.role for block in blocks] == [
        BlockRole.TITLE,
        BlockRole.PARAGRAPH,
        BlockRole.FORMULA,
        BlockRole.TABLE,
    ]
    assert len(document.formulas) == 2
    assert all(formula.source_kind == "mineru" for formula in document.formulas)
    assert all(
        formula.pdf_formula is not None
        and formula.pdf_formula.replay_kind == "source_clip"
        for formula in document.formulas
    )
    assert "{{formula:" in blocks[1].text_for_translation
    assert blocks[2].text_for_translation.startswith("{{formula:")
    assert result.diagnostics["discarded_block_count"] == 1
    assert result.diagnostics["copied_asset_count"] == 3


def test_mineru_adapter_uses_content_list_v2_when_middle_has_no_blocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        output_dir = Path(kwargs["cwd"])
        middle = {
            "_version_name": "3.4.0",
            "pdf_info": [{"page_idx": 0, "page_size": [600, 800]}],
        }
        content_v2 = [
            [
                {
                    "type": "title",
                    "bbox": [0.1, 0.1, 0.9, 0.2],
                    "content": "Fallback Heading",
                }
            ]
        ]
        (output_dir / "sample_middle.json").write_text(
            json.dumps(middle),
            encoding="utf-8",
        )
        (output_dir / "sample_content_list_v2.json").write_text(
            json.dumps(content_v2),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = parse_pdf_with_mineru(
        tmp_path / "paper.pdf",
        "doc_1",
        asset_output_dir=tmp_path / "assets",
        output_dir=tmp_path / "mineru",
    )

    block = result.document.pages[0].blocks[0]
    assert block.source_text == "Fallback Heading"
    assert block.bbox.x0 == 60
    assert block.bbox.y1 == 160
    assert result.diagnostics["text_block_count"] == 1


def test_parse_pdf_with_config_writes_mineru_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p0001",
                size=PageSize(width=100, height=100),
                blocks=[],
            )
        ],
        extraction_backend="mineru",
        quality_flags=["mineru_extraction"],
    )

    def fake_parse_pdf_with_mineru(*_args: object, **_kwargs: object) -> MinerUParseResult:
        return MinerUParseResult(
            document=document,
            diagnostics={"kind": "mineru_diagnostics", "status": "completed"},
            artifacts={
                "mineru-middle": {"pdf_info": []},
                "mineru-content-list": [],
                "mineru-content-list-v2": [[]],
            },
        )

    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(orchestrator, "parse_pdf_with_mineru", fake_parse_pdf_with_mineru)

    parsed = orchestrator._parse_pdf_with_config(tmp_path / "paper.pdf", "doc_1")

    assert parsed.extraction_backend == "mineru"
    assert storage.read_output_json("doc_1", "mineru-diagnostics.json")["status"] == "completed"
    assert storage.read_output_json("doc_1", "mineru-middle.json") == {"pdf_info": []}


def test_parse_pdf_with_config_falls_back_to_pymupdf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = Storage(tmp_path)
    fallback_document = DocumentIR(
        doc_id="doc_1",
        pages=[
            DocumentPage(
                page_id="p0001",
                size=PageSize(width=100, height=100),
                blocks=[],
            )
        ],
    )

    def fake_parse_pdf_with_mineru(*_args: object, **_kwargs: object) -> MinerUParseResult:
        raise MinerUAdapterError(
            "missing mineru",
            diagnostics={"invocation": {"input_path_exists": True}},
        )

    def fake_parse_pdf(*_args: object, **_kwargs: object) -> DocumentIR:
        return fallback_document

    monkeypatch.setattr(orchestrator, "storage", storage)
    monkeypatch.setattr(orchestrator, "parse_pdf_with_mineru", fake_parse_pdf_with_mineru)
    monkeypatch.setattr(orchestrator, "parse_pdf", fake_parse_pdf)

    parsed = orchestrator._parse_pdf_with_config(tmp_path / "paper.pdf", "doc_1")

    diagnostics = storage.read_output_json("doc_1", "mineru-diagnostics.json")
    assert parsed.extraction_backend == "pymupdf"
    assert "pymupdf_fallback_used" in parsed.quality_flags
    assert diagnostics["status"] == "fallback_to_pymupdf"
    assert diagnostics["fallback_backend"] == "pymupdf"
    assert diagnostics["invocation"]["input_path_exists"] is True
