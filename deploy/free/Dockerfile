# Free-tier backend image (Render free web service, 512 MB). See docs/FREE_DEPLOYMENT.md.
# One process: the API runs the background tasks itself (TASK_RUNNER=inline). No torch, Docling
# or OCR (deploy/free/requirements.txt). Build context: the repository root.
FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend \
    LOG_JSON=true \
    MODEL_CACHE_DIR=/app/models \
    HF_HOME=/app/models/hf
RUN groupadd --system --gid 10001 app && groupadd --gid 1000 render-secrets \
    && useradd --system --uid 10001 --gid app --groups render-secrets --home /app app
WORKDIR /app
COPY deploy/free/requirements.txt deploy/free/requirements.txt
RUN pip install -r deploy/free/requirements.txt
# Bake the small BM25 vocabulary into the image so a cold start needs no model download.
RUN mkdir -p /app/models && python -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding('Qdrant/bm25', cache_dir='/app/models')" \
    && chown -R app:app /app/models
COPY --chown=app:app backend/ /app/backend/
WORKDIR /app/backend
USER app
EXPOSE 10000
# Render's free plan has no pre-deploy step, so migrations run at start (idempotent; one instance).
CMD ["sh", "-c", "alembic -c alembic.ini upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --workers 1"]
