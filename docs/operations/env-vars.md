# Environment variable contract

The Feature Memory V3 service is configured entirely through environment
variables (the `Dockerfile` `CMD` takes no positional args). Anything not
listed here is hard-coded.

## Required (production)

| Variable              | Example                                        | Purpose                                                              |
| --------------------- | ---------------------------------------------- | -------------------------------------------------------------------- |
| `STORAGE_BACKEND`     | `s3`                                           | Selects the AWS backend path (V3).                                   |
| `S3_BUCKET`           | `prod.connecteam.feature-memory`               | Markdown bucket. Holds `features/*.md`, `caches/index.json`, `audit/*`. |
| `AWS_REGION`          | `eu-central-1`                                 | Default region for all AWS clients.                                  |
| `S3_VECTOR_BUCKET`    | `prod.connecteam.feature-memory-vectors`       | Amazon S3 Vectors bucket. Required for `search_features` to work.    |

## Optional

| Variable                  | Default                              | Purpose                                                                 |
| ------------------------- | ------------------------------------ | ----------------------------------------------------------------------- |
| `S3_PREFIX`               | (empty)                              | Key prefix inside the markdown bucket, e.g. `prod` or `staging`.        |
| `S3_VECTOR_INDEX_NAME`    | `features`                           | Index name inside the vector bucket.                                    |
| `S3_VECTOR_REGION`        | falls back to `AWS_REGION`           | Override if the vector bucket is in a different region.                 |
| `BEDROCK_REGION`          | falls back to `AWS_REGION`           | Override if Titan v2 isn't enabled in the markdown bucket's region.     |
| `BEDROCK_MODEL_ID`        | `amazon.titan-embed-text-v2:0`       | Embedding model. Other valid choices: `amazon.titan-embed-text-v1`, `cohere.embed-english-v3`. Must match the index dimension. |
| `EMBEDDING_DIM`           | `1024`                               | Vector dimension. Titan v2 supports 256/512/1024.                       |
| `MCP_TRANSPORT`           | `streamable-http`                    | Set to `stdio` for the V1 single-user mode.                             |
| `MCP_HOST`                | `0.0.0.0`                            | Bind address.                                                           |
| `MCP_PORT`                | `8080`                               | Bind port. Match the kosmos ingress.                                    |
| `AUTH_HEADER`             | (unset)                              | If set, server overrides `last_update.author` with this header. Recommended: `X-Connecteam-User`. |
| `FEATURES_DIR`            | (unset)                              | Required for `STORAGE_BACKEND=local`. Path inside the container.        |
| `S3_ENDPOINT_URL`         | (unset)                              | Override for the markdown S3 endpoint (LocalStack). Does NOT affect Bedrock or S3 Vectors. |

## AWS credentials and IAM

Standard boto3 credential chain. On kosmos, prefer IAM roles for service
accounts (IRSA) over baked-in keys. The pod role needs:

**Markdown bucket** (`S3_BUCKET`):
- `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `arn:aws:s3:::{bucket}/*`
- `s3:ListBucket` on `arn:aws:s3:::{bucket}`
- Conditional writes via `s3:PutObject` with `If-Match` require no extra
  permission - it's a standard PUT.

**Vector bucket** (`S3_VECTOR_BUCKET`):
- `s3vectors:PutVectors`, `s3vectors:DeleteVectors`, `s3vectors:QueryVectors`,
  `s3vectors:GetVectors` on the vector bucket ARN.
- `s3vectors:GetIndex` for startup health checks (optional but useful).

**Bedrock**:
- `bedrock:InvokeModel` on the model ARN, e.g.
  `arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0`.
- The Bedrock model itself must be **enabled** in the account via the AWS
  Console (Bedrock -> Model access). This is one-time, per account, per region.

Versioning on the markdown bucket is **strongly recommended** so accidental
writes can be rolled back. The vector bucket does not support versioning -
re-running the migration is the recovery path if a vector goes bad.

## Healthchecks

- `GET /healthz` -> `{"ok": true, "features": <int>, "transport": "streamable-http"}`. Use for kosmos liveness + readiness.
- `GET /ready` -> `ok` plaintext. Cheaper liveness probe if needed.

## Operational notes

- **Stateless server.** Unlike V2, V3 keeps no in-memory vector index. Every
  pod reads vectors from S3 Vectors on demand. Multi-replica is safe without
  coherence machinery; spin up as many replicas as you need.
- **Restart cost**: ~1s cold start. Only reads `caches/index.json` (small
  JSON) at boot. No re-embedding, no FAISS load.
- **Search-side AWS dependency**: if S3 Vectors or Bedrock is unavailable,
  `search_features` returns `[]`. All other tools continue normally.
- **Per-search latency**: ~150-300ms (1 Bedrock invoke + 1 S3 Vectors query).
  Invisible inside an agent flow.
