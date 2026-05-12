---
name: Feature Memory
slug: feature-memory
summary: Local MCP server + Cursor skill that maintain a markdown knowledge base of
  product features.
key_paths:
- src/feature_memory/**
- .cursor/skills/feature-memory/**
- docs/superpowers/specs/2026-04-23-feature-memory-mcp-design.md
dependencies: []
tags:
- mcp
- cursor-skill
- knowledge-base
created_at: '2026-04-24'
updated_at: '2026-04-26'
---

## Overview
Feature Memory MCP is a local Python MCP server plus a Cursor skill that maintain a markdown knowledge base of product features. Cursor agents call the server BEFORE planning to load expert context on a feature, and AFTER implementation to write back what changed as an append-and-merge patch.

The goal: stop losing feature expertise when a Cursor chat ends. Build a shared, structured memory layer instead of ad-hoc agent training per session.

## Architecture
- Local FastMCP server over stdio. Configured in `~/.cursor/mcp.json`.
- One markdown file per feature in `features/<slug>.md`. YAML frontmatter holds machine-surface fields (slug, summary, key_paths, tags). Body holds agent-surface prose in standard sections (Overview, Architecture, Flows, Gotchas, History).
- `index.json` is derived — auto-rebuilt from frontmatter on every write. Never edited by hand.
- Engine modules are pure: `merge.py` (apply_patch), `correction.py` (apply_corrections). `store.py` is the only module that touches disk for feature files. `server.py` is a thin adapter that wires the tools.
- Six MCP tools: `list_features`, `get_feature`, `update_feature`, `create_feature`, `correct_feature`, `archive_feature`. The first four are the default loop. The last two are gated by skill rules to explicit user requests.

## Flows
- Before-planning loop: skill activates on implement/build/fix/refactor intents -> asks user which feature -> named-feature path calls `get_feature(slug)` directly; auto-detect path calls `list_features()` + always confirms with the user before fetching.
- After-implementation loop: agent constructs a `FeaturePatch` (history_entry required, add_* lists optional, summary_override optional, notes_append optional) -> applies the quality filter (feature-level insights only, no commit-level noise) -> calls `update_feature(slug, patch)` -> shows unified diff and any size warnings to the user.
- Correction loop: only on explicit user request -> agent restates the planned change in plain language and asks to confirm -> calls `correct_feature` (surgical removals) or `archive_feature` (soft delete to `_archived/`).
- [CRITICAL] items in flows/gotchas must be surfaced at the top of every plan, separate from the rest of the injected context.
- list_features() reads index.json directly when fresh; falls back to build_index() (full rescan) only if the cache is missing/malformed/stale. Stalness checks: any .md file mtime > index mtime, or slug-set on disk differs from slug-set in the index. Roughly 4x faster at 4 features; speedup grows linearly with corpus size.

## Gotchas
- [CRITICAL] The agent must never call `correct_feature` or `archive_feature` proactively. These tools are gated by skill rules to explicit user removal/archival requests, and require an in-conversation confirmation step before the call.
- [CRITICAL] `update_feature` only accepts a typed `FeaturePatch` — it cannot rewrite the body. This is the structural defense against silent knowledge loss. Do not try to work around it via `notes_append`.
- Removals in `correct_feature` are exact-string match. The server errors if the target text isn't currently in the list. Run `get_feature` first and copy the exact wording.
- Archived features are excluded from `list_features` and `get_feature` errors with a clear hint when asked for an archived slug. Restoration is by hand (move the file back to `features/`).
- Soft size limits are warnings, not blocks: >25 flows or >40 gotchas triggers a recommendation to split via `parent_feature`. Writes always succeed.
- The MCP server returns `(content_blocks, structured_dict)` from `call_tool` only when the tool's return type can be structured (pydantic model, list, primitive). Plain `dict` returns lose the structured payload — that's why response models like `GetFeatureResult` and `CreateFeatureResult` exist.
- Index cache is invalidated by mtime + slug-set checks, not by content hashing. Touching a .md without modifying it (e.g. `touch features/x.md`) will force a rebuild on the next list_features() call. Cheap, but worth knowing.

## Notes
The system is designed git-syncable: when you want to share with the team, push the `features/` repo. No backend changes needed. Embeddings, hierarchical index display, web UI, and team sync server are explicitly deferred to V2 — see Section 7 of the design spec.

## Last Update
- 2026-04-26 - agent - Optimized list_features() to use index.json as a cache with mtime+slug-set staleness checks; falls back to full rebuild on miss. ~4x speedup at 4 features, grows linearly.
