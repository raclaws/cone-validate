# cone-validate

**Dependency-scoped code context for LLM agents.**

An MCP server that extracts dependency cones from codebases using tree-sitter static analysis. Point it at a symbol, get the minimal file set needed to safely edit it — reducing LLM context by 86-97% while preserving correctness.

## Why?

LLM coding agents typically load entire codebases or use semantic search (embeddings). Both have problems:

| Approach | Problem |
|----------|---------|
| Full codebase | Blows context window, expensive, slow |
| Semantic search | Misses transitive dependencies, finds "similar" not "required" |
| **Dependency cone** | Follows actual imports/calls, gets exactly what's needed |

cone-validate computes the **transitive closure** of dependencies for any symbol — the minimum context to understand and safely modify it.

## Languages Supported

| Language | Extensions | Status |
|----------|-----------|--------|
| TypeScript | `.ts`, `.tsx` | ✅ Full (path aliases, type hierarchy) |
| JavaScript | `.js`, `.jsx` | ✅ via TS parser |
| Python | `.py` | ✅ Full |
| Go | `.go` | ✅ Full |
| Rust | `.rs` | ✅ Full |

## Quick Start

### 1. Install

```bash
git clone https://github.com/raclaws/cone-validate
cd cone-validate
pip install tree-sitter tree-sitter-typescript tree-sitter-python tree-sitter-go tree-sitter-rust tiktoken mcp
```

### 2. Use as MCP Server

Add to your MCP client config (Claude Code, Cline, Continue, Hermes):

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

### 3. Use from LLM

```
1. set_project("/path/to/your/src")     → Point at codebase
2. list_symbols(query="User")           → Find symbols
3. get_cone(symbol="UserService")       → Get dependency cone
4. get_file_context(file="api/users.ts")→ Get cone for entire file
5. validate_change(file, newCode)       → Run tsc before writing
6. get_stats()                          → See token savings
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `set_project` | Point at any codebase dynamically (clears cache) |
| `get_project` | Show current config + graph stats |
| `list_symbols` | Query symbols by file/kind/name substring |
| `get_cone` | Get transitive dependency cone for a symbol |
| `get_file_context` | Get combined cone for all symbols in a file |
| `validate_change` | Run tsc on proposed code (TypeScript only) |
| `get_stats` | Session token stats: cone vs full, savings % |

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                        LLM Agent                            │
│                           │                                 │
│    "Add reset() to UserService"                            │
│                           ▼                                 │
│    ┌───────────────────────────────────────────────────┐   │
│    │ MCP: get_file_context("services/user.ts")         │   │
│    │      → 12 files (not 200) + sources + token stats │   │
│    └───────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ stdio JSON-RPC
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                   cone-validate MCP Server                  │
│                                                             │
│  1. tree-sitter AST parsing (TS/Py/Go/Rust)                │
│  2. Symbol extraction (functions, classes, methods, etc.)   │
│  3. Call graph + import graph construction                  │
│  4. Type hierarchy (extends/implements for TS)              │
│  5. BFS transitive closure → dependency cone                │
│  6. Token counting + stats                                  │
└─────────────────────────────────────────────────────────────┘
```

## Results

Tested on real codebases:

| Metric | Value |
|--------|-------|
| Average cone size | 14% of repo |
| Token reduction | 86% average, 97.8% for leaf symbols |
| Parse errors | 0 (across TS, Python, Go, Rust) |

## Example: Subagent Vibe Coding

A delegated subagent used cone tools to ship a real feature:

```
1. set_project(twenty-dollar)           → Pointed at codebase
2. list_symbols("format")               → Found existing utils
3. get_file_context("lib/format.ts")    → Got 37 files (not 61)
4. [wrote formatCompact + formatPercentChange]
5. validate_change(...)                 → tsc passed ✅
6. git commit                           → Shipped
```

Context was 36% smaller than full codebase. The agent never saw irrelevant files.

## Static Analysis Approach

cone-validate uses **static analysis** (not runtime tracing):

**What it tracks:**
- Import edges (`import { X } from './Y'`, `from X import Y`, `use crate::X`)
- Call edges (function calls, method calls)
- Type hierarchy (class extends/implements for TypeScript)

**Limitations:**
- No dynamic imports (`import()`, `__import__()`)
- No runtime polymorphism (includes all implementations of a method name)
- No cross-language cones (Python importing TS won't resolve)

**Trade-off:** Over-approximation is correct for LLM context. Missing a dependency breaks understanding; extra files just cost tokens.

## Type Hierarchy (TypeScript)

For TypeScript, cone-validate extracts `extends` and `implements` relationships:

```typescript
interface Renderer { render(): void }
class HTMLRenderer implements Renderer { render() { ... } }
class PDFRenderer implements Renderer { render() { ... } }
```

When you call `obj.render()`, the cone includes both implementations — scoped to the type lineage, not every `render` in the codebase.

## CLI Usage

```bash
# Build graph and show stats
python3 cli.py graph

# Get cone for a symbol
python3 cli.py cone formatMoney

# JSON output (for piping)
python3 cli.py cone formatMoney --json

# List symbols
python3 cli.py symbols --query User --kind class

# Cost dashboard
python3 cli.py dashboard
```

## Files

| File | Purpose |
|------|---------|
| `mcp_server.py` | MCP server (7 tools) |
| `validate.py` | Graph builder + cone computation |
| `cli.py` | CLI wrapper |
| `oracle_loop.py` | tsc feedback loop with escalation |
| `subscription.py` | Delta subscription bus |
| `persistence.py` | SQLite graph/subscription store |
| `ledger.py` | Token accounting |
| `dashboard.py` | Cost viewer |
| `config.py` | YAML + env config loader |

## Documentation

- [SUPPORTED.md](SUPPORTED.md) — Language support matrix, frameworks tested
- [INTEGRATION.md](INTEGRATION.md) — Setup for Claude Code, Cline, Continue, Hermes

## Adding a Language

1. `pip install tree-sitter-<lang>`
2. Add parser in `validate.py`
3. Implement `extract_symbols_<lang>()`
4. Implement `extract_calls_<lang>()`
5. Implement `extract_imports_<lang>()`
6. Implement `resolve_<lang>_import()`
7. Update `build_graph()` to glob new extensions

See existing implementations for Python, Go, Rust as templates.

## License

MIT
