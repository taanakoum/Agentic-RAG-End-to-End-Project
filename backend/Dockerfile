# UNVERIFIED: not built yet (Docker is not installed on the dev machine; see KNOWN_ISSUES KI-003).
# Build from the repository root:  docker build -f backend/Dockerfile -t agentic-rag-backend .
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim AS runtime
# Shared libraries the ONNX / PyTorch / OpenCV wheels expect on a slim image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend \
    PATH=/opt/venv/bin:$PATH \
    LOG_JSON=true
RUN groupadd --system --gid 10001 app && groupadd --gid 1000 render-secrets \
    && useradd --system --uid 10001 --gid app --groups render-secrets --home /app app \
    && mkdir -p /data/objects /data/outbox /data/models /data/beat && chown -R app:app /data
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app/backend
COPY --chown=app:app backend/ /app/backend/
USER app
EXPOSE 8000
# Default command is the API; the worker overrides it in docker-compose.yml.
# WEB_CONCURRENCY overrides the single-process default. Each process
# has its own event loop, DB pool and warm sandbox, so throughput scales with processes.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --workers ${WEB_CONCURRENCY:-1}"]
