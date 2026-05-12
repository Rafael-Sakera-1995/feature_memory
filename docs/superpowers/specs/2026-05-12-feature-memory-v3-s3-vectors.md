# Feature Memory V3 - S3 Vectors + Bedrock

**Status**: Active. Supersedes [V2](2026-05-12-feature-memory-v2-hosted.md).
**Author**: Rafael Sakera, with input from Oren Haliva (S3 Vectors recommendation).
**Date**: 2026-05-12.

## Summary

V3 pivots away from FAISS-in-RAM + OpenAI embeddings to a fully AWS-native
stack: **Amazon S3 Vectors** for the vector index and **AWS Bedrock Titan
Text Embeddings v2** for the embedding model. The server becomes stateless
with respect to search - no in-process vector cache, no debounced flush, no
cold-start re-embed.

This is a net deletion: ~300 lines removed, ~200 added, fewer moving parts,
one fewer external vendor.

## Why

V2 worked but carried real architectural complexity:

1. **In-process FAISS** meant every server pod had its own copy of the vector
   index. Multi-instance coherence relied on a debounced flush to S3 + a
   reload-on-cache-miss path. Workable, but easy to get wrong.
2. **Embeddings cache (`caches/embeddings.jsonl`)** existed only to avoid
   re-embedding all features on every pod restart. Pure infrastructure tax.
3. **OpenAI as the embedding vendor** was an extra security review surface
   (one more key to rotate, one more legal contract) when the team is
   already an AWS shop.

S3 Vectors collapses (1) and (2): the index lives in S3, every pod reads
the same view, restart is free. Bedrock collapses (3): same IAM, same
billing, same audit trail as the rest of our AWS surface.

The cost is per-query latency: FAISS was sub-millisecond, S3 Vectors is
~100-300ms over the network. Inside an agent flow (where the chat LLM is
already the long-tail latency) this is invisible. We are not building a hot
path for end users; we are building an agent-side knowledge base.

## Architecture

```mermaid
flowchart LR
    agent[Cursor/Claude agent] -->|streamable-http| pod[MCP pod]
    subgraph aws [AWS account]
        pod -->|GetObject / PutObject| md["S3 markdown bucket<br/>features/*.md<br/>caches/index.json<br/>audit/*"]
        pod -->|QueryVectors / PutVectors| vec[S3 Vectors bucket<br/>index: features]
        pod -->|InvokeModel| bed["Bedrock<br/>amazon.titan-embed-text-v2:0"]
    end
```

### Module map

| Module | V2 | V3 |
|---|---|---|
| `models.py` Config | `openai_api_key`, `openai_model`, `embedding_dim`, `cache_debounce_seconds` | `s3_vector_bucket`, `s3_vector_index_name`, `s3_vector_region`, `bedrock_region`, `bedrock_model_id`, `embedding_dim=1024` |
| `search.py` `Embedder` | OpenAI `text-embedding-3-small`, 1536-dim | Bedrock Titan v2, 1024-dim |
| `search.py` `FAISSIndex` | In-process IndexIDMap over IndexFlatIP | **Deleted** |
| `search.py` `S3VectorsIndex` | n/a | **New**: thin boto3 `s3vectors` wrapper |
| `index.py` `MemoryIndex` | FAISS + debouncer + dual-cache flush | Just `dict[slug -> IndexEntry]` + synchronous `index.json` flush |
| `index.py` `_Debouncer` | Coalesces cache writes | **Deleted** |
| `index.py` `EMBEDDINGS_FILENAME` | `embeddings.jsonl` | **Deleted** |
| `server.py` | Wires FAISS + OpenAI | Wires S3VectorsIndex + Bedrock Embedder |
| `scripts/migrate_to_s3.py` | Writes embeddings.jsonl cache | Writes vectors to S3 Vectors |
| `Dockerfile` | Installs `libgomp1` for FAISS | No native deps |
| `pyproject.toml` | `faiss-cpu`, `openai` | Just `boto3>=1.40` |

## Storage layout

### Markdown bucket (regular S3)

Unchanged from V2:

```
features/{slug}.md
features/_archived/{slug}.md
caches/index.json            # frontmatter + summary, used for fast cold-start
audit/YYYY-MM-DD/*.json
```

`caches/embeddings.jsonl` is **gone**. Embeddings live in S3 Vectors only.

### Vector bucket (Amazon S3 Vectors)

```
{vector_bucket}/
  features/                  # one index, name="features"
    vectors keyed by {slug}
      data: float32[1024]
      metadata: {name, tags[], parent_feature?}
```

Index config: `dataType=float32, dimension=1024, distanceMetric=cosine`.

## Concurrency model

V2 used `If-Match` ETags on the markdown bucket + a debouncer for the embeddings
cache. V3 keeps the markdown ETag flow (unchanged - that's a `Storage` concern,
not search) and **removes the debouncer entirely**.

S3 Vectors `PutVectors` has upsert semantics keyed by `(bucket, index, key)`.
Concurrent writes for the same slug are last-writer-wins on the vector side -
acceptable, because the markdown write goes first under an ETag, so a stale
agent that loses the markdown race also doesn't get to write its embedding.

