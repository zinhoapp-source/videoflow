import gc
import os
import re
import shutil
import signal
import subprocess
import time

import httpx

from config import OUTPUT_DIR, TEMP_DIR
from jobs import JobStatus
from media_utils import ffprobe, generate_thumbnail, validate_video_file, validate_png_file

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _extract_error(stderr_lines):
    """Extract the real error line from FFmpeg stderr (last matching line wins)."""
    error_kws = (
        "Error", "error", "ERROR", "failed", "Failed", "FAILED",
        "Invalid", "invalid", "No such file", "No such", "Cannot", "cannot",
        "not found", "Unable", "unable", "missing", "Missing",
        "Unsupported", "unsupported", "Permission denied",
        "out of memory", "OOM", "Cannot allocate",
        "Broken pipe", "reset by peer", "timed out",
        "syntax", "argument", "refers", "does not exist",
        "matches no streams", "not a valid",
    )
    # Markers that identify x264/x265 encoder config dump lines (key=value spam).
    x264_markers = ("me_range=", "deadzone=", "sliced_threads=", "cabac=",
                    "trellis=", "chroma_me=", "fast_pskip=", "lookahead_threads=")
    for line in reversed(stderr_lines):
        s = line.strip()
        if not s:
            continue
        # Skip noise prefixes (banner, input info, config lines).
        if s.startswith(("ffmpeg version", "configuration:", "  lib", "libav", "libsw",
                         "Input #", "Output #", "Stream ", "  Metadata:", "  Duration:",
                         "Press [q]", "frame=", "size=", "At least ",
                         "[libx264", "[libx265", "[aac", "[vorbis", "[opus",
                         "[SWScal", "[AVIO", "[graph", "[Parsed",
                         "[auto", "[format")):
            continue
        # Skip x264/x265 encoder config dump (long key=value sequence).
        if any(m in s for m in x264_markers):
            continue
        # Skip lines that are just "key=value key=value" codec options.
        if s.startswith(("264 -", "265 -", "options:", "using ", "profile ")):
            continue
        if any(kw in s for kw in error_kws):
            return s
    return ""


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
        scale = f"scale={ww}:{wh}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={ww}:{wh}:(ow-iw)/2:(oh-ih)/2:black"

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


def _cleanup_temp(temp_dir):
    try:
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass


