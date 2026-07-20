# VideoFlow Python Worker

Worker FastAPI para conteúdos próprios ou autorizados. Faz download com `yt-dlp`, gera thumbnails e aplica um overlay PNG transparente com FFmpeg.

## Rotas

- `GET /api/health`
- `POST /api/downloads`
- `POST /api/renders`
- `GET /api/jobs/{jobId}`
- `POST /api/jobs/{jobId}/cancel`
- `POST /api/jobs/{jobId}/retry`
- `GET /files/{category}/{filename}`

As rotas de jobs exigem `Authorization: Bearer <WORKER_API_KEY>`.

## Publicação rápida no Railway

1. Crie um repositório no GitHub e envie todos os arquivos desta pasta.
2. No Railway, crie `New Project` e escolha `Deploy from GitHub repo`.
3. O Railway detectará o `Dockerfile` automaticamente.
4. Em **Variables**, adicione:
   - `WORKER_API_KEY`: uma senha longa e aleatória.
   - `WEBHOOK_SECRET`: outra senha longa e diferente.
   - `DATA_DIR=/data`
   - `MAX_WORKERS=2`
5. Em **Networking**, gere um domínio público.
6. Em **Deploy > Healthcheck Path**, confirme `/api/health`.
7. Para preservar vídeos após reinícios, adicione um Volume montado em `/data`.
8. Abra `https://SEU-DOMINIO/api/health` e confirme `"ok": true`.

## Valores para a Base44

- `PYTHON_WORKER_API_URL=https://SEU-DOMINIO`
- `PYTHON_WORKER_API_KEY` = mesmo valor de `WORKER_API_KEY` no Railway.
- `PYTHON_WORKER_WEBHOOK_SECRET` = mesmo valor de `WEBHOOK_SECRET` no Railway.

## Exemplo: criar download

```bash
curl -X POST "https://SEU-DOMINIO/api/downloads" \
  -H "Authorization: Bearer SUA_CHAVE" \
  -H "Content-Type: application/json" \
  -d '{
    "userId":"usuario-1",
    "assetId":"asset-1",
    "sourceUrl":"https://exemplo.com/video",
    "callbackUrl":"https://seu-app/webhook"
  }'
```

## Exemplo: renderizar

O `overlayUrl` deve apontar para um PNG transparente no tamanho total do canvas.

```bash
curl -X POST "https://SEU-DOMINIO/api/renders" \
  -H "Authorization: Bearer SUA_CHAVE" \
  -H "Content-Type: application/json" \
  -d '{
    "userId":"usuario-1",
    "videoUrl":"https://SEU-DOMINIO/files/downloads/video.mp4",
    "outputWidth":720,
    "outputHeight":1280,
    "videoWindow":{"x":63,"y":490,"width":595,"height":439},
    "fitMode":"cover",
    "template":{"overlayUrl":"https://exemplo.com/moldura.png","layers":[]},
    "callbackUrl":"https://seu-app/webhook"
  }'
```

## Observações para produção

Este pacote é um MVP funcional. A fila usa threads no próprio processo. Para volume alto, separe API e workers e use Redis/Celery ou uma fila gerenciada. Não exponha as chaves no frontend. Os arquivos são servidos pelo próprio worker; para escalar, migre-os para S3/R2/Supabase Storage.
