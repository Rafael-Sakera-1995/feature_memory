---
name: feature-memory
description: |
  Use the Feature Memory MCP server to load expert context BEFORE planning a feature
  and to write back what changed AFTER implementation. Trigger this skill whenever
  the user asks to implement, build, fix, refactor, migrate, or extend a feature in
  their product. Also use it when the user explicitly asks to correct or archive a
  feature in the memory.
---

# Feature Memory — workflow

You are working with the Feature Memory MCP server (tools live under the
`feature-memory` namespace). The server holds one markdown file per product
feature. Treat that knowledge base as the team's collective brain. Always load
before planning. Always write back after implementing. Never modify it
proactively for corrections.

## When to activate

Activate on user messages with intents like:
- "implement / build / add / wire up X"
- "fix / debug / refactor / clean up X"
- "migrate X from … to …"
- "extend X to also …"
- "remove / archive / fix the doc for X" (correction loop)

If the message is purely conversational ("how does X work?"), you may still
load context with `get_feature` but you do not need to run the after-implementation
loop.

---

## Loop 1 — Before planning ("expert mode")

**Step 1. Ask the user which feature this is for.**

> *"Which feature is this for? Name it, or say 'auto' / 'I don't know' and I'll figure it out."*

Do this even if you think you know — the user-named path is faster, cheaper,
and more reliable than auto-detect.

**Step 2a. Named-feature path.**

If the user names a feature:
1. Convert the name to a slug if needed (lowercase, hyphenated).
2. Call `get_feature(slug)`.
3. Skip step 2b. Go to step 3.

If `get_feature` returns "not found", ask the user:
> *"No memory for that feature yet. Should I create one with `create_feature`, or did you mean another name?"*

**Step 2b. Auto-detect fallback (only if user said `auto` / `I don't know`).**

1. Call `list_features()`.
2. Rank candidates by:
   - Matching the user's prompt text against each feature's `name` and `summary`.
   - Matching currently-open file paths against each feature's `key_paths` globs.
3. **Always show the top 3 candidates with concrete signals — never silently auto-pick.** Example:

   ```
   Auto-detect found:
     1. Quick Task — 3 of your open files match key_paths, "task" appears in your prompt
     2. Onboarding — 1 file match, no prompt keywords
     3. (none of these — start a new feature)
   Pick one, pick multiple (e.g. "1 and 2"), or say "new feature".
   ```

4. Wait for explicit user confirmation. Then call `get_feature(slug)` for each pick.

**Step 3. Acknowledge by name only. Load the rest silently.**

After receiving each feature's `body_markdown`:

1. Read it carefully and keep it loaded internally. This is your expertise on this feature — you will use it when the user gives you the actual task.
2. **Do not surface anything from the memory to the user.** No gotchas. No flows. No `[CRITICAL]` items. No architecture overview. No recipe. No related-feature inventory. Nothing. The user owns this feature and does not need their own docs read back to them.
3. Reply with exactly one short line — the feature name and a prompt for what's next. That is the entire response:

   > *"Found memory for **[Feature Name]**. What do you want to do next?"*

   If multiple features were selected in step 2b, list them in the same line:

   > *"Found memory for **Quick Task** and **Onboarding**. What do you want to do next?"*

4. When the user gives the actual instruction, ground your plan silently in the loaded `## Architecture`, `## Flows`, and `## Gotchas`. `## Last Update` is freshness signal only (when this memory was last touched, by whom, for what) — it is not part of the working knowledge of the feature. `[CRITICAL]` items still drive your plan — you respect them in what you propose and what you refuse to do — but you do not announce them as a preamble. They surface (if at all) inside the plan, on the steps they actually constrain.

---

## Loop 2 — After implementation ("update memory")

**Step 1. Detect "we're done."**

When the user signals completion ("PR opened", "we're done", "wrap it up",
"ship it", or after the implementation finishes successfully), prompt:

> *"Should I update the [feature name] memory with what we just did?"*

Wait for an affirmative. Do not write without consent.

**Step 2. Construct a patch — never a rewrite.**

Build a `FeaturePatch` object containing only the deltas:

- `last_update` — **required**. `{ date: today, author: <user or 'agent'>, change: <one-line summary> }`. This **overwrites** the previous `## Last Update` entry on disk. There is no append-only history list; the goal is a small, high-signal context payload, not an audit log.
- `add_flows` / `add_gotchas` — append-only. Use these for genuinely new behavior the agent should know about next time.
- `add_dependencies` — slugs of newly-introduced cross-feature couplings.
- `add_key_paths` — only if you touched files outside the existing globs.
- `summary_override` — only if the one-line `summary` is now misleading.
- `set_parent_feature` — only if this feature is now a sub-feature of another.
- `notes_append` — for prose richer than a one-liner.

