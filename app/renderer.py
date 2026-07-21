import gc
import os
import re
import subprocess

import httpx

from config import OUTPUT_DIR, TEMP_DIR
from jobs import JobStatus
from media_utils import ffprobe, generate_thumbnail, get_duration

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

# Lines that are always noise — banner, library info, input/output config,
# stream mapping, progress stats, metadata. These never contain the actual
# error and must be excluded from the error message shown to the user.
_NOISE_PREFIXES = (
    "ffmpeg version", "configuration:", "  lib", "libav", "libsw", "libpost",
    "Input #", "Output #", "Stream mapping", "Stream #", "  Metadata:",
    "  Duration:", "  major_brand", "  minor_version", "  compatible_brands",
    "  handler_name", "  vendor_id", "  encoder ", "  Side data", "  cpb:",
    "Press [q]", "At least ", "frame=", "size=", "q=2-31", "Truncating",
    "Option ", "Aquitting",
)
# Lines starting with these bracket-prefixed filters are libx264/libavcodec
# config lines (e.g. "[libx264 @ 0x...] using SAR=1/1") — not errors.
_CONFIG_BRACKETS = ("[libx264", "[libavcodec", "[libavformat", "[libavfilter",
                    "[libswscale", "[libswresample", "[libavutil")


def _extract_ffmpeg_errors(output_lines):
    """Return only lines that look like actual FFmpeg errors.

    FFmpeg errors are lines that:
    - Start with '[' (filter/codec context) but are NOT config lines
    - Contain an error keyword (Error, failed, Invalid, No such, etc.)
    Everything else (banner, input info, output config, progress) is noise.
    """
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
    for raw in output_lines:
        s = raw.strip()
        if not s:
            continue
        # Skip noise lines (banner, input info, output config, progress).
        if any(s.startswith(p) for p in _NOISE_PREFIXES):
            continue
        # Skip libx264/libav config lines that start with [lib...
        if any(s.startswith(b) for b in _CONFIG_BRACKETS):
            # But keep them if they contain an actual error keyword.
            if not any(kw in s for kw in error_kws):
                continue
        # An error line must contain an error keyword.
        if any(kw in s for kw in error_kws):
            err_lines.append(s)
    return err_lines


def build_filter_complex(canvas, win, fit_mode, has_overlay):
    """Build the FFmpeg filter_complex for the renderSpec.

    Uses `pad` instead of `color=black` + `overlay` to place the video window
    on the canvas. This is dramatically more memory-efficient: `color=black`
    creates an INFINITE source of full-canvas frames (1080×1920 × 3 bytes ×
    thread count), which causes OOM kills on Railway. `pad` simply adds black
    borders to the existing scaled video frames — one buffer at a time.

    1. Scale + crop/pad the source video into the video window (ww×wh).
    2. Pad the scaled video to the full canvas (W×H) with black at (wx, wy).
    3. Overlay the transparent PNG frame on top (always in front of the video).
    """
    # Ensure even dimensions — libx264 with yuv420p rejects odd width/height.
    W = max(2, (int(canvas["width"]) // 2) * 2)
    H = max(2, (int(canvas["height"]) // 2) * 2)
    # Clamp the video window to fit within the canvas.
    wx = max(0, min(W, (int(win.get("x", 0)) // 2) * 2))
    wy = max(0, min(H, (int(win.get("y", 0)) // 2) * 2))
    ww = max(2, min(W, (int(win.get("width", W)) // 2) * 2))
    wh = max(2, min(H, (int(win.get("height", H)) // 2) * 2))
    # If the window is at least as large as the canvas, treat it as full canvas.
    if wx + ww >= W and wy + wh >= H:
        wx, wy, ww, wh = 0, 0, W, H

    if fit_mode in ("cover", "fill"):
        scale = f"scale={ww}:{wh}:force_original_aspect_ratio=increase:force_divisible_by=2,crop={ww}:{wh}"
    elif fit_mode == "stretch":
        scale = f"scale={ww}:{wh}"
    else:  # contain / center
        # force_divisible_by=2 ensures the scaled dimensions are even, so
        # (ow-iw)/2 and (oh-ih)/2 are always integers (pad rejects non-integer x/y).
        scale = f"scale={ww}:{wh}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={ww}:{wh}:(ow-iw)/2:(oh-ih)/2:black"

    # If the video window fills the entire canvas, no outer pad is needed.
    full_canvas = (wx == 0 and wy == 0 and ww == W and wh == H)
    if full_canvas:
        if has_overlay:
            return f"[0:v]{scale}[scaled];[scaled][1:v]overlay=0:0[outv]", "[outv]"
        return f"[0:v]{scale}[outv]", "[outv]"

    # Pad the scaled video to the full canvas at (wx, wy) with black borders.
    if has_overlay:
        chain = f"[0:v]{scale}[scaled];[scaled]pad={W}:{H}:{wx}:{wy}:black[padded];[padded][1:v]overlay=0:0[outv]"
        return chain, "[outv]"
    chain = f"[0:v]{scale}[scaled];[scaled]pad={W}:{H}:{wx}:{wy}:black[outv]"
    return chain, "[outv]"


def run_render(job):
    """Download source video + overlay, run FFmpeg, probe the result."""
    p = job.params
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

    video_url = p["videoUrl"]
    output_path = os.path.join(OUTPUT_DIR, f"{job.id}.mp4")
    input_path = os.path.join(TEMP_DIR, f"{job.id}_input.mp4")
    thumb_path = os.path.join(OUTPUT_DIR, f"{job.id}.jpg")

    job.set_status(JobStatus.VALIDANDO, progress=5, stage="Baixando vídeo de origem")
    # Synchronous download of the input video (it is a Base44 storage URL).
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

    # Download the transparent overlay PNG if provided.
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
    # --- Temp files no longer needed: clean up immediately to free RAM ---
    for _tmp in (input_path, overlay_path):
        try:
            if _tmp and os.path.isfile(_tmp):
                os.remove(_tmp)
        except Exception:
            pass
    overlay_path = None
    if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        # Detect signal kills (OOM, manual termination, etc.)
        if proc.returncode < 0:
            import signal
            try:
                sig_name = signal.Signals(-proc.returncode).name
            except ValueError:
                sig_name = f"Sinal {-proc.returncode}"
            hint = ""
            if proc.returncode == -9:
                hint = " — provável falta de memória no servidor. Reduza a resolução de saída."
            elif proc.returncode == -11:
                hint = " — falha de segmentação no FFmpeg."
            job.set_status(JobStatus.ERRO, error=f"FFmpeg foi encerrado ({sig_name}{hint})")
            return

        # Extract only actual error lines from the FFmpeg output.
        # FFmpeg errors typically appear as:
        #   [AVFilterGraph @ 0x...] Error parsing filterchain ...
        #   [Parsed_pad_0 @ 0x...] Failed to configure input pad ...
        #   [libx264 @ 0x...] broken on default
        # They always start with '[' and contain an error keyword.
        # Everything else (banner, library versions, input/stream info,
        # output config, progress lines) is noise and must be excluded.
        err_lines = _extract_ffmpeg_errors(ffmpeg_output)
        if err_lines:
            full_output = "\n".join(err_lines[-5:])[:400]
        else:
            # No recognizable error line → FFmpeg was likely killed silently
            # (OOM, container limit) or the error format is unexpected.
            full_output = (
                f"FFmpeg encerrou com código {proc.returncode} sem mensagem de erro "
                f"identificável. Possível falta de memória — reduza a resolução."
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
    gc.collect()  # release decoder buffers before the next job starts