# Symbiote — Docker container for HTTP service mode
#
# Build:  docker build -t symbiote .
# Run:    docker run -p 8008:8008 -v symbiote-data:/data symbiote
#
# Environment variables:
#   SYMBIOTE_DB_PATH      — SQLite path (default: /data/symbiote.db)
#   SYMBIOTE_LLM_PROVIDER — LLM provider (default: mock)
#   SYMGATEWAY_API_KEY    — API key for SymGateway provider
#   SYMGATEWAY_BASE_URL   — SymGateway base URL

ARG PYTHON_IMAGE=python:3.12-slim@sha256:9e01bf1ae5db7649a236da7be1e94ffbbbdd7a93f867dd0d8d5720d9e1f89fab

FROM ${PYTHON_IMAGE} AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml README.md ./
COPY requirements/ requirements/
COPY src/ src/
RUN python -m pip install --no-cache-dir -r requirements/bootstrap-linux.lock \
    && python -m pip install --no-cache-dir --no-build-isolation -r requirements/runtime-linux.lock \
    && python -m pip install --no-cache-dir --no-build-isolation --no-deps .

# CI stage. The runner host does not need a matching Python installation.
FROM base AS quality

RUN python -m pip install --no-cache-dir --no-build-isolation -r requirements/ci-linux.lock \
    && python -m pip check
COPY ruff.toml ./
COPY tests/ tests/
# Guides whose code blocks the tests check (tests/unit/test_docs_references.py).
COPY QUICKSTART.md ./
COPY docs/HOST_INTEGRATION.md docs/
# Coverage gate: fails the build below 85% (pyproject [tool.coverage.report]).
RUN python -m pytest -q --cov=src/symbiote --cov-fail-under=85 \
    && ruff check src tests \
    && mypy src/symbiote \
    && python -c 'import json; from importlib.metadata import distribution, version; forge=distribution("forge-llm"); provenance=json.loads(forge.read_text("direct_url.json")); assert version("forge-llm") == "0.7.19"; assert provenance["vcs_info"]["requested_revision"] == "ba0c0ca80a46f0421e157d7c932d406036ccac8d"; assert provenance["vcs_info"]["commit_id"] == "ba0c0ca80a46f0421e157d7c932d406036ccac8d"'

# Production stage
FROM ${PYTHON_IMAGE}

WORKDIR /app

# curl is needed for the docker-compose healthcheck used in staging.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

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

# Build-time commit, propagated to GIT_COMMIT env var so /health can report
# the exact SHA in containers where the .git directory isn't available.
# CI injects this via `docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD)`.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
LABEL org.opencontainers.image.revision=${GIT_COMMIT}
LABEL org.opencontainers.image.source="https://github.com/symlabs-ai/symbiote"

EXPOSE 8008

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/health')" || exit 1

CMD ["uvicorn", "symbiote.api.http:app", "--host", "0.0.0.0", "--port", "8008"]
