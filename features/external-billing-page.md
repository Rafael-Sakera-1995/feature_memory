---
name: External Billing Page
slug: external-billing-page
summary: Public unauthorized /billing?link=... page that lets non-Connecteam recipients
  pay an existing payment-request via the shared payment form. Backed by 5 link-authenticated
  Tornado endpoints under /api/External/Payment/PaymentRequest/ with a 5-attempt failed-payment
  lockout.
key_paths:
- app/scripts/categories/external-payment/**
- app/scripts/base-line-react/components/routers/unauthorized-routes.ts
- app/scripts/services/route/router-service.ts
- app/scripts/services/http/Endpoints.js
- app/scripts/infra/types/feature.ts
- v2/features/payment/api/external/**
- v2/features/payment/external_payment_request_logic.py
- v2/features/payment/orm_models/CompanyPaymentRequests.py
- v2/features/payment/PaymentRequestLogic.py
- v2/features/payment/tests/test_external_payment_request_logic.py
- ct_services/product_matrix_sync/routes/billing.py
- ct_services/product_matrix_sync/schemas/billing/payment_subscription.py
- ct_services/product_matrix_sync/tests/routes/test_billing.py
- logic/webhandler/chargebee/ChargeBee.py
- v2/features/payment/data/PaymentRequestCreateRequest.py
- v2/features/payment/emails/PaymentRequestEmail.py
- v2/features/payment/emails/html/PaymentRequestEmail.html
- v2/features/payment/tests/test_payment_request_email.py
- locales/en.json
- app/scripts/base-line/boarding/models/CompanyPaymentRequest.ts
- app/scripts/infra/pricing/modals/directives/full-screen-payment-page/components/full-payment-screen-summary-component.tsx
- app/scripts/infra/pricing/modals/directives/full-screen-payment-page/components/full-payment-screen-summary-utils.ts
- app/scripts/infra/pricing/modals/directives/full-screen-payment-page/components/full-payment-screen-summary-utils.test.ts
dependencies:
- full-screen-payment-page
parent_feature: full-screen-payment-page
tags:
- frontend
- payments
- unauthorized-route
- pay-by-link
- external
created_at: '2026-04-27'
updated_at: '2026-04-29'
---

## Overview
The public `/billing?link=...` page lets people who are NOT logged into
Connecteam pay an existing payment-request. It's registered as an
unauthorized route alongside `/apply` (external hiring) and reuses the
same atom-style payment form the in-app modal uses, wired to a stubbed
adapter built from a public payment-request payload.

Today the only supported flow is "pay an existing payment-request". Plan
upgrades, add-seats, change-card, and similar mutations are unsupported
and are either hidden by the read-only payment-request mode or throw
`unsupported()` if invoked.

## Architecture
New code lives under `app/scripts/categories/external-payment/`:

- `pages/external-billing-page/external-billing-page.tsx` — the public page.
  Loads `ExternalPaymentRequest` via `externalPaymentApi`, builds a real
  `SelectedPlan` from the payload (so `paymentData.packages` is concrete),
  and renders `<FullScreenPaymentPage>`.
- `services/external-payment-form-deps.ts` —
  `createExternalPaymentFormDeps(externalData)` returns a `PaymentFormDeps`
  where:
  - `data.planStore` and `data.paymentStore` are structural stubs that
    satisfy `PaymentFormPlanContext` / `PaymentFormPaymentContext`.
  - `data.companyName` and `data.currentUser` come from the payload.
  - `data.companySettings` is freshly instantiated with empty address
    fields and `featureFlagSettings.isUpgradeDisabled = false`.
  - `addSeatsPaymentStore` is intentionally absent.
  - `api.settings.updateSettings` is a no-op.
  - `modals.openCantChargeModal` is a TODO; the rest throw `unsupported()`
    or are `noop`.
  - `events.*`, `telemetry.*`, `intercom.*`, `location.*` are no-ops.
- `apis/external-payment-api.ts` — frontend-defined API surface for the
  public payment-request endpoints.

Wiring outside the directory:

- `unauthorized-routes.ts` registers `{ id: 'externalBilling', path: '/billing', component: ExternalBillingPage, kind: 'neutral', feature: 'login' }`.
- `router-service.ts` adds `'externalBilling'` to `UnauthorizedRouteId`.
- `Endpoints.js` exposes `nonAuthenticatedEndpoints.payment.externalPaymentRequest`
  and `.externalPay` — FE-defined placeholders pending backend implementation.
- Path alias `@external-payment` is registered in `tsconfig.json`,
  `jest.config.ts`, `vitest.config.ts`, `rspack.config.js`,
  `.storybook/main.ts` (mirrors `@hiring`).

## Flows
- Visit /billing?link=<token>: page loads ExternalPaymentRequest via externalPaymentApi, builds externalCompanyPlan = new CompanyPlan(...) and externalSelectedPlan = new SelectedPlan(externalCompanyPlan), assembles paymentData with from: 'payment-request' and the companyPaymentRequest, then renders <FullScreenPaymentPage paymentData deps={createExternalPaymentFormDeps(externalData)} onClose />.
- Form renders in read-only payment-request mode: the companyPaymentRequest.isPaymentRequestValidForPlan() === true stub keeps the plan picker disabled.
- Visit /billing?link=<uuid>: page calls GET External/Payment/PaymentRequest/?link=... and switches on response.status. payable renders FullScreenPaymentPage; already_paid/expired/invalid/unavailable render the matching status sub-component; missing or empty link renders the same screen as 'invalid'; network failure on GET / also collapses to 'invalid' (no useful retry path for the recipient).
- Render payable: page builds new CompanyPlan(plan, opsPlan, commsPlan, hrPlan), new SelectedPlan(companyPlan), new BillingInfo(companyName), createExternalPaymentFormDeps(context); assembles paymentData with from='payment-request' and paymentRequest set so the form runs in read-only payment-request mode (plan picker, add-seats, and change-card CTAs are all hidden by isPaymentRequestValidForPlan).
- Form bootstraps: FullScreenPaymentPageStore constructs MobxQuery/MobxMutation against the wrapped external mutations; braintreeClientToken query routes to GET External/Payment/PaymentRequest/ClientToken/?link=...; getCoupon and getAmountDue route to External/Payment/PaymentRequest/Coupon/ and External/Payment/PaymentRequest/AmountDue/ with link auto-injected from the closure.
- Pay: form-store calls deps.api.payments.mutations.createByPaymentRequestId({ token, billingAddress, additionalSeatCount, is3dsAuthenticated }) which the adapter rewrites as POST External/Payment/PaymentRequest/Subscribe/ with link auto-injected; on success the page state flips to paymentSucceeded=true and renders the AlreadyPaidScreen synchronously to avoid a refetch flicker.
- Pay failure: BE bumps failed_payment_attempts and re-raises the gateway exception so the FE sees the same error shape as the in-app modal; once the counter reaches 5 the next GET / returns status='invalid' (lockout deliberately collapsed into the generic invalid screen so a brute-forcer can't probe the boundary).
- BE non-PAYABLE on action endpoints: any mutation against an already_paid/expired/invalid/unavailable link returns HTTP 410 (Gone) with reason='payment-request-not-payable:<status>' so the FE knows to refetch GET / and re-render the matching screen rather than treating it as a generic error.

## Gotchas
- [CRITICAL] Supported flow is ONLY paying an existing payment-request — plan-change / add-seats / change-card CTAs must remain hidden by the read-only companyPaymentRequest mode; do not loosen this without shipping a real public auth model first.
- The external adapter MUST NOT throw when rootStore is unpopulated — the defensive guard inside SelectedPlan.getDefaultPlans is what enables `new SelectedPlan(externalCompanyPlan)` on this page; do not remove it.
- updateSettings is a no-op by design (external recipients have no auth to mutate company settings); the billing address still reaches the BE through the payment-request mutation.
- CompanySettings is constructed locally with empty companyDetails and featureFlagSettings.isUpgradeDisabled = false; if a new field becomes required by BillingInfoComponent or downstream code, populate it here.
- The page mirrors the existing external-hiring /apply pattern; future external public pages should follow the same shape (@<domain> alias, unauthorized route, categories/external-* directory, dedicated *-form-deps adapter).
- [CRITICAL] BE intentionally collapses unknown-link, malformed-link, and brute-force lockout (failed_payment_attempts >= 5) into status='invalid' so a brute-forcer cannot probe the lockout boundary; FE mirrors this opacity by rendering the same 'invalid' copy in all three cases AND on network failure - do not split into per-cause messages without first re-evaluating the threat model.
- addSeatsPaymentStore is intentionally undefined in the external adapter; the page-store has an early-return guard for openAddSeatsModal() that handles this gracefully. If you re-broaden the type to required, the page will crash on first add-seats CTA render.
- Public recipients have no Connecteam identity, so /Subscribe/ attributes the charge to the admin who created the payment-request (request.userId loaded via UserLogicV2.get_user_by_id with suspended=1 to keep attribution working post-deactivation). If the admin can't be loaded, current_user falls back to {id: admin_id} so the BI/error pipeline still has a usable id.
- All non-GET / external handlers extend BaseExternalPaymentRequestHandler which short-circuits non-PAYABLE statuses with HTTP 410 (Gone) carrying reason='payment-request-not-payable:<status>'. The FE expects this and refetches GET /; do NOT change to 4xx-other or the FE will treat it as a generic error.
- GET / response is intentionally redacted: only the whitelist in external_payment_request_handler.py::_PUBLIC_PAYMENT_REQUEST_FIELDS reaches the wire. Adding fields without updating the whitelist drops them silently; adding sensitive fields (admin user_id, internal pricing, etc.) is a security regression - do not bypass the whitelist.
- feature: 'billing' was added to ConnecteamFeature for this route (Bugsnag breadcrumbs); the precedent /apply uses 'hiring' and there's no shared 'payments' feature. Reuse 'billing' for any future public payment surfaces.
- External adapter wraps getCoupon and getAmountDue to drop the in-app caller's bundles arg in favor of the row's stored plan - the BE rebuilds the plan from the row anyway, so passing untrusted client-supplied bundles would just be ignored. If you ever need bundle overrides for the public page, change BE first; the wrap is the boundary.
- _add_payment_request gained an is_external=False kwarg that mints a UUID4 link when True; ALL existing in-app callers must continue to omit it (or pass False) so admin-created requests stay link-less and only land in the in-app modal.
- [CRITICAL] The recipient-side flow is fully wired, but the admin-side mint flow is NOT: PaymentRequestLogic._add_payment_request accepts is_external=True (mints the UUID link), but no caller passes True yet. The Matrix POST /Matrix/Admin/PaymentRequest/ endpoint still always creates in-app-only requests. Until that route is extended (planned: make userId optional + add receiverEmail; is_external = userId is None), there is no production path to actually create a payable external request - the page is reachable but nothing legitimate will land at status='payable'.
- PaymentRequestLogic.create(...) returns the 2-tuple (payment_request, deep_link_url) - the public /billing?link=... URL is NOT in that return value. Callers that need the public URL (today: only the Matrix POST /Matrix/Admin/PaymentRequest/ route when userId is None) must derive it after-the-fact via the public classmethod PaymentRequestLogic.build_external_payment_url(payment_request), guarded on payment_request.link being populated. An earlier refactor that broadened create() into a 3-tuple was rolled back precisely because ~17 in-app callers would have had to be updated; do not re-broaden the signature.
- All terminal FE states (already_paid / expired / invalid / unavailable / immediate post-payment success / GET / network failure) intentionally render the SAME LinkUnavailableScreen ('This link has expired.' + hourglass illustration). The recipient cannot distinguish causes by design - this mirrors the BE INVALID-collapse anti-brute-force posture. Splitting copy per status undoes both the security and UX intent; if you need to differentiate, change the threat model first.
- The 'Billing info: {company name}' heading is EXTERNAL-ONLY: external-billing-page.tsx passes it via the new FullScreenPaymentPage.billingInfoTitle prop, which forwards to BillingInfoComponent.title. The in-app upgrade modal does NOT pass this prop and therefore continues to render the long-standing 'Billing address' heading. Don't move the company-name title into BillingInfoComponent's default - it would silently change in-app copy too. Also: the page passes undefined when companyName is empty so we never render a dangling 'Billing info:' colon.
- [CRITICAL] Future-dated billing (`startPaymentDate`) uses ACTIVATE-NOW semantics, NOT defer-activation: when the recipient redeems, `_subscribe_customer` runs unchanged and immediately flips the company to the paid plan via `_update_company_payment` + `activate_features_by_plan_upgrade`; only the FIRST CHARGEBEE INVOICE is deferred via `request['start_date']`. Net effect: the company gets paid-plan service from redemption day until the actual charge date (free service in between). If product ever wants real defer-activation, you must wire a `subscription_activated` webhook handler and stop activating in the synchronous path - simply changing `billingStartDate` is NOT enough.
- `startPaymentDate` is end-to-end EXTERNAL-ONLY by enforcement at the matrix route boundary: `Matrix POST /Matrix/Admin/PaymentRequest/` rejects with HTTP 400 when `startPaymentDate` is set alongside a `userId` (in-app request), and also rejects when `startPaymentDate <= now()`. The in-app modal has no UI for 'you'll be charged on X' and no FE branch was wired - lifting the `userId is None` guard without also building that UI will result in users paying immediately while the BE quietly schedules the charge, which is the worst of both worlds.
- [CRITICAL] PaymentRequestEmail.html is SHARED between `PaymentRequestEmail` (in-app: sent to admins via `_send_payment_request_email`) and `ExternalPaymentRequestEmail` (public /billing recipients via `_send_external_payment_request_email`). The two classes only differ by `email_type` (BI label) and the lang arg (in-app uses manager.device_locale, external defaults to 'en'). ANY visual/copy change to the template ships to BOTH flows. The Figma offer-email spec note 'Same content as email sent to people with access to system' codifies this on purpose - if you ever need to diverge, fork into two HTML files AND override `email_template` on `ExternalPaymentRequestEmail`, otherwise the test `test_external_payment_request_email_should_render_identical_html_to_in_app_email` will catch the drift.
- The 'Company name' row sits inside the .pricing table as the first row (no icon - the spec uses a building/columns glyph but no equivalent exists in s3://connecteam.content.assets/icons; add the path to `get_company_name_row` once design supplies one in the same bucket). The company name passes through `self.escape(...)` because it's user-controlled - DO NOT remove the escape, see `test_should_html_escape_company_name_in_pricing_table_row`.
- The future-charge indicator lives in the right-side payment summary panel (`FullPaymentScreenSummaryComponent.renderChargeOnLine`, between Total and the Confirm-payment CTA) - NOT in the email and NOT in the left-column footer of the external billing page. Per PM, the email is intentionally silent about the scheduled charge; the recipient first learns the charge date when they reach the payment screen. The stale-date guard is centralized in `getChargeOnDateLabel` (`full-payment-screen-summary-utils.ts`), symmetric with the BE guard in `build_create_payment_request_from_request_id` (two copies total instead of the previous three).
- Recipient-side stale-link defence: `build_create_payment_request_from_request_id` silently drops a past `startPaymentDate` back to `billingStartDate=None` (immediate billing) if a stale link is redeemed after the date passes - Chargebee would 400 on a past `start_date`. The FE mirrors this in `getChargeOnDateLabel` (`full-payment-screen-summary-utils.ts`): the helper returns `null` when `startPaymentDate <= moment().unix()`, hiding the summary 'Charge on' row instead of rendering a misleading past date. If you change one side of this guard, change both.
- `startPaymentDate` flows BE -> public GET payload -> `external-billing-page.tsx` -> `buildLegacyPaymentRequest(payload)` (`external-payment-form-deps.ts`) -> `CompanyPaymentRequest` boarding model -> `FullScreenPaymentPageStore.companyPaymentRequest` -> `FullPaymentScreenSummaryComponent.renderChargeOnLine`. Any new consumer of the boarding `CompanyPaymentRequest` model gets `startPaymentDate` for free; in-app paths never populate it (Matrix BE rejects future dates with HTTP 400 when `userId` is set), so the new summary 'Charge on' row stays hidden in the in-app upgrade modal by construction (no extra `paymentData.from` gate needed).

## Last Update
- 2026-04-29 - agent - Moved future-charge indicator out of PaymentRequestEmail.html and out of the external-billing-page left-column footer; added a 'Charge on | <date>' row to the right-side `FullPaymentScreenSummaryComponent` between Total and the Confirm-payment CTA. New `getChargeOnDateLabel` helper (`full-payment-screen-summary-utils.ts`) with 5 jest unit tests centralizes the stale-date guard. `startPaymentDate` now plumbs through the boarding `CompanyPaymentRequest` model via `buildLegacyPaymentRequest`. Backend dropped `start_payment_date` from `PaymentRequestEmailParams`, removed the `PAYMENT_REQUEST_EMAIL.SCHEDULED_CHARGE` translation key, and trimmed the 3 scheduled-charge tests in `test_payment_request_email.py` (3 remaining tests still cover company-name row + escape + in-app/external HTML parity). Email still carries the company-name row (separate Figma item, kept). Added `PLANS_MODAL.PAYMENT.CHARGE_ON` to the FE locales.
