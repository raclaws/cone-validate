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
import tree_sitter_go as ts_go
import tree_sitter_rust as ts_rust

# ── Setup ────────────────────────────────────────────────────────────────────
TS_LANGUAGE = Language(ts_typescript.language_typescript())
PY_LANGUAGE = Language(ts_python.language())
GO_LANGUAGE = Language(ts_go.language())
RS_LANGUAGE = Language(ts_rust.language())

ts_parser = Parser(TS_LANGUAGE)
py_parser = Parser(PY_LANGUAGE)
go_parser = Parser(GO_LANGUAGE)
rs_parser = Parser(RS_LANGUAGE)

enc = tiktoken.get_encoding("cl100k_base")

def get_parser_for_file(path: Path):
    """Return appropriate parser and language for file extension."""
    ext = path.suffix.lower()
    if ext in ('.ts', '.tsx'):
        return ts_parser, 'typescript'
    elif ext == '.py':
        return py_parser, 'python'
    elif ext == '.go':
        return go_parser, 'go'
    elif ext == '.rs':
        return rs_parser, 'rust'
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
    elif lang == 'go':
        return extract_symbols_go(src, tree, file_str)
    elif lang == 'rust':
        return extract_symbols_rust(src, tree, file_str)
    
    # TypeScript extraction (existing code)
    out = []

    def first_ident(node) -> str | None:
        for c in node.children:
            if c.type in ("identifier", "property_identifier", "type_identifier"):
                return src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
        return None
    
    def get_heritage(node) -> dict:
        """Extract implements/extends from class or interface."""
        heritage = {"extends": [], "implements": []}
        for c in node.children:
            if c.type == "class_heritage":
                for h in c.children:
                    if h.type == "extends_clause":
                        for t in h.children:
                            if t.type == "identifier" or t.type == "type_identifier":
                                heritage["extends"].append(src[t.start_byte:t.end_byte].decode("utf-8", errors="replace"))
                    elif h.type == "implements_clause":
                        for t in h.children:
                            if t.type == "identifier" or t.type == "type_identifier":
                                heritage["implements"].append(src[t.start_byte:t.end_byte].decode("utf-8", errors="replace"))
            elif c.type == "extends_type_clause":
                # interface Foo extends Bar, Baz
                for t in c.children:
                    if t.type == "identifier" or t.type == "type_identifier":
                        heritage["extends"].append(src[t.start_byte:t.end_byte].decode("utf-8", errors="replace"))
        return heritage

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
                                start=node.start_byte, end=node.end_byte, parent_type=parent))
        elif t == "class_declaration":
            name = first_ident(node)
            if name:
                heritage = get_heritage(node)
                out.append(dict(name=name, file=file_str, kind="class",
                                start=node.start_byte, end=node.end_byte,
                                extends=heritage["extends"], implements=heritage["implements"]))
                for c in node.children:
                    walk(c, parent=name)
                return
        elif t == "interface_declaration":
            name = first_ident(node)
            if name:
                heritage = get_heritage(node)
                out.append(dict(name=name, file=file_str, kind="interface",
                                start=node.start_byte, end=node.end_byte,
                                extends=heritage["extends"]))
                # Extract interface methods
                for c in node.children:
                    if c.type == "object_type" or c.type == "interface_body":
                        for member in c.children:
                            if member.type in ("property_signature", "method_signature"):
                                mname = first_ident(member)
                                if mname:
                                    out.append(dict(name=f"{name}.{mname}", file=file_str, kind="interface_method",
                                                    start=member.start_byte, end=member.end_byte, parent_type=name))
        elif t == "type_alias_declaration":
            name = first_ident(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="type",
                                start=node.start_byte, end=node.end_byte))
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


