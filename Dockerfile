# Banana Patterns in one container: the React dashboard is built in a Node stage, then served by the FastAPI
# backend (with the scheduler, Telegram bot and alerts) from a slim Python image.
#   docker compose up -d --build      (or ./docker.sh up)

# Base images (official Docker Hub images; override only to use a mirror of them)
ARG NODE_IMAGE=node:20-slim
ARG PYTHON_IMAGE=python:3.12-slim

# --- 1. dashboard -------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- 2. app -------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata BP_HOST=0.0.0.0 MPLCONFIGDIR=/tmp/matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY backend/ backend/
COPY config/ config/
COPY run.py .
COPY --from=web /web/build frontend/build
RUN useradd --uid 1000 --create-home app && mkdir -p data logs && chown -R app:app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["python", "run.py"]
