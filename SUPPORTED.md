# Supported Languages & Features

## Languages

| Language | Extensions | Implemented | Validated |
|----------|-----------|-------------|-----------|
| TypeScript | `.ts`, `.tsx` | ✅ Full (path aliases, type hierarchy) | ✅ twenty-dollar (61 files) |
| JavaScript | `.js`, `.jsx` | ✅ via TS parser | ❌ Not yet |
| Python | `.py` | ✅ Full | ⚠️ Parsing only (cone-validate itself) |
| Go | `.go` | ✅ Full | ❌ Not yet |
| Rust | `.rs` | ✅ Full | ⚠️ Parsing only (CodeRLM, 34 files) |
| Java | `.java` | 🔜 Planned | — |

**Implemented** = parser works, symbols/calls/imports extracted.  
**Validated** = tested on a real codebase with cone computation and correctness verification.

## Symbol Extraction

| Feature | TypeScript | Python | Go | Rust |
|---------|-----------|--------|-----|------|
| Functions | ✅ | ✅ | ✅ | ✅ |
| Arrow functions | ✅ | N/A | N/A | N/A |
| Classes | ✅ | ✅ | N/A | ✅ (struct) |
| Methods | ✅ | ✅ | ✅ | ✅ (impl) |
| Interfaces | ✅ | N/A | ✅ | ✅ (trait) |
| Types | ✅ | N/A | ✅ | ✅ (enum) |
| Constants | ✅ | ✅ (UPPER_CASE) | ✅ | ✅ |
| Modules | N/A | N/A | N/A | ✅ |

## Type Hierarchy (TypeScript)

| Feature | Status |
|---------|--------|
| `extends` clause | ✅ Classes and interfaces |
| `implements` clause | ✅ Classes |
| Interface methods | ✅ Extracted with parent_type |
| Class methods | ✅ Extracted with parent_type |
| Implementor tracking | ✅ build_type_hierarchy() |

This reduces name collision pollution — two unrelated `.render()` methods won't both pollute the cone unless they share a type lineage.

## Call Graph

| Feature | TypeScript | Python | Go | Rust |
|---------|-----------|--------|-----|------|
| Direct calls `fn()` | ✅ | ✅ | ✅ | ✅ |
| Method calls `obj.method()` | ✅ | ✅ | ✅ | ✅ |
| Selector calls `pkg.Func()` | ✅ | ✅ | ✅ | N/A |
| Scoped calls `Type::method()` | N/A | N/A | N/A | ✅ |
| Chained calls | Partial | Partial | Partial | Partial |
| Dynamic calls | ❌ | ❌ | ❌ | ❌ |

## Import Resolution

| Feature | TypeScript | Python | Go | Rust |
|---------|-----------|--------|-----|------|
| Relative imports | ✅ `./foo` | ✅ `.module` | N/A | ✅ sibling `.rs` |
| Absolute imports | ✅ tsconfig paths | ✅ from root | ✅ package path | ✅ from crate root |
| Barrel imports | ✅ `index.ts` | ✅ `__init__.py` | ✅ package dir | ✅ `mod.rs` |
| External packages | ❌ (skipped) | ❌ (skipped) | ❌ (skipped) | ❌ (skipped) |
| Aliased imports | ✅ tsconfig `paths` | ❌ | ❌ | ❌ |

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
| Gin | Go | ✅ |
| Echo | Go | ✅ |
| Actix-web | Rust | ✅ |
| Axum | Rust | ✅ |

## MCP Tools

| Tool | Description |
|------|-------------|
| `set_project` | Point at any codebase dynamically (clears graph cache) |
| `get_project` | Show current config + graph stats |
| `list_symbols` | Query symbols by file/kind/name substring |
| `get_cone` | Transitive dependency cone for a symbol |
| `get_file_context` | Combined cone for all symbols in a file |
| `validate_change` | Run tsc on proposed code (TypeScript only) |
| `get_stats` | Session token stats: cone vs full, % saved, cost estimate |

## Limitations

- **No cross-language cones**: A Python file importing a TS module won't resolve
- **No dynamic imports**: `import()`, `__import__()`, `importlib` not tracked
- **No runtime analysis**: Only static AST, no execution tracing
- **No monorepo awareness**: Each `set_project` is a single directory tree
- **tsc validation**: Only for TypeScript (Python would need pyright/mypy)
- **Dynamic dispatch**: Includes all implementations of a method name (over-approximates)

## Precision Stack

cone-validate uses a layered approach to balance precision vs soundness:

```
┌─────────────────────────────────────────────┐
│ 1. Name-based matching (baseline)           │ ← catches functions, classes
├─────────────────────────────────────────────┤
│ 2. Interface→impl narrowing (TypeScript)    │ ← scopes methods to type lineage
├─────────────────────────────────────────────┤
│ 3. Call-site type extraction (future)       │ ← "r: Renderer" → only Renderer impls
├─────────────────────────────────────────────┤
│ 4. User hints @cone-include (future)        │ ← manual override escape hatch
└─────────────────────────────────────────────┘
```

**Trade-off:** Over-approximation is correct for LLM context. Missing a dependency breaks understanding; extra files just cost tokens.

## Adding a Language

1. `pip install tree-sitter-<lang>`
2. Add parser init in `validate.py`
3. Implement `extract_symbols_<lang>()`
4. Implement `extract_calls_<lang>()`
5. Implement `extract_imports_<lang>()`
6. Implement `resolve_<lang>_import()`
7. Update `build_graph()` to glob new extensions

See existing implementations for Python, Go, Rust as templates. PRs welcome for Java, C#, C++.
