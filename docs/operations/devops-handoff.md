# DevOps handoff - feature-memory.kosmos.connecteam.com

This is the runbook to ship the V3 Feature Memory service onto kosmos.
Same pattern as the existing DeepWiki deployment.

## What I'm asking for

1. **A markdown S3 bucket** for canonical `.md` files + audit blobs.
2. **An Amazon S3 Vectors bucket** for the semantic search index.
3. **Bedrock model access** for `amazon.titan-embed-text-v2:0` in the chosen
   region (one-time Console toggle per AWS account).
4. **An IAM role for the pod** with permissions to all three above.
5. **A kosmos service** running this repo's `Dockerfile`, exposed at
   `feature-memory.kosmos.connecteam.com`.
6. **An ingress that stamps `X-Connecteam-User`** on every request, the same
   way DeepWiki gets the actor. The server uses this header to attribute
   writes server-side so the agent cannot self-attribute.

## Service shape

| Detail | Value |
| --- | --- |
| Image | Built from `Dockerfile` at the repo root |
| Port | `8080` (configurable via `MCP_PORT`) |
| Healthcheck | `GET /healthz` returns `{"ok": true, "features": <int>, "transport": "streamable-http"}` |
| Liveness probe | `GET /ready` returns `ok` (cheaper) |
| Replicas | Any. V3 is stateless w.r.t. search; horizontal scaling is free. |
| CPU/Memory | ~0.1 vCPU / 128 MiB. No FAISS, no in-process vector cache. |
| Restart cost | ~1s cold start. Only reads `caches/index.json`. |
| Egress | AWS APIs only: S3, S3 Vectors, Bedrock. No third-party endpoints. |

## Environment variables

Required:

| Variable             | Value (suggested)                                  |
| -------------------- | -------------------------------------------------- |
| `STORAGE_BACKEND`    | `s3`                                               |
| `S3_BUCKET`          | `prod.connecteam.feature-memory`                   |
| `AWS_REGION`         | `eu-central-1` (Connecteam standard)               |
| `S3_VECTOR_BUCKET`   | `prod.connecteam.feature-memory-vectors`           |
| `AUTH_HEADER`        | `X-Connecteam-User`                                |

Optional - only set if you need to override defaults:

| Variable                  | Default                              | When to set                                                    |
| ------------------------- | ------------------------------------ | -------------------------------------------------------------- |
| `S3_VECTOR_INDEX_NAME`    | `features`                           | Multi-tenant inside one bucket. We don't need this today.      |
| `BEDROCK_REGION`          | falls back to `AWS_REGION`           | Titan v2 isn't available in `eu-central-1` as of 2026; set to `us-east-1`. |
| `BEDROCK_MODEL_ID`        | `amazon.titan-embed-text-v2:0`       | Swap embedding model. Requires re-migration if dim changes.    |
| `EMBEDDING_DIM`           | `1024`                               | Must match the vector index's `dimension`.                     |

Full reference: [docs/operations/env-vars.md](./env-vars.md).

## AWS provisioning

### 1. Markdown bucket (regular S3)

- **Name:** `prod.connecteam.feature-memory`
- **Region:** `eu-central-1`
- **Versioning:** enabled
- **Encryption:** SSE-S3 (SSE-KMS if compliance asks)
- **Lifecycle:** optional - expire `audit/*` after 365 days

### 2. Vector bucket (Amazon S3 Vectors)

- **Name:** `prod.connecteam.feature-memory-vectors`
- **Region:** `eu-central-1` (or wherever S3 Vectors is available)
- **Index inside it:**
  - Name: `features`
  - Data type: `float32`
  - Dimension: `1024`
  - Distance metric: `cosine`

Vector buckets are a separate AWS resource type from regular S3 buckets. They
do not appear in the S3 console UI - use the `s3vectors` API or the new S3
Vectors console section.

The migration script can create both in one shot for dev environments with
`--create-vector-bucket --create-vector-index`. Do **not** use those flags in
prod - the bucket should be Terraform/IaC provisioned with the right tag
taxonomy.

### 3. Bedrock model access

1. AWS Console -> Bedrock -> Model access -> Manage model access
2. Enable **Amazon Titan Text Embeddings V2**
3. Submit (usually instant approval for first-party Amazon models)

This is per-account per-region. If you set `BEDROCK_REGION=us-east-1`, enable
it in `us-east-1`.

### 4. IAM role for the pod

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "MarkdownBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::prod.connecteam.feature-memory/*"
    },
    {
      "Sid": "MarkdownBucketList",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::prod.connecteam.feature-memory"
    },
    {
      "Sid": "VectorBucket",
      "Effect": "Allow",
      "Action": [
        "s3vectors:PutVectors",
        "s3vectors:DeleteVectors",
        "s3vectors:QueryVectors",
        "s3vectors:GetVectors",
        "s3vectors:GetIndex"
      ],
      "Resource": [
        "arn:aws:s3vectors:eu-central-1:*:bucket/prod.connecteam.feature-memory-vectors",
        "arn:aws:s3vectors:eu-central-1:*:bucket/prod.connecteam.feature-memory-vectors/index/features"
      ]
    },
    {
      "Sid": "BedrockEmbeddings",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0"
    }
  ]
}
```

## Seeding the buckets (one-time)

After AWS resources exist and you have a machine with appropriate
credentials, run the migration script once:

```bash
feature-memory-migrate \
  --features-dir /path/to/repo/features \
  --bucket prod.connecteam.feature-memory \
  --vector-bucket prod.connecteam.feature-memory-vectors \
  --region eu-central-1 \
  --bedrock-region us-east-1   # only if Titan v2 is in a different region
```

Outputs `{uploaded: 12, skipped: 0, errors: 0, vectors: 12}` on a clean
first run; subsequent runs report `{uploaded: 0, skipped: 12, vectors: 12}`
(markdown is ETag-skipped, vector upserts are cheap and always re-run).

## Source code

[github.com/Rafael-Sakera-1995/feature_memory](https://github.com/Rafael-Sakera-1995/feature_memory)

## Smoke test from your laptop, once deployed

```bash
curl https://feature-memory.kosmos.connecteam.com/healthz
# expected: {"ok": true, "features": 12, "transport": "streamable-http"}
```

Then install the Feature Memory plugin (PR coming to `connecteam/plugins`)
and ask the agent in Cursor: *"What features do you remember?"* It should
call `list_features` and rattle off the 12 we migrated.
