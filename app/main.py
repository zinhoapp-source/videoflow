from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl

APP_NAME = "VideoFlow Python Worker"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DOWNLOAD_DIR = DATA_DIR / "downloads"
OUTPUT_DIR = DATA_DIR / "outputs"
THUMB_DIR = DATA_DIR / "thumbnails"
TEMP_DIR = DATA_DIR / "temp"
DB_PATH = DATA_DIR / "jobs.sqlite3"
MAX_WORKERS = max(1, int(os.getenv("MAX_WORKERS", "2")))
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

for directory in (DATA_DIR, DOWNLOAD_DIR, OUTPUT_DIR, THUMB_DIR, TEMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="videoflow")
cancel_events: dict[str, threading.Event] = {}

app = FastAPI(title=APP_NAME, version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class DownloadRequest(BaseModel):
    userId: str = Field(min_length=1, max_length=200)
    assetId: str | None = Field(default=None, max_length=200)
    sourceUrl: HttpUrl
    callbackUrl: HttpUrl | None = None


class VideoWindow(BaseModel):
    x: int = 0
    y: int = 0
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class RenderTemplate(BaseModel):
    overlayUrl: HttpUrl
    layers: list[dict[str, Any]] = Field(default_factory=list)


class RenderRequest(BaseModel):
    userId: str = Field(min_length=1, max_length=200)
    sourceJobId: str | None = None
    videoUrl: HttpUrl
    outputWidth: int = Field(default=720, gt=0, le=4096)
    outputHeight: int = Field(default=1280, gt=0, le=4096)
    videoWindow: VideoWindow
    fitMode: Literal["cover", "contain"] = "cover"
    template: RenderTemplate
    callbackUrl: HttpUrl | None = None


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                stage TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


init_db()


def auth(authorization: str | None = Header(default=None)) -> None:
    if not WORKER_API_KEY:
        raise HTTPException(status_code=503, detail="WORKER_API_KEY não configurada no servidor")
    expected = f"Bearer {WORKER_API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Token inválido")


def create_job(job_type: str, payload: dict[str, Any]) -> str:
    job_id = str(uuid.uuid4())
    now = time.time()
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id,type,status,progress,stage,request_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, job_type, "queued", 0, "Aguardando", json.dumps(payload), now, now),
        )
        conn.commit()
    cancel_events[job_id] = threading.Event()
    return job_id


def update_job(job_id: str, **fields: Any) -> None:
    allowed = {"status", "progress", "stage", "result_json", "error"}
    filtered = {key: value for key, value in fields.items() if key in allowed}
    if not filtered:
        return
    filtered["updated_at"] = time.time()
    assignments = ",".join(f"{key}=?" for key in filtered)
    values = [filtered[key] for key in filtered]
    with db_conn() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values, job_id))
        conn.commit()


def read_job(job_id: str) -> dict[str, Any]:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Trabalho não encontrado")
    data = dict(row)
    data["request"] = json.loads(data.pop("request_json"))
    data["result"] = json.loads(data.pop("result_json")) if data.get("result_json") else None
    return data


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:180]


