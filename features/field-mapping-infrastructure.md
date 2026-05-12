---
name: Field Mapping Infrastructure
slug: field-mapping-infrastructure
summary: Reusable FE infra for mapping external fields to Connecteam concepts via
  a pluggable adapter pattern.
key_paths:
- app/scripts/categories/common/components/fields-mapping/**
dependencies: []
tags:
- frontend
- field-mapping
- infrastructure
- mobx
created_at: '2026-04-26'
updated_at: '2026-04-26'
---

## Overview
Generic, reusable FE infrastructure for "map an external field to a Connecteam concept" UIs. Provides a MobX store + a row-level value-object + a pluggable adapter pattern + the popover UI that ties them together. Lives at `app/scripts/categories/common/components/fields-mapping/**` and knows nothing about any specific consumer (user-sync, hiring, …).

## Architecture
### Three layers

1. **`FieldsMappingStore`** — the orchestrator. Owns:
   - `mappingEntries: MappingEntry[]` (one per visible row)
   - `externalFields` catalog + `externalFieldsById` lookup
   - `targetAdapters: MappingTargetAdapter[]` (pluggable extensions)
   - `customFieldsStore` for resolving Connecteam custom fields

   Computes "what's still available", "is the whole thing valid", and emits two output streams to the consumer.

2. **`MappingEntry` (implements `MappingEntryView`)** — one row. Holds the row's chosen ids; delegates anything that needs cross-row context (filtering, compatibility, lookups) to the store via a back-reference.

3. **`MappingTargetAdapter`** — a four-method plug-in (`appliesTo`, `isConnecteamCompatible`, `renderMenuItem`, `useSelectedLabel`) that lets the right side of a row resolve to **something other than a Connecteam custom field** (e.g., a Document target). Adapters live in their feature's folder; the store discovers them only via the `targetAdapters` array passed at construction.

### Row data shape — strict xor

Each entry binds `externalFieldId` to **either** `connecteamFieldId` **or** `(targetType, targetId)`, never both. All three setters enforce the xor:
- `setConnecteamFieldId` clears target.
- `setTarget` clears connecteam.
- `setExternalFieldId` re-validates both sides and clears whichever became stale.

### Two output streams, one pulse

`notifyValueChanged()` fires both callbacks together:
- `onValueChange(currentValidMapping, isValid)` — rows with a `connecteamFieldId`.
- `onExtraMappingsChange(currentValidExtraMappings)` — rows with a `targetId`, grouped by `targetType`.

Consumers convert each stream to whatever BE shape they need via tiny per-feature wire helpers (kept at the consumer's folder, not here).

### Construction (options object)

```ts
new FieldsMappingStore({
    initialData,                      // { orderedMappings, orderedExtraMappings, ...nonEditable sets }
    externalFields,
    customFieldsStore,
    onValueChange,
    onExtraMappingsChange?,           // needed if any adapter is registered
    targetAdapters?,
    externalFieldFallbacks?,
    filterUsedExternalFields?,
    filterSensitiveFields?,
});
```

## Flows
- **Initialize from BE.** Consumer passes `initialData.orderedMappings` (regular) and `initialData.orderedExtraMappings` (per-target-type). `initializeMapping` resolves special-type `connecteamId`s, builds entries, sorts by `sortOrder`, then appends "additional" entries for non-editable fields not yet mapped.
- **Pick an external field on a row.** `setExternalFieldId` updates the id, re-checks adapter applicability, and clears stale `targetId` (no adapter applies anymore) or stale `connecteamFieldId` (pair no longer compatible).
- **Pick a right-side value.** `FieldMatcher` decides per row: if `store.getAdapterForEntry(entry)` returns an adapter, render `FileTargetMenu` (popover with "User Fields" submenu + per-adapter submenus); otherwise render the regular `<Select>`.
- **Pick a target via adapter.** Adapter's submenu calls `entry.setTarget(adapterId, targetId)` then `store.triggerValueChanged()`; the store fires both callbacks.
- **Add / remove rows.** `addEntry()` appends a new empty entry; `removeEntry(id)` filters it out. `isAddEntryDisabled` drives the "Add" button based on remaining-available fields/targets.

## Gotchas
- **`useSelectedLabel` is a React hook on the adapter interface.** Consumers must invoke it inside a render path. If `entry.targetType` can switch between adapters with different hook signatures, the menu must `key={adapter.id}` the wrapping component to force a remount — otherwise it's a rules-of-hooks violation. Latent today (single adapter).
- **Adapter applicability hinges on `entry.externalFieldType`.** Adapters only see rows whose external field has a type they claim. Changing the external field can flip applicability, which is why `setExternalFieldId` does the dual-cleanup dance.
- **`isPairCompatible` couples adapters globally per store instance.** When no adapter applies to a row, a Connecteam field is rejected if **any** registered adapter claims it. This keeps file-typed CFs from showing up next to non-file external fields. Adapters that claim the same CF type partition the regular list for everyone in that store instance.
- **`relevantConnecteamFields` hides any CF claimed by an adapter.** Such CFs are unreachable via the regular `<Select>` — only via the adapter's submenu. `adapterCompatibleConnecteamFields` re-includes them in `relevantConnecteamFieldsById` so persisted entries can still resolve their bound CF.
- **`mappedTargetIdsByType` enforces "one target per row across rows".** Adapters receive `mappedTargetIds: Set<TargetId>` in their `renderMenuItem` props and are expected to filter their own option list against it.
- **`MappingEntry` constructor takes 9 positional params.** Convert to an options object next time it's touched (the store already migrated).
- **`availableExternalFields` per-row filter is gated on `connecteamFieldId`.** If the row only has a `targetId` set, no compatibility filter runs; `setExternalFieldId` self-corrects after the user picks. Asymmetry is intentional.
- **Two adapters claiming the same CF type collide silently.** Both submenus render and the CF type disappears from the regular list. Usually fine, but design adapters to partition CF types.

## Consumers (informational)
The infrastructure is consumed by features that own their own per-feature wire layer (BE shape → `OrderedMappings` / `OrderedExtraMappings`). Document those as their own feature memories, not here.
