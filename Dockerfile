# Feature Memory V2 service image. Targets kosmos.connecteam.com.
#
# Single-stage build is fine here: dependencies are small (~120MB including
# faiss-cpu wheels), and the image is run as a long-lived service so a few
# seconds of startup is not a concern.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System dependencies. faiss-cpu wheels ship a built libfaiss but rely on
# libgomp (OpenMP) at runtime which is not in python:slim by default.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --upgrade pip \
    && pip install .

# Healthcheck targets `/healthz` exposed by the HTTP transport.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        sys.exit(0 if urllib.request.urlopen('http://localhost:${MCP_PORT:-8080}/healthz', timeout=3).status == 200 else 1)"

EXPOSE 8080

# Default to S3 + HTTP. The kosmos deployment overrides envs via the platform's
# secret manager. Local-dev users can run with STORAGE_BACKEND=local + --features-dir.
ENV STORAGE_BACKEND=s3 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080

CMD ["feature-memory-mcp"]
