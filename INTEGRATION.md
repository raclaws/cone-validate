# Integrating cone-validate with Agentic Coders

## Option 1: MCP Server (Claude Code, Cline, Continue)

The MCP server exposes four tools to any MCP-compatible client:

| Tool | Description |
|------|-------------|
| `get_cone` | Get dependency cone for a symbol — minimal context to safely edit it |
| `get_file_context` | Get combined cone for all symbols in a file |
| `list_symbols` | List symbols (filterable by file, kind, query) |
| `validate_change` | Run tsc on proposed code before writing |

### Setup for Claude Code

1. Install the MCP package:
```bash
pip install mcp
```

2. Add to `~/.config/claude-code/config.json` (or Claude Desktop's `claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "cone": {
      "command": "python3",
      "args": ["/path/to/cone-validate/mcp_server.py"],
      "env": {
        "CONE_TARGET_DIR": "/path/to/your/src",
        "CONE_PROJECT_ROOT": "/path/to/your/project"
      }
    }
  }
}
```

3. Restart Claude Code. The tools appear automatically.

### Setup for Cline / Continue

Same config format — add to their respective MCP config files.

---

## Option 2: AGENTS.md / CLAUDE.md (Project Instructions)

Drop this into your project root as `AGENTS.md` or `CLAUDE.md`:

```markdown
# Coding Guidelines

## Before editing any file

1. Run `python3 /path/to/cone-validate/cli.py cone <symbol>` to understand dependencies
2. The cone shows all files that depend on or are depended by your change
3. Only edit files within the cone — changes outside risk breaking untracked callers

## After editing

1. Run `npx tsc --noEmit` to validate
2. If errors appear in files you didn't touch, check if they're in the cone
3. If not in cone, the graph may need rebuilding: `python3 cli.py graph --save`

## Symbol lookup

- `python3 cli.py cone formatMoney` — show cone for a function
- `python3 cli.py cone --json formatMoney` — machine-readable output
- `python3 cli.py graph` — rebuild dependency graph
```

---

## Option 3: Pre-prompt Injection (Any Agent)

For agents that don't support MCP, inject context via system prompt:

```python
# Before sending to agent
from validate import build_graph, compute_cone

symbols, sym_by_file, _, call_file_edges, import_edges, sources, *_ = build_graph(target)

# Get cone for the file being edited
cone = compute_cone("targetSymbol", symbols, sym_by_file, call_file_edges, import_edges)

# Build context
context = "Relevant files for this edit:\n\n"
for f in cone:
    context += f"=== {f} ===\n{sources[f].decode()}\n\n"

# Send to agent
prompt = f"{context}\n\nTask: {user_task}"
```

---

## Option 4: Git Hook (Automatic Validation)

Install the post-commit hook to validate changes automatically:

```bash
ln -sf /path/to/cone-validate/git_hook.py /path/to/your/project/.git/hooks/post-commit
chmod +x /path/to/your/project/.git/hooks/post-commit
```

This emits deltas when files change, notifying any subscribed agents.

---

## Quick Reference

```bash
# Build graph
python3 cli.py graph --save

# Query a symbol's cone
python3 cli.py cone createBudgetStore

# JSON output (for piping to agents)
python3 cli.py cone createBudgetStore --json

# List all symbols
python3 cli.py list_symbols

# Cost dashboard
python3 cli.py dashboard
```
