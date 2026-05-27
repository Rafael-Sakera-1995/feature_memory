# Kosmos image for the Feature Memory MCP service (V3 = S3 Vectors + Bedrock).
#
# Why this is so much smaller than pymobi's kosmos Dockerfile: the V3 service is a
# stateless Python MCP that talks to AWS. No FAISS, no PDF rendering, no SMS gateway,
# no fonts. Just Python + boto3 + the project code.

FROM connecteampublic.azurecr.io/amd64/python:3.11.9-bullseye

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ca-certificates: boto3 TLS to AWS. curl: kosmos liveness probes sometimes use it.
RUN apt-get update --quiet=2 \
    && apt-get install --quiet=2 --yes --no-install-recommends ca-certificates curl \
    && apt-get clean --quiet=2 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONPATH=/app
WORKDIR /app

# Install deps first for layer caching - pyproject.toml + src/ rarely change together.
COPY pyproject.toml README.md /app/
COPY src/ /app/src/
RUN pip install --quiet --no-color uv \
    && uv pip install --quiet --no-color --system .

# Copy the rest of the repo (features/, scripts/, etc.) - migrations may need them.
COPY . /app/

# Streamable-http transport binds to 8080 by default. kosmos ingress maps 443 -> here.
EXPOSE 8080

# Liveness via /healthz - one S3 LIST, returns the count of active features. Kosmos
# probes `/ready` for the cheaper "process is up" check. Both routes are defined in
# src/feature_memory/server.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${MCP_PORT:-8080}/healthz" >/dev/null || exit 1

ENV STORAGE_BACKEND=s3 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080

CMD ["feature-memory-mcp"]