**Step 3. Apply the quality filter (mandatory).**

Before sending the patch, compress and generalize:

- Each item must be a **feature-level insight**, not a commit-level change.
- Reject things like *"fixed button color"*, *"renamed variable foo to bar"*, *"updated copy"* — those belong in the PR description, not the feature memory.
- Prefer fewer high-quality additions over many low-quality ones. If nothing in the change rises above commit-level noise, send only a `last_update` and skip the `add_*` lists entirely.
- The `last_update.change` is a *one-line* summary — it is the freshness marker, not a changelog. Do not pack multi-step chronology into it. Git history exists for that.
- Use `[CRITICAL]` prefix only for items the agent should surface in **every** future plan (security, data loss, breaking-change risks). Do not inflate.

**Step 4. Call `update_feature(slug, patch)`.**

The server will:
- Merge the patch (deduped append on `add_*` lists).
- Overwrite `## Last Update` with your entry (the previous one is replaced, not preserved).
- Bump `updated_at`.
- Rewrite the `.md` file.
- Rebuild `index.json`.
- Return a unified diff and any size warnings.

**Step 5. Show the diff and warnings to the user.**

> *"Here's what I added — looks right? Server also flagged: '[warning]'."*

If the server warned about size limits (>25 flows or >40 gotchas), suggest the
user split the feature via `parent_feature`:

> *"Quick Task now has 27 flows. Want to split off `quick-task-editing` as a
> sub-feature with `parent_feature: quick-task`?"*

---

## Loop 3 — Correction (only on explicit user request)

The MCP exposes `correct_feature` and `archive_feature`. These tools are
strictly off-limits to you unless the user **explicitly** asks to remove,
correct, or archive something.

### Strict rules

1. **Never proactively suggest a removal or archival.** Even if you spot something that looks wrong in a feature doc, do not propose removal mid-flow. Mention it as a question if at all: *"This gotcha looks stale to me — want me to flag it for correction later?"* and move on.
2. **Trigger phrases** that justify entering this loop:
   - "that gotcha is wrong, remove it"
   - "delete that flow"
   - "the summary is wrong, change it to …"
   - "this feature is obsolete / merged into X / gone"
3. **Always confirm in plain language before calling the tool.** Restate the planned correction with the user's stated reason:

   > *"I'll remove the gotcha 'React migration incomplete' from Quick Task and stamp `## Last Update` with the reason — you said because the migration is now complete. Confirm?"*

4. **One feature per call.** No bulk archives. No multi-feature corrections.
5. **`reason` is required** on every correction and on `archive_feature`. Use the user's own words. Do not paraphrase away their intent.
6. **Use `correct_feature` for surgical removals** (one item from a list, or a summary replacement):
   - `remove_flow(text, reason)`
   - `remove_gotcha(text, reason)`
   - `remove_key_path(path, reason)`
   - `remove_dependency(slug, reason)`
   - `replace_summary(new_summary, reason)`
7. **Use `archive_feature(slug, reason)` only when the entire feature is gone or merged into another.** It is a soft delete (the file moves to `_archived/`), but still: confirm with the user.
8. **Deep prose rewrites are out of scope for the MCP.** If a section like `## Architecture` is wrong (not just one bullet), say so and let the user (or you, via normal file-editing tools) hand-edit the markdown directly. Do not try to encode prose rewrites as corrections.

After any correction, show the diff for sanity check and stop. Do not chain
corrections without re-confirming.

---

## Tool reference (concise)

| Tool | When | Modifies state? |
|---|---|---|
| `list_features()` | Auto-detect path only. | No |
| `get_feature(slug)` | Always, before planning. | No |
| `create_feature(name, summary, …)` | When the work is on a feature with no memory yet. | Yes (creates) |
| `update_feature(slug, patch)` | After implementation, with user consent. | Yes (append-merge) |
| `correct_feature(slug, corrections)` | **Only on explicit user removal request.** | Yes (surgical remove) |
| `archive_feature(slug, reason)` | **Only on explicit user "this is obsolete" request.** | Yes (soft delete) |

## What you must never do

- Never rewrite a feature file's body in `update_feature`. The patch shape forbids it; do not try to work around it via `notes_append`.
- Never call `correct_feature` or `archive_feature` without an explicit user request and an explicit confirmation step.
- Never silently auto-pick a feature in the auto-detect path. Always confirm.
- Never recite the loaded feature body back to the user — not gotchas, not flows, not `[CRITICAL]` items, not architecture. Acknowledge by feature name in one line, ask what's next, and wait. Apply the memory silently when you plan.
- Never write commit-level noise into the memory. Apply the quality filter.
