import asyncio
import shutil
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, HttpUrl

from app.config import (
    DOWNLOAD_DIR,
    FILE_ACCESS_TOKEN,
    FILES_PUBLIC,
    OUTPUT_DIR,
    THUMB_DIR,
    WORKER_API_KEY,
)
from app.downloader import run_download
from app.jobs import (
    JobStatus,
    JobType,
    call_webhook,
    create_job,
    enqueue,
    get_job,
    list_jobs,
)
from app.renderer import run_render


class DownloadRequest(BaseModel):
    sourceUrl: HttpUrl
    userId: str | None = None
    assetId: str | None = None
    callbackUrl: HttpUrl | None = None


class VideoWindow(BaseModel):
    x: int = Field(default=0, ge=0)
    y: int = Field(default=0, ge=0)
    width: int = Field(default=720, ge=1)
    height: int = Field(default=1280, ge=1)


class TemplateSettings(BaseModel):
    overlayUrl: HttpUrl | None = None


class RenderRequest(BaseModel):
    videoUrl: HttpUrl
    userId: str | None = None
    jobId: str | None = None
    outputWidth: int = Field(default=720, ge=1, le=7680)
    outputHeight: int = Field(default=1280, ge=1, le=7680)
    videoWindow: VideoWindow | None = None
    fitMode: str = Field(default="cover", pattern="^(cover|contain)$")
    backgroundColor: str = "black"
    crf: int = Field(default=22, ge=0, le=51)
    template: TemplateSettings | None = None
    callbackUrl: HttpUrl | None = None


app = FastAPI(
    title="VideoFlow Python Worker",
    version="1.1.0",
    description="Worker de download e renderização de vídeos com yt-dlp e FFmpeg.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_auth(authorization: str | None) -> None:
    if not WORKER_API_KEY:
        return
    expected = f"Bearer {WORKER_API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_file_auth(
    authorization: str | None,
    token: str | None,
) -> None:
    if FILES_PUBLIC:
        return
    if FILE_ACCESS_TOKEN and token == FILE_ACCESS_TOKEN:
        return
    require_auth(authorization)


def public_base_url(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}".rstrip("/")
    return str(request.base_url).rstrip("/")


@app.get("/api/health")
async def health() -> JSONResponse:
    checks = {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
    }
    try:
        import yt_dlp  # noqa: F401

        checks["ytdlp"] = True
    except Exception:
        checks["ytdlp"] = False

    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "checks": checks, "version": app.version},
    )


@app.post("/api/downloads", status_code=202)
async def create_download_endpoint(
    body: DownloadRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    require_auth(authorization)
    params = {
        "userId": body.userId,
        "assetId": body.assetId,
        "sourceUrl": str(body.sourceUrl),
        "callbackUrl": str(body.callbackUrl) if body.callbackUrl else None,
        "workerBaseUrl": public_base_url(request),
    }
    job = create_job(JobType.DOWNLOAD, params)

    async def runner(current_job) -> None:
        await asyncio.to_thread(run_download, current_job)
        await call_webhook(current_job)

    await enqueue(job, runner)
    await call_webhook(job)
    return {"jobId": job.id, "status": job.status.value, "progress": job.progress}


@app.post("/api/renders", status_code=202)
async def create_render_endpoint(
    body: RenderRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    require_auth(authorization)
    window = body.videoWindow.model_dump() if body.videoWindow else None
    template = body.template.model_dump(mode="json") if body.template else {}
    params = {
        "userId": body.userId,
        "jobId": body.jobId,
        "videoUrl": str(body.videoUrl),
        "outputWidth": body.outputWidth,
        "outputHeight": body.outputHeight,
        "videoWindow": window,
        "fitMode": body.fitMode,
        "backgroundColor": body.backgroundColor,
        "crf": body.crf,
        "template": template,
        "callbackUrl": str(body.callbackUrl) if body.callbackUrl else None,
        "workerBaseUrl": public_base_url(request),
    }
    try:
        job = create_job(JobType.RENDER, params, job_id=body.jobId)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def runner(current_job) -> None:
        await asyncio.to_thread(run_render, current_job)
        await call_webhook(current_job)

    await enqueue(job, runner)
    await call_webhook(job)
    return {"jobId": job.id, "status": job.status.value, "progress": job.progress}


@app.get("/api/jobs")
async def list_jobs_endpoint(
    authorization: str | None = Header(default=None),
) -> dict:
    require_auth(authorization)
    jobs = [job.to_dict() for job in list_jobs()]
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/api/jobs/{job_id}")
async def get_job_endpoint(
    job_id: str,
    authorization: str | None = Header(default=None),
) -> dict:
    require_auth(authorization)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabalho não encontrado")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job_endpoint(
    job_id: str,
    authorization: str | None = Header(default=None),
) -> dict:
    require_auth(authorization)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabalho não encontrado")
    if job.status in (JobStatus.CONCLUIDO, JobStatus.ERRO, JobStatus.CANCELADO):
        return {"jobId": job.id, "status": job.status.value}
    job.cancelled = True
    job.set_status(JobStatus.CANCELADO, stage="Cancelamento solicitado")
    await call_webhook(job)
    return {"jobId": job.id, "status": job.status.value}


@app.get("/files/{category}/{filename}")
async def serve_file(
    category: str,
    filename: str,
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> FileResponse:
    require_file_auth(authorization, token)
    directories: dict[str, Path] = {
        "downloads": DOWNLOAD_DIR,
        "outputs": OUTPUT_DIR,
        "thumbnails": THUMB_DIR,
    }
    directory = directories.get(category)
    if not directory:
        raise HTTPException(status_code=404, detail="Categoria inválida")

    safe_filename = Path(filename).name
    path = directory / safe_filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    media_type = "image/jpeg" if category == "thumbnails" else "video/mp4"
    return FileResponse(path, media_type=media_type)


@app.get("/")
async def root() -> dict:
    return {
        "service": "VideoFlow Python Worker",
        "status": "ok",
        "health": "/api/health",
        "docs": "/docs",
    }
