# Environment variable contract

The Feature Memory V2 service is configured entirely through environment
variables (the `Dockerfile` `CMD` takes no positional args). Anything not
listed here is hard-coded.

## Required (production, S3 backend)

| Variable             | Example                              | Purpose                                                              |
| -------------------- | ------------------------------------ | -------------------------------------------------------------------- |
| `STORAGE_BACKEND`    | `s3`                                 | Selects the storage class. `local` falls back to a mounted volume.   |
| `S3_BUCKET`          | `connecteam-feature-memory-prod`     | Bucket holding `features/*.md`, `caches/*`, `audit/*`.               |
| `AWS_REGION`         | `us-east-1`                          | Bucket region.                                                       |
| `OPENAI_API_KEY`     | `sk-...`                             | Powers `search_features`. Service still boots if missing; search degrades to empty results and falls back to `list_features`. |

## Optional

| Variable                  | Default                  | Purpose                                                                 |
| ------------------------- | ------------------------ | ----------------------------------------------------------------------- |
| `S3_PREFIX`               | (empty)                  | Key prefix inside the bucket, e.g. `prod` or `staging`.                 |
| `OPENAI_MODEL`            | `text-embedding-3-small` | Embedding model.                                                        |
| `EMBEDDING_DIM`           | `1536`                   | Must match the model's native dim.                                      |
| `MCP_TRANSPORT`           | `streamable-http`        | Set to `stdio` for the V1 single-user mode.                             |
| `MCP_HOST`                | `0.0.0.0`                | Bind address.                                                           |
| `MCP_PORT`                | `8080`                   | Bind port. Match the kosmos ingress.                                    |
| `AUTH_HEADER`             | (unset)                  | If set, server overrides `last_update.author` with the value of this header on every write. Recommended: `X-Connecteam-User`. |
| `CACHE_DEBOUNCE_SECONDS`  | `60`                     | Coalesce window for cache rewrites to S3.                               |
| `FEATURES_DIR`            | (unset)                  | Required for `STORAGE_BACKEND=local`. Path inside the container.        |

## AWS credentials

Standard boto3 credential chain. On kosmos, prefer IAM roles for service
accounts (IRSA) over baked-in keys. Minimum bucket policy:

- `s3:GetObject` / `s3:PutObject` / `s3:DeleteObject` on `arn:.../bucket/*`
- `s3:ListBucket` on the bucket itself

Versioning enabled on the bucket is **strongly recommended** so accidental
or buggy writes can be rolled back at the object level. Audit blobs are
already append-only by key, so versioning is icing on that side.

## Healthchecks

- `GET /healthz` returns `{"ok": true, "features": <int>, "transport": "streamable-http"}`. Use this for kosmos liveness + readiness.
- `GET /ready` returns `ok` plaintext. Cheaper liveness probe if needed.

## Operational notes

- Single replica is the V1 design contract. Spinning up a second replica is
  safe for reads but introduces a window where the in-memory FAISS in
  replica B is stale until the next debounce-flush from replica A. We will
  add S3 EventBridge -> SNS fanout in V2.1 if this ever becomes a problem.
- Restart cost: ~5s cold start to load the index + embeddings caches. The
  service is read-mostly so a rolling restart of the single replica is
  acceptable as long as it happens off-hours.
- Search-side OpenAI dependency: if the OpenAI API is unavailable, the only
  affected tool is `search_features` (it returns `[]`). All reads/writes
  continue normally.
