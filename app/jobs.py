import asyncio
import gc
import inspect
import os
import time
import uuid
from enum import Enum
from typing import Any, Awaitable, Callable

import httpx

from app.config import MAX_CONCURRENT, OUTPUT_TTL_SECONDS, WEBHOOK_SECRET


class JobType(str, Enum):
    DOWNLOAD = "download"
    RENDER = "render"


class JobStatus(str, Enum):
    AGUARDANDO = "aguardando"
    VALIDANDO = "validando"
    BAIXANDO = "baixando"
    JUNTANDO = "juntando"
    CONVERTENDO = "convertendo"
    THUMBNAIL = "thumbnail"
    ENVIANDO = "enviando"
    RENDERIZANDO = "renderizando"
    CONCLUIDO = "concluido"
    ERRO = "erro"
    CANCELADO = "cancelado"


STAGE_LABELS = {
    JobStatus.AGUARDANDO: "Aguardando na fila",
    JobStatus.VALIDANDO: "Validando",
    JobStatus.BAIXANDO: "Baixando vídeo",
    JobStatus.JUNTANDO: "Juntando áudio e vídeo",
    JobStatus.CONVERTENDO: "Convertendo para MP4",
    JobStatus.THUMBNAIL: "Gerando thumbnail",
    JobStatus.ENVIANDO: "Enviando",
    JobStatus.RENDERIZANDO: "Renderizando com FFmpeg",
    JobStatus.CONCLUIDO: "Concluído",
    JobStatus.ERRO: "Erro",
    JobStatus.CANCELADO: "Cancelado",
}


class Job:
    def __init__(self, job_id: str, job_type: JobType, params: dict[str, Any]):
        self.id = job_id
        self.type = job_type
        self.params = params
        self.status = JobStatus.AGUARDANDO
        self.progress = 0
        self.stage = STAGE_LABELS[JobStatus.AGUARDANDO]
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.speed = 0
        self.eta = 0
        self.result: dict[str, Any] = {}
        self.error = ""
        self.created_at = time.time()
        self.updated_at = time.time()
        self.cancelled = False
        self._task: asyncio.Task | None = None
        self.diagnostics: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        public_result = {
            key: value
            for key, value in self.result.items()
            if not key.startswith("__")
        }
        return {
            "jobId": self.id,
            "type": self.type.value,
            "status": self.status.value,
            "progress": max(0, min(100, int(self.progress))),
            "stage": self.stage or STAGE_LABELS.get(self.status, ""),
            "downloadedBytes": self.downloaded_bytes,
            "totalBytes": self.total_bytes,
            "speed": self.speed,
            "eta": self.eta,
            "result": public_result,
            "error": self.error,
            "diagnostics": self.diagnostics,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    def set_status(
        self,
        status: JobStatus,
        progress: int | None = None,
        stage: str | None = None,
        error: str | None = None,
        **result_extra: Any,
    ) -> None:
        self.status = status
        if progress is not None:
            self.progress = max(0, min(100, int(progress)))
        self.stage = stage or STAGE_LABELS.get(status, self.stage)
        self.updated_at = time.time()
        if error is not None:
            self.error = str(error)
        if result_extra:
            self.result.update(result_extra)


_store: dict[str, Job] = {}
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


def get_job(job_id: str) -> Job | None:
    return _store.get(job_id)


def list_jobs() -> list[Job]:
    return sorted(_store.values(), key=lambda job: job.created_at, reverse=True)


def create_job(
    job_type: JobType,
    params: dict[str, Any],
    job_id: str | None = None,
) -> Job:
    resolved_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
    if resolved_id in _store:
        raise ValueError(f"Já existe um trabalho com o ID {resolved_id}")
    job = Job(resolved_id, job_type, params)
    _store[resolved_id] = job
    return job


def _safe_remove(path: str | os.PathLike[str] | None) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


async def call_webhook(job: Job) -> None:
    callback_url = job.params.get("callbackUrl")
    if not callback_url:
        return

    payload = {
        **job.to_dict(),
        "jobType": job.type.value,
        "assetId": job.params.get("assetId"),
        "renderJobId": job.params.get("jobId"),
    }
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SECRET

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.post(callback_url, json=payload, headers=headers)
            job.diagnostics["lastWebhookStatus"] = response.status_code
    except Exception as exc:  # webhook failure must not fail the media job
        job.diagnostics["lastWebhookError"] = str(exc)


async def _delayed_output_cleanup(job: Job) -> None:
    if OUTPUT_TTL_SECONDS <= 0:
        return
    await asyncio.sleep(OUTPUT_TTL_SECONDS)
    _safe_remove(job.result.get("__filePath"))
    _safe_remove(job.result.get("__thumbPath"))


async def enqueue(
    job: Job,
    runner: Callable[[Job], Awaitable[None] | None],
) -> None:
    async def wrapper() -> None:
        async with _semaphore:
            if job.cancelled:
                job.set_status(JobStatus.CANCELADO)
                return
            try:
                outcome = runner(job)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:
                if job.status not in (
                    JobStatus.CONCLUIDO,
                    JobStatus.CANCELADO,
                    JobStatus.ERRO,
                ):
                    job.set_status(JobStatus.ERRO, error=str(exc))
                await call_webhook(job)
            finally:
                gc.collect()
                if job.status == JobStatus.CONCLUIDO:
                    asyncio.create_task(_delayed_output_cleanup(job))

    job._task = asyncio.create_task(wrapper())
