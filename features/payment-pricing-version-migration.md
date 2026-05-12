---
name: Payment Pricing Version Migration
slug: payment-pricing-version-migration
summary: One-shot scripts that move companies from one Chargebee pricing version to
  the next; updates pricing_version in CompanyPayments and rewrites Chargebee subscription
  addons/coupons at end-of-term, with dry-run, gifted-seats, discount, and ARR-estimate
  support.
key_paths:
- scripts/data_migrations/migrations_*/migrate_*_migrate_payments_from_v*_to_v*.py
- scripts/data_migrations/migrations_*/migrate_*_migrate_payments_from_v*_to_v*_companies.py
- v2/features/payment/constants.py
- v2/features/payment/data/company_payment.py
- v2/features/payment/data/coupon_request.py
- v2/features/payment/data/plan_types.py
- v2/features/payment/orm_models/company_payments.py
- logic/webhandler/chargebee/ChargeBee.py
- k8s/cron_jobs.yaml
dependencies: []
tags:
- payments
- pricing
- chargebee
- migration
- backend
- one-shot-script
- cron
created_at: '2026-04-29'
updated_at: '2026-04-29'
---

## Overview
Whenever Connecteam introduces a new Chargebee pricing version (v1 → v2 → v3 → …), a one-shot data-migration script moves an explicit list of legacy companies onto the new version. The script lives under `scripts/data_migrations/migrations_<year>/` and is paired with a `_companies.py` file that lists the target companies (and per-company overrides like discount or gifted seats).

The canonical reference implementations are:

- **V1→V2** (2024-04-30): `scripts/data_migrations/migrations_2024/migrate_2024_04_30_migrate_payments_from_v1_to_v2.py` — the most complete reference; has discount, gifted seats, NPO coupon carry-over, ARR estimation, dry-run.
- **V2→V3** (2025-11-09, deprecated): `scripts/data_migrations/migrations_2025/migrate_2025_11_09_migrate_payments_from_v2_to_v3.py` — incomplete; missing gifted seats, discount, coupon management; uses `print` and a manual `_v3` addon-id patch.
- **V2→V3** (2026-04-13, current): `scripts/data_migrations/migrations_2026/migrate_2026_04_13_migrate_payments_from_v2_to_v3.py` — combines V1→V2 robustness with V3 OPS-only addon logic.

## Architecture
Each migration is a class (`MigratePaymentsFromV<N>ToV<N+1>`) with this lifecycle per company:

1. **`init()`** — load `last_payment` (`BasePaymentLogic.get_last_payment`), `current_plan` (`CompanyData.get_company_current_plan`), build a `ChargeBee` logic instance, call `init_base_seats()`, count exceeded seats. Set `skip_company` if already on the target version or not on a renewable plan (`MONTHLY`/`YEARLY`).
2. **`init_base_seats()`** — derive gifted seats from DB: `max(last_payment.base_seat_count - BASE_SEAT_COUNT, 0)`. Add to `self.base_seats` so `exceeded_seats` is computed against the gifted base, not the default 30.
3. **`coupons` property** — preserve any existing NPO coupons from `last_payment.current_coupons` (a `CouponRequest` with `ops`/`comms`/`hr`/`legacyOps`/`legacyComms` slots), then append `CC-COUPON{discount}PERCENT-{HUB}` per non-free hub if `self.discount > 0`.
4. **`get_payload(new_account=False)`** — build the Chargebee subscription payload. Calls `chargebee_logic.get_addons_with_price_update(current_plan, exceeded_seats, additional_addons, target_version)`. For an existing subscription, sets `end_of_term=True`, `replace_addon_list=True`, `replace_coupon_list=True`, `force_term_reset=False`. The `new_account=True` variant strips coupons and the subscription id — used only for ARR estimation.
5. **`get_estimate()`** — calls Chargebee `Estimate.update_subscription` (amount due at next renewal) and `Estimate.create_subscription` (clean ARR). Sets `skip_company` if `next_billing_at > now + 24h` (only migrate companies due to renew within 24h). Both numbers go into the result dict for auditing.
6. **`update_company_plan()`** — DB-first then Chargebee. Inserts a new `CompanyPayments` row via `CompanyPayments.build_from_payment(company, last_payment)`, then sets `pricing_version = '<target>'` and `base_seat_count = self.base_seats`. Commits, then calls `chargebee.Subscription.update(subscription_id, payload)`. **Rolls back the new DB row if Chargebee fails.** No-op when `is_dry_run` is True.
7. **`migrate()`** — orchestrates the above and returns a rich result dict: `company`, `status`, `amount_due`, `arr`, `discount`, `base_seats`, `exceeded_seats`, `coupons`. On exception returns `{status: 'failed', error: str(e)}`.
8. **Batch runner + email** — `migrate_companies()` iterates the companies list and emails an HTML summary report (success/failed tables) to the admins after the run.

