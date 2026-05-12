# Local end-to-end smoke test with LocalStack

> **HISTORICAL.** This runbook was written for V2 (FAISS in-memory, OpenAI
> embeddings, single S3 bucket). V3 uses Amazon S3 Vectors + Bedrock, neither
> of which LocalStack community edition implements. The markdown-bucket parts
> below still work for testing the `Storage` layer in isolation, but you
> **cannot** exercise `search_features` end-to-end against LocalStack.
>
> For full local dev on V3, see [docs/operations/env-vars.md](./env-vars.md)
> and point at a real shared AWS dev vector bucket. The migration script's
> `--create-vector-bucket --create-vector-index` flags make this a one-command
> bootstrap.

Lets you exercise the markdown storage layer (`S3Storage` + migration of `.md`
files + `streamable-http` transport) without real AWS for that path. Vector
search will be disabled in this configuration.

## What you need

- Docker (already on your machine; we use `localstack/localstack`)
- The feature-memory venv with deps installed (`pip install -e ".[dev]"`)

## Step 1 — Start LocalStack

```bash
docker run --rm -d \
  --name fm-localstack \
  -p 4566:4566 \
  -e SERVICES=s3 \
  localstack/localstack:3.8
```

Pin to `3.8` (community). The `:latest` tag on LocalStack now gates S3 behind
a paid license and refuses to start without `LOCALSTACK_AUTH_TOKEN`. 3.x is
the last fully-free community line and is plenty for S3 smoke tests.

Healthcheck:

```bash
curl -s http://localhost:4566/_localstack/health | grep '"s3"'
# expect: "s3": "available" (or "running")
```

## Step 2 — Point `.env` at LocalStack

In `feature-memory/.env`:

```
STORAGE_BACKEND=s3
S3_BUCKET=feature-memory-local
S3_ENDPOINT_URL=http://localhost:4566
AWS_REGION=eu-central-1
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
```

LocalStack ignores the credential values but boto3 refuses to start without
them — `test`/`test` is the conventional placeholder.

## Step 3 — Migrate the features

```bash
source .venv/bin/activate
feature-memory-migrate \
  --features-dir features/ \
  --bucket feature-memory-local \
  --endpoint-url http://localhost:4566 \
  --region eu-central-1 \
  --create-bucket
```

`--create-bucket` provisions the bucket in LocalStack (idempotent — re-running
is safe). Expect: `migration complete: {'uploaded': 12, 'skipped': 0, 'errors': 0}`.

Verify the upload:

```bash
docker exec fm-localstack awslocal s3 ls s3://feature-memory-local/features/
# expect: 12 .md files
docker exec fm-localstack awslocal s3 ls s3://feature-memory-local/caches/
# expect: index.json
```

## Step 4 — Start the MCP server in HTTP mode

```bash
MCP_TRANSPORT=streamable-http MCP_PORT=8080 feature-memory-mcp
```

The `.env` provides everything else. Smoke-test the health endpoint:

```bash
curl -s http://localhost:8080/healthz | python -m json.tool
# expect: {"ok": true, "features": 12, "transport": "streamable-http"}
```

## Step 5 — Hit the MCP from a client (optional)

Point Cursor's MCP config at `http://localhost:8080/mcp` to test the live
agent flow against your LocalStack-backed service. Roll back to the real
production URL when done.

## Cleanup

```bash
docker stop fm-localstack
```

LocalStack data lives only in the container — stopping wipes the bucket. To
persist across restarts, mount a volume (`-v $PWD/.localstack:/var/lib/localstack`).

## When NOT to use this

LocalStack's `If-Match` ETag behavior is **not** identical to real S3. The
migration's idempotency check (sha256 == ETag) works on LocalStack for the
upload path but won't catch real-S3-specific quirks. Don't rely on this for
testing the conditional-write retry loop — that path is covered by `moto`
unit tests in `tests/test_storage.py`.
