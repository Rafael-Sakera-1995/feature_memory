---
name: Users Page Settings Modal
slug: users-page-settings-modal
summary: Fullscreen side-tabbed settings hub opened from the Users page; central registry
  adds/gates per-feature settings tabs.
key_paths:
- app/scripts/categories/users/modals/users-page-settings-modal/**
- app/scripts/categories/users/services/users-page-settings-modal-event-service.ts
dependencies:
- user-sync-field-mapping
tags:
- frontend
- modal
- settings
- users
- side-tabs
- tab-registry
created_at: '2026-04-26'
updated_at: '2026-04-26'
---

## Overview
The `UsersPageSettingsModal` is a fullscreen settings hub opened from the Users page. It hosts independent settings tabs (custom fields, employee card, performance, paystubs, early wage access / Clair, user-sync integrations) behind a shared left-rail `SideTabs` navigation. The modal is also the deep-link target for several callers that need to land users directly on a specific settings tab (e.g. `USER_INTEGRATION_TAB_ID`, `EMPLOYEE_PERFORMANCE_TAB_ID`).

Key design property: tabs are **decoupled** from each other. Each tab brings its own store/context/queries; the modal only owns the shell, the side-tab navigation, BI on tab change, and a per-company FTE local-storage flag. Adding or removing a tab is a single-file change in the registry plus a new component folder.

## Architecture
- **Shell** — `users-page-settings-modal.tsx` exports `openUsersPageSettingsModal(initialTab?: UsersSettingsTabId)` which calls `ModalServiceV2.openModal(UsersPageSettingsModal, { initialTab })`. The component renders a `FullscreenModal` with three slots: header (title + Divider), body (`UsersPageSettingsModalBody`), footer (a single Close `Button`). It also wraps everything in `UserPageSettingsContext.Provider` exposing `{ closeModal }` so deep children can dismiss the modal.
- **Body** — `components/users-page-settings-modal-body.tsx` builds tabs once via `useMemo(getUsersSettingsModalTabs, [])` and renders `SideTabs` with `renderOnlySelectedTab` (lazy mount). Tab change handler does two things: (a) sets `${company}_${targetTabId}` FTE flag in local storage on first visit, (b) fires BI `user_settings_tab:changed` via `users-page-settings-modal-event-service.ts`.
- **Tab registry** — `components/tabs/modal-tabs.ts` exports `getUsersSettingsModalTabs(): Tab[]`. Each entry is `{ id, testId, titleTranslationKey, component }`. Conditional tabs use `<flag-or-setting> && { ... }` and the array is `.filter(Boolean)`-ed at the end. This is the **only** place tabs are registered.
- **Tab IDs** — `components/tabs/modal-tab-constants.ts` declares the string ID constants AND the `UsersSettingsTabId` union used by `openUsersPageSettingsModal` and all deep-link callers.
- **Context** — `context/userPageSettingsContext.ts` provides `UserPageSettingsContext` with `{ closeModal }` (used by tabs that need to dismiss the modal, e.g. on success).
- **BI service** — `app/scripts/categories/users/services/users-page-settings-modal-event-service.ts` (feature `users` / `users_settings`). Two events: `users_settings:opened` (fired by callers that open the modal) and `user_settings_tab:changed` (auto-fired by the body on tab change with `source_tab_id` / `destination_tab_id`).

### Tab inventory (current)

| ID | Component | Visibility gate | Notes |
|---|---|---|---|
| `user-details` | `UserDetailsTab` | always | Renders `ManageCustomFields` (custom fields manage modal) with a fresh `CustomFieldsManageStore`. |
| `employee-card` | `EmployeeCardTab` | `rootStore.companySettings?.featureFlagSettings?.isEmployeeCardEnabled` | Wraps `EmployeeCardContent` in `EmployeeCardContext` with a new `EmployeeCardStore`. |
| `employee-performance` | `EmployeePerformanceTab` | always | Two stores (main `getInitializedEmployeePerformanceStore` + tab `EmployeePerformanceSettingsStore`), header + metrics + mobile preview. |
| `employee-paystubs` | `EmployeePaystubsTab` | always | `PaystubsTabContext` via `useCreatePaystubsTabStore`; loader gate; ADP/etc. integration configs. |
| `early-wage-access` | `EarlyWageAccessTab` | feature flag `IS_CLAIR_ENABLED` | React Query for Clair settings, owner-only Toggle gated by `AdminPermissionGuardArea`, opens `turnOffClairModal` on disable. |
| `user-integration` | `EmployeeIntegrationsTab` | always | Renders `UserSyncIntegrationsList`; fires `external_user_sync_modal:opened` BI on mount. |

## Flows
- **Open from Users page header** — `users-page-header-buttons-row.tsx` calls `openUsersPageSettingsModal()` (no initial tab) when the gear button is clicked.
- **Open with deep-link tab** — callers pass a `UsersSettingsTabId`, e.g. `add-users-action-menu-button.tsx` opens `USER_INTEGRATION_TAB_ID`; the employee-performance action bar opens `EMPLOYEE_PERFORMANCE_TAB_ID`.
- **URL deep-link** — `users-page.tsx` reads a URL parameter and forwards it as `initialTab` to `openUsersPageSettingsModal`, then `Router.clearParameters()`.
- **Tab change** — `SideTabs.onTabChange` fires `user_settings_tab:changed` BI and writes a per-company FTE flag (`${company}_${tabId}`) to local storage on first visit per tab.
- **Adding a new tab** —
- Adding a tab whose setting is a global company flag — extend the existing `Creator/Setting` PATCH (`Category.description['company_info']`) and the shared `AccountSettings` MobX model under `rootStore.companySettings`, rather than introducing a new endpoint. The `user-activation` tab followed this pattern for `preventNotificationsBeforeFirstLogin`.

## Gotchas
- **`renderOnlySelectedTab` is on by default in the body.** Tabs are mounted lazily and unmounted when the user switches away. Don't rely on a tab keeping its in-memory state across switches; persist anything important in a store/query cache.
- **Tab-registry is the only place to register tabs.** Don't try to add a tab elsewhere — IDs and conditional gating both live in `modal-tabs.ts` + `modal-tab-constants.ts`.
- **Conditional tabs are filtered via `.filter(Boolean)`.** The pattern `<gate> && { ... }` produces `false` when the gate fails; the cast `as Tab[]` after `.filter(Boolean)` is intentional. Keep this pattern when adding flag-gated tabs.
- **The modal still uses `ModalServiceV2` (not V3).** Use `ModalServiceV2.openModal` from `@connecteam/bookshelf/ModalV2` and the `FullscreenModal` shell; do not migrate to V3 ad-hoc.
- **BI on tab change is automatic.** Don't double-fire `user_settings_tab:changed` from inside a tab; the body already does it. New tab BI should use a different label scoped to that tab's feature.
- **`UserPageSettingsContext.closeModal` is the proper way for a tab to dismiss the modal** (e.g. after a successful action). Don't pass `close` down through props.
- **Deep-link consumers depend on the `UsersSettingsTabId` union.** Renaming or removing a tab ID breaks the typed callers in `add-users-action-menu-button.tsx`, the employee-performance action bar, and the URL deep-link in `users-page.tsx`.
- [CRITICAL] The `user-activation` tab is not UI-only. Its `preventNotificationsBeforeFirstLogin` toggle is read by the backend `PreventNotificationsFilter` on every email/SMS dispatch and silences non-activated users (with a strict invite-only allowlist). Renaming the FE key, the JSON path under `Category.description['company_info']`, or removing the BE filter silently breaks the gate — coordinate FE + BE when touching this flag.

## Notes
### User Activation tab (added 2026-04-26)
New tab `user-activation` (`USER_ACTIVATION_TAB_ID`) renders `UserActivationTab`, which exposes a single `CheckboxV2WithDescription` bound to `accountSettings.preventNotificationsBeforeFirstLogin`. State lives on the shared `AccountSettings` MobX model (`rootStore.companySettings`); persistence reuses the global `Creator/Setting` PATCH that already covered `company_info` — no new endpoint. Backend stores the value at `Category.description['company_info']['preventNotificationsBeforeFirstLogin']` and applies it via `v2/features/settings/prevent_notifications_filter.py`, which drops non-activated recipients from email and SMS (`SendTextMessagesLogic.send` + `EmailHandler.send_email_with_objected_filter_unsubscribe`). "Activated" = owner OR `first_login_timestamp` set OR `mobile_last_active > 0` OR `dashboard_last_active > 0` (mirrors `UserLogicV2.get_logged_in_users_ids`, also filters `User.deleted == 0`). Visibility: always on.

## Last Update
- 2026-04-26 - Rafael - Added the 'User activation' tab with a single 'Prevent notifications before first login' toggle; persisted via the existing global Creator/Setting endpoint under Category.description['company_info'] (no new API).