## Flows
- **Adding a new pricing version (e.g. v3 → v4):** add `PRICE_BY_PLAN_AND_CHARGE_V4` and (if hub-specific) `SEAT_PRICE_BY_HUB_AND_PLAN_V4` to `v2/features/payment/constants.py`. Bump `CURRENT_PRICING_VERSION`. If the new version changes any hub's seat addons, add the hub to `PRICING_ADDONS_VERSIONS = {BundlePlan.<HUB>: ['v4', ...]}` so `get_addons` auto-suffixes the user-addon ids. Then upload the new addons to Chargebee via a `migrate_*_upload_*_chargebee_addons.py` script before running the migration.
- **Writing the migration script:** copy the latest robust reference (currently the 2026-04-13 v2→v3) and change three things: (a) the target version string passed to `get_addons_with_price_update` and written to `CompanyPayments.pricing_version` and `cf_pricing_version`; (b) the import of the `_companies.py` companion file; (c) the skip guard from `pricing_version == 'v<N>'` to the target.
- **Building the companies list:** the input is usually one or more CSVs from the pricing team. Parse with `csv.DictReader` (use `encoding='utf-8-sig'` for the monthly file BOM), filter empty trailing rows, sort by company id, and emit a list of `{"company": <id>, "discount": <int>}` dicts. Standard discount is 5 (CS support). Save next to the migration script as `<migration>_companies.py`.
- **Running the migration:** schedule via `k8s/cron_jobs.yaml` inside the `{% if env_type == 'production' %}` block, daily at low-traffic UTC time. The 24h billing-date guard means daily reruns only touch companies whose renewal is imminent; the `pricing_version == target` skip guard means already-migrated companies are no-ops. Remove the cron entry once the company list is exhausted.
- **Dry-run vs wet-run:** `is_dry_run = True` runs everything except the DB insert and Chargebee update — only estimates and logs. Always do a dry-run first, eyeball the ARR numbers against the source CSV, then flip to `False`.

## Gotchas
- **[CRITICAL] Order matters in `update_company_plan()`:** insert+commit the new `CompanyPayments` row *before* calling Chargebee, and on Chargebee failure delete the row and commit again. Reverse order leaves Chargebee mutated with no DB record.
- **[CRITICAL] Always pass the correct `pricing_version` to `get_addons_with_price_update`.** It drives the `_v<N>` suffix on hub user-addon ids via `PRICING_ADDONS_VERSIONS` in `v2/features/payment/constants.py`. The deprecated 2025 v2→v3 script passed `'v2'` and then string-patched `_v3` onto OPS user addon ids — do not copy that pattern; pass the target version instead.
- **Gifted seats live in `last_payment.base_seat_count`, not in the input CSV.** The 2025 v2→v3 script forgot this and would have charged gifted-seat customers for those seats as overages. Always derive: `max(last_payment.base_seat_count - BASE_SEAT_COUNT, 0)`.
- **24h billing-date guard is required.** Without it, end-of-term changes get scheduled far in the future where pricing or plan state may drift before they apply. Skip companies whose `next_billing_at > now + 24h`.
- **Skip non-renewable plans.** Only `Plan.MONTHLY` and `Plan.YEARLY` are migrated; `FREE`, `TRIAL`, `ENTERPRISE`, `FREEZE`, `CHURN`, SBP, etc. are out of scope and would break the addon-build flow.
- **`replace_coupon_list=True` + `replace_addon_list=True` are intentional.** They wipe the existing lists and replace with what the script computed. The `coupons` property must therefore re-emit any coupon you want to keep (NPO carry-over) — anything not in the list is dropped at next renewal.
- **Coupon naming is convention-based:** `CC-COUPON{int}PERCENT-{COMMS|OPS|HR}`. The discount integer comes from the per-company input; only emitted for hubs that aren't `PlanCharge.FREE`. NPO coupons are detected by substring match `'NPO' in coupon`.
- **`get_amount_due` reports annualized values.** Yearly plans return `estimate.amountDue` as-is; monthly plans return `amountDue * 12`. Use this when comparing against an ARR column in the source CSV.
- **Use `logger`, not `print`.** The first v2→v3 script used `print` — output never made it to structured logs. All scripts must use `logging.getLogger('payments_migration_<from>_to_<to>')` and `app_logger.init_logger(...)` in `__main__`.
- **CSV encoding pitfall:** the monthly OPS price-increase CSV has a UTF-8 BOM that makes the `Company` column read as `'\ufeffCompany'` with default encoding. Use `encoding='utf-8-sig'`.
- **`CompanyPayments.build_from_payment` clones a CompanyPayment (data class) into a new ORM row** — it does *not* duplicate `pricing_version` or `base_seat_count` correctly for a migration; you must overwrite both explicitly after `build_from_payment(...)` and before `session.add(...)`.

## Last Update
- **Date:** 2026-04-29
- **Author:** rafael
- **Change:** Initial memory created from V1→V2 (2024) and V2→V3 (2025 deprecated, 2026 current) migrations.
