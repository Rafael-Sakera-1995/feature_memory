---
name: Prevent Notifications Before First Login
slug: prevent-notifications-before-first-login
summary: Company-wide gate that drops email/SMS to users who haven't logged in yet,
  with a strict invite-only allowlist.
key_paths:
- v2/features/settings/prevent_notifications_filter.py
- v2/features/settings/tests/test_prevent_notifications_filter.py
- v2/features/text_messages/apis/text_messages_handler.py
- v2/features/text_messages/logic/SendTextMessagesLogic.py
- v2/infra/message_notifier/email/EmailHandler.py
- v2/infra/message_notifier/email/tests/test_email_handler_prevent_notifications.py
- v2/features/users/UserLogicV2.py
- app/scripts/categories/users/modals/users-page-settings-modal/components/tabs/user-activation-tab/**
- app/scripts/categories/text-messages/services/text-messages-modals-service.tsx
- app/scripts/categories/text-messages/stores/text-messages-new-message-modal-store.ts
dependencies:
- users-page-settings-modal
tags:
- notifications
- email
- sms
- backend
- frontend
- notification-gate
- settings
created_at: '2026-04-26'
updated_at: '2026-04-26'
---

## Overview
Company-wide notification gate. When the admin enables `Prevent notifications before first login` (User activation tab in the Users page settings modal), the backend filters every outbound email and SMS so that recipients who have never "activated" stop receiving messages — *except* a strict invite-only allowlist that lets first-login flows through. The setting is a single boolean stored on the company `Category` row; the actual gating happens at central email/SMS chokepoints, not at every call site.

The feature spans:
- **Setting storage** (BE) — `Category.description['company_info']['preventNotificationsBeforeFirstLogin']`.
- **"Activated" identity** (BE) — `UserLogicV2.get_activated_user_ids`, mirrored against `get_logged_in_users_ids`.
- **Gate** (BE) — `PreventNotificationsFilter` applied inside `SendTextMessagesLogic.send` and `EmailHandler.send_email_with_objected_filter_unsubscribe`.
- **Frontend error UX** (FE) — dedicated `RecipientsNotActivatedHTTPError` -> `RECIPIENTS_NOT_ACTIVATED` modal in the new-message flow (no retry).
- **Settings UI** (FE) — owned by the `users-page-settings-modal` feature (User activation tab).

## Architecture
### Setting source of truth
- Stored at `Category.description['company_info']['preventNotificationsBeforeFirstLogin']` (boolean, default `false`). Reuses the global `Creator/Setting` PATCH endpoint — no new API.
- Read on the BE via `CompanyData` (DAO around `Category.description`). Cached per-request like the rest of `company_info`.
- Read on the FE from `rootStore.companySettings.accountSettings.preventNotificationsBeforeFirstLogin` (MobX `AccountSettings` model).

### "Activated" user definition
`UserLogicV2.get_activated_user_ids(user_ids)` returns the subset that counts as activated. The filter is:
```
User.id IN user_ids
AND User.company == self.company
AND User.deleted == 0
AND (
     User.is_owner == 1
  OR User.first_login_timestamp IS NOT NULL
  OR User.mobile_last_active > 0
  OR User.dashboard_last_active > 0
)
```
This intentionally mirrors `get_logged_in_users_ids` so legacy users whose `first_login_timestamp` was backfilled from `mobi_login_history` (see `migrate_2022_10_19_migrate_first_login_to_mdl_user.py`) without a corresponding `mobile_last_active` value still count. Owners pass unconditionally. Soft-deleted users (`deleted=1`) are explicitly excluded so a removed user can't keep receiving notifications.

### Gate (`PreventNotificationsFilter`)
- Module: `v2/features/settings/prevent_notifications_filter.py`. Class `PreventNotificationsFilter(BaseLogic)`. Inherits `BaseLogic` so it gets `self.company`, `self.logger`, etc.
- Two entry points: `filter_email_recipients(recipients, email_type)` and `filter_sms_recipients(recipients, sms_type)`.
- Behaviour: if the company flag is off — pass-through. If on — (a) fetch activated user IDs via `UserLogicV2.get_activated_user_ids`, (b) keep only activated recipients, (c) but unconditionally keep recipients whose `email_type` / `sms_type` is in the strict invite-only allowlist (first-login invitations).
- The gate is plugged into central chokepoints, not callers:
  - **SMS:** `SendTextMessagesLogic.send(...)` filters before Twilio dispatch. Defensive early-exit kept in `create_and_send_message`.
  - **Email:** `EmailHandler.send_email_with_objected_filter_unsubscribe(...)` filters before SES dispatch.
- When all recipients are filtered out:
  - **SMS** returns `TextMessageStatus.NOT_SENT` (new enum value, distinct from `DONE` / `ERROR` / `INSUFFICIENT_BUDGET`).
  - **Email** short-circuits the SES send and returns success-without-send.

### API error surface
- `v2/features/text_messages/apis/text_messages_handler.py` defines `RecipientsNotActivatedHTTPError(HTTPError)` — status `400`, `reason="RECIPIENTS_NOT_ACTIVATED"`. The `post` handler raises it on `TextMessageStatus.NOT_SENT`, replacing the previous generic `HTTPError(400, "Couldn't send message")`.
- The `reason` string is the contract with the FE; renaming it silently breaks the FE detection.

### Frontend error handling (text messages)
- `app/scripts/categories/text-messages/stores/text-messages-new-message-modal-store.ts` defines `const RECIPIENTS_NOT_ACTIVATED_REASON = 'RECIPIENTS_NOT_ACTIVATED'` and an `isRecipientsNotActivatedError(error)` helper that inspects `error.data.exception` / `error.data.type`.
- On match in `sendTextMessage`'s catch block: call `TextMessagesModalsService.openRecipientsNotActivatedPopup()` and `closeModal(false)`. Bypasses the existing generic try-again confirmation.
- `text-messages-modals-service.tsx -> openRecipientsNotActivatedPopup()` opens a `openGeneralNotificationModalV2` info modal (red invalid icon, "Got it" CTA only — no retry).
- Translation keys: `TEXT_MESSAGES.NEW_MESSAGE_MODAL.NOT_ACTIVATED_MODAL.TITLE` and `...CONTENT`.

## Flows
- **Admin toggles the setting on** — User activation tab -> `CheckboxV2WithDescription` -> updates `accountSettings` -> Creator/Setting PATCH writes `company_info.preventNotificationsBeforeFirstLogin = true` -> next email/SMS dispatch sees it via `CompanyData`.
- **Outbound SMS, mixed recipients (some activated, some not)** — `SendTextMessagesLogic.send` calls `PreventNotificationsFilter.filter_sms_recipients`; non-activated recipients are dropped; remaining list goes to Twilio; status returned is `QUEUED`.
- **Outbound SMS, all recipients non-activated, non-invite type** — filter empties the list; `send` returns `TextMessageStatus.NOT_SENT`; `text_messages_handler.post` raises `RecipientsNotActivatedHTTPError`; FE store detects `RECIPIENTS_NOT_ACTIVATED`, shows the dedicated modal, closes the new-message modal.
- **Outbound SMS/email, allowlisted invite type** — filter is bypassed regardless of activation status; message goes through. Used by the first-login invitation pipeline.
- **Outbound email, all recipients non-activated, non-invite type** — `EmailHandler.send_email_with_objected_filter_unsubscribe` short-circuits the SES call; no exception bubbles up; caller sees normal success.
- **Owner recipient** — always passes the gate (treated as activated) even with no recorded login activity.
- **Soft-deleted recipient (`deleted=1`)** — always blocked when the flag is on, even if they previously logged in.

## Gotchas
- **[CRITICAL] The "activated" definition lives in one place — `UserLogicV2.get_activated_user_ids`.** It must stay aligned with `get_logged_in_users_ids` (legacy `first_login_timestamp` backfill, `deleted == 0`). Diverging the two has caused at least one CR-bot regression already.
- **[CRITICAL] The gate sits at central chokepoints, not at every API.** SMS goes through `SendTextMessagesLogic.send`; email goes through `EmailHandler.send_email_with_objected_filter_unsubscribe`. Don't sprinkle the filter at call sites — you'll get inconsistent coverage and double-filtering. A defensive early-exit in `create_and_send_message` is intentional belt-and-braces; the central gate is the contract.
- **[CRITICAL] The strict invite-only allowlist is the only bypass.** Adding a new transactional email/SMS type does NOT auto-bypass the gate. New types must be reviewed against the allowlist explicitly; the default is "silenced for non-activated users".
- **`RECIPIENTS_NOT_ACTIVATED` is a string contract between BE `HTTPError.reason` and FE `isRecipientsNotActivatedError`.** Renaming on either side without the other silently degrades to the generic "try again" modal.
- **`TextMessageStatus.NOT_SENT` is distinct from `DONE`, `ERROR`, and `INSUFFICIENT_BUDGET`.** Switch statements / response branches that only check `QUEUED` vs `ERROR` will miss it. The handler maps it to a 400 with the dedicated reason; non-handler callers should treat it as "filtered, do not retry".
- **Owners are exempt from the gate.** Even if `mobile_last_active`, `dashboard_last_active`, and `first_login_timestamp` are all empty, an owner is considered activated. This is product-required — don't tighten it without a product call.
- **Dashboard activity counts as activation.** Mobile login is the canonical signal but `dashboard_last_active > 0` is a valid alternative — a user who only ever logs in via the dashboard still receives notifications. Don't revert this without checking the User activation BI.
- **The setting is a single boolean on `Category.description['company_info']`.** It is NOT a feature flag. Don't add it to the Unleash flag set; don't gate it behind one.
- **The FE error modal has no "Try again" button — by design.** This error is non-retriable from the user's perspective (the gate won't lift on retry); offering retry is misleading.
- **Email tests pass full user dictionaries**, not the slim user object. `EmailHandler.send_email_with_objected_filter_unsubscribe` -> `get_user_email` reads `is_manager`, `email`, `employee_email`, `userid`. Test fixtures must include all four, or `KeyError` (see `_user_dict` helper in `test_email_handler_prevent_notifications.py`).
