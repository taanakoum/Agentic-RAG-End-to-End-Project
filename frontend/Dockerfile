# Streamlit frontend image (single-box deployment, deploy/compose.single-box.yml).
# UNVERIFIED: never built on the development machine (no Docker; KNOWN_ISSUES A2).
# Build from the repository root:  docker build -f frontend/Dockerfile -t agentic-rag-frontend .
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --system --gid 10002 ui && useradd --system --uid 10002 --gid ui --home /app ui
WORKDIR /app
# Versions are pinned to the uv.lock versions (frontend/requirements.txt).
COPY frontend/requirements.txt frontend/requirements.txt
RUN pip install -r frontend/requirements.txt
COPY --chown=ui:ui frontend/ frontend/
COPY --chown=ui:ui .streamlit/config.toml .streamlit/config.toml
USER ui
EXPOSE 8501
# app.py puts the repository root on sys.path itself, so no PYTHONPATH is needed.
CMD ["streamlit", "run", "frontend/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
