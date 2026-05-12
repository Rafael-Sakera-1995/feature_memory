---
name: Full Screen Payment Page
slug: full-screen-payment-page
summary: Self-managing payment form + PaymentFormDeps DI contract shared by the in-app
  upgrade modal and the public /billing page.
key_paths:
- app/scripts/infra/pricing/modals/directives/full-screen-payment-page/**
- app/scripts/infra/pricing/models/BillingInfo.ts
- app/scripts/infra/pricing/models/selected-plan.ts
- app/scripts/infra/pricing/pricing.types.ts
dependencies: []
tags:
- frontend
- mobx
- dependency-injection
- payments
- pricing
- atom-component
created_at: '2026-04-27'
updated_at: '2026-04-29'
---

## Overview
`FullScreenPaymentPage` is an atom-style React component that renders the
multi-step payment UI (plan picker → billing info → card → review) and
internally constructs/disposes its own `FullScreenPaymentPageStore`. It
runs in two contexts wired via the same `PaymentFormDeps` DI contract:

- Authenticated in-app `FullScreenPaymentModal` (plan upgrades, add seats,
  payment-due, churn, benefits-token purchase) → `createInAppPaymentFormDeps(rootStore)`.
- Public unauthorized `/billing?link=...` page → `createExternalPaymentFormDeps(externalData)`.

The component, its store, sub-components, and the portable models never
import `rootStore`, `paymentsApi`, BI services, modal services, or any
other authenticated globals. Everything is reached through injected deps.

## Architecture
- `full-screen-payment-page.tsx` — the atom-style component. Builds the
  `FullScreenPaymentPageStore` with `useMemo`, disposes in `useEffect`,
  wraps children in `PaymentFormDepsProvider`.
- `full-screen-payment-page-store.ts` — MobX store driving data fetching,
  plan state, BillingInfo, and payment submission. Uses `MobxQuery` /
  `MobxMutation` from `deps.api.payments`. Has a MobX reaction on
  `paymentData.packages` so callers don't have to fire imperative methods.
- `payment-form-deps.ts` — the DI contract: `data` (planStore,
  paymentStore, addSeatsPaymentStore?, companySettings, currentUser,
  companyName, externalSubscriptionProvider), `api.payments`, `api.settings`,
  `modals`, `events`, `telemetry`, `location`, `intercom`. Also exports
  `PaymentFormDepsProvider` and `usePaymentFormDeps()` for sub-components.
- `payment-form-payment-context.ts` — narrow interface for the
  `PaymentData.paymentStore` shape the form actually consumes.
- `payment-form-plan-context.ts` — narrow interface for `PlanStore` reads
  (`isMonthly`, `isChurn`, `isTrial`, `lastPlanId`, `companyPlan`).
- `components/billing-info-component.tsx` and `components/full-payment-screen-summary-component.tsx`
  use `usePaymentFormDeps()` to access deps without prop-drilling.

Portable models the form relies on:

- `BillingInfo` — constructor takes `companyName: string` (in-app:
  `rootStore.companyName`; external: payment-request payload).
- `SelectedPlan` — `getDefaultPlans()` has a runtime guard returning neutral
  defaults when `rootStore.paymentStore` / `rootStore.companySettings`
  aren't populated yet.

## Flows
- Mount: component builds the page-store with useMemo, disposes in useEffect, sets up MobX reactions on paymentData.packages, and fetches current payment / customer / billing-address through deps.api.payments.
- Sub-component reads (BillingInfoComponent, FullPaymentScreenSummaryComponent): use usePaymentFormDeps() inside the PaymentFormDepsProvider boundary; never reach into globals.
- Settings save (in-app only): deps.api.settings.updateSettings(...) invalidates the query cache; external adapter no-ops.
- Pay: deps.api.payments.mutations.createByPaymentRequestId is the primary mutation; createPayment / changePayment are in-app-only paths.

## Gotchas
- [CRITICAL] Form code (full-screen-payment-page.tsx, full-screen-payment-page-store.ts, sub-components, portable models) MUST stay free of rootStore, paymentsApi, PricingModalService, PricingEventsService, PaymentsEventsService, IntercomService, SendErrorEmailService — only *-payment-form-deps.ts adapters may import them.
- [CRITICAL] SelectedPlan.getDefaultPlans() has a defensive guard returning [YEARLY, FREE, FREE, FREE] when rootStore.paymentStore or rootStore.companySettings aren't populated; do not remove without re-checking the external /billing page (it's the only thing keeping the unauthenticated context from crashing on `new SelectedPlan(...)`).
- BillingInfo constructor requires companyName (string); every caller (including dashboard-modal-service.ts) must pass it.
- PaymentFormDataDeps.addSeatsPaymentStore is the FULL PaymentStore and is in-app-only; the narrow paymentStore: PaymentFormPaymentContext is what the rest of the form uses, and the external adapter never sets addSeatsPaymentStore.
- paymentData.paymentStore is typed as PaymentFormPaymentContext in pricing.types.ts; full-screen-payment-modal-store.ts bridges with `as unknown as PaymentData` deliberately — do not 'fix' by re-broadening the type.
- The external updateSettings adapter is a no-op by design; the billing address still reaches the BE through the payment-request mutation.
- The narrow PaymentFormPaymentContext / PaymentFormPlanContext interfaces have a real second consumer now (createExternalPaymentFormDeps in @external-payment); broadening or removing fields from these interfaces silently breaks the public /billing page until you update its adapter too. Always grep both adapters before touching the contract.
- FullScreenPaymentPage NO LONGER renders any header itself - the previous in-component default header was removed. Callers must pass header?: ReactNode if they want one (in-app modal passes its private FullScreenPaymentModalHeader; the public /billing page intentionally passes nothing). An earlier API used a hideHeader: boolean prop with the header baked in; that shape was rejected because the external page needed full content control, not just suppression.
- Two new optional slot props - paymentStepFooter?: ReactNode and billingInfoTitle?: ReactNode - let callers extend the form without forking it. billingInfoTitle is forwarded to BillingInfoComponent.title and overrides the default 'Billing address' heading; the external /billing page uses it for 'Billing info: {company name}' while the in-app modal omits it. Don't add company-name rendering to BillingInfoComponent's default - keep the seam at the prop or in-app copy will silently change too.

## Last Update
- 2026-04-29 - agent - Component no longer owns its header - replaced the boolean hideHeader prop with header?: ReactNode (caller-provided, omitted = no header); added paymentStepFooter?: ReactNode and billingInfoTitle?: ReactNode slot props (the latter forwarded to BillingInfoComponent.title to allow per-context heading overrides without forking the form).
