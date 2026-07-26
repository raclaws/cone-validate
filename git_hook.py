#!/usr/bin/env python3
"""
Git hook: post-commit delta emission.

On each commit:
  1. Get list of changed .ts/.tsx files from git diff
  2. Reparse only those files (incremental graph update)
  3. AST diff before/after → extract changed symbols
  4. Emit deltas to SubscriptionBus

Install: ln -sf $(pwd)/git_hook.py /path/to/repo/.git/hooks/post-commit
"""

import os, sys, subprocess, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from validate import (
    build_graph, load_path_aliases, resolve_import,
    parse_file, extract_symbols, extract_calls, extract_imports,
    build_call_file_edges, parser,
)
from subscription import SubscriptionBus

# ── Config ────────────────────────────────────────────────────────────────────
TARGET_DIR   = Path("/root/repos/twenty-dollar/frontend/src")  # Legacy default
PROJECT_ROOT = Path("/root/repos/twenty-dollar/frontend")      # Legacy default

# Import config - these override the legacy defaults when config is set
try:
    from config import get_target_dir, get_project_root
    TARGET_DIR = get_target_dir()
    PROJECT_ROOT = get_project_root()
except (ImportError, ValueError):
    pass  # Use legacy defaults if config not available

# Global bus — in production this would be a persistent service
_bus: SubscriptionBus | None = None


def get_bus() -> SubscriptionBus:
    global _bus
    if _bus is None:
        _bus = SubscriptionBus(window=300)
    return _bus


# ── Git helpers ───────────────────────────────────────────────────────────────
def get_changed_files(project_root: Path, ref: str = "HEAD~1..HEAD") -> list[str]:
    """Get .ts/.tsx files changed between refs. Returns paths relative to git root."""
    result = subprocess.run(
        ["git", "diff", "--name-only", ref, "--", "*.ts", "*.tsx"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def get_git_root(project_root: Path) -> Path:
    """Get the git repository root."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=project_root, capture_output=True, text=True
    )
    if result.returncode != 0:
        return project_root
    return Path(result.stdout.strip())


def get_file_at_ref(project_root: Path, filepath: str, ref: str) -> bytes | None:
    """Get file contents at a specific git ref."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{filepath}"],
        cwd=project_root, capture_output=True
    )
    if result.returncode != 0:
        return None
    return result.stdout


# ── AST diff ──────────────────────────────────────────────────────────────────
def ast_diff(old_src: bytes | None, new_src: bytes, file_str: str) -> dict:
    """Compare symbol sets before/after. Returns added/removed/changed."""
    if old_src is None:
        # New file — everything is added
        new_tree = parser.parse(new_src)
        new_syms = [s["name"] for s in extract_symbols(new_src, new_tree, file_str)]
        return {"added": sorted(new_syms), "removed": [], "changed": []}

    old_tree = parser.parse(old_src)
    new_tree = parser.parse(new_src)

    old_syms = {s["name"]: old_src[s["start"]:s["end"]] for s in extract_symbols(old_src, old_tree, file_str)}
    new_syms = {s["name"]: new_src[s["start"]:s["end"]] for s in extract_symbols(new_src, new_tree, file_str)}

    added   = set(new_syms) - set(old_syms)
    removed = set(old_syms) - set(new_syms)
    changed = {n for n in set(old_syms) & set(new_syms) if old_syms[n] != new_syms[n]}

    return {
        "added":   sorted(added),
        "removed": sorted(removed),
        "changed": sorted(changed),
    }


# ── Incremental graph update ──────────────────────────────────────────────────
class IncrementalGraph:
    """
    Maintains a live graph that can be incrementally updated.
    Wraps the static build_graph() for initial load, then patches on deltas.
    """

    def __init__(self, target_dir: Path):
        self.target_dir = target_dir
        self.symbols = {}
        self.sym_by_file = {}
        self.call_edges = {}
        self.call_file_edges = {}
        self.import_edges = {}
        self.sources = {}
        self.aliases = []
        self._loaded = False

    def load(self):
        """Full graph build (expensive, do once at startup)."""
        from collections import defaultdict
        result = build_graph(self.target_dir)
        (self.symbols, self.sym_by_file, self.call_edges,
         self.call_file_edges, self.import_edges, self.sources,
         _, _) = result
        self.aliases = load_path_aliases(self.target_dir)
        self._loaded = True

    def update_file(self, rel_path: str, new_src: bytes) -> dict:
        """
        Update graph for a single file change.
        Returns AST diff (added/removed/changed symbols).
        """
        if not self._loaded:
            self.load()

        old_src = self.sources.get(rel_path)
        diff = ast_diff(old_src, new_src, rel_path)

        # Update sources
        self.sources[rel_path] = new_src

        # Reparse and update edges
        try:
            _, tree = parse_file.__wrapped__(new_src) if hasattr(parse_file, '__wrapped__') else (None, None)
            tree = parser.parse(new_src)

            # Update symbols
            old_syms = set(self.sym_by_file.get(rel_path, []))
            new_syms_list = extract_symbols(new_src, tree, rel_path)

            # Remove old symbols
            for name in old_syms:
                self.symbols.pop(name, None)

            # Add new symbols
            self.sym_by_file[rel_path] = []
            for s in new_syms_list:
                self.symbols[s["name"]] = s
                self.sym_by_file[rel_path].append(s["name"])

            # Update call edges
            self.call_edges[rel_path] = extract_calls(new_src, tree)

            # Update import edges (with resolution)
            abs_path = self.target_dir / rel_path
            raw_imps = extract_imports(new_src, tree)
            resolved = []
            for imp in raw_imps:
                r = resolve_import(imp, abs_path, self.target_dir, self.aliases)
                if r:
                    resolved.append(r)
            self.import_edges[rel_path] = resolved

            # Rebuild call_file_edges (could be optimized to incremental)
            self.call_file_edges = build_call_file_edges(self.call_edges, self.symbols)

        except Exception as e:
            print(f"  [WARN] Failed to update graph for {rel_path}: {e}")

        return diff

    def remove_file(self, rel_path: str) -> dict:
        """Remove a file from the graph."""
        diff = {"added": [], "removed": [], "changed": []}

        if rel_path in self.sources:
            # Get symbols that will be removed
            diff["removed"] = list(self.sym_by_file.get(rel_path, []))

            # Clean up
            for name in diff["removed"]:
                self.symbols.pop(name, None)
            self.sym_by_file.pop(rel_path, None)
            self.call_edges.pop(rel_path, None)
            self.import_edges.pop(rel_path, None)
            self.sources.pop(rel_path, None)

            # Rebuild call_file_edges
            self.call_file_edges = build_call_file_edges(self.call_edges, self.symbols)

        return diff