def run_render(job):
    p = job.params
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

    job_temp = os.path.join(TEMP_DIR, job.id)
    os.makedirs(job_temp, exist_ok=True)

    video_url = p["videoUrl"]
    output_path = os.path.join(OUTPUT_DIR, f"{job.id}.mp4")
    input_path = os.path.join(job_temp, "input.mp4")
    thumb_path = os.path.join(OUTPUT_DIR, f"{job.id}.jpg")

    diag = {
        "videoUrl": video_url,
        "videoPath": input_path,
        "outputPath": output_path,
        "tempDir": job_temp,
    }

    # Stage 1: Download
    job.set_status(JobStatus.VALIDANDO, progress=3, stage="Baixando vídeo de origem")
    t0 = time.time()
    try:
        with httpx.Client(timeout=300, follow_redirects=True) as client:
            r = client.get(video_url)
            r.raise_for_status()
            with open(input_path, "wb") as f:
                f.write(r.content)
    except Exception as e:
        diag["downloadError"] = str(e)
        job.diagnostics = diag
        job.set_status(JobStatus.ERRO, error=f"Falha ao baixar vídeo: {e}")
        return

    diag["downloadDuration"] = round(time.time() - t0, 2)

    # Stage 2: Validate video
    job.set_status(JobStatus.VALIDANDO, progress=8, stage="Validando vídeo de entrada")
    vval = validate_video_file(input_path)
    diag["videoValidation"] = vval
    job.diagnostics = diag
    if not vval["valid"]:
        job.set_status(JobStatus.ERRO, error=f"Vídeo inválido: {vval['reason']}")
        return

    total_duration = vval.get("duration", 0)
    has_audio = vval.get("has_audio", False)
    diag["hasAudio"] = has_audio

    # Stage 3: Download overlay
    overlay_path = None
    overlay_url = (p.get("template") or {}).get("overlayUrl", "")
    diag["overlayUrl"] = overlay_url

    if overlay_url:
        overlay_path = os.path.join(job_temp, "overlay.png")
        job.set_status(JobStatus.VALIDANDO, progress=12, stage="Baixando overlay PNG")
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                resp = client.get(overlay_url)
                if resp.status_code == 200 and resp.content:
                    with open(overlay_path, "wb") as f:
                        f.write(resp.content)
                else:
                    diag["overlayError"] = f"HTTP {resp.status_code}"
                    overlay_path = None
        except Exception as e:
            diag["overlayError"] = str(e)
            overlay_path = None

    if overlay_path:
        oval = validate_png_file(overlay_path)
        diag["overlayValidation"] = oval
        if not oval["valid"]:
            job.diagnostics = diag
            job.set_status(JobStatus.ERRO, error=f"Overlay inválido: {oval['reason']}")
            return
    else:
        diag["overlayValidation"] = {"valid": False, "reason": "Não baixado"}

    # Stage 4: Build filter_complex
    canvas = {"width": p.get("outputWidth", 720), "height": p.get("outputHeight", 1280)}
    win = p.get("videoWindow") or {
        "x": 0, "y": 0, "width": canvas["width"], "height": canvas["height"],
    }
    fit_mode = p.get("fitMode", "cover")
    remove_audio = bool((p.get("template") or {}).get("removeAudio"))

    filter_complex, out_label = build_filter_complex(canvas, win, fit_mode, bool(overlay_path))
    diag["filterComplex"] = filter_complex
    diag["canvas"] = canvas
    diag["videoWindow"] = win
    diag["fitMode"] = fit_mode
    diag["hasOverlay"] = bool(overlay_path)
    diag["removeAudio"] = remove_audio

    # Stage 5: Build FFmpeg command
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

    if not remove_audio and has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]

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
        "-movflags", "+faststart",
        output_path,
    ]

    diag["ffmpegCommand"] = cmd
    job.diagnostics = diag

    # Stage 6: Run FFmpeg
    job.set_status(JobStatus.RENDERIZANDO, progress=15)
    t0 = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    last_progress = 15
    stderr_lines = []
    for line in proc.stderr:
        stderr_lines.append(line)
        m = TIME_RE.search(line)
        if m and total_duration > 0:
            current = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            effective = max_duration or total_duration
            pct = min(95, int(current / effective * 80) + 15)
            if pct > last_progress:
                last_progress = pct
                job.progress = pct

    proc.wait()
    render_duration = round(time.time() - t0, 2)

    diag["exitCode"] = proc.returncode
    diag["renderDuration"] = render_duration
    diag["stderrTail"] = "".join(stderr_lines[-80:])
    job.diagnostics = diag

    # Stage 7: Check result
    success = (proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0)
    if not success:
        if proc.returncode < 0:
            try:
                sig_name = signal.Signals(-proc.returncode).name
            except ValueError:
                sig_name = f"Sinal {-proc.returncode}"
            hint = ""
            if proc.returncode == -9:
                hint = " — provável falta de memória. Reduza a resolução."
            elif proc.returncode == -11:
                hint = " — falha de segmentação no FFmpeg."
            error_msg = f"FFmpeg encerrado por sinal ({sig_name}{hint})"
        else:
            real_error = _extract_error(stderr_lines)
            if real_error:
                error_msg = f"FFmpeg falhou (código {proc.returncode}): {real_error}"
            else:
                # No identifiable error line → almost always OOM on small instances.
                error_msg = (f"FFmpeg encerrado (código {proc.returncode}) — "
                           f"provável falta de memória (OOM). "
                           f"Reduza a resolução ou use uma instância maior.")
        job.set_status(JobStatus.ERRO, error=error_msg)
        return  # keep temp files for debugging

    # Stage 8: Thumbnail + result
    job.set_status(JobStatus.THUMBNAIL, progress=97, stage="Gerando thumbnail")
    generate_thumbnail(output_path, thumb_path, 1.0)
    meta = ffprobe(output_path)

    job.set_status(JobStatus.ENVIANDO, progress=98, stage="Preparando URL de saída")

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

    _cleanup_temp(job_temp)
    gc.collect()