#!/usr/bin/env python3
"""
Surface validation: tree-sitter dependency cone extraction
Hypothesis: cone-scoped context is significantly smaller than full-file context

Measures:
  1. Can we reliably extract symbol → callers/callees from real TS?
  2. Token ratio: cone-constrained vs full file
"""

import os, sqlite3, json, re
from pathlib import Path
from collections import defaultdict, deque
import tiktoken
from tree_sitter import Language, Parser
import tree_sitter_typescript as ts_typescript
import tree_sitter_python as ts_python

# ── Setup ────────────────────────────────────────────────────────────────────
TS_LANGUAGE = Language(ts_typescript.language_typescript())
PY_LANGUAGE = Language(ts_python.language())

ts_parser = Parser(TS_LANGUAGE)
py_parser = Parser(PY_LANGUAGE)

enc = tiktoken.get_encoding("cl100k_base")

def get_parser_for_file(path: Path):
    """Return appropriate parser and language for file extension."""
    ext = path.suffix.lower()
    if ext in ('.ts', '.tsx'):
        return ts_parser, 'typescript'
    elif ext == '.py':
        return py_parser, 'python'
    return None, None

TARGET_DIR = Path("/root/repos/twenty-dollar/frontend/src/lib")  # Legacy default, use config

def tokens(text: str) -> int:
    return len(enc.encode(text))


# ── AST extraction ───────────────────────────────────────────────────────────
def parse_file(path: Path):
    """Parse a file with the appropriate language parser."""
    src = path.read_bytes()
    parser, lang = get_parser_for_file(path)
    if parser is None:
        return src, None, None
    return src, parser.parse(src), lang


def extract_symbols(src: bytes, tree, file_str: str, lang: str = 'typescript') -> list[dict]:
    """Return function/class/arrow declarations with byte spans."""
    if tree is None:
        return []
    
    if lang == 'python':
        return extract_symbols_python(src, tree, file_str)
    
    # TypeScript extraction (existing code)
    out = []

    def first_ident(node) -> str | None:
        for c in node.children:
            if c.type in ("identifier", "property_identifier"):
                return src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
        return None

    def walk(node, parent=None):
        t = node.type
        if t in ("function_declaration", "function_expression"):
            name = first_ident(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="function",
                                start=node.start_byte, end=node.end_byte))
        elif t == "method_definition":
            name = first_ident(node)
            if name and parent:
                out.append(dict(name=f"{parent}.{name}", file=file_str, kind="method",
                                start=node.start_byte, end=node.end_byte))
        elif t == "class_declaration":
            name = first_ident(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="class",
                                start=node.start_byte, end=node.end_byte))
                for c in node.children:
                    walk(c, parent=name)
                return
        elif t == "lexical_declaration":
            for c in node.children:
                if c.type == "variable_declarator":
                    vname = first_ident(c)
                    for sub in c.children:
                        if sub.type == "arrow_function" and vname:
                            out.append(dict(name=vname, file=file_str, kind="arrow",
                                            start=node.start_byte, end=node.end_byte))
        for c in node.children:
            walk(c, parent)

    walk(tree.root_node)
    return out


def extract_symbols_python(src: bytes, tree, file_str: str) -> list[dict]:
    """Extract symbols from Python AST."""
    out = []
    
    def get_name(node) -> str | None:
        for c in node.children:
            if c.type == "identifier":
                return src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
        return None
    
    def walk(node, parent=None):
        t = node.type
        if t == "function_definition":
            name = get_name(node)
            if name:
                full_name = f"{parent}.{name}" if parent else name
                out.append(dict(name=full_name, file=file_str, kind="function",
                                start=node.start_byte, end=node.end_byte))
        elif t == "class_definition":
            name = get_name(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="class",
                                start=node.start_byte, end=node.end_byte))
                # Walk class body for methods
                for c in node.children:
                    if c.type == "block":
                        for stmt in c.children:
                            walk(stmt, parent=name)
                return
        elif t == "assignment" and parent is None:
            # Top-level variable assignment
            for c in node.children:
                if c.type == "identifier":
                    name = src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
                    if name.isupper():  # Constants
                        out.append(dict(name=name, file=file_str, kind="constant",
                                        start=node.start_byte, end=node.end_byte))
                    break
        for c in node.children:
            walk(c, parent)
    
    walk(tree.root_node)
    return out


