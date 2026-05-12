---
name: User Sync Field Mapping
slug: user-sync-field-mapping
summary: Generic FE/BE infra for mapping third-party user fields to Connecteam custom
  fields, with optional per-value dropdown mapping.
key_paths:
- app/scripts/categories/common/components/fields-mapping/**
- app/scripts/infra/integrations/users-sync-integrations/**
- app/scripts/infra/integrations/api/integration-metadata-api.ts
- ct_services/integration_service/clients/common/pending_user_integration/metadata/**
- ct_services/integration_service/clients/common/pending_user_integration/setup/**
- ct_services/integration_service/infra/routers/metadata_router.py
dependencies: []
parent_feature: field-mapping-infrastructure
tags:
- user-sync
- integrations
- field-mapping
- frontend
- backend
created_at: '2026-04-26'
updated_at: '2026-04-28'
---

## Overview
Generic infrastructure used by every user-sync provider (PrismHR, BambooHR, ADP, Microsoft Entra, etc.) to let admins map third-party user fields to Connecteam custom fields. When the Connecteam side is a dropdown, admins can also map external values (per-row) to Connecteam dropdown options via the `MapDropdownValuesModal`. When the third-party ships a closed list of values for a field, the row renders a real selector; otherwise, free-text.

## Architecture
### Backend
- `GET /api/Integration/Metadata/{third_party}/?integrationId=N` is the single source of truth for setup metadata (`ct_services/integration_service/infra/routers/metadata_router.py`).
- Response is `PendingUserIntegrationMetaDataResponse`:
  - `fields: list[ThirdPartyField]` — each with `id`, `name`, `type`, optional `allowed_values: list[ExternalFieldValue]`.
  - `defaultIntegrationMapping`, `externalNonEditableFields`, `connecteamNonEditableFields`, `externalFieldFallbacks`.
- `PendingUserMetadataLogic` (abstract) is the base for every user-sync provider's metadata logic. Hook for closed-list values:
  - `_resolve_allowed_values_for_fields(fields) -> dict[field_id, list[ExternalFieldValue]]` — providers override when they want to ship closed-list values.
  - `_enrich_fields_with_allowed_values(fields)` — wraps the hook in soft-fail, populates each field's `allowed_values`, returns the (possibly rebuilt) list. Callers MUST use the returned list.
- `ExternalFieldValue { code, label }` — `code` is the persisted `value_mappings` key + sync token; `label` is the raw description.

### Frontend
- Modal: `app/scripts/categories/common/components/fields-mapping/components/map-dropdown-values-modal/map-dropdown-values-modal.tsx`.
- Hydration & validation: `map-dropdown-values-utils.ts` (pure functions, no store).
- Per-row UI: `value-mapping-row-view.tsx`. Connecteam side is always `CustomFieldsDropdownSelect`. Third-party side is a `Select` if `allowedValues` is a non-empty array, otherwise `TextInput`.
- Field-level UI: `field-matcher.tsx` + `fields-mapping-manager.tsx`.
- Tenant-aware client query: `app/scripts/infra/integrations/api/integration-metadata-api.ts` passes `integrationId` into the React Query key.

## Flows
- `buildValueMappings` skips half-match rows (external text but no Connecteam option) — they're savable but not persisted.
- Orphan options (Connecteam picked, no external text) block save.
- Duplicate external texts block save with an error on the latest row in each duplicate group.
- Multiple external values → same Connecteam option is allowed (e.g. "Worker" + "Staff" → "Employee").
- blockedMatches: BE ships list[BlockedMatch{externalField, connecteamField}] on PendingUserIntegrationMetaDataResponse (default []). FE threads it through FieldsMappingInitialData -> FieldsMappingStore. When FieldMatcher opens MapDropdownValuesModal, it calls store.isBlockedEntry(entry); if true, the modal opens read-only (dropdowns disabled, Add/trash/Save hidden, Cancel becomes Close). The outer matcher row stays editable; no auto-seeding.

## Gotchas
- `[CRITICAL]` Server-side query params on integration-service routers MUST use `Query(None, alias="integrationId")` for camelCase. Without the alias, FastAPI uses the snake_case Python parameter name and silently drops the value; `_get_token` then falls back to an arbitrary tenant token. Silent multi-tenant data corruption — invisible for single-integration companies, breaks for multi-integration. Sibling routers (e.g. `entity_mapping_router.py`) follow this convention with `alias="includeDeleted"`.
- `[CRITICAL]` `_enrich_fields_with_allowed_values` is value-based: callers MUST capture the return — `fields = await self._enrich_fields_with_allowed_values(fields)`. Today the implementation mutates the pydantic v1 model in place, but a future immutable swap (or pydantic v2 `model_copy`) would silently ship un-enriched fields if the return is discarded.
- `_enrich_fields_with_allowed_values` soft-fails: any raise from `_resolve_allowed_values_for_fields` leaves `allowed_values=None` so the FE degrades to free-text rather than the setup page failing to load.
- Empty `allowedValues` array (vs `undefined`) must still fall back to `TextInput`. Don't render a disabled empty `Select`.
- Width of `Select` / `TextInput` / `CustomFieldsDropdownSelect` is set via the `width="100%"` prop from Bookshelf's `FieldStyleProps`. Do NOT use `:global(.popover-anchor-box)` overrides — that anchor class is shared across the app and global rules leak.
- Display formatting (`label (code)`) lives in the FE (`formatExternalFieldValueLabel`). BE returns raw `code`/`label`. When `code === label` the formatter collapses to a single label.
- FE auto-suggest hydration is synchronous in the `useState` initializer (`hydrateRows`). Do NOT move it to a `useEffect` — that introduced a render flicker and was removed in review.
- Custom-field add-value button limitation/permissions match the user-import convention (plan + RBAC).
- [CRITICAL] BlockedMatch.connecteamField is the CustomFieldsSpecialType string (e.g. 'workerType'), not the resolved per-tenant custom-field id — same convention as defaultIntegrationMapping. FieldsMappingStore.isBlockedEntry resolves entry.connecteamFieldId back through UserCustomField.specialType for comparison, and also accepts a direct id match as fallback. Anyone adding a blocked pair for a non-special-type custom field must keep this dual-lookup intact or the FE silently won't lock the row.
- MapDropdownValuesModal.isReadOnly does NOT change hydrateRows — the full catalog still surfaces. The contract is purely 'inputs disabled, save/add/trash hidden'. Don't try to short-circuit hydration when read-only or you'll show a blank modal for blocked pairs that have no saved valueMappings yet.

## Last Update
- 2026-04-28 - rafael - Added blockedMatches: per-pair read-only value mapping (BE response field + FE store lookup + modal isReadOnly mode).