def extract_symbols_go(src: bytes, tree, file_str: str) -> list[dict]:
    """Extract symbols from Go AST."""
    out = []
    
    def get_name(node) -> str | None:
        for c in node.children:
            if c.type == "identifier":
                return src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
        return None
    
    def walk(node):
        t = node.type
        if t == "function_declaration":
            name = get_name(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="function",
                                start=node.start_byte, end=node.end_byte))
        elif t == "method_declaration":
            # func (r Receiver) Method()
            name = None
            receiver = None
            for c in node.children:
                if c.type == "parameter_list" and receiver is None:
                    # First param list is receiver
                    for p in c.children:
                        if p.type == "parameter_declaration":
                            for t in p.children:
                                if t.type == "type_identifier" or t.type == "pointer_type":
                                    receiver = src[t.start_byte:t.end_byte].decode("utf-8", errors="replace").strip("*")
                elif c.type == "field_identifier":
                    name = src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
            if name:
                full_name = f"{receiver}.{name}" if receiver else name
                out.append(dict(name=full_name, file=file_str, kind="method",
                                start=node.start_byte, end=node.end_byte))
        elif t == "type_declaration":
            for c in node.children:
                if c.type == "type_spec":
                    name = get_name(c)
                    kind = "type"
                    for sub in c.children:
                        if sub.type == "struct_type":
                            kind = "struct"
                        elif sub.type == "interface_type":
                            kind = "interface"
                    if name:
                        out.append(dict(name=name, file=file_str, kind=kind,
                                        start=node.start_byte, end=node.end_byte))
        elif t == "const_declaration" or t == "var_declaration":
            for c in node.children:
                if c.type == "const_spec" or c.type == "var_spec":
                    name = get_name(c)
                    if name:
                        out.append(dict(name=name, file=file_str, 
                                        kind="constant" if t == "const_declaration" else "variable",
                                        start=node.start_byte, end=node.end_byte))
        for c in node.children:
            walk(c)
    
    walk(tree.root_node)
    return out


def extract_symbols_rust(src: bytes, tree, file_str: str) -> list[dict]:
    """Extract symbols from Rust AST."""
    out = []
    
    def get_name(node) -> str | None:
        for c in node.children:
            if c.type == "identifier" or c.type == "type_identifier":
                return src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
        return None
    
    def walk(node, impl_target=None):
        t = node.type
        if t == "function_item":
            name = get_name(node)
            if name:
                full_name = f"{impl_target}.{name}" if impl_target else name
                out.append(dict(name=full_name, file=file_str, kind="function",
                                start=node.start_byte, end=node.end_byte))
        elif t == "struct_item":
            name = get_name(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="struct",
                                start=node.start_byte, end=node.end_byte))
        elif t == "enum_item":
            name = get_name(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="enum",
                                start=node.start_byte, end=node.end_byte))
        elif t == "trait_item":
            name = get_name(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="trait",
                                start=node.start_byte, end=node.end_byte))
        elif t == "impl_item":
            # impl Type { ... } or impl Trait for Type { ... }
            target = None
            for c in node.children:
                if c.type == "type_identifier":
                    target = src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
                    break
                elif c.type == "generic_type":
                    target = get_name(c)
                    break
            for c in node.children:
                if c.type == "declaration_list":
                    for item in c.children:
                        walk(item, impl_target=target)
            return
        elif t == "const_item":
            name = get_name(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="constant",
                                start=node.start_byte, end=node.end_byte))
        elif t == "static_item":
            name = get_name(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="static",
                                start=node.start_byte, end=node.end_byte))
        elif t == "mod_item":
            name = get_name(node)
            if name:
                out.append(dict(name=name, file=file_str, kind="module",
                                start=node.start_byte, end=node.end_byte))
        for c in node.children:
            walk(c, impl_target)
    
    walk(tree.root_node)
    return out


def extract_calls(src: bytes, tree, lang: str = 'typescript') -> set[str]:
    """Called identifiers within this file (simple + member.prop)."""
    if tree is None:
        return set()
    
    calls = set()
    
    if lang == 'python':
        return extract_calls_python(src, tree)
    elif lang == 'go':
        return extract_calls_go(src, tree)
    elif lang == 'rust':
        return extract_calls_rust(src, tree)

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


