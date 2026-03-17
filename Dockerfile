# Symbiote — Docker container for HTTP service mode
#
# Build:  docker build -t symbiote .
# Run:    docker run -p 8000:8000 -v symbiote-data:/data symbiote
#
# Environment variables:
#   SYMBIOTE_DB_PATH    — SQLite path (default: /data/symbiote.db)
#   SYMBIOTE_LLM_PROVIDER — LLM provider (default: mock)
#   ANTHROPIC_API_KEY   — API key for Anthropic provider

FROM python:3.12-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .

# Production stage
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from build stage
COPY --from=base /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=base /usr/local/bin/symbiote /usr/local/bin/symbiote
COPY --from=base /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Copy source (needed for module resolution)
COPY src/ src/

# Data volume for SQLite persistence
VOLUME /data

# Default environment
ENV SYMBIOTE_DB_PATH=/data/symbiote.db
ENV SYMBIOTE_LLM_PROVIDER=mock
ENV PYTHONPATH=/app/src

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "symbiote.api.http:app", "--host", "0.0.0.0", "--port", "8000"]