def command_version(command: str) -> str | None:
    try:
        result = subprocess.run([command, "-version"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return (result.stdout or result.stderr).splitlines()[0]
    except Exception:
        return None
    return None


def yt_dlp_version() -> str | None:
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def ffprobe_metadata(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe falhou")
    parsed = json.loads(result.stdout)
    video_stream = next((s for s in parsed.get("streams", []) if s.get("codec_type") == "video"), {})
    fmt = parsed.get("format", {})
    return {
        "duration": float(fmt.get("duration") or 0),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "size": int(fmt.get("size") or path.stat().st_size),
        "videoCodec": video_stream.get("codec_name"),
    }


def generate_thumbnail(video_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-ss", "1", "-i", str(video_path),
        "-frames:v", "1", "-q:v", "2", str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-1000:] or "Falha ao gerar thumbnail")


def public_url(request_base: str, category: str, filename: str) -> str:
    return f"{request_base.rstrip('/')}/files/{category}/{filename}"


def notify(callback_url: str | None, payload: dict[str, Any]) -> None:
    if not callback_url:
        return
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SECRET
    try:
        requests.post(callback_url, json=payload, headers=headers, timeout=20)
    except Exception:
        pass


def cancelled(job_id: str) -> bool:
    return cancel_events.setdefault(job_id, threading.Event()).is_set()


def run_download(job_id: str, payload: dict[str, Any], request_base: str) -> None:
    callback = payload.get("callbackUrl")
    try:
        update_job(job_id, status="validating", progress=3, stage="Validando link")
        notify(callback, {"jobId": job_id, "status": "validating", "progress": 3, "stage": "Validando link"})
        if cancelled(job_id):
            raise InterruptedError("Cancelado")

        output_template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

        def hook(data: dict[str, Any]) -> None:
            if cancelled(job_id):
                raise RuntimeError("CANCELLED")
            status = data.get("status")
            if status == "downloading":
                downloaded = int(data.get("downloaded_bytes") or 0)
                total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
                pct = (downloaded / total * 80) if total else 15
                progress = min(82, max(8, pct))
                stage = "Baixando vídeo"
                update_job(job_id, status="downloading", progress=progress, stage=stage)
            elif status == "finished":
                update_job(job_id, status="processing", progress=84, stage="Finalizando arquivo")

        import yt_dlp  # imported here so /health can report executable separately

        ydl_opts = {
        "format": "best/bestvideo+bestaudio",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "noplaylist": True,
        "restrictfilenames": True,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
    }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(payload["sourceUrl"], download=True)
            candidate = Path(ydl.prepare_filename(info))

        if cancelled(job_id):
            raise InterruptedError("Cancelado")

        mp4_path = DOWNLOAD_DIR / f"{job_id}.mp4"
        if not mp4_path.exists():
            matches = sorted(DOWNLOAD_DIR.glob(f"{job_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not matches:
                raise RuntimeError("yt-dlp terminou sem criar arquivo")
            source = matches[0]
            if source.suffix.lower() != ".mp4":
                update_job(job_id, status="converting", progress=87, stage="Convertendo para MP4")
                cmd = [
                    "ffmpeg", "-y", "-i", str(source),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(mp4_path),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr[-2000:] or "FFmpeg falhou")
            else:
                source.replace(mp4_path)

        update_job(job_id, status="thumbnail", progress=93, stage="Gerando thumbnail")
        thumb_path = THUMB_DIR / f"{job_id}.jpg"
        generate_thumbnail(mp4_path, thumb_path)
        metadata = ffprobe_metadata(mp4_path)
        result_payload = {
            "jobId": job_id,
            "assetId": payload.get("assetId"),
            "status": "completed",
            "progress": 100,
            "stage": "Concluído",
            "filename": mp4_path.name,
            "fileUrl": public_url(request_base, "downloads", mp4_path.name),
            "thumbnailUrl": public_url(request_base, "thumbnails", thumb_path.name),
            "mimeType": "video/mp4",
            **metadata,
        }
        update_job(job_id, status="completed", progress=100, stage="Concluído", result_json=json.dumps(result_payload), error=None)
        notify(callback, result_payload)
    except InterruptedError:
        update_job(job_id, status="cancelled", progress=0, stage="Cancelado", error="Cancelado pelo usuário")
        notify(callback, {"jobId": job_id, "status": "cancelled", "progress": 0, "error": "Cancelado pelo usuário"})
    except Exception as exc:
        message = str(exc)
        if "CANCELLED" in message:
            update_job(job_id, status="cancelled", progress=0, stage="Cancelado", error="Cancelado pelo usuário")
            notify(callback, {"jobId": job_id, "status": "cancelled", "progress": 0, "error": "Cancelado pelo usuário"})
        else:
            update_job(job_id, status="error", progress=0, stage="Erro", error=message)
            notify(callback, {"jobId": job_id, "status": "error", "progress": 0, "error": message})


def download_file(url: str, destination: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("URL inválida")
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with destination.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def run_ffmpeg_with_progress(job_id: str, cmd: list[str], duration: float, callback: str | None) -> None:
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert process.stdout is not None
    for line in process.stdout:
        if cancelled(job_id):
            process.terminate()
            raise InterruptedError("Cancelado")
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                out_seconds = int(line.split("=", 1)[1]) / 1_000_000
                pct = 20 + (out_seconds / duration * 70 if duration > 0 else 0)
                progress = min(92, max(20, pct))
                update_job(job_id, status="rendering", progress=progress, stage="Renderizando com FFmpeg")
                notify(callback, {"jobId": job_id, "status": "rendering", "progress": round(progress, 1), "stage": "Renderizando com FFmpeg"})
            except ValueError:
                pass
    stderr = process.stderr.read() if process.stderr else ""
    code = process.wait()
    if code != 0:
        raise RuntimeError(stderr[-3000:] or f"FFmpeg encerrou com código {code}")


def run_render(job_id: str, payload: dict[str, Any], request_base: str) -> None:
    callback = payload.get("callbackUrl")
    try:
        update_job(job_id, status="preparing", progress=5, stage="Baixando arquivos de entrada")
        notify(callback, {"jobId": job_id, "status": "preparing", "progress": 5, "stage": "Baixando arquivos de entrada"})
        video_path = TEMP_DIR / f"{job_id}_video.mp4"
        overlay_path = TEMP_DIR / f"{job_id}_overlay.png"
        download_file(payload["videoUrl"], video_path)
        download_file(payload["template"]["overlayUrl"], overlay_path)
        metadata = ffprobe_metadata(video_path)
        duration = metadata["duration"]
        output_path = OUTPUT_DIR / f"{job_id}.mp4"
        window = payload["videoWindow"]
        out_w = int(payload["outputWidth"])
        out_h = int(payload["outputHeight"])
        win_w = int(window["width"])
        win_h = int(window["height"])
        x = int(window["x"])
        y = int(window["y"])
        fit_mode = payload.get("fitMode", "cover")
        if fit_mode == "contain":
            fit_filter = f"scale={win_w}:{win_h}:force_original_aspect_ratio=decrease,pad={win_w}:{win_h}:(ow-iw)/2:(oh-ih)/2:color=black"
        else:
            fit_filter = f"scale={win_w}:{win_h}:force_original_aspect_ratio=increase,crop={win_w}:{win_h}"

        filter_complex = (
            f"color=c=black:s={out_w}x{out_h}:r=30[bg];"
            f"[0:v]{fit_filter},setsar=1[vid];"
            f"[bg][vid]overlay={x}:{y}:shortest=1[base];"
            f"[1:v]scale={out_w}:{out_h},format=rgba[ov];"
            f"[base][ov]overlay=0:0:shortest=1[outv]"
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path), "-loop", "1", "-i", str(overlay_path),
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-shortest",
            "-progress", "pipe:1", "-nostats", str(output_path),
        ]
        update_job(job_id, status="rendering", progress=20, stage="Renderizando com FFmpeg")
        run_ffmpeg_with_progress(job_id, cmd, duration, callback)
        if cancelled(job_id):
            raise InterruptedError("Cancelado")

        update_job(job_id, status="thumbnail", progress=95, stage="Gerando thumbnail final")
        thumb_path = THUMB_DIR / f"render_{job_id}.jpg"
        generate_thumbnail(output_path, thumb_path)
        final_metadata = ffprobe_metadata(output_path)
        result_payload = {
            "jobId": job_id,
            "sourceJobId": payload.get("sourceJobId"),
            "status": "completed",
            "progress": 100,
            "stage": "Concluído",
            "filename": output_path.name,
            "fileUrl": public_url(request_base, "outputs", output_path.name),
            "thumbnailUrl": public_url(request_base, "thumbnails", thumb_path.name),
            "mimeType": "video/mp4",
            **final_metadata,
        }
        update_job(job_id, status="completed", progress=100, stage="Concluído", result_json=json.dumps(result_payload), error=None)
        notify(callback, result_payload)
    except InterruptedError:
        update_job(job_id, status="cancelled", progress=0, stage="Cancelado", error="Cancelado pelo usuário")
        notify(callback, {"jobId": job_id, "status": "cancelled", "progress": 0, "error": "Cancelado pelo usuário"})
    except Exception as exc:
        message = str(exc)
        update_job(job_id, status="error", progress=0, stage="Erro", error=message)
        notify(callback, {"jobId": job_id, "status": "error", "progress": 0, "error": message})
    finally:
        for path in (TEMP_DIR / f"{job_id}_video.mp4", TEMP_DIR / f"{job_id}_overlay.png"):
            path.unlink(missing_ok=True)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": APP_NAME, "status": "ok"}


@app.get("/api/health")
def health() -> dict[str, Any]:
    ffmpeg = command_version("ffmpeg")
    ffprobe = command_version("ffprobe")
    ytdlp = yt_dlp_version()
    writable = os.access(DATA_DIR, os.W_OK)
    healthy = all([ffmpeg, ffprobe, ytdlp, writable, WORKER_API_KEY])
    return {
        "ok": healthy,
        "service": APP_NAME,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "ytDlp": ytdlp,
        "dataDirWritable": writable,
        "apiKeyConfigured": bool(WORKER_API_KEY),
        "maxWorkers": MAX_WORKERS,
    }


@app.post("/api/downloads", dependencies=[Depends(auth)], status_code=202)
def create_download(data: DownloadRequest, request: Request) -> dict[str, Any]:
    payload = data.model_dump(mode="json")
    job_id = create_job("download", payload)
    executor.submit(run_download, job_id, payload, str(request.base_url).rstrip("/"))
    return {"jobId": job_id, "status": "queued", "progress": 0}


@app.post("/api/renders", dependencies=[Depends(auth)], status_code=202)
def create_render(data: RenderRequest, request: Request) -> dict[str, Any]:
    payload = data.model_dump(mode="json")
    job_id = create_job("render", payload)
    executor.submit(run_render, job_id, payload, str(request.base_url).rstrip("/"))
    return {"jobId": job_id, "status": "queued", "progress": 0}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
def get_job(job_id: str) -> dict[str, Any]:
    return read_job(job_id)


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
def cancel_job(job_id: str) -> dict[str, str]:
    read_job(job_id)
    cancel_events.setdefault(job_id, threading.Event()).set()
    update_job(job_id, status="cancelling", stage="Cancelando")
    return {"jobId": job_id, "status": "cancelling"}


@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(auth)], status_code=202)
def retry_job(job_id: str, request: Request) -> dict[str, Any]:
    old = read_job(job_id)
    payload = old["request"]
    new_id = create_job(old["type"], payload)
    runner = run_download if old["type"] == "download" else run_render
    executor.submit(runner, new_id, payload, str(request.base_url).rstrip("/"))
    return {"jobId": new_id, "status": "queued", "progress": 0, "retriedFrom": job_id}


@app.get("/files/{category}/{filename}")
def serve_file(category: str, filename: str) -> FileResponse:
    directories = {"downloads": DOWNLOAD_DIR, "outputs": OUTPUT_DIR, "thumbnails": THUMB_DIR}
    if category not in directories:
        raise HTTPException(status_code=404, detail="Categoria inválida")
    filename = safe_filename(filename)
    path = directories[category] / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    media_type = "video/mp4" if path.suffix.lower() == ".mp4" else "image/jpeg"
    return FileResponse(path, media_type=media_type, filename=path.name)
