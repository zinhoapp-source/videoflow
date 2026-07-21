import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

from app.config import FILE_ACCESS_TOKEN, FILES_PUBLIC


def ffprobe(path: str | os.PathLike[str]) -> dict:
    try:
        output = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            stderr=subprocess.STDOUT,
        )
        data = json.loads(output)
        streams = data.get("streams", [])
        video = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            {},
        )
        audio = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            {},
        )
        file_format = data.get("format", {})
        duration = float(file_format.get("duration") or video.get("duration") or 0)
        size = int(file_format.get("size") or 0) or os.path.getsize(path)
        return {
            "duration": round(duration, 2),
            "width": int(video.get("width", 0) or 0),
            "height": int(video.get("height", 0) or 0),
            "size": size,
            "has_audio": bool(audio),
            "codec": video.get("codec_name", ""),
        }
    except Exception:
        return {}


def generate_thumbnail(
    src: str | os.PathLike[str],
    out_path: str | os.PathLike[str],
    at_sec: float = 1.0,
) -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            str(at_sec),
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(out_path),
        ],
        check=False,
        capture_output=True,
    )
    return str(out_path) if os.path.exists(out_path) else ""


def validate_video_file(path: str | os.PathLike[str]) -> dict:
    if not os.path.exists(path):
        return {"valid": False, "reason": "Arquivo não encontrado"}
    if os.path.getsize(path) == 0:
        return {"valid": False, "reason": "Arquivo vazio (0 bytes)"}

    try:
        output = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            stderr=subprocess.STDOUT,
        )
        data = json.loads(output)
        streams = data.get("streams", [])
        video = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            None,
        )
        if not video:
            return {"valid": False, "reason": "Nenhum stream de vídeo encontrado"}

        file_format = data.get("format", {})
        duration = float(file_format.get("duration") or video.get("duration") or 0)
        return {
            "valid": True,
            "width": int(video.get("width", 0) or 0),
            "height": int(video.get("height", 0) or 0),
            "duration": round(duration, 2),
            "has_audio": any(
                stream.get("codec_type") == "audio" for stream in streams
            ),
            "codec": video.get("codec_name", ""),
            "streams": len(streams),
        }
    except Exception as exc:
        return {"valid": False, "reason": f"Erro ao ler arquivo: {exc}"}


def validate_png_file(path: str | os.PathLike[str]) -> dict:
    if not os.path.exists(path):
        return {"valid": False, "reason": "Overlay não encontrado"}
    if os.path.getsize(path) == 0:
        return {"valid": False, "reason": "Overlay vazio (0 bytes)"}
    try:
        with open(path, "rb") as file:
            signature = file.read(8)
        if signature != b"\x89PNG\r\n\x1a\n":
            return {"valid": False, "reason": "Não é um arquivo PNG válido"}
    except Exception as exc:
        return {"valid": False, "reason": f"Erro ao ler overlay: {exc}"}
    return {"valid": True}


def build_file_url(base_url: str, category: str, filename: str) -> str:
    url = f"{base_url.rstrip('/')}/files/{category}/{quote(filename)}"
    if not FILES_PUBLIC and FILE_ACCESS_TOKEN:
        return f"{url}?token={quote(FILE_ACCESS_TOKEN)}"
    return url
