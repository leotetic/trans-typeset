from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class DocxConversionError(RuntimeError):
    def __init__(self, message: str, diagnostics: dict[str, object]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def find_libreoffice_binary() -> str | None:
    configured = os.getenv("LIBREOFFICE_BIN", "").strip()
    if configured:
        return configured
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert_docx_to_pdf(
    *,
    docx_path: Path,
    output_dir: Path,
    filename: str,
    timeout_seconds: int = 120,
) -> tuple[Path, dict[str, object]]:
    converter = find_libreoffice_binary()
    diagnostics: dict[str, object] = {
        "kind": "docx_conversion",
        "source_filename": filename,
        "source_path": str(docx_path),
        "converter": converter,
        "status": "pending",
    }
    if not converter:
        diagnostics.update(
            {
                "status": "failed",
                "error": "LibreOffice/soffice was not found. Install LibreOffice or set LIBREOFFICE_BIN.",
                "recoverable": True,
            }
        )
        raise DocxConversionError(str(diagnostics["error"]), diagnostics)

    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        converter,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(docx_path),
    ]
    diagnostics["command"] = [converter, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(docx_path)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        diagnostics.update(
            {
                "status": "failed",
                "error": f"DOCX conversion timed out after {timeout_seconds}s.",
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "recoverable": True,
            }
        )
        raise DocxConversionError(str(diagnostics["error"]), diagnostics) from exc

    diagnostics.update(
        {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    )
    pdf_path = output_dir / f"{docx_path.stem}.pdf"
    if completed.returncode != 0 or not pdf_path.exists():
        candidates = sorted(output_dir.glob("*.pdf"))
        if candidates:
            pdf_path = candidates[0]
        else:
            diagnostics.update(
                {
                    "status": "failed",
                    "error": "DOCX conversion did not produce a PDF.",
                    "recoverable": True,
                }
            )
            raise DocxConversionError(str(diagnostics["error"]), diagnostics)

    diagnostics.update(
        {
            "status": "completed",
            "converted_pdf_path": str(pdf_path),
            "converted_pdf_bytes": pdf_path.stat().st_size,
            "recoverable": False,
        }
    )
    return pdf_path, diagnostics
