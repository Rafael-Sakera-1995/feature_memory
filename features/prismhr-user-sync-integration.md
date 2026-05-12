---
name: PrismHR User Sync Integration
slug: prismhr-user-sync-integration
summary: PrismHR provider for user-sync — token/session, employee fetch, webhook subscriptions,
  and dropdown-field catalog enrichment.
key_paths:
- ct_services/integration_service/clients/prismhr/**
- app/scripts/infra/integrations/components/prism-hr/**
- app/scripts/infra/integrations/users-sync-integrations/modals/prism-hr/**
dependencies:
- user-sync-field-mapping
parent_feature: user-sync-field-mapping
tags:
- prismhr
- user-sync
- integration-provider
- backend
created_at: '2026-04-26'
updated_at: '2026-05-03'
---

## Overview
PrismHR is a US-only user-sync integration. The provider implements the generic user-sync field-mapping infra (see `user-sync-field-mapping`) and adds two provider-specific concerns: (1) tenant-scoped session creation against PrismHR's API, and (2) closed-list dropdown values for fields where PrismHR ships either a static enum (e.g. `payMethod`, `gender`) or a per-tenant dynamic catalog (Department / Job / Location).

## Architecture
### Auth + session
- `PrismHRSDKClient` (`clients/prismhr/sdk/prism_hr_sdk_client.py`) extends `PendingUserIntegrationClient` + `BaseRequestClient`.
- Token JSON in `IntegrationToken.refresh_token` holds `{ username, password, peo_id, client_id, on_prem_api_url? }`. On-prem tenants override the API base URL.
- `create_session` POSTs `/login/v1/createPeoSession` and stores `sessionId` in `default_headers`. All subsequent calls reuse the session.

### Field catalog
- `PRISM_HR_FIELDS` (`clients/prismhr/utils/prism_hr_consts.py`) — canonical list of supported fields.
- `clients/prismhr/utils/prism_hr_dropdown_values.py` classifies fields:
  - `PRISM_HR_STATIC_DROPDOWN_VALUES` — in-process enum tables (payMethod, payPeriod, gender, maritalStatus, employeeStatus, state/driverState, plus calculated workerType/payType/overtimeEligibility/employmentType).
  - `PRISM_HR_DYNAMIC_DROPDOWN_RESOLVERS` — `homeDepartment`, `homeLocation`, `jobCode` resolved via `getClientCodes`.
  - `get_dropdown_type(field_id)` → STATIC / DYNAMIC / None.

### Metadata enrichment
- `PrismHRMetadataLogic._resolve_allowed_values_for_fields` (`clients/prismhr/logic/prism_hr_metadata_logic.py`) overrides the abstract hook from `PendingUserMetadataLogic`. Static fields read locally (no network); dynamic fields share a single batched `getClientCodes` HTTP call (Department + Job + Location + Pay).
- Dynamic path is wrapped in its own scoped `try/except`: a PrismHR outage or missing `client_id` leaves dynamic fields with `[]` (FE → free-text), but static catalog values still ship. Without this scoping the base-class catch-all in `_enrich_fields_with_allowed_values` would discard the entire `result` dict on any raise.

### Sync runtime
- `PrismHRUserSyncLogic` + `PrismHRPendingUserImportLogic` handle the user fetch + import. Employees come via `/employee/v1/getEmployee` in chunks of `PRISMHR_GET_EMPLOYEE_DETAILS_CHUNK_MAX_SIZE = 20`.
- Webhook subscriptions: `create_subscription` / `append_filter` / `cancel_subscription` against PrismHR's subscription API; comma-separated `webhookUrls`.

### Calculated fields
- `_compute_calculated_field` in `prism_hr_sdk_client.py` translates PrismHR booleans / single-letter codes into locale-translated strings for `workerType` (employee1099), `payType` (payMethod), `overtimeEligibility` (flsaExempt), `employmentType` (typeClass).
- The `value_mappings` key for these fields is the English label (PrismHR is US-only; company locale resolves to English). `code == label` for these entries — the FE `formatExternalFieldValueLabel` collapses the display to a single label.

## Flows
- PRISMHR_BLOCKED_MATCHES (prism_hr_consts.py) ships the four calculated compensation pairs via PrismHRMetadataLogic.get_integration_metadata's blockedMatches: workerType, payType, overtimeEligibility, employmentType — each external field paired with the matching CustomFieldsSpecialTypes value (WorkerType / PayType / OvertimeEligibility / CompensationEmploymentType). PrismHR is the first provider to populate blockedMatches; other providers default to [].

## Gotchas
- `[CRITICAL]` PrismHR is US-only in production. Static dropdown labels (`prism_hr_dropdown_values.py`) are stored as raw English strings, NOT translation keys. Calculated-field `code == label` invariant depends on the company locale resolving to English. Rolling out to non-US tenants requires revisiting both the calculated-field mapping keys and the static-value labels.
- `[CRITICAL]` `_resolve_allowed_values_for_fields` MUST keep its dynamic-path soft-fail scoped (own try/except around `get_client_codes`). Letting that raise reach the base-class catch-all would discard static catalog values already in `result` — breaking gender/payMethod/etc. dropdowns whenever PrismHR's API is slow.
- `get_client_codes` is decorated with `@with_retry_async(max_retries=3)` and uses `timeout=10` on `_request_fetch`. Match this pattern for any new metadata-time PrismHR call — the metadata endpoint fans out on every setup-page open and a slow tenant must not hang the whole response.
- `_get_token` requires `integration_id` for tenant scoping. The metadata route passes it via `Query(None, alias="integrationId")`; without the alias the param silently becomes `None` and `IntegrationTokenLogic.get_token_by_company_id` falls back to an arbitrary token. Same root cause as the parent-feature gotcha.
- `_extract_employee_id` strips a leading client-id prefix from employee IDs before calling `/employee/v1/getEmployee`. Don't bypass it — PrismHR returns prefixed IDs in some flows but the `getEmployee` endpoint expects the bare numeric.
- PrismHR responds with `errorCode` as either a string `"0"` or an int `0`; always compare against both (`response.get("errorCode") not in ("0", 0)`).
- `PRISMHR_GET_EMPLOYEE_DETAILS_CHUNK_MAX_SIZE = 20`. Increasing it has hit PrismHR-side timeouts in practice.
- PrismHR has separate connection modals for user-sync, punch-clock, and paystubs. They share the same token under the hood but have distinct setup flows; don't conflate.
- PRISMHR_BLOCKED_MATCHES Connecteam side mirrors PRISMHR_DEFAULT_FIELD_MAPPING's CustomFieldsSpecialTypes.*.value strings (NOT raw field ids). Keep the two lists in sync: any new calculated/derived field added to the default mapping that admins must not remap should also be added here, otherwise the FE will silently allow per-tenant value remapping of a server-computed value.
- [CRITICAL] `PrismHrEmployeeUpdateHandler._get_mapped_external_field_ids` MUST union both `internal_to_external_field_id` AND `extra_fields_internal_to_external.get_external_field_ids()`. Webhook `Compensation/UPDATE` events arrive with `modifiedAttribute=['hourlyPayRate', 'hourlyPayPeriod', 'effectiveDate', ...]` and pay-rate fields live ONLY on the extra-fields slot. Forgetting either side causes `_filter_event_to_process` to silently drop every compensation-only webhook ('no changed field is mapped'), so post-import pay-rate corrections never reach the user. Same gap will hit any future extra-field type (benefits/deductions/etc.) added to `ExtraFieldsMapping`.
- `PrismHRUserSyncLogic._get_prism_user_pay_rate` returns `{}` when `effectiveDate` is missing/null. PrismHR Compensation records can ship `hourlyPayRate` without an `effectiveDate`; without the guard `PayRateAssignment.effectiveDate` becomes `None`, `parse_datetime(None)` raises, and the pay-rate worker message is poisoned. Empty dict is the documented contract — `PayRateUserCreationLogic.assign_pay_rate_from_extra_fields` already short-circuits on missing `wageType`/`defaultWage`.

## Last Update
- 2026-05-03 - rafael - Pay-rate webhook filter fix: union extra_fields_internal_to_external IDs into _get_mapped_external_field_ids, add effectiveDate guard in _get_prism_user_pay_rate.