def extract_calls(src: bytes, tree, lang: str = 'typescript') -> set[str]:
    """Called identifiers within this file (simple + member.prop)."""
    if tree is None:
        return set()
    
    calls = set()
    
    if lang == 'python':
        return extract_calls_python(src, tree)

    # TypeScript extraction (existing)

    def walk(node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn:
                if fn.type == "identifier":
                    calls.add(src[fn.start_byte:fn.end_byte].decode("utf-8", errors="replace"))
                elif fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop:
                        calls.add(src[prop.start_byte:prop.end_byte].decode("utf-8", errors="replace"))
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return calls


def extract_calls_python(src: bytes, tree) -> set[str]:
    """Extract called identifiers from Python AST."""
    calls = set()
    
    def walk(node):
        if node.type == "call":
            fn = node.child_by_field_name("function")
            if fn:
                if fn.type == "identifier":
                    calls.add(src[fn.start_byte:fn.end_byte].decode("utf-8", errors="replace"))
                elif fn.type == "attribute":
                    # obj.method() -> extract method name
                    attr = fn.child_by_field_name("attribute")
                    if attr:
                        calls.add(src[attr.start_byte:attr.end_byte].decode("utf-8", errors="replace"))
        for c in node.children:
            walk(c)
    
    walk(tree.root_node)
    return calls


def extract_imports(src: bytes, tree, lang: str = 'typescript') -> list[str]:
    """All import paths referenced by this file (relative and aliased)."""
    if tree is None:
        return []
    
    if lang == 'python':
        return extract_imports_python(src, tree)
    
    # TypeScript extraction (existing)
    imps = []

    def walk(node):
        if node.type == "import_statement":
            for c in node.children:
                if c.type == "string":
                    val = src[c.start_byte:c.end_byte].decode("utf-8", errors="replace").strip("\"'")
                    # capture relative and alias imports; skip bare node_modules
                    if val.startswith(".") or "/" in val:
                        imps.append(val)
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return imps


def extract_imports_python(src: bytes, tree) -> list[str]:
    """Extract import paths from Python AST."""
    imps = []
    
    def walk(node):
        if node.type == "import_statement":
            # import foo, bar
            for c in node.children:
                if c.type == "dotted_name":
                    name = src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
                    imps.append(name)
        elif node.type == "import_from_statement":
            # from foo import bar
            module = node.child_by_field_name("module_name")
            if module:
                name = src[module.start_byte:module.end_byte].decode("utf-8", errors="replace")
                imps.append(name)
        for c in node.children:
            walk(c)
    
    walk(tree.root_node)
    return imps


def resolve_python_import(imp: str, current_file: Path, target_dir: Path) -> str | None:
    """Resolve a Python import to a repo-relative file path."""
    # Handle relative imports (leading dots)
    if imp.startswith("."):
        dots = len(imp) - len(imp.lstrip("."))
        rest = imp[dots:]
        base = current_file.parent
        for _ in range(dots - 1):
            base = base.parent
        if rest:
            parts = rest.split(".")
            candidate = base / "/".join(parts)
        else:
            candidate = base
    else:
        # Absolute import - try as path from target_dir
        parts = imp.split(".")
        candidate = target_dir / "/".join(parts)
    
    # Try .py extension and __init__.py for packages
    for suffix in [".py", "/__init__.py"]:
        path = Path(str(candidate) + suffix)
        if path.exists():
            try:
                return str(path.relative_to(target_dir))
            except ValueError:
                pass
    return None


# ── Graph build ───────────────────────────────────────────────────────────────
def build_call_file_edges(call_edges: dict, symbols: dict) -> dict[str, set]:
    """Resolve called names → defining files. Returns file → set[file]."""
    name_to_file = {name: sym["file"] for name, sym in symbols.items()}
    result: dict[str, set] = defaultdict(set)
    for calling_file, called_names in call_edges.items():
        for name in called_names:
            if name in name_to_file:
                target = name_to_file[name]
                if target != calling_file:
                    result[calling_file].add(target)
    return result


# ── Path alias resolution ─────────────────────────────────────────────────────
def load_path_aliases(target_dir: Path) -> list[tuple[str, str]]:
    """Walk up from target_dir to find tsconfig.json and extract path aliases.
    Returns [(pattern_prefix, replacement_prefix), ...] sorted longest-first."""
    for candidate in [target_dir, *target_dir.parents]:
        tsconfig = candidate / "tsconfig.json"
        if tsconfig.exists():
            try:
                data = json.loads(tsconfig.read_text())
                base_url = (candidate / data.get("compilerOptions", {}).get("baseUrl", ".")).resolve()
                paths = data.get("compilerOptions", {}).get("paths", {})
                aliases = []
                for pattern, replacements in paths.items():
                    prefix = pattern.rstrip("/*").rstrip("/")
                    for rep in replacements[:1]:
                        rep_prefix = (base_url / rep.rstrip("/*").rstrip("/")).resolve()
                        aliases.append((prefix, str(rep_prefix)))
                return sorted(aliases, key=lambda x: -len(x[0]))
            except Exception:
                pass
    return []


def resolve_import(imp: str, current_file: Path, target_dir: Path,
                   aliases: list[tuple[str, str]]) -> str | None:
    """Resolve an import path to a repo-relative string, or None if unresolvable."""
    if imp.startswith("."):
        resolved = (current_file.parent / imp).resolve()
        for ext in ("", ".ts", ".tsx"):
            candidate = Path(str(resolved) + ext)
            if candidate.exists():
                try:
                    return str(candidate.relative_to(target_dir))
                except ValueError:
                    pass
        return None
    for prefix, rep_prefix in aliases:
        if imp == prefix or imp.startswith(prefix + "/"):
            suffix = imp[len(prefix):].lstrip("/")
            resolved = Path(rep_prefix) / suffix
            for ext in ("", ".ts", ".tsx"):
                candidate = Path(str(resolved) + ext)
                if candidate.exists():
                    try:
                        return str(candidate.relative_to(target_dir))
                    except ValueError:
                        pass
    return None


def build_graph(target_dir: Path):
    """Parse all .ts/.tsx/.py files; return (symbols dict, call edges, import edges)."""
    symbols: dict[str, dict] = {}        # name → symbol record
    sym_by_file: dict[str, list] = defaultdict(list)
    call_edges: dict[str, set] = defaultdict(set)   # caller_file → {called_name}
    import_edges: dict[str, list] = defaultdict(list)  # file → [imported_file]
    sources: dict[str, bytes] = {}

    aliases = load_path_aliases(target_dir)
    
    # Collect all supported files
    ts_files = list(target_dir.rglob("*.ts")) + list(target_dir.rglob("*.tsx"))
    py_files = list(target_dir.rglob("*.py"))
    all_source_files = ts_files + py_files
    
    parse_errors = 0
    unresolved_imports = 0

    for path in all_source_files:
        rel = str(path.relative_to(target_dir))
        try:
            src, tree, lang = parse_file(path)
            if tree is None:
                parse_errors += 1
                continue
        except Exception as e:
            parse_errors += 1
            continue

        sources[rel] = src
        syms = extract_symbols(src, tree, rel, lang)
        for s in syms:
            symbols[s["name"]] = s
            sym_by_file[rel].append(s["name"])

        calls = extract_calls(src, tree, lang)
        call_edges[rel] = calls

        raw_imps = extract_imports(src, tree, lang)
        resolved = []
        for imp in raw_imps:
            if lang == 'python':
                r = resolve_python_import(imp, path, target_dir)
            else:
                r = resolve_import(imp, path, target_dir, aliases)
            if r:
                resolved.append(r)
            else:
                unresolved_imports += 1
        import_edges[rel] = resolved

    call_file_edges = build_call_file_edges(call_edges, symbols)
    return symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_source_files, parse_errors


# ── Cone computation ─────────────────────────────────────────────────────────
def compute_cone(target_name: str, symbols: dict, sym_by_file: dict,
                 call_file_edges: dict, import_edges: dict) -> set[str]:
    """BFS: files reachable from target symbol via call-file edges + imports."""
    if target_name not in symbols:
        return set()
    origin_file = symbols[target_name]["file"]
    visited_files = set()
    queue = deque([origin_file])
    while queue:
        f = queue.popleft()
        if f in visited_files:
            continue
        visited_files.add(f)
        # follow call-resolved file edges
        for target_file in call_file_edges.get(f, set()):
            if target_file not in visited_files:
                queue.append(target_file)
        # follow import edges — already resolved to repo-relative paths by build_graph
        for resolved_file in import_edges.get(f, []):
            if resolved_file in sym_by_file and resolved_file not in visited_files:
                queue.append(resolved_file)
    return visited_files


# ── Measurement ───────────────────────────────────────────────────────────────
def measure(symbols, sym_by_file, call_file_edges, import_edges, sources, all_files):
    total_repo_tokens = sum(tokens(s.decode("utf-8", errors="replace")) for s in sources.values())
    total_files = len(all_files)

    results = []
    # sample: symbols reachable via import or call edges
    sampled = [
        name for name, sym in symbols.items()
        if len(import_edges.get(sym["file"], [])) > 0
        or len(call_file_edges.get(sym["file"], set())) > 0
    ][:20]

    for name in sampled:
        cone_files = compute_cone(name, symbols, sym_by_file, call_file_edges, import_edges)
        cone_tokens = sum(
            tokens(sources[f].decode("utf-8", errors="replace"))
            for f in cone_files if f in sources
        )
        origin = symbols[name]["file"]
        origin_tokens = tokens(sources[origin].decode("utf-8", errors="replace")) if origin in sources else 0
        results.append(dict(
            name=name,
            kind=symbols[name]["kind"],
            origin_file=origin,
            cone_files=len(cone_files),
            cone_tokens=cone_tokens,
            origin_tokens=origin_tokens,
            cone_ratio=round(cone_tokens / total_repo_tokens * 100, 1),
        ))

    return results, total_repo_tokens, total_files


# ── Persistence helpers ───────────────────────────────────────────────────────
def save_graph(graph_data: tuple, db_path=None) -> float:
    """
    Save graph to SQLite. Returns save time in ms.
    
    Args:
        graph_data: tuple from build_graph() - (symbols, sym_by_file, call_edges,
                    call_file_edges, import_edges, sources, all_files, parse_errors)
        db_path: optional custom database path
    """
    from persistence import GraphStore
    store = GraphStore(db_path) if db_path else GraphStore()
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, parse_errors = graph_data
    return store.save(symbols, sym_by_file, call_edges, call_file_edges,
                      import_edges, sources, all_files, parse_errors)


def load_graph(db_path=None) -> tuple | None:
    """
    Load graph from SQLite. Returns tuple matching build_graph() output,
    or None if no saved state.
    
    Returns: (symbols, sym_by_file, call_edges, call_file_edges,
              import_edges, sources, all_files, parse_errors)
              Plus load_time_ms as 9th element.
    """
    from persistence import GraphStore
    store = GraphStore(db_path) if db_path else GraphStore()
    return store.load()


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Use config if available, otherwise fall back to hardcoded default
    try:
        from config import get_target_dir
        target = get_target_dir()
    except (ImportError, ValueError):
        target = TARGET_DIR
    
    print(f"Parsing {target} ...")
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, errors = build_graph(target)

    print(f"  Files parsed:  {len(all_files) - errors} / {len(all_files)}")
    print(f"  Parse errors:  {errors}")
    print(f"  Symbols found: {len(symbols)}")
    print(f"  Files w/ calls: {sum(1 for v in call_edges.values() if v)}")
    print(f"  Cross-file call edges: {sum(len(v) for v in call_file_edges.values())}")
    print()

    results, total_tokens, total_files = measure(
        symbols, sym_by_file, call_file_edges, import_edges, sources, all_files
    )

    print(f"{'Symbol':<40} {'Kind':<8} {'Cone files':>10} {'Cone tok':>10} {'Origin tok':>11} {'% of repo':>10}")
    print("-" * 95)
    for r in sorted(results, key=lambda x: x["cone_tokens"]):
        print(f"{r['name'][:39]:<40} {r['kind']:<8} {r['cone_files']:>10} "
              f"{r['cone_tokens']:>10,} {r['origin_tokens']:>11,} {r['cone_ratio']:>9.1f}%")

    print()
    print(f"Total repo tokens (lib/): {total_tokens:,}")
    print(f"Total files: {total_files}")
    if results:
        avg_ratio = sum(r["cone_ratio"] for r in results) / len(results)
        avg_files = sum(r["cone_files"] for r in results) / len(results)
        print(f"Avg cone size: {avg_ratio:.1f}% of repo, {avg_files:.1f} files")
        print(f"Avg cone reduction vs full repo: {100 - avg_ratio:.1f}%")
