import gc
import os
import re
import subprocess

import httpx

from config import OUTPUT_DIR, TEMP_DIR
from jobs import JobStatus
from media_utils import ffprobe, generate_thumbnail, get_duration

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _extract_ffmpeg_errors(output_lines):
    """Return only actual FFmpeg error lines, ignoring all banners and info."""
    error_kws = (
        "error", "Error", "ERROR", "failed", "Failed", "FAILED",
        "Invalid", "invalid", "No such", "Cannot", "cannot",
        "not found", "Unable", "unable", "missing", "Missing",
        "Unsupported", "unsupported", "Permission denied",
        "out of memory", "OOM", "Cannot allocate",
        "Broken pipe", "reset by peer", "timed out",
        "syntax", "argument", "refers",
    )
    err_lines = []
    started_processing = False
    
    for raw in output_lines:
        s = raw.strip()
        if not s:
            continue
            
        # O FFmpeg só começa a processar após ler os inputs e mapear os streams.
        # Ignoramos tudo o que vier antes para não capturar versões e banners.
        if "Stream #0:" in s or "Output #" in s or "press 'q' to stop" in s.lower():
            started_processing = True
            
        if not started_processing:
            continue
            
        # Se já começou, procuramos apenas por linhas que contenham palavras-chave de erro real
        if any(kw in s for kw in error_kws):
            err_lines.append(s)
            
    return err_lines


