---
name: Integration Activity Log
slug: integration-activity-log
summary: Cross-service audit log for integration runs (SETUP / DISCONNECTED / DAILY_SYNC
  / WEBHOOK / APPROVAL) — publisher writes JSON to S3 + rows to integration_service
  tables, surfaced in the matrix admin Integration Logs tab.
key_paths:
- ct_services/integration_service/infra/activity_log/**
- ct_services/integration_service/infra/routers/activity_log_router.py
- ct_services/integration_service/infra/routers/setup_router.py
- ct_services/integration_service/clients/common/pending_user_integration/**
- ct_services/integration_service/clients/common/integration_disconnect_logic.py
- logic/invite_links/user_join_requests_logic.py
- logic/invite_links/webhook_runner_logic.py
- v2/infra/pubsub/models/event_models.py
- app/pages/company/components/integration-logs-tab/**
dependencies:
- prismhr-user-sync-integration
- user-sync-field-mapping
- pos-sales-integration
tags:
- integrations
- activity-log
- audit-log
- backend
- frontend
- matrix
- s3
- mysql
- pubsub
created_at: '2026-05-11'
updated_at: '2026-05-11'
---

## Overview
The Integration Activity Log is a cross-service audit trail for everything an integration does on a company: initial setup, settings updates, daily sync runs, inbound webhooks, manual user approvals/declines/deletions, and disconnects.

It has two physical halves:

- **Write side** lives in `pymobiengine` (the `integration_service` slice). `IntegrationActivityPublisher` uploads the raw payload to S3 and publishes an `IntegrationActivityEvent` (pubsub) per logical entity. A worker materializes the event into rows in two MySQL tables: `integration_activity_log` (one row per run/trigger) and `integration_activity_log_entry` (one row per affected entity, e.g. a single pending user). The `provider` field on the event is a free-form `str` — historically constraining it to the `IntegrationOption` enum silently dropped APPROVAL logs.
- **Read side** is `IntegrationActivityLogQueryLogic.get_logs_with_entries` in pymobiengine, exposed via the `ActivityLog/` router. The matrixapp `IntegrationLogsTab` paginates and renders rows in a `SmartTable`, with a `PayloadLogModal` that pulls the S3 payload, extracts the single relevant entity for the row, and renders both the entity slice and the full payload in a `jsoneditor-react` tree with per-tree search + copy-log buttons.

## Architecture
- **Tables**: `integration_activity_log` (run-level: company, provider, integration_id, integration_type, connection_label, trigger_type, run_id, payload_url, timecreated) and `integration_activity_log_entry` (per-entity: run_id FK, entity_display_name, internal_id, external_id, event_type, timecreated). Both `internal_id` and `external_id` are plain `String(255)` with no explicit collation → search defaults to the connection's collation, which can be case-sensitive (see Gotchas).
- **Triggers** (`IntegrationActivityTriggerType`): `SETUP`, `DISCONNECTED`, `DAILY_SYNC`, `WEBHOOK`, `MANUAL_REFETCH`, `APPROVAL`. `APPROVAL` is the synthetic trigger used for manual approve/decline/delete actions on pending users.
- **Entity events**: `ActivityEntityEventType` (FAILED / SUCCEEDED) for run-level outcomes, and `PendingUserSyncActivityEventType` (CREATED / UPDATED / DELETED / REHIRED / PENDING / AUTO_APPROVED / APPROVED / DECLINED) for per-user events.
- **S3 payload doc** is a JSON object uploaded by `IntegrationActivityPublisher.upload_payload`. The doc shape varies by trigger; for APPROVAL the entities live in a `users` array (wrapped object), for raw imports it's a flat array. The frontend's `extractEntityData` must handle both shapes.
- **Activity log writes are best-effort.** Publishing failures are swallowed (logged, not re-raised) so they never break the surrounding sync/webhook flow. Same rule applies to `_fetch_external_id_to_user_id_map`, which wraps its lookup in try/except and returns `{}` on failure.
- **Manual APPROVED/DECLINED logging is centralized in `integration_service`.** Product code in `logic/invite_links/user_join_requests_logic.py` no longer publishes APPROVAL activity events directly; instead, approvals/declines emit a `user_fulfilled` invite-link webhook (`InviteLinkWebhookEventType.USER_FULFILLED`), and `CTPendingUsersRunner._publish_decision_activity_log` translates that webhook into the right APPROVAL activity log rows. This keeps all approval-side logging in one service.
- **Auto-approve dedup**: When a pending user is created and auto-approved in the same import (`_upsert_pending_users_locked` in `base_pending_user_sync_logic.py`), the entry is logged once as `AUTO_APPROVED` (not as separate CREATED + APPROVED rows). The webhook-driven path (`_publish_decision_activity_log`) skips users whose `acted_by_user_id is None` for the same reason — re-publishing them would be a duplicate row.
- **Search**: `IntegrationActivityLogQueryLogic.get_logs_with_entries` uses an extracted `_build_search_filter(search)` helper that returns the case-insensitive OR-`LIKE` clause (or `None` for empty / whitespace input). Both sides are `func.lower(...)`, LIKE wildcards (`%`, `_`, `\`) in user input are escaped, and the clause carries an explicit `ESCAPE '\'`.

## Flows
- **SETUP success/failure** — `setup_router` calls `IntegrationActivityPublisher.record_success` or `record_failure`. `_sanitize_setup_settings_for_log` normalizes `integration_settings` (which can arrive snake_case from DB `previous_settings` or camelCase from the UI's `current_settings`) to a stable shape: `{ transformers_settings, users_filter, auto_approve: { createNewUsers, matchExistingUsers, updateMatchedUsers }, connection_metadata: { client_name, client_id } }`. UUIDs (`external_id`) are intentionally NOT included on FAILED/SUCCEEDED entries (no useful info for support).
- **Pending-user import** — `base_pending_user_sync_logic._upsert_pending_users_locked` upserts pending users from a provider import and publishes a DAILY_SYNC/WEBHOOK activity log. Newly-created users that are also auto-approved in the same run are typed `AUTO_APPROVED`. If a run produces zero entity changes, no row is published at all (suppresses empty import logs).
- **Manual approve / decline** — Admin acts in matrix → product publishes `InviteLinkWebhookEventType.USER_FULFILLED` with `fulfilled_type` (APPROVED/DECLINED) and `acted_by_user_id`. `CTPendingUsersRunner._publish_decision_activity_log` groups payload rows by (provider, integration_id, connection_label) and emits one APPROVAL activity log per group, with each entity carrying `APPROVED` or `DECLINED`. Auto-approved users (`acted_by_user_id is None`) are skipped here — they are already represented in the import log.
- **Pending-user delete** — same `user_fulfilled` channel, surfaces as `PendingUserSyncActivityEventType.DELETED` under an APPROVAL trigger with `acted_by_user_id` in the JSON.
- **Disconnect** — `integration_disconnect_logic` publishes a DISCONNECTED activity log on success (and on failure). `acted_by_user_id` is included in the JSON payload when disconnect was admin-initiated.
- **Read / display** — matrixapp `IntegrationLogsTab` calls `GET IntegrationService/ActivityLog/` with filter params (provider, integration_type, date_from, date_to, search). The `PayloadLogModal` fetches a pre-signed S3 URL (via `payload_url`), then runs `extractEntityData` to find the single row matching the entry's `external_id`/`internal_id` and renders both that slice and the full payload as `jsoneditor-react` trees. Each tree has its own `SearchBox` (case-insensitive, recursive on keys + values) and Copy log button.

## Gotchas
- [CRITICAL] **Don't constrain `IntegrationActivityEvent.provider` to the `IntegrationOption` enum.** It must stay `str`. Mixed casing in the wild (`PrismHR` vs `prismhr`) is intentional and Pydantic validation would silently drop APPROVAL events that don't match the enum's lowercase form.
- [CRITICAL] **Activity-log publishing must never raise into the caller.** `IntegrationActivityPublisher` and `_fetch_external_id_to_user_id_map` swallow exceptions; preserve this when refactoring. Breaking it has cascaded into broken syncs and broken approvals before.
- **Manual approve/decline goes through `user_fulfilled`, not through direct publish.** Do not add APPROVAL `IntegrationActivityPublisher.publish` calls back into `logic/invite_links/user_join_requests_logic.py` — the canonical path is the webhook → `CTPendingUsersRunner._publish_decision_activity_log`. Adding both creates duplicate rows.
- **Skip APPROVED rows when `acted_by_user_id is None`.** `_publish_decision_activity_log` must skip these — they are auto-approves and are already logged as `AUTO_APPROVED` on the import side. Adding them back here = duplicate row per user.
- **Skip empty import rows.** `_upsert_pending_users_locked` must only publish when `entity_refs` is non-empty, or you'll get a no-op activity row on every import.
- **`integration_settings` arrives in both camelCase and snake_case.** Anywhere you read it (e.g. `_sanitize_setup_settings_for_log`), use a `_pick(d, *keys)` style helper that checks both. DB-loaded `previous_settings` are snake_case; UI-loaded `current_settings` are camelCase.
- **Don't include UUID `external_id` in FAILED/SUCCEEDED rows.** It carries no useful info and clutters CS support views.
- **The `users`/`data`/`entities` wrapping in S3 payloads matters.** Frontend `extractEntityData` must handle both flat-array and wrapped-object payloads, and the ID match must accept `external_user_id`/`externalUserId`/`user_id`/`userId` (snake + camel).
- [CRITICAL] **Activity-log search is case-sensitive without `func.lower` on both sides.** `entity_display_name`, `external_id`, `internal_id` are `String(255)` with no explicit collation, so the connection collation wins. If you re-introduce a `column.like('%x%')` here, `search=max` will silently miss `Max Sandoval`. Use `_build_search_filter(search)` (it lowers both sides, strips whitespace, and escapes `%` `_` `\`).
- **Wix `test_setup_router` is brittle around `publish_model` call counts.** Adding new `IntegrationActivityPublisher` calls in `setup_router.py` increases `publish_model` invocations; tests that read `mock_publish_model.call_args` (last call) will break — iterate `call_args_list` and find by message type.
- **The matrixapp logs UI uses `jsoneditor-react`, not raw `<pre>`.** Bookshelf has no JSON-tree component; if you add a new payload view, reuse the `PayloadLogModal` pattern (Editor in `view` mode + a recursive `filterJson` helper + a `key={query}` remount on filter change). Valid copy/check icons are `'copy'` and `'valid'` — `'check'` is not in the Bookshelf icon set.