## Author attribution

Unchanged from V2: the server reads the `X-Connecteam-User` header (configurable
via `AUTH_HEADER`) and overrides `patch.last_update.author` server-side so the
agent cannot self-attribute.

## Tool surface

Identical to V2:

- `list_features` / `search_features` / `get_feature` (read)
- `create_feature` / `update_feature` / `correct_feature` / `archive_feature` (write)

`search_features` semantics change slightly: scores now come from S3 Vectors'
cosine distance converted to similarity (`1 - distance`), and the round-trip
is network-latency-bound. Treat scores <0.3 as weak matches; that threshold is
unchanged.

## Environment contract

| Env var | Required when | Purpose |
|---|---|---|
| `STORAGE_BACKEND=s3` | Always (V3 path) | Enables the AWS backend |
| `S3_BUCKET` | `STORAGE_BACKEND=s3` | Markdown bucket name |
| `AWS_REGION` | `STORAGE_BACKEND=s3` | Default region for all AWS clients |
| `S3_VECTOR_BUCKET` | Search enabled | Vector bucket name |
| `S3_VECTOR_INDEX_NAME` | optional | Defaults to `features` |
| `S3_VECTOR_REGION` | optional | Override if vector bucket is in a different region |
| `BEDROCK_REGION` | optional | Override (e.g. `us-east-1` if Titan v2 isn't in your S3 region) |
| `BEDROCK_MODEL_ID` | optional | Defaults to `amazon.titan-embed-text-v2:0` |
| `EMBEDDING_DIM` | optional | Defaults to 1024; must match the vector index |
| `AUTH_HEADER` | HTTP mode | Server-side author override |
| `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT` | HTTP mode | |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | local dev only | Pod uses IAM role |

If `S3_VECTOR_BUCKET` is unset, the server still boots, list/get/create/update
all work, and `search_features` returns `[]` (with a startup warning). This is
the local/dev fallback - nobody runs prod this way.

## Rollout plan

1. Merge V3 PR to `main` on the upstream feature_memory repo.
2. DevOps provisions:
   - One **vector bucket** per env (`dev.connecteam.feature-memory-vectors`,
     `prod.connecteam.feature-memory-vectors`), with the same tag taxonomy as
     the existing markdown buckets.
   - One **vector index** inside each: `features`, dim=1024, metric=cosine.
   - **Bedrock model access** enabled for `amazon.titan-embed-text-v2:0` in
     the target region. (This is per-account, one-time, via the AWS Console.)
   - IAM grants for the kosmos pod's role: `s3vectors:PutVectors`,
     `s3vectors:QueryVectors`, `s3vectors:DeleteVectors`, plus the existing S3
     grants and `bedrock:InvokeModel` for the Titan model ARN.
3. Run `feature-memory-migrate` against dev, smoke-test from Cursor.
4. Run against prod, deploy V3 image, smoke-test the hosted endpoint.
5. Bump plugin to 3.0.0 in `Connecteam/plugins`. Marketplace rolls out to devs.

## Risk register

| Risk | Mitigation |
|---|---|
| Bedrock Titan v2 unavailable in target region | `BEDROCK_REGION` env var lets us point at `us-east-1` even if S3 is in `eu-central-1`. Cross-region traffic is small per-call (1KB) so latency penalty is negligible. |
| moto doesn't fully support `s3vectors` yet | Tests use in-process fakes (Protocol-stubbed clients), so moto coverage is irrelevant for the vector path. |
| S3 Vectors per-query latency (100-300ms) | Confirmed acceptable inside agent flow. If we ever build a UI hot-path, revisit (could add a tiny local LRU). |
| S3 Vectors quotas (TPS, vector count) | We're at ~12 features, ceiling is millions of vectors. Years of headroom. |
| Bedrock cost | Titan v2 = $0.00002 per 1K tokens. ~$5/month at 50 devs × 100 searches/day. Trivial. |
| Bedrock model access not pre-approved | DevOps handoff explicitly calls this out. One Console toggle. |

## Out of scope for V3

- Hybrid search (BM25 + vectors)
- Metadata filtering on `query_vectors` (supported by S3 Vectors; we'll wire
  it when a real use case appears, e.g. "search only within tag=X")
- Multi-region replication of the vector bucket
- Provider abstraction. We have one backend (Bedrock + S3 Vectors). If we
  later need a second, we'll add a `Protocol` seam at that point.
- LocalStack support for the vector path. LocalStack community doesn't
  implement `s3vectors`. Local dev uses a real shared dev vector bucket.

## Deleted from V2

For posterity, V3 deletes:

- `src/feature_memory/search.py`: `FAISSIndex` class (~150 lines)
- `src/feature_memory/index.py`: `_Debouncer` class, `EMBEDDINGS_FILENAME` constant, embeddings-cache hydration branches (~80 lines)
- `pyproject.toml`: `faiss-cpu`, `openai` dependencies
- `Dockerfile`: `libgomp1` system package
- `docs/operations/localstack.md`: still useful for the markdown-only flow but no longer covers the full end-to-end path; marked historical
