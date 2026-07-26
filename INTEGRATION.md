# Integrating cone-validate with Agentic Coders

## Option 1: MCP Server (Recommended)

The MCP server exposes 7 tools to any MCP-compatible client:

| Tool | Description |
|------|-------------|
| `set_project` | Point at any codebase dynamically (clears cache) |
| `get_project` | Show current config + graph stats |
| `list_symbols` | Query symbols by file/kind/name substring |
| `get_cone` | Get transitive dependency cone for a symbol |
| `get_file_context` | Get combined cone for all symbols in a file |
| `validate_change` | Run tsc on proposed code (TypeScript only) |
| `get_stats` | Session token stats + savings |

### Setup for Hermes Agent

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  cone:
    command: "python3"
    args: ["/path/to/cone-validate/mcp_server.py"]
```

Restart Hermes. Tools appear as `mcp__cone__*`.

### Setup for Claude Code

Add to `~/.config/claude-code/config.json`:

```json
{
  "mcpServers": {
    "cone": {
      "command": "python3",
      "args": ["/path/to/cone-validate/mcp_server.py"]
    }
  }
}
```

### Setup for Cline / Continue

Same JSON format — add to their respective MCP config files.

### Dynamic Project Switching

No need to hardcode `CONE_TARGET_DIR`. The LLM calls `set_project()` at runtime:

```
1. get_project()                        → "No project configured"
2. set_project("/path/to/repo/src")     → Builds graph, ready
3. get_cone("UserService")              → Returns cone for that project
4. set_project("/other/project/src")    → Switches to different project
```

---

## Option 2: AGENTS.md / CLAUDE.md (Project Instructions)

Drop this into your project root as `AGENTS.md` or `CLAUDE.md`:

```markdown
# Coding Guidelines

## Before editing any file

1. Use MCP tool `get_file_context` to understand the dependency cone
2. The cone shows all files that depend on or are depended by your change
3. Only edit files within the cone — changes outside risk breaking untracked callers

## After editing

1. Use `validate_change` to run tsc before writing
2. If errors appear in files you didn't touch, check if they're in the cone

## Symbol lookup

- `list_symbols(query="User")` — find symbols by name
- `get_cone(symbol="UserService")` — get dependency cone
- `get_file_context(file="api/users.ts")` — get cone for a file
```

---

## Option 3: Pre-prompt Injection (Any Agent)

For agents that don't support MCP, inject context via system prompt:

```python
from validate import build_graph, compute_cone
from pathlib import Path

target = Path("/path/to/your/src")
symbols, sym_by_file, _, call_file_edges, import_edges, sources, *_ = build_graph(target)

# Get cone for the symbol being edited
cone = compute_cone("targetSymbol", symbols, sym_by_file, call_file_edges, import_edges)

# Build context
context = "Relevant files for this edit:\n\n"
for f in cone:
    context += f"=== {f} ===\n{sources[f].decode()}\n\n"

# Send to agent
prompt = f"{context}\n\nTask: {user_task}"
```

---

## Option 4: CLI Usage

```bash
# Point at a project and build graph
export CONE_TARGET_DIR=/path/to/your/src
python3 cli.py graph

# Query a symbol's cone
python3 cli.py cone UserService

# JSON output (for piping to agents)
python3 cli.py cone UserService --json

# List all symbols
python3 cli.py symbols

# Filter by kind
python3 cli.py symbols --kind class --query User

# Cost dashboard (if token tracking enabled)
python3 cli.py dashboard
```

---

## Option 5: Git Hook (Automatic Validation)

```bash
# Install post-commit hook
ln -sf /path/to/cone-validate/git_hook.py /path/to/project/.git/hooks/post-commit
chmod +x /path/to/project/.git/hooks/post-commit
```

This emits deltas when files change, notifying any subscribed agents.

---

## Example: Subagent Workflow

A delegated subagent using cone tools to ship a feature:

```
1. set_project("/path/to/repo/src")     → Point at codebase
2. list_symbols(query="format")         → Explore what exists
3. get_file_context("lib/format.ts")    → Get scoped context (37 files, not 61)
4. [LLM writes code]
5. validate_change("lib/format.ts", newCode) → tsc passes ✅
6. git commit                            → Shipped
```

Context was 36% smaller than full codebase. The agent never saw irrelevant files.

---

## Token Savings

Each `get_cone` and `get_file_context` call returns token stats:

```json
{
  "token_stats": {
    "cone_tokens": 59853,
    "total_tokens": 93931,
    "reduction": "36.3%"
  }
}
```

Use `get_stats()` for session totals:

```json
{
  "queries": 5,
  "cone_tokens": 250000,
  "total_tokens": 470000,
  "tokens_saved": 220000,
  "reduction_pct": "46.8%",
  "cost_saved_estimate": "$0.66"
}
```
