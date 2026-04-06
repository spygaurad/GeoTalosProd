# =============================================================================
# AwakeForest — Application Dockerfile
# =============================================================================
# Used by: api, celery-worker-*, celery-beat, flower
# Base:    python:3.11-slim (rasterio 1.4+ ships its own GDAL on Linux)
# =============================================================================

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.4 \
    POETRY_HOME=/opt/poetry \
    POETRY_CACHE_DIR=/opt/.cache \
    PIP_DEFAULT_TIMEOUT=180 \
    POETRY_REQUESTS_TIMEOUT=180 \
    PATH="/opt/poetry/bin:${PATH}"

# System deps — curl for poetry installer, build-essential for any src wheels
# libexpat1 is required for geospatial libraries (e.g., pyuwsgi, rasterio)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        libpq-dev \
        libexpat1 \
        && rm -rf /var/lib/apt/lists/*

# Install Poetry via official installer
RUN curl -sSL https://install.python-poetry.org | python3 -

WORKDIR /app

# Copy dependency files first so Docker caches this layer
COPY pyproject.toml poetry.lock* ./

# Install Python dependencies into the system Python (no virtualenv in container)
RUN poetry config virtualenvs.create false \
    && poetry config installer.max-workers 4 \
    && ok=0 \
    && for i in 1 2 3; do \
        if poetry install --no-interaction --no-ansi --no-root --without dev; then \
            ok=1; break; \
        fi; \
        echo "poetry install failed (attempt $i), retrying..."; \
        sleep 20; \
    done \
    && test "$ok" -eq 1

# Copy application source
COPY . .

# Default entrypoint (overridden per-service in docker-compose.yml)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
