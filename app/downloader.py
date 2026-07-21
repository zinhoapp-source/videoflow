import gc
import os
from pathlib import Path

import yt_dlp

from app.config import DOWNLOAD_DIR, THUMB_DIR, YT_DLP_COOKIES_FILE
from app.jobs import Job, JobStatus
from app.media_utils import build_file_url, ffprobe, generate_thumbnail


class DownloadCancelled(Exception):
    pass


def _locate_download(job_id: str) -> Path | None:
    preferred = DOWNLOAD_DIR / f"{job_id}.mp4"
    if preferred.exists() and preferred.stat().st_size > 0:
        return preferred

    candidates = sorted(
        (
            path
            for path in DOWNLOAD_DIR.glob(f"{job_id}.*")
            if path.is_file() and not path.name.endswith(('.part', '.ytdl'))
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def run_download(job: Job) -> None:
    url = job.params["sourceUrl"]
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    output_template = str(DOWNLOAD_DIR / f"{job.id}.%(ext)s")
    thumb_path = THUMB_DIR / f"{job.id}.jpg"

    job.set_status(JobStatus.VALIDANDO, progress=5, stage="Validando link")

    def hook(data: dict) -> None:
        if job.cancelled:
            raise DownloadCancelled("Download cancelado")

        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = int(data.get("downloaded_bytes") or 0)
            progress = 10
            if total:
                progress = min(80, int(downloaded / total * 70) + 10)
            job.status = JobStatus.BAIXANDO
            job.stage = "Baixando vídeo"
            job.progress = progress
            job.total_bytes = int(total)
            job.downloaded_bytes = downloaded
            job.speed = int(data.get("speed") or 0)
            job.eta = int(data.get("eta") or 0)
            job.updated_at = __import__("time").time()
        elif status == "finished":
            job.set_status(
                JobStatus.JUNTANDO,
                progress=82,
                stage="Juntando áudio e vídeo",
            )

    options = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            )
        },
        "postprocessors": [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
        ],
    }
    if YT_DLP_COOKIES_FILE and os.path.isfile(YT_DLP_COOKIES_FILE):
        options["cookiefile"] = YT_DLP_COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        if job.cancelled or isinstance(exc, DownloadCancelled):
            job.set_status(JobStatus.CANCELADO, stage="Download cancelado")
            return
        raise RuntimeError(f"Falha no yt-dlp: {exc}") from exc

    if job.cancelled:
        job.set_status(JobStatus.CANCELADO, stage="Download cancelado")
        return

    final_path = _locate_download(job.id)
    if not final_path or final_path.stat().st_size == 0:
        raise RuntimeError("Arquivo não encontrado após o download")

    job.set_status(JobStatus.CONVERTENDO, progress=88)
    metadata = ffprobe(final_path)
    if not metadata:
        raise RuntimeError("O arquivo baixado não pôde ser validado pelo FFprobe")

    job.set_status(JobStatus.THUMBNAIL, progress=94)
    generate_thumbnail(final_path, thumb_path, 1.0)

    base_url = job.params.get("workerBaseUrl", "")
    result = {
        "fileUrl": build_file_url(base_url, "downloads", final_path.name),
        "filename": final_path.name,
        "duration": metadata.get("duration"),
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "size": metadata.get("size"),
        "hasAudio": metadata.get("has_audio"),
        "mimeType": "video/mp4",
    }
    if thumb_path.exists():
        result["thumbnailUrl"] = build_file_url(
            base_url,
            "thumbnails",
            thumb_path.name,
        )

    job.result["__filePath"] = str(final_path)
    job.result["__thumbPath"] = str(thumb_path)
    job.set_status(JobStatus.CONCLUIDO, progress=100, **result)
    gc.collect()
