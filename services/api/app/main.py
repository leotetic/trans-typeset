from __future__ import annotations

from contextlib import asynccontextmanager

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .pipeline.resume import resume_incomplete_jobs
from .routes.documents import router as documents_router
from .storage import storage


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await resume_incomplete_jobs(storage)
    yield


app = FastAPI(title="Trans Typesetting API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(documents_router)