def build_filter_complex(canvas, win, fit_mode, has_overlay):
    W = max(2, (int(canvas["width"]) // 2) * 2)
    H = max(2, (int(canvas["height"]) // 2) * 2)
    wx = max(0, min(W, (int(win.get("x", 0)) // 2) * 2))
    wy = max(0, min(H, (int(win.get("y", 0)) // 2) * 2))
    ww = max(2, min(W, (int(win.get("width", W)) // 2) * 2))
    wh = max(2, min(H, (int(win.get("height", H)) // 2) * 2))
    if wx + ww >= W and wy + wh >= H:
        wx, wy, ww, wh = 0, 0, W, H

    if fit_mode in ("cover", "fill"):
        scale = f"scale={ww}:{wh}:force_original_aspect_ratio=increase:force_divisible_by=2,crop={ww}:{wh}"
    elif fit_mode == "stretch":
        scale = f"scale={ww}:{wh}"
    else:
        scale = f"scale={ww}:{wh}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={ww}:{wh}:-1:-1:black"

    full_canvas = (wx == 0 and wy == 0 and ww == W and wh == H)
    if full_canvas:
        if has_overlay:
            return f"[0:v]{scale}[scaled];[scaled][1:v]overlay=0:0[outv]", "[outv]"
        return f"[0:v]{scale}[outv]", "[outv]"

    if has_overlay:
        chain = f"[0:v]{scale}[scaled];[scaled]pad={W}:{H}:{wx}:{wy}:black[padded];[padded][1:v]overlay=0:0[outv]"
        return chain, "[outv]"
    chain = f"[0:v]{scale}[scaled];[scaled]pad={W}:{H}:{wx}:{wy}:black[outv]"
    return chain, "[outv]"


def run_render(job):
    p = job.params
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

    video_url = p["videoUrl"]
    output_path = os.path.join(OUTPUT_DIR, f"{job.id}.mp4")
    input_path = os.path.join(TEMP_DIR, f"{job.id}_input.mp4")
    thumb_path = os.path.join(OUTPUT_DIR, f"{job.id}.jpg")

    job.set_status(JobStatus.VALIDANDO, progress=5, stage="Baixando vídeo de origem")
    with httpx.Client(timeout=300, follow_redirects=True) as client:
        r = client.get(video_url)
        r.raise_for_status()
        with open(input_path, "wb") as f:
            f.write(r.content)

    if os.path.getsize(input_path) == 0:
        try:
            os.remove(input_path)
        except Exception:
            pass
        job.set_status(JobStatus.ERRO, error="Vídeo de origem vazio")
        return

    overlay_path = None
    overlay_url = (p.get("template") or {}).get("overlayUrl")
    if overlay_url:
        overlay_path = os.path.join(TEMP_DIR, f"{job.id}_overlay.png")
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                resp = client.get(overlay_url)
                if resp.status_code == 200 and resp.content:
                    with open(overlay_path, "wb") as f:
                        f.write(resp.content)
                else:
                    overlay_path = None
        except Exception:
            overlay_path = None

    total_duration = get_duration(input_path)
    canvas = {"width": p.get("outputWidth", 720), "height": p.get("outputHeight", 1280)}
    win = p.get("videoWindow") or {
        "x": 0, "y": 0, "width": canvas["width"], "height": canvas["height"],
    }
    fit_mode = p.get("fitMode", "cover")
    remove_audio = bool((p.get("template") or {}).get("removeAudio"))

    filter_complex, out_label = build_filter_complex(canvas, win, fit_mode, bool(overlay_path))

    max_duration = p.get("maxDuration")
    trim_start = p.get("trimStart")
    trim_end = p.get("trimEnd")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin"]
    if trim_start:
        cmd += ["-ss", str(trim_start)]
    cmd += ["-i", input_path]
    if overlay_path:
        cmd += ["-i", overlay_path]
    cmd += ["-filter_complex", filter_complex, "-map", out_label]
    if not remove_audio:
        cmd += ["-map", "0:a?"]
    if max_duration:
        cmd += ["-t", str(max_duration)]
    elif trim_end and trim_start:
        cmd += ["-t", str(float(trim_end) - float(trim_start))]
    elif trim_end:
        cmd += ["-to", str(trim_end)]
    cmd += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "23",
        "-threads", "1",
        "-x264-params", "threads=1",
    ]
    if not remove_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", output_path]

    job.set_status(JobStatus.RENDERIZANDO, progress=10)

    proc = subprocess.Popen(  # noqa: S603
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    last_progress = 10
    ffmpeg_output = []
    for line in proc.stdout:
        ffmpeg_output.append(line)
        m = TIME_RE.search(line)
        if m and total_duration > 0:
            current = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            effective_duration = max_duration or total_duration
            pct = min(99, int(current / effective_duration * 89) + 10)
            if pct > last_progress:
                last_progress = pct
                job.progress = pct

    proc.wait()
    for _tmp in (input_path, overlay_path):
        try:
            if _tmp and os.path.isfile(_tmp):
                os.remove(_tmp)
        except Exception:
            pass
    overlay_path = None

    if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        if proc.returncode < 0:
            import signal
            try:
                sig_name = signal.Signals(-proc.returncode).name
            except ValueError:
                sig_name = f"Sinal {-proc.returncode}"
            hint = ""
            if proc.returncode == -9:
                hint = " — provável falta de memória no servidor. Reduza a resolução de saída."
            job.set_status(JobStatus.ERRO, error=f"FFmpeg foi encerrado ({sig_name}{hint})")
            return

        err_lines = _extract_ffmpeg_errors(ffmpeg_output)
        if err_lines:
            full_output = "\n".join(err_lines[-5:])[:400]
        else:
            full_output = (
                f"Processamento concluído com código {proc.returncode}, mas sem erros reais identificados. "
                f"Verifique se o vídeo gerado foi corrompido."
            )
        job.set_status(JobStatus.ERRO, error=f"FFmpeg falhou (código {proc.returncode})\n{full_output}")
        return

    job.set_status(JobStatus.THUMBNAIL, progress=96)
    generate_thumbnail(output_path, thumb_path, 1.0)
    meta = ffprobe(output_path)

    base = (p.get("workerBaseUrl") or "").rstrip("/")
    result = {
        "fileUrl": f"{base}/files/{job.id}.mp4",
        "filename": f"{job.id}.mp4",
        "duration": meta.get("duration"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "size": meta.get("size"),
        "mimeType": "video/mp4",
    }
    if os.path.exists(thumb_path):
        result["thumbnailUrl"] = f"{base}/files/{job.id}.jpg"
    job.result["__filePath"] = output_path
    job.result["__thumbPath"] = thumb_path
    job.set_status(JobStatus.CONCLUIDO, progress=100, **result)
    gc.collect()