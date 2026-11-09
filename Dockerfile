FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS python-dependencies
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY dividend/ ./dividend/
COPY data/ ./data/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAYMENTS_MODE=fake \
    PAYPAL_ENV=sandbox \
    ENRICH_MODE=fixtures \
    LLM_LIVE_ENABLED=false \
    LLM_KILL_SWITCH=true \
    DEMO_SEED_ON_BOOT=true \
    PORT=8000
WORKDIR /app
RUN groupadd --system dividend && useradd --system --gid dividend --home-dir /app dividend \
    && mkdir -p /data && chown dividend:dividend /data
COPY --from=python-dependencies /install/ /usr/local/
COPY --chown=dividend:dividend dividend/ ./dividend/
COPY --chown=dividend:dividend data/ ./data/
COPY --from=frontend-build --chown=dividend:dividend /build/frontend/dist ./frontend/dist
USER dividend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8000\")}/health', timeout=3)" || exit 1
CMD ["sh", "-c", "uvicorn dividend.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
