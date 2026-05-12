# DevOps handoff — feature-memory.kosmos.connecteam.com

This is the runbook to ship the V2 Feature Memory service onto kosmos.
Same pattern as the existing DeepWiki deployment.

## What I'm asking for

1. **An S3 bucket** for the canonical markdown + caches + audit blobs.
2. **A kosmos service** running this repo's `Dockerfile`, exposed at
   `feature-memory.kosmos.connecteam.com`.
3. **An ingress that stamps `X-Connecteam-User`** on every request with
   the verified Connecteam user identity, the same way DeepWiki gets the
   actor. The server uses this header to attribute writes; it falls back
   to agent self-attribution if the header is missing, so this is
   important for security but not a hard liveness dependency.

## Service shape

| Detail | Value |
| --- | --- |
| Image | Built from `/Users/rafaelsakera/Desktop/connecteam_super_power/feature-memory/Dockerfile` |
| Port | `8080` (configurable via `MCP_PORT`) |
| Healthcheck | `GET /healthz` returns `{"ok": true, "features": <int>, "transport": "streamable-http"}` |
| Liveness probe | `GET /ready` returns `ok` (cheaper) |
| Replicas | **1** for V1. Multi-replica needs S3 EventBridge fan-out, parked for V2.1. |
| CPU/Memory | ~0.25 vCPU / 256 MiB. FAISS index is ~30 MB at 5K features. |
| Restart cost | ~5 s cold start to load index + embeddings caches from S3. |
| Egress | Outbound to `api.openai.com` for query embeddings (search only). |

## Environment variables

Required:

| Variable           | Value (suggested)                        |
| ------------------ | ---------------------------------------- |
| `STORAGE_BACKEND`  | `s3`                                     |
| `S3_BUCKET`        | `connecteam-feature-memory-prod`         |
| `AWS_REGION`       | `us-east-1` (or wherever the bucket lives) |
| `OPENAI_API_KEY`   | (secret) - existing Connecteam OpenAI key |
| `AUTH_HEADER`      | `X-Connecteam-User`                      |

Optional / defaulted (no need to set unless overriding):

| Variable                  | Default               |
| ------------------------- | --------------------- |
| `S3_PREFIX`               | empty (use `prod` for staging segregation) |
| `MCP_TRANSPORT`           | `streamable-http`     |
| `MCP_HOST`                | `0.0.0.0`             |
| `MCP_PORT`                | `8080`                |
| `CACHE_DEBOUNCE_SECONDS`  | `60`                  |

Full reference: [docs/operations/env-vars.md](./env-vars.md).

## S3 bucket setup

- **Versioning:** enabled (recommended — gives us free per-object rollback).
- **Encryption:** SSE-S3 is enough; SSE-KMS if compliance asks.
- **Lifecycle:** none for `features/*` (source of truth). Optional: expire
  `audit/*` after 365 days. Caches are self-healing — no rule needed.

Minimum IAM permissions for the service:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::connecteam-feature-memory-prod/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::connecteam-feature-memory-prod"
    }
  ]
}
```

## Seeding the bucket (one-time)

After the bucket exists and the service has credentials, run the migration
script once from a machine with access:

```bash
OPENAI_API_KEY=sk-... \
feature-memory-migrate \
  --features-dir /path/to/current/features \
  --bucket connecteam-feature-memory-prod \
  --prefix prod \
  --region us-east-1
```

This uploads all 12 current `features/*.md` files plus the `caches/index.json`
and `caches/embeddings.jsonl`. Idempotent on re-run.

## Source code

[github.com/Rafael-Sakera-1995/feature_memory](https://github.com/Rafael-Sakera-1995/feature_memory) -
all source, tests, Dockerfile.

## Smoke test from your laptop, once deployed

```bash
curl https://feature-memory.kosmos.connecteam.com/healthz
# expected: {"ok": true, "features": 12, "transport": "streamable-http"}
```

Then install the plugin (PR pending on the `connecteam/plugins` repo) and
ask the agent in Cursor: *"What features do you remember?"* The agent
should call `list_features` and rattle off the 12 we migrated.
