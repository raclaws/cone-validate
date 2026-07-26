# Supported Languages & Features

## Languages

| Language | Extensions | Status | Notes |
|----------|-----------|--------|-------|
| TypeScript | `.ts`, `.tsx` | ✅ Full | JSX/TSX, path aliases via tsconfig |
| JavaScript | `.js`, `.jsx` | ✅ via TS parser | Same parser handles JS |
| Python | `.py` | ✅ Full | Relative + absolute imports |
| Go | `.go` | ✅ Full | Package-based imports |
| Rust | `.rs` | ✅ Full | use/mod, impl methods |
| Java | `.java` | 🔜 Planned | |

## Symbol Extraction

| Feature | TypeScript | Python |
|---------|-----------|--------|
| Functions | ✅ | ✅ |
| Arrow functions | ✅ | N/A |
| Classes | ✅ | ✅ |
| Methods | ✅ | ✅ |
| Interfaces/Types | ✅ | N/A |
| Constants | ✅ | ✅ (UPPER_CASE) |

## Call Graph

| Feature | TypeScript | Python |
|---------|-----------|--------|
| Direct calls `fn()` | ✅ | ✅ |
| Method calls `obj.method()` | ✅ | ✅ |
| Chained calls | Partial | Partial |
| Dynamic calls | ❌ | ❌ |

## Import Resolution

| Feature | TypeScript | Python |
|---------|-----------|--------|
| Relative imports | ✅ `./foo` | ✅ `.module` |
| Absolute imports | ✅ via tsconfig paths | ✅ from project root |
| Barrel imports | ✅ `index.ts` | ✅ `__init__.py` |
| Node modules | ❌ (skipped) | ❌ (skipped) |
| Aliased imports | ✅ tsconfig `paths` | ❌ |

## Frameworks Tested

| Framework | Language | Status |
|-----------|----------|--------|
| SolidJS | TypeScript | ✅ |
| React | TypeScript | ✅ |
| Vue (SFC) | TypeScript | ⚠️ Script block only |
| Svelte | TypeScript | ⚠️ Script block only |
| FastAPI | Python | ✅ |
| Django | Python | ✅ |
| Flask | Python | ✅ |

## MCP Tools

| Tool | Description |
|------|-------------|
| `set_project` | Point at any codebase dynamically |
| `get_project` | Show current config + graph status |
| `list_symbols` | Query symbols by file/kind/name |
| `get_cone` | Transitive dependency cone for a symbol |
| `get_file_context` | Combined cone for all symbols in a file |
| `validate_change` | Run tsc on proposed code (TS only) |
| `get_stats` | Session token stats + savings |

## Limitations

- **No cross-language cones**: A Python file importing a TS module won't resolve
- **No dynamic imports**: `import()`, `__import__()`, `importlib` not tracked
- **No runtime analysis**: Only static AST, no execution tracing
- **No monorepo awareness**: Each `set_project` is a single directory tree
- **tsc validation**: Only for TypeScript (Python would need pyright/mypy)

## Adding a Language

1. `pip install tree-sitter-<lang>`
2. Add parser init in `validate.py`
3. Implement `extract_symbols_<lang>()`
4. Implement `extract_calls_<lang>()`
5. Implement `extract_imports_<lang>()`
6. Implement `resolve_<lang>_import()`
7. Update `build_graph()` to glob new extensions

PRs welcome for Go, Rust, Java.
