import os
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


WORKER_API_KEY = os.getenv(
    "PYTHON_WORKER_API_KEY",
    os.getenv("WORKER_API_KEY", ""),
).strip()
WEBHOOK_SECRET = os.getenv(
    "PYTHON_WORKER_WEBHOOK_SECRET",
    os.getenv("WEBHOOK_SECRET", ""),
).strip()

DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(DATA_DIR / "downloads"))).resolve()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(DATA_DIR / "outputs"))).resolve()
THUMB_DIR = Path(os.getenv("THUMB_DIR", str(DATA_DIR / "thumbnails"))).resolve()
TEMP_DIR = Path(os.getenv("TEMP_DIR", str(DATA_DIR / "temp"))).resolve()

MAX_CONCURRENT = max(
    1,
    int(os.getenv("MAX_CONCURRENT_JOBS", os.getenv("MAX_WORKERS", "2"))),
)
PORT = int(os.getenv("PORT", "8000"))
OUTPUT_TTL_SECONDS = max(0, int(os.getenv("OUTPUT_TTL_SECONDS", "86400")))
FILES_PUBLIC = _as_bool(os.getenv("FILES_PUBLIC"), default=True)
FILE_ACCESS_TOKEN = os.getenv("FILE_ACCESS_TOKEN", "").strip()
YT_DLP_COOKIES_FILE = os.getenv("YT_DLP_COOKIES_FILE", "").strip()

for directory in (DATA_DIR, DOWNLOAD_DIR, OUTPUT_DIR, THUMB_DIR, TEMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)
