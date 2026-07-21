# VideoFlow Python Worker

Servidor FastAPI para:

- baixar vídeos com `yt-dlp`;
- acompanhar fila, etapa, velocidade, bytes e progresso;
- renderizar vídeos com FFmpeg;
- encaixar o vídeo em uma janela específica do canvas;
- aplicar uma moldura PNG transparente;
- gerar thumbnails e URLs para preview/download;
- enviar atualizações por webhook.

## Estrutura

```text
videoflow-python-worker/
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── downloader.py
│   ├── jobs.py
│   ├── main.py
│   ├── media_utils.py
│   └── renderer.py
├── .env.example
├── .gitignore
├── Dockerfile
├── railway.json
├── README.md
└── requirements.txt
```

## Subir no Railway

1. Envie esta pasta para um repositório no GitHub.
2. No Railway, crie um novo projeto usando esse repositório.
3. Adicione um **Volume** montado em `/data`.
4. Cadastre as variáveis copiando `.env.example`.
5. Gere um domínio público para o serviço.
6. Teste `https://SEU-DOMINIO/api/health`.

O healthcheck é público para a Railway conseguir verificar o deploy mesmo quando `WORKER_API_KEY` está configurada.

## Autenticação

Nos endpoints protegidos, envie:

```http
Authorization: Bearer SUA_WORKER_API_KEY
```

Por padrão, os arquivos em `/files/...` são públicos para o preview funcionar em elementos `<video>` e `<img>`. Para restringir, use:

```env
FILES_PUBLIC=false
FILE_ACCESS_TOKEN=uma-chave-separada
```

Nesse modo, as URLs devolvidas pelo worker já recebem `?token=...`.

## Criar download

```http
POST /api/downloads
Content-Type: application/json
Authorization: Bearer SUA_WORKER_API_KEY
```

```json
{
  "sourceUrl": "https://www.instagram.com/reel/EXEMPLO/",
  "userId": "usuario_123",
  "assetId": "asset_123",
  "callbackUrl": "https://seu-app.com/api/webhooks/worker"
}
```

Resposta inicial:

```json
{
  "jobId": "job_abc123def456",
  "status": "aguardando",
  "progress": 0
}
```

Consulte o progresso em:

```http
GET /api/jobs/job_abc123def456
Authorization: Bearer SUA_WORKER_API_KEY
```

## Criar renderização

Exemplo para sua moldura de **720 × 1280** com janela transparente em **x=63, y=490, largura=595, altura=439**:

```http
POST /api/renders
Content-Type: application/json
Authorization: Bearer SUA_WORKER_API_KEY
```

```json
{
  "jobId": "render_001",
  "videoUrl": "https://url-publica-do-video.mp4",
  "outputWidth": 720,
  "outputHeight": 1280,
  "videoWindow": {
    "x": 63,
    "y": 490,
    "width": 595,
    "height": 439
  },
  "fitMode": "cover",
  "backgroundColor": "black",
  "template": {
    "overlayUrl": "https://url-publica-da-moldura.png"
  },
  "callbackUrl": "https://seu-app.com/api/webhooks/worker"
}
```

`fitMode` aceita:

- `cover`: preenche toda a janela e corta as sobras;
- `contain`: mostra o vídeo inteiro e adiciona barras quando necessário.

## Webhook

O worker envia o mesmo formato retornado por `GET /api/jobs/{jobId}`. Quando `WEBHOOK_SECRET` estiver definido, também envia:

```http
X-Webhook-Secret: SEU_WEBHOOK_SECRET
```

Estados possíveis:

```text
aguardando
validando
baixando
juntando
convertendo
thumbnail
renderizando
concluido
erro
cancelado
```

## Testar localmente com Docker

```bash
docker build -t videoflow-worker .
docker run --rm -p 8000:8000 \
  -e WORKER_API_KEY=teste123 \
  -e FILES_PUBLIC=true \
  -v videoflow-data:/data \
  videoflow-worker
```

Abra:

```text
http://localhost:8000/docs
http://localhost:8000/api/health
```

## Observações sobre Instagram

Alguns links podem exigir login, cookies ou podem bloquear servidores de nuvem. Nesses casos, exporte cookies de uma conta autorizada para um arquivo `cookies.txt`, monte esse arquivo no serviço e configure `YT_DLP_COOKIES_FILE` com o caminho. Respeite os direitos autorais, as permissões do conteúdo e os termos da plataforma.

## Correções incluídas nesta versão

- adiciona a dependência `httpx`;
- não bloqueia o servidor enquanto FFmpeg ou yt-dlp trabalham;
- salva vídeos e thumbnails exatamente nas pastas servidas pela API;
- não apaga a thumbnail antes do preview;
- mantém arquivos por 24 horas, em vez de somente 120 segundos;
- torna o healthcheck compatível com Railway;
- corrige a renderização sem overlay para manter o canvas completo;
- valida vídeo, PNG, dimensões e saída final;
- acompanha o progresso real do FFmpeg;
- permite cancelamento durante renderização;
- impede exposição de caminhos internos no JSON da API;
- evita path traversal na rota de arquivos.
