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
# libgl1 + libglib2.0-0: opencv-python (pulled by rapidocr-onnxruntime, 📷 Tips screenshots) loads them at import
RUN apt-get update && apt-get install -y --no-install-recommends tzdata libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
# 📈 the candle model worker: torch from the CPU index (aarch64 / x86_64 CPU wheels; never the CUDA build), then the rest
ARG TORCH_VERSION=2.15.0
RUN pip install "torch==${TORCH_VERSION}" --index-url https://download.pytorch.org/whl/cpu
RUN pip install -r requirements.txt
# the Kronos-mini weights + tokenizer (~31 MB) baked in, so the container starts offline; the worker copies them to
# data/ai/kronos at first run and keeps them there
ENV BP_KRONOS_BAKED=/app/models/kronos HF_HUB_DISABLE_TELEMETRY=1
RUN python -c "from huggingface_hub import snapshot_download as d; [d(r, local_dir='/app/models/kronos/' + r.split('/')[-1]) for r in ('NeoQuasar/Kronos-mini', 'NeoQuasar/Kronos-Tokenizer-2k')]"
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
