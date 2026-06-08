from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from pdf_translator_schema import DocumentIR

from .config import settings
from .models import JobStatus


class Storage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.uploads = root / "uploads"
        self.documents = root / "documents"
        self.jobs = root / "jobs"
        self.outputs = root / "outputs"
        self.config = root / "config"
        for path in (self.uploads, self.documents, self.jobs, self.outputs, self.config):
            path.mkdir(parents=True, exist_ok=True)

    def new_doc_id(self) -> str:
        return f"doc_{uuid4().hex}"

    def new_job_id(self) -> str:
        return f"job_{uuid4().hex}"

    async def save_upload(self, doc_id: str, upload: UploadFile, max_bytes: int) -> Path:
        suffix = Path(upload.filename or "upload.pdf").suffix or ".pdf"
        path = self.uploads / f"{doc_id}{suffix}"
        total_bytes = 0
        with path.open("wb") as out:
            while chunk := await upload.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="PDF upload is too large")
                out.write(chunk)
        return path

    async def save_upload_file(
        self,
        doc_id: str,
        upload: UploadFile,
        max_bytes: int,
        default_suffix: str = ".bin",
    ) -> Path:
        suffix = Path(upload.filename or f"upload{default_suffix}").suffix or default_suffix
        path = self.uploads / f"{doc_id}{suffix}"
        total_bytes = 0
        with path.open("wb") as out:
            while chunk := await upload.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="Upload is too large")
                out.write(chunk)
        return path

    def find_upload(self, doc_id: str) -> Path | None:
        for path in self.uploads.glob(f"{doc_id}.*"):
            if path.is_file():
                return path
        return None

    def save_document_ir(self, document: DocumentIR) -> Path:
        path = self.documents / f"{document.doc_id}.json"
        path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load_document_ir(self, doc_id: str) -> DocumentIR:
        path = self.documents / f"{doc_id}.json"
        return DocumentIR.model_validate_json(path.read_text(encoding="utf-8"))

    def save_status(self, status: JobStatus) -> None:
        path = self.jobs / f"{status.job_id}.json"
        path.write_text(status.model_dump_json(indent=2), encoding="utf-8")

    def load_status(self, job_id: str) -> JobStatus:
        path = self.jobs / f"{job_id}.json"
        return JobStatus.model_validate_json(path.read_text(encoding="utf-8"))

    def list_statuses(self, limit: int = 25) -> list[JobStatus]:
        statuses: list[tuple[float, JobStatus]] = []
        for path in self.jobs.glob("*.json"):
            try:
                status = JobStatus.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            statuses.append((path.stat().st_mtime, status))
        statuses.sort(key=lambda item: item[0], reverse=True)
        return [status for _, status in statuses[:limit]]

    def output_dir(self, doc_id: str) -> Path:
        path = self.outputs / doc_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def asset_dir(self, doc_id: str) -> Path:
        path = self.output_dir(doc_id) / "assets"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def find_asset_file(self, doc_id: str, asset_id: str) -> Path | None:
        for path in self.asset_dir(doc_id).glob(f"{asset_id}.*"):
            if path.is_file():
                return path
        return None

    def save_preview_html(self, doc_id: str, html: str) -> Path:
        path = self.output_dir(doc_id) / "preview.html"
        path.write_text(html, encoding="utf-8")
        return path

    def preview_html_path(self, doc_id: str) -> Path:
        return self.output_dir(doc_id) / "preview.html"

    def output_pdf_path(self, doc_id: str) -> Path:
        return self.output_dir(doc_id) / "translated.pdf"

    def write_json(self, doc_id: str, name: str, payload: object) -> Path:
        path = self.output_dir(doc_id) / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_output_json(self, doc_id: str, name: str) -> object:
        path = self.output_dir(doc_id) / name
        return json.loads(path.read_text(encoding="utf-8"))

    def output_json_path(self, doc_id: str, name: str) -> Path:
        return self.output_dir(doc_id) / name

    def copy_output_pdf(self, source: Path, doc_id: str) -> Path:
        target = self.output_pdf_path(doc_id)
        shutil.copyfile(source, target)
        return target

    def runtime_config_path(self) -> Path:
        return self.config / "runtime-config.json"

    def read_runtime_config(self) -> dict:
        path = self.runtime_config_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def write_runtime_config(self, payload: dict) -> Path:
        path = self.runtime_config_path()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


storage = Storage(settings.storage_dir)
