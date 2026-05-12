---
name: In-App Upgrade Payment Modal
slug: in-app-upgrade-payment-modal
summary: Authenticated FullScreenPaymentModal for plan upgrades, add-seats, payment-due,
  and churn flows; wires shared form to rootStore.
key_paths:
- app/scripts/infra/pricing/modals/full-screen-payment-modal/**
- app/scripts/infra/pricing/modals/directives/full-screen-payment-page/in-app-payment-form-deps.ts
- app/scripts/infra/pricing/modals/PricingModalService.ts
- app/scripts/categories/benefits/modals/benefits-token-purchase-modal/benefits-token-purchase-modal.tsx
dependencies:
- full-screen-payment-page
parent_feature: full-screen-payment-page
tags:
- frontend
- payments
- pricing
- upgrade
- add-seats
- modal
- mobx
created_at: '2026-04-27'
updated_at: '2026-04-29'
---

## Overview
The authenticated `FullScreenPaymentModal` is the in-app surface that
renders the shared atom-style payment form. It's the entry point for plan
upgrades, add-seats flows, payment-due collection, churn flows, and
benefits-token purchase.

## Architecture
- `full-screen-payment-modal.tsx` / `full-screen-payment-modal-store.ts` —
  Modal shell that owns `paymentData` and constructs the in-app
  `PaymentFormDeps` once via `createInAppPaymentFormDeps(rootStore)`. Hands
  `paymentData` + `deps` to `<FullScreenPaymentPage>` as props.
- `in-app-payment-form-deps.ts` — the only adapter that may import
  `rootStore`, `paymentsApi`, `PricingModalService`, `PricingEventsService`,
  `PaymentsEventsService`, `LocationService`, `IntercomService`,
  `SendErrorEmailService`, `SettingsAPI`. Returns a `PaymentFormDeps`
  populated with live MobX getters.
- `PricingModalService.openFullScreenPaymentModal(...)` — primary entry
  used by upgrade buttons, payment-due flows, back-while-churned modal,
  monthly-to-yearly modal, etc.
- `BenefitsTokenPurchaseModal` constructs `FullScreenPaymentPageStore`
  directly (not via the modal shell) and must pass
  `createInAppPaymentFormDeps(rootStore)` as the third ctor arg.

## Flows
- Open: await rootStore.getPaymentStore() to ensure PaymentStore is loaded; build paymentData with the live paymentStore, paymentRequest, and packages: SelectedPlan; render <FullScreenPaymentPage paymentData deps onClose />.
- Add seats CTA: deps.modals.openAddSeatsModal(...) routes to openAddMoreSeatsModal which consumes the in-app-only addSeatsPaymentStore field.
- BI events: fan out through PaymentsEventsService and PricingEventsService via deps.events.*.
- Settings persistence: SettingsAPI.mutations.updateSettings via deps.api.settings.updateSettings.

## Gotchas
- [CRITICAL] rootStore.getPaymentStore() MUST resolve before constructing FullScreenPaymentPageStore — the in-app adapter relies on rootStore.paymentStore! being populated; forgetting this re-introduces the lazy-load races already guarded against in SelectedPlan.getDefaultPlans.
- The add-seats modal is the only flow that needs the full PaymentStore surface; that's why PaymentFormDataDeps.addSeatsPaymentStore exists — don't fold it back into the narrow paymentStore field.
- BenefitsTokenPurchaseModal is a non-shell entry point that builds the page-store manually; if you change the page-store ctor signature, audit this file too.
- Do not import rootStore from inside the page-store, the page component, the sub-components, or the portable models — only this adapter (in-app-payment-form-deps.ts) may.
- The in-app 'Review and payment' header bar (title + Need help link, with the externalSubscriptionProvider conditional) lives HERE now - defined as a private FullScreenPaymentModalHeader inside full-screen-payment-modal.tsx and passed to <FullScreenPaymentPage header={<FullScreenPaymentModalHeader />} />. The shared payment page no longer renders any header on its own. If you need to change the in-app header copy, wire-up, or the externalSubscriptionProvider branching, edit this file - don't go looking inside full-screen-payment-page.tsx.

## Last Update
- 2026-04-29 - agent - Took ownership of the in-app 'Review and payment' header bar: defined private FullScreenPaymentModalHeader in full-screen-payment-modal.tsx and pass it via the new FullScreenPaymentPage.header prop, since the shared page no longer renders a default header.
