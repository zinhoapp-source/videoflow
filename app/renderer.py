import gc
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from app.config import OUTPUT_DIR, TEMP_DIR, THUMB_DIR
from app.jobs import Job, JobStatus
from app.media_utils import (
    build_file_url,
    ffprobe,
    generate_thumbnail,
    validate_png_file,
    validate_video_file,
)


def _download_to_file(url: str, destination: Path, timeout: int = 300) -> None:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as file:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    file.write(chunk)


def _validate_dimensions(value: object, default: int, minimum: int = 1) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = default
    return max(minimum, resolved)


def run_render(job: Job) -> None:
    params = job.params
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    job_temp = TEMP_DIR / job.id
    shutil.rmtree(job_temp, ignore_errors=True)
    job_temp.mkdir(parents=True, exist_ok=True)

    video_url = params["videoUrl"]
    output_path = OUTPUT_DIR / f"{job.id}.mp4"
    input_path = job_temp / "input-video"
    overlay_path = job_temp / "overlay.png"
    thumb_path = THUMB_DIR / f"{job.id}.jpg"

    job.diagnostics = {"videoUrl": video_url}
    job.set_status(
        JobStatus.VALIDANDO,
        progress=5,
        stage="Baixando e validando o vídeo",
    )

    try:
        _download_to_file(video_url, input_path)
    except Exception as exc:
        shutil.rmtree(job_temp, ignore_errors=True)
        raise RuntimeError(f"Falha ao baixar vídeo: {exc}") from exc

    if job.cancelled:
        job.set_status(JobStatus.CANCELADO)
        shutil.rmtree(job_temp, ignore_errors=True)
        return

    video_validation = validate_video_file(input_path)
    job.diagnostics["input"] = video_validation
    if not video_validation.get("valid"):
        shutil.rmtree(job_temp, ignore_errors=True)
        raise RuntimeError(
            f"Vídeo inválido: {video_validation.get('reason', 'erro desconhecido')}"
        )

    overlay_url = (params.get("template") or {}).get("overlayUrl", "").strip()
    has_overlay = bool(overlay_url)
    if has_overlay:
        job.set_status(
            JobStatus.VALIDANDO,
            progress=12,
            stage="Baixando e validando a moldura PNG",
        )
        try:
            _download_to_file(overlay_url, overlay_path, timeout=90)
        except Exception as exc:
            shutil.rmtree(job_temp, ignore_errors=True)
            raise RuntimeError(f"Falha ao baixar overlay: {exc}") from exc

        overlay_validation = validate_png_file(overlay_path)
        job.diagnostics["overlay"] = overlay_validation
        if not overlay_validation.get("valid"):
            shutil.rmtree(job_temp, ignore_errors=True)
            raise RuntimeError(
                f"Overlay inválido: {overlay_validation.get('reason')}"
            )

    output_width = _validate_dimensions(params.get("outputWidth"), 720)
    output_height = _validate_dimensions(params.get("outputHeight"), 1280)
    window = params.get("videoWindow") or {}
    window_width = _validate_dimensions(window.get("width"), output_width)
    window_height = _validate_dimensions(window.get("height"), output_height)
    x = max(0, int(window.get("x", 0) or 0))
    y = max(0, int(window.get("y", 0) or 0))

    if x + window_width > output_width or y + window_height > output_height:
        shutil.rmtree(job_temp, ignore_errors=True)
        raise RuntimeError(
            "A janela do vídeo ultrapassa o tamanho do canvas. "
            "Revise x, y, width e height."
        )

    fit_mode = str(params.get("fitMode", "cover")).lower()
    if fit_mode == "contain":
        fit_filter = (
            f"scale={window_width}:{window_height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={window_width}:{window_height}:"
            "(ow-iw)/2:(oh-ih)/2:color=black"
        )
    else:
        fit_filter = (
            f"scale={window_width}:{window_height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={window_width}:{window_height}"
        )

    background = str(params.get("backgroundColor", "black"))
    filter_parts = [
        f"color=c={background}:s={output_width}x{output_height}:r=30[bg]",
        f"[0:v]{fit_filter},setsar=1[vid]",
        f"[bg][vid]overlay={x}:{y}:shortest=1[base]",
    ]

    command = ["ffmpeg", "-y", "-i", str(input_path)]
    if has_overlay:
        command += ["-loop", "1", "-i", str(overlay_path)]
        filter_parts += [
            f"[1:v]scale={output_width}:{output_height},format=rgba[ov]",
            "[base][ov]overlay=0:0:shortest=1[outv]",
        ]
    else:
        filter_parts.append("[base]null[outv]")

    filter_complex = ";".join(filter_parts)
    command += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[outv]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(params.get("crf", 22)),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-shortest",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output_path),
    ]

    duration = float(video_validation.get("duration") or 0)
    job.set_status(JobStatus.RENDERIZANDO, progress=20)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    assert process.stdout is not None
    while True:
        if job.cancelled:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            job.set_status(JobStatus.CANCELADO, stage="Renderização cancelada")
            shutil.rmtree(job_temp, ignore_errors=True)
            return

        line = process.stdout.readline()
        if line:
            key, _, value = line.strip().partition("=")
            if key in {"out_time_ms", "out_time_us"} and duration > 0:
                try:
                    seconds = int(value) / 1_000_000
                    progress = 20 + int(min(1, seconds / duration) * 73)
                    job.set_status(
                        JobStatus.RENDERIZANDO,
                        progress=min(93, progress),
                    )
                except ValueError:
                    pass
        elif process.poll() is not None:
            break
        else:
            time.sleep(0.05)

    stderr = process.stderr.read() if process.stderr else ""
    if process.returncode != 0:
        shutil.rmtree(job_temp, ignore_errors=True)
        raise RuntimeError(f"FFmpeg falhou: {stderr[-1200:]}")

    output_validation = validate_video_file(output_path)
    job.diagnostics["output"] = output_validation
    if not output_validation.get("valid"):
        shutil.rmtree(job_temp, ignore_errors=True)
        raise RuntimeError(
            f"Saída inválida: {output_validation.get('reason', 'erro desconhecido')}"
        )

    job.set_status(JobStatus.THUMBNAIL, progress=95)
    generate_thumbnail(output_path, thumb_path, 1.0)
    metadata = ffprobe(output_path)

    base_url = params.get("workerBaseUrl", "")
    result = {
        "fileUrl": build_file_url(base_url, "outputs", output_path.name),
        "filename": output_path.name,
        "mimeType": "video/mp4",
        "duration": metadata.get("duration"),
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "size": metadata.get("size"),
        "hasAudio": metadata.get("has_audio"),
    }
    if thumb_path.exists():
        result["thumbnailUrl"] = build_file_url(
            base_url,
            "thumbnails",
            thumb_path.name,
        )

    job.result["__filePath"] = str(output_path)
    job.result["__thumbPath"] = str(thumb_path)
    job.set_status(JobStatus.CONCLUIDO, progress=100, **result)
    shutil.rmtree(job_temp, ignore_errors=True)
    gc.collect()
