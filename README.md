# Feature Memory MCP

A local MCP server that maintains a markdown knowledge base of product features, plus a Cursor skill that drives the before/after/correction workflows. The agent loads expert context from the knowledge base before planning and writes back what changed after implementation. Updates are append-and-merge patches — the agent can add knowledge but cannot silently delete it.

Design spec: [docs/superpowers/specs/2026-04-23-feature-memory-mcp-design.md](docs/superpowers/specs/2026-04-23-feature-memory-mcp-design.md).

## Requirements

- Python 3.11+
- [Cursor](https://cursor.com)
- Optional: [`uv`](https://github.com/astral-sh/uv) for faster installs

## Install

### 1. Install the package

From this project root:

With `uv`:

```bash
uv venv
uv pip install -e ".[dev]"
```

Without `uv` (stdlib only):

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This puts `feature-memory-mcp` on your PATH inside the venv. Verify:

```bash
which feature-memory-mcp
feature-memory-mcp --help
```

### 2. Register the MCP server with Cursor

Add the server to `~/.cursor/mcp.json`. If the file doesn't exist, create it. If it does, merge the `feature-memory` entry into the existing `mcpServers` block.

```json
{
  "mcpServers": {
    "feature-memory": {
      "command": "/Users/rafaelsakera/Desktop/connecteam_super_power/feature-memory/.venv/bin/feature-memory-mcp",
      "args": [
        "--features-dir",
        "/Users/rafaelsakera/Desktop/connecteam_super_power/feature-memory/features"
      ]
    }
  }
}
```

> Use the **absolute** path to the venv binary so Cursor finds it regardless of which shell launched it. Replace the path if you cloned the repo somewhere else.

Restart Cursor so it picks up the new MCP server.

### 3. Symlink the Cursor skill

Make the skill globally available across all your repos:

```bash
mkdir -p ~/.cursor/skills
ln -s "$PWD/.cursor/skills/feature-memory" ~/.cursor/skills/feature-memory
```

Verify the symlink:

```bash
ls -l ~/.cursor/skills/feature-memory
```

Restart Cursor (or reload skills) so the new skill is registered.

## Quick start

1. Start a chat in Cursor and ask the agent to **create your first feature memory**:

   > *"Create a feature memory for Quick Task. Summary: 'Lightweight tasks users create during onboarding and from the dashboard.' Key paths: src/quick-task/\*\*."*

   The agent will call `create_feature` and a file will appear at `features/quick-task.md`.

2. Then test the **before-planning loop**:

   > *"Implement a new tag filter for Quick Task."*

   The skill should activate, ask which feature, you say `quick-task`, the agent calls `get_feature("quick-task")`, and you'll see it inject the body markdown into its plan. Any `[CRITICAL]` items will be surfaced at the top.

3. After "implementing" something (even a no-op), test the **after-implementation loop**:

   > *"We're done. Update the memory."*

   The agent will propose a patch, you confirm, it calls `update_feature(...)`, and you'll see the unified diff plus any size warnings.

4. Test the **correction loop**:

   > *"That last gotcha is wrong, remove it."*

   The agent will restate the planned removal, ask you to confirm, then call `correct_feature(...)`. `## Last Update` will be overwritten with an entry recording the removal and your reason.

## Running tests

```bash
source .venv/bin/activate
pytest -q
```

You should see ~100 tests passing.

## Project layout

```
feature-memory/
├── src/feature_memory/
│   ├── server.py        FastMCP entry + tool definitions
│   ├── store.py         read/write .md files, frontmatter parsing
│   ├── index.py         build/refresh index.json from frontmatter
│   ├── merge.py         apply_patch (additive)
│   ├── correction.py    apply_corrections (surgical removals)
│   └── models.py        pydantic schemas
├── tests/               unit + end-to-end pytest
├── features/            the knowledge base (git-tracked)
│   └── _archived/       soft-deleted features
├── docs/superpowers/specs/   design specs
└── .cursor/skills/feature-memory/SKILL.md   the workflow skill
```

## Troubleshooting

**The MCP server doesn't show up in Cursor.**
- Check `~/.cursor/mcp.json` is valid JSON (no trailing commas).
- Use an absolute path to the venv binary in `command`.
- Restart Cursor fully, not just the chat.
- Check the Cursor log panel for an `feature-memory: …` startup message.

**`feature-memory-mcp: command not found`.**
- You're not in the right venv. `source .venv/bin/activate`, then `which feature-memory-mcp`.
- Or use the absolute path: `./.venv/bin/feature-memory-mcp --features-dir …`.

**The skill doesn't activate.**
- Restart Cursor so it re-scans `~/.cursor/skills/`.
- Check the symlink: `ls -l ~/.cursor/skills/feature-memory` should point at this repo.
- Make the user message more explicit: include "implement", "build", "fix", "refactor", or "feature".

**`get_feature` says "feature not found" but I see the file.**
- The file might be in `features/_archived/`. The MCP excludes archived features from `list_features` and refuses to serve them via `get_feature`. Move it back to `features/` by hand if you want it active again.

**`correct_feature` errors with "target not found".**
- Removals are exact-string match. Run `get_feature` and copy the exact text of the flow/gotcha you want removed. Whitespace matters.

**The `[CRITICAL]` prefix isn't being surfaced.**
- The skill instructs the agent to surface it. If your model is missing it, remind it: *"Make sure to surface CRITICAL items at the top of your plan."*

## License

MIT — see [LICENSE](LICENSE).