def extract_calls_go(src: bytes, tree) -> set[str]:
    """Extract called identifiers from Go AST."""
    calls = set()
    
    def walk(node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn:
                if fn.type == "identifier":
                    calls.add(src[fn.start_byte:fn.end_byte].decode("utf-8", errors="replace"))
                elif fn.type == "selector_expression":
                    # pkg.Func() or obj.Method()
                    field = fn.child_by_field_name("field")
                    if field:
                        calls.add(src[field.start_byte:field.end_byte].decode("utf-8", errors="replace"))
        for c in node.children:
            walk(c)
    
    walk(tree.root_node)
    return calls


def extract_calls_rust(src: bytes, tree) -> set[str]:
    """Extract called identifiers from Rust AST."""
    calls = set()
    
    def walk(node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn:
                if fn.type == "identifier":
                    calls.add(src[fn.start_byte:fn.end_byte].decode("utf-8", errors="replace"))
                elif fn.type == "field_expression":
                    # obj.method()
                    field = fn.child_by_field_name("field")
                    if field:
                        calls.add(src[field.start_byte:field.end_byte].decode("utf-8", errors="replace"))
                elif fn.type == "scoped_identifier":
                    # Type::method() or module::func()
                    name = fn.child_by_field_name("name")
                    if name:
                        calls.add(src[name.start_byte:name.end_byte].decode("utf-8", errors="replace"))
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
    elif lang == 'go':
        return extract_imports_go(src, tree)
    elif lang == 'rust':
        return extract_imports_rust(src, tree)
    
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


def extract_imports_go(src: bytes, tree) -> list[str]:
    """Extract import paths from Go AST."""
    imps = []
    
    def walk(node):
        if node.type == "import_declaration":
            for c in node.children:
                if c.type == "import_spec":
                    for s in c.children:
                        if s.type == "interpreted_string_literal":
                            path = src[s.start_byte:s.end_byte].decode("utf-8", errors="replace").strip('"')
                            imps.append(path)
                elif c.type == "import_spec_list":
                    for spec in c.children:
                        if spec.type == "import_spec":
                            for s in spec.children:
                                if s.type == "interpreted_string_literal":
                                    path = src[s.start_byte:s.end_byte].decode("utf-8", errors="replace").strip('"')
                                    imps.append(path)
        for c in node.children:
            walk(c)
    
    walk(tree.root_node)
    return imps


def extract_imports_rust(src: bytes, tree) -> list[str]:
    """Extract use/mod paths from Rust AST."""
    imps = []
    
    def walk(node):
        if node.type == "use_declaration":
            for c in node.children:
                if c.type in ("scoped_identifier", "use_as_clause", "scoped_use_list", "identifier"):
                    path = src[c.start_byte:c.end_byte].decode("utf-8", errors="replace")
                    if "::" in path:
                        base = path.split("::")[0]
                        if base not in ("self", "super", "crate"):
                            imps.append(base)
                    elif path not in ("self", "super", "crate"):
                        imps.append(path)
        elif node.type == "mod_item":
            has_body = any(c.type == "declaration_list" for c in node.children)
            if not has_body:
                for c in node.children:
                    if c.type == "identifier":
                        imps.append(src[c.start_byte:c.end_byte].decode("utf-8", errors="replace"))
        for c in node.children:
            walk(c)
    
    walk(tree.root_node)
    return imps


def resolve_go_import(imp: str, current_file: Path, target_dir: Path) -> str | None:
    """Resolve a Go import to a repo-relative file path."""
    parts = imp.split("/")
    for i in range(len(parts)):
        subpath = "/".join(parts[i:])
        candidate = target_dir / subpath
        if candidate.is_dir():
            go_files = list(candidate.glob("*.go"))
            if go_files:
                try:
                    return str(go_files[0].relative_to(target_dir))
                except ValueError:
                    pass
    return None


def resolve_rust_import(imp: str, current_file: Path, target_dir: Path) -> str | None:
    """Resolve a Rust use/mod to a repo-relative file path."""
    for candidate in [
        current_file.parent / f"{imp}.rs",
        current_file.parent / imp / "mod.rs",
        target_dir / f"{imp}.rs",
        target_dir / imp / "mod.rs",
    ]:
        if candidate.exists():
            try:
                return str(candidate.relative_to(target_dir))
            except ValueError:
                pass
    return None


# ── Graph build ───────────────────────────────────────────────────────────────
def build_type_hierarchy(symbols: dict) -> dict:
    """Build type hierarchy: interface/class → implementors/extenders."""
    # type_name → set of types that implement/extend it
    implementors: dict[str, set] = defaultdict(set)
    # type_name → set of method names defined on it
    type_methods: dict[str, set] = defaultdict(set)
    # method_name → set of types that define it
    method_to_types: dict[str, set] = defaultdict(set)
    
    for name, sym in symbols.items():
        kind = sym.get("kind", "")
        
        # Track class/interface inheritance
        if kind in ("class", "interface"):
            for ext in sym.get("extends", []):
                implementors[ext].add(name)
            for impl in sym.get("implements", []):
                implementors[impl].add(name)
        
        # Track methods and their parent types
        if kind in ("method", "interface_method"):
            parent = sym.get("parent_type")
            if parent:
                method_name = name.split(".")[-1]  # "Foo.render" -> "render"
                type_methods[parent].add(method_name)
                method_to_types[method_name].add(parent)
    
    return {
        "implementors": dict(implementors),
        "type_methods": dict(type_methods),
        "method_to_types": dict(method_to_types),
    }


def build_call_file_edges(call_edges: dict, symbols: dict, type_hierarchy: dict = None) -> dict[str, set]:
    """Resolve called names → defining files. Returns file → set[file].
    
    With type_hierarchy: narrows method calls to types in the same lineage.
    Without: falls back to name-based matching (may over-approximate).
    """
    name_to_file = {name: sym["file"] for name, sym in symbols.items()}
    result: dict[str, set] = defaultdict(set)
    
    # Build reverse lookup: method_name → [(full_name, parent_type, file), ...]
    method_lookup: dict[str, list] = defaultdict(list)
    for name, sym in symbols.items():
        if sym.get("kind") in ("method", "interface_method"):
            method_name = name.split(".")[-1]
            parent = sym.get("parent_type")
            method_lookup[method_name].append((name, parent, sym["file"]))
    
    # Get implementors for type hierarchy narrowing
    implementors = type_hierarchy.get("implementors", {}) if type_hierarchy else {}
    
    def get_type_lineage(type_name: str, seen: set = None) -> set[str]:
        """Get all types in the lineage (type + all implementors recursively)."""
        if seen is None:
            seen = set()
        if type_name in seen:
            return seen
        seen.add(type_name)
        for impl in implementors.get(type_name, []):
            get_type_lineage(impl, seen)
        return seen
    
    for calling_file, called_names in call_edges.items():
        for name in called_names:
            # Direct match (functions, classes, etc.)
            if name in name_to_file:
                target = name_to_file[name]
                if target != calling_file:
                    result[calling_file].add(target)
            # Method call - check if we can narrow by type
            elif name in method_lookup:
                candidates = method_lookup[name]
                if len(candidates) == 1:
                    # Only one definition - no ambiguity
                    _, _, target = candidates[0]
                    if target != calling_file:
                        result[calling_file].add(target)
                else:
                    # Multiple definitions - include all (can't narrow without call-site type info)
                    # Future: extract receiver type from call site for better narrowing
                    for _, _, target in candidates:
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
    go_files = list(target_dir.rglob("*.go"))
    rs_files = list(target_dir.rglob("*.rs"))
    all_source_files = ts_files + py_files + go_files + rs_files
    
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
            elif lang == 'go':
                r = resolve_go_import(imp, path, target_dir)
            elif lang == 'rust':
                r = resolve_rust_import(imp, path, target_dir)
            else:
                r = resolve_import(imp, path, target_dir, aliases)
            if r:
                resolved.append(r)
            else:
                unresolved_imports += 1
        import_edges[rel] = resolved

    call_file_edges = build_call_file_edges(call_edges, symbols, build_type_hierarchy(symbols))
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
