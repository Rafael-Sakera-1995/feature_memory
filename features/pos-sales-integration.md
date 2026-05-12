---
name: POS Sales Integration
slug: pos-sales-integration
summary: POS sales sync (Lightspeed/Clover/Shopify/Square) into mobi_sales + daily
  aggregation rollup users see.
key_paths:
- pymobiengine/ct_services/integration_service/clients/common/sales_data_integration/**
- pymobiengine/ct_services/integration_service/clients/light_speed/**
- pymobiengine/ct_services/integration_service/clients/clover/**
- pymobiengine/ct_services/integration_service/clients/shopify/**
- pymobiengine/ct_services/integration_service/clients/square/**
- pymobiengine/ct_services/integration_service/clients/pos/**
- pymobiengine/v2/features/sales/**
- pymobiengine/worker/sales/**
dependencies: []
tags:
- pos
- sales
- integration
- lightspeed
- clover
- shopify
- square
created_at: '2026-04-30'
updated_at: '2026-04-30'
---

## Overview
Four POS providers (Lightspeed, Clover, Shopify, Square) sync sales transactions into Connecteam. Each provider has a `*SetupLogic` extending the abstract `SalesDataPeriodicIntegrationSetupLogic` and an SDK client that converts the provider's raw API into `SaleData`. Raw transactions land in `mobi_sales`; a background worker rolls them up into `mobi_sales_daily`, which is what dashboards/reports read.

Default calculation mode is `NET_SALES` (sales minus refunds). `GROSS_SALES` excludes refunds.

## Architecture
- **Setup base:** `SalesDataIntegrationSetupLogic` (abstract) owns `import_sales_locations()` and `import_sales(start, end)`. Per-provider subclasses only implement `_get_sdk_client`, `_get_locations_data`, `_get_transactions_data`.
- **Periodic scheduling:** `prepare_settings_creation` registers a daily `MultiStageType.POS_DATA_IMPORT` periodic; `on_disconnect` deletes it. One-off backfill runs via `create_and_start` when the user provides `init_query_start_date`/`init_query_end_date`.
- **Multi-stage handler:** `SalesMultiStageDataImport` (period = 1 day). Subtask 0 also runs `import_sales_locations()`. For recurring runs without timestamps, resumes from the latest `SALES_TRANSACTION` entity-mapping (or now-1day fallback).
- **Sales SDK boundary:** All writes/reads of `mobi_sales`, `mobi_sales_location`, and transactions go through `CTSalesSDK` (not direct DB writes from the integration layer). `import_sales` deletes the location/window before re-inserting (idempotent re-import).
- **Storage (regular MySQL tables, NOT spatial despite the naming):**
  - `mobi_sales` — one row per transaction. Cols include `sale_type`, `provider`, `location_id` (internal), `created_at`, `sale_amount`, `tax_amount`, `currency`, `is_addition_to_total`.
  - `mobi_sales_location` — per-company locations with both external `location_id` (string) and internal numeric `id`. External↔internal mapping also lives in `entity_mapping` (`EntityType.SALES_LOCATION`).
  - `mobi_sales_daily` — pre-aggregated `(company, location_id, date)` with `total_sales` and `projected_sales` (unique constraint on the triple).
- **Aggregation worker:** `SalesAggregationRunner` subscribes to topic `SALES_DAILY_AGGREGATION_REQUESTED`. Triggered by `SalesDataLogic.add_sales`/`delete_sales` via `SalesAggregationPublisher`. Runs `SalesDailyAggregationLogic.calculate_and_upsert_daily_aggregations` under a per-company `BlockingRedisLock`.
- **Daily total formula:** two SQL `GROUP BY date(...)` queries on `mobi_sales` (split by `is_addition_to_total`) bucketed in **company timezone** via MySQL `convert_tz`; positive sums minus negative sums → daily net per location.
- **Positive/negative classification:** `SaleData` validator: POSITIVE = `[SALE, SPLIT]`, NEGATIVE = `[REFUND, TRANSFER, VOID]`. Manual edits set `is_addition_to_total` explicitly.
- **Sale-type filter sets:** `NET_SALES = [SALE, SPLIT, TRANSFER, REFUND, VOID, MANUAL]`; `GROSS_SALES = [SALE, SPLIT, TRANSFER, VOID, MANUAL]` (REFUND excluded).

## Flows
- **Connect provider → first sync:** OAuth → `prepare_settings_creation` → daily POS_DATA_IMPORT periodic registered. If user picked `init_query_start_date`/`init_query_end_date`, one-off backfill task is started, processing one day per subtask.
- **Each daily subtask:** `_get_locations_data` (subtask 0) → upsert via Sales SDK + `entity_mapping` (`SALES_LOCATION`) → `_get_transactions_data` per enriched location → delete that day's existing rows for the location → batch-create transactions in chunks of **100** via `ct_sdk.add_transactions` → store latest `SALES_TRANSACTION` entity-mapping for resume.
- **Aggregation update:** `SalesDataLogic.add_sales`/`delete_sales` publishes `SALES_DAILY_AGGREGATION_REQUESTED` → `SalesAggregationRunner` (Redis-locked per company) recomputes `mobi_sales_daily` for the affected window. Days that previously had data but no longer do are explicitly set back to NULL.
- **Manual edit (Dashboard source):** `apply_manual_sales_edit` creates synthetic `SaleType.MANUAL` rows in `mobi_sales` with `provider=DASHBOARD` and explicit `is_addition_to_total`, then directly upserts `mobi_sales_daily.total_sales`/`projected_sales` (skips the aggregation publisher).
- **Disconnect:** `on_disconnect` updates settings + deletes the POS_DATA_IMPORT periodic.
- **Lightspeed:** `GET /f/finance/{loc}/financials/{start}/{end}?include=payments`. Inclusion = any sale with a `receiptId` (no status filter). `sale_type` is **passed through directly from Lightspeed's `type` field** (so this is the only provider that can produce `TRANSFER`/`VOID` in our system). Amount = `SUM(salesLines.totalNetAmountWithoutTax)` (net of tax, abs). Tax tracked separately. **Currency hardcoded `USD`** regardless of location currency. Includes inactive locations.
- **Clover:** Single location per merchant — hardcoded as `"Clover POS"` country `US`, timezone `America/New_York`. Three parallel endpoints per day: `/orders`, `/refunds`, `/credits`. Orders included only if `state == "locked" AND total > 0`. `payType` mapping: `FULL → SALE`, `SPLIT_* → SPLIT`. Order amount = `order.total/100` (**includes tax and tips**). Tax computed separately by per-order GET (sums first taxRate of each line item only). All refunds and all credits subtract (credits are mapped to `SaleType.REFUND`).
- **Shopify:** GraphQL `2025-07`. Up to 10 store locations (includes inactive). **All orders attributed to `sales_locations[0]`** — multi-location stores roll everything under the first location. Filter: `processed_at` window AND `(financial_status:paid OR partially_refunded)` AND `(status:open OR closed)`. Excludes pending/authorized/partially_paid/fully_refunded/voided/expired/cancelled. Always `sale_type = SALE` with `is_addition_to_total = True`. Amount = `currentTotalPriceSet.shopMoney.amount` (current total, **refunds already netted in**, includes tax). **No standalone refund import** — refunds reflect only via the order's current total dropping on the next sync.
- **Square:** REST `Square-Version: 2025-07-16`. Locations filtered to `status == "ACTIVE"`. `POST /v2/orders/search` per location (one location per call). Only `state == "COMPLETED"` orders are kept; `total_money == 0` skipped. Amount formula: `(|total_money| - |tip_money| - |tax_money|) / 100` — **strips both tips and taxes** from the figure. Sign of `net_amounts.total_money` decides direction: positive → `SALE`, negative → `REFUND`. **`tax_amount` column ends up NULL** for Square rows (computed locally only to subtract). No separate `/v2/refunds` call — refunds reflect via negative net order amounts.

## Gotchas
- [CRITICAL] **Re-import is destructive per location/window.** `import_sales` deletes the location's existing rows for the date range before re-inserting. Any out-of-band writes (e.g. via the Sales SDK directly) for that day/location will be wiped on the next periodic run.
- [CRITICAL] **Shopify multi-location bug.** Every imported Shopify order is hardcoded to `sales_locations[0].id`, regardless of the order's actual fulfillment location. Customers with multiple Shopify locations will see all sales rolled up under one location.
- [CRITICAL] **Tax/tip handling is inconsistent across providers** — this is the #1 reason customer numbers don't match the POS dashboard:
- Lightspeed: amount is **net of tax**, no tips.
- Clover: amount **includes tax and tips** (order total).
- Shopify: amount **includes tax** (current order total), tips included in order total.
- Square: amount **excludes both tax and tips** (explicitly stripped).
- [CRITICAL] **Lightspeed currency is hardcoded to USD** for every transaction regardless of the location's actual currency. Known limitation.
- [CRITICAL] **Clover location metadata is hardcoded.** `"Clover POS"` / `US` / `America/New_York` is shipped to the user regardless of the merchant's real timezone or country (the code path that fetches real merchant data is dead — return statement before it).
- **Aggregation lag.** `mobi_sales_daily` is rebuilt asynchronously after every raw insert/delete, under a per-company Redis lock. There can be a short delay between `add_sales` completing and the dashboard reflecting the new total.
- **Day bucketing uses company timezone**, not the location timezone. A multi-location US/EU company will see all locations bucketed against the company's TZ.
- **Order state filters silently drop transactions** the customer might expect: Clover non-`locked` orders, Square non-`COMPLETED`, Shopify cancelled/draft/pending/voided/fully-refunded.
- **Resume-from-last logic** (`SalesMultiStageDataImport._calculate_start_time`): recurring runs without explicit timestamps resume from the latest stored `SALES_TRANSACTION` entity-mapping; if none exists, defaults to `now - 1 day` (so first periodic might miss data older than 1 day).
- **Latest-transaction tracking uses `entity_mapping` with a UUID external_id**, not a stable provider-side identifier — only the most recent matters for resume.
- **Clover tax computation only sums the first taxRate per line item** (`taxRates.elements[0]`); line items with multiple tax rates are under-counted in `tax_amount`.
- **Batch limit:** `ct_sdk.add_transactions` is capped at 100 transactions per call; the importer chunks into batches of 100.
- **Manual edits skip the publisher.** `apply_manual_sales_edit` writes `mobi_sales_daily` directly and passes `skip_aggregation=True`, bypassing the `SalesAggregationPublisher` flow.
- **`SalesProviderEnum` lists `TOAST` and `TOUCHBISTRO`** but neither has setup logic — they are placeholders only. `API` and `DASHBOARD` are the manual/external sources.

## Last Update
- date: 2026-04-30
- author: rafael
- change: Initial memory created from a deep-dive investigation into POS sync, daily aggregation worker, and per-provider transaction-conversion logic.