# ── Hook entry point ──────────────────────────────────────────────────────────
def process_commit(
    project_root: Path,
    target_dir: Path,
    graph: IncrementalGraph | None = None,
    bus: SubscriptionBus | None = None,
    ref: str = "HEAD~1..HEAD",
    emitting_agent: str = "git_hook",
) -> list[dict]:
    """
    Process a commit and emit deltas for changed files.
    Returns list of emitted deltas.
    """
    if graph is None:
        graph = IncrementalGraph(target_dir)
        graph.load()

    if bus is None:
        bus = get_bus()

    git_root = get_git_root(project_root)
    changed_files = get_changed_files(project_root, ref)
    if not changed_files:
        print("  No .ts/.tsx files changed.")
        return []

    print(f"  Changed files: {changed_files}")

    deltas = []
    for filepath in changed_files:
        # filepath is relative to git root (e.g., "frontend/src/lib/format.ts")
        full_path = git_root / filepath

        # Compute relative path from target_dir
        try:
            rel_path = str(full_path.relative_to(target_dir))
        except ValueError:
            # File not under target_dir, skip
            print(f"    {filepath}: outside target_dir, skipping")
            continue

        # Get OLD version from git (before the commit) for proper diffing
        ref_parts = ref.split("..")
        old_ref = ref_parts[0] if len(ref_parts) == 2 else f"{ref}~1"
        old_src = get_file_at_ref(git_root, filepath, old_ref)

        # Get new content (current HEAD)
        if full_path.exists():
            new_src = full_path.read_bytes()
            # Compute AST diff using old_src from git, not from loaded graph
            diff = ast_diff(old_src, new_src, rel_path)
            # Update graph with new content
            graph.sources[rel_path] = new_src
        else:
            # File was deleted
            diff = graph.remove_file(rel_path)
            new_src = b""

        all_changed = diff["added"] + diff["changed"] + diff["removed"]
        if not all_changed:
            continue

        print(f"  {rel_path}: +{len(diff['added'])} ~{len(diff['changed'])} -{len(diff['removed'])}")

        # Emit delta
        notifications = bus.emit_delta(
            emitting_agent=emitting_agent,
            changed_file=rel_path,
            changed_symbols=all_changed,
            new_src=new_src.decode("utf-8", errors="replace") if full_path.exists() else "",
        )

        delta = {
            "file": rel_path,
            "diff": diff,
            "notifications": len(notifications),
        }
        deltas.append(delta)

        for n in notifications:
            print(f"    → {n.summary()}")

    return deltas


# ── CLI / hook entry ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser_cli = argparse.ArgumentParser(description="Git hook: emit deltas on commit")
    parser_cli.add_argument("--ref", default="HEAD~1..HEAD", help="Git ref range")
    parser_cli.add_argument("--project", default=str(PROJECT_ROOT), help="Project root")
    parser_cli.add_argument("--target", default=str(TARGET_DIR), help="Target dir for graph")
    parser_cli.add_argument("--dry-run", action="store_true", help="Don't emit deltas")
    args = parser_cli.parse_args()

    project_root = Path(args.project)
    target_dir = Path(args.target)

    print(f"Git hook processing {args.ref} in {project_root}")
    print(f"Graph target: {target_dir}")

    # Build graph
    print("Loading graph ...")
    graph = IncrementalGraph(target_dir)
    graph.load()
    print(f"  {len(graph.symbols)} symbols loaded")

    # Process
    if args.dry_run:
        print("\n[DRY RUN] Would process:")
        changed = get_changed_files(project_root, args.ref)
        for f in changed:
            print(f"  {f}")
    else:
        print("\nProcessing commit ...")
        deltas = process_commit(project_root, target_dir, graph, ref=args.ref)
        print(f"\nEmitted {len(deltas)} deltas")
