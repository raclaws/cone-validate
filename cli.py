#!/usr/bin/env python3
"""
cone-validate CLI — multi-agent coding architecture toolkit.

Usage:
    cone init [--target DIR]              Initialize project config
    cone graph [--save]                   Build dependency graph
    cone cone SYMBOL [--json]             Show cone for a symbol
    cone run TASK [--file FILE]           Run oracle loop on a task
    cone watch                            Watch for changes (git hook mode)
    cone dashboard [--json]               Show cost dashboard
    cone test [--suite SUITE]             Run debugger tests
"""

import argparse
import json
import sys
from pathlib import Path

def cmd_init(args):
    """Initialize cone-validate config for a project."""
    from config import DEFAULT_CONFIG
    
    config_dir = Path.home() / ".cone-validate"
    config_file = config_dir / "config.yaml"
    
    if config_file.exists() and not args.force:
        print(f"Config already exists: {config_file}")
        print("Use --force to overwrite")
        return 1
    
    config_dir.mkdir(parents=True, exist_ok=True)
    
    # Copy example config
    example = Path(__file__).parent / "config.example.yaml"
    if example.exists():
        content = example.read_text()
        if args.target:
            content = content.replace(
                "target_dir: /root/repos/twenty-dollar/frontend/src",
                f"target_dir: {args.target}"
            )
        config_file.write_text(content)
        print(f"✓ Created {config_file}")
        print(f"  Edit target_dir and project_root for your project")
    else:
        print("Example config not found, creating minimal config...")
        config_file.write_text(f"""# cone-validate config
target_dir: {args.target or '/path/to/your/src'}
project_root: {Path(args.target).parent if args.target else '/path/to/your/project'}
gateway_url: https://gateway.ai.cloudflare.com/v1/.../custom-deadog/v1/chat/completions
model_cheap: claude-haiku-4.5
model_strong: claude-sonnet-4
""")
        print(f"✓ Created {config_file}")
    
    return 0

def cmd_graph(args):
    """Build and optionally save the dependency graph."""
    from validate import build_graph
    from config import get_target_dir
    import time
    
    target = args.target or get_target_dir()
    print(f"Building graph for {target}...")
    
    start = time.time()
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, errors = build_graph(target)
    elapsed = time.time() - start
    
    print(f"\n  Files parsed:  {len(all_files)}")
    print(f"  Parse errors:  {errors}")
    print(f"  Symbols found: {len(symbols)}")
    print(f"  Cross-file edges: {len(call_file_edges)}")
    print(f"  Build time:    {elapsed:.2f}s")
    
    if args.save:
        from persistence import GraphStore
        store = GraphStore()
        store.save(symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files)
        print(f"\n✓ Graph saved to {store.db_path}")
    
    if args.json:
        print(json.dumps({
            "files": len(all_files),
            "symbols": len(symbols),
            "edges": len(call_file_edges),
            "errors": len(errors),
            "build_time_s": round(elapsed, 2)
        }))
    
    return 0

def cmd_cone(args):
    """Show the dependency cone for a symbol."""
    from validate import build_graph, compute_cone
    from config import get_target_dir
    import tiktoken
    
    target = get_target_dir()
    
    # Try to load from persistence first
    try:
        from persistence import GraphStore
        store = GraphStore()
        data = store.load()
        if data:
            symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files = data
            print("(loaded from cache)")
        else:
            raise ValueError("No cached graph")
    except:
        print("Building graph...")
        symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, _ = build_graph(target)
    
    # Find symbol (symbols is dict: name -> {name, file, kind, start, end})
    symbol = args.symbol
    
    if symbol not in symbols:
        print(f"Symbol '{symbol}' not found")
        print(f"\nDid you mean one of these?")
        for name in sorted(symbols.keys())[:30]:
            if symbol.lower() in name.lower():
                info = symbols[name]
                print(f"  {name} ({info['kind']}) in {Path(info['file']).name}")
        return 1
    
    info = symbols[symbol]
    name = symbol
    kind = info['kind']
    origin_file = info['file']
    
    # Compute cone
    cone_files = compute_cone(name, symbols, sym_by_file, call_file_edges, import_edges)
    
    enc = tiktoken.get_encoding("cl100k_base")
    
    def decode_source(src):
        if isinstance(src, bytes):
            return src.decode('utf-8', errors='replace')
        return src or ""
    
    cone_tokens = sum(len(enc.encode(decode_source(sources.get(f)))) for f in cone_files)
    total_tokens = sum(len(enc.encode(decode_source(s))) for s in sources.values())
    
    if args.json:
        print(json.dumps({
            "symbol": name,
            "kind": kind,
            "origin": origin_file,
            "cone_files": sorted(cone_files),
            "cone_size": len(cone_files),
            "total_files": len(all_files),
            "cone_tokens": cone_tokens,
            "total_tokens": total_tokens,
            "reduction_pct": round(100 * (1 - cone_tokens / total_tokens), 1)
        }, indent=2))
    else:
        print(f"\n  Symbol: {name} ({kind})")
        print(f"  Origin: {origin_file}")
        print(f"\n  Cone files ({len(cone_files)}/{len(all_files)}):")
        for f in sorted(cone_files)[:20]:
            print(f"    {f}")
        if len(cone_files) > 20:
            print(f"    ... and {len(cone_files) - 20} more")
        
        print(f"\n  Tokens: {cone_tokens:,} / {total_tokens:,} ({100 * cone_tokens / total_tokens:.1f}%)")
        print(f"  Reduction: {100 * (1 - cone_tokens / total_tokens):.1f}%")
    
    return 0

def cmd_run(args):
    """Run the oracle loop on a coding task."""
    from oracle_loop import oracle_loop
    from validate import build_graph
    from config import get_target_dir, get_project_root
    
    task = args.task
    if args.file:
        task = Path(args.file).read_text()
    
    if not task:
        print("Error: provide a task description or --file")
        return 1
    
    print("Building graph...")
    target = get_target_dir()
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, _ = build_graph(target)
    
    print(f"\nTask: {task[:100]}{'...' if len(task) > 100 else ''}\n")
    
    result = oracle_loop(symbols, sym_by_file, call_file_edges, import_edges, sources)
    
    if result.get("success"):
        print("\n✓ Task completed successfully")
        print(f"  Attempts: {result.get('attempts', 1)}")
        print(f"  Tokens used: {result.get('total_tokens', 'N/A')}")
    else:
        print("\n✗ Task failed")
        print(f"  Error: {result.get('error', 'Unknown')}")
    
    return 0 if result.get("success") else 1

def cmd_watch(args):
    """Watch for git changes and emit deltas."""
    from git_hook import main as git_hook_main
    
    if args.dry_run:
        sys.argv = ["git_hook.py", "--dry-run"]
    else:
        sys.argv = ["git_hook.py"]
    
    return git_hook_main()

def cmd_dashboard(args):
    """Show the cost dashboard."""
    from dashboard import main as dashboard_main
    
    argv = ["dashboard.py"]
    if args.json:
        argv.append("--json")
    if args.run:
        argv.extend(["--run", args.run])
    if args.summary:
        argv.append("--summary")
    
    sys.argv = argv
    return dashboard_main()

def cmd_test(args):
    """Run the debugger test suite."""
    import subprocess
    
    cmd = ["python3", "debugger.py"]
    if args.suite:
        cmd.extend(["--test", args.suite])
    if args.report:
        cmd.append("--report")
    
    return subprocess.call(cmd, cwd=Path(__file__).parent)

def main():
    parser = argparse.ArgumentParser(
        prog="cone",
        description="Multi-agent coding architecture toolkit"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # init
    p_init = subparsers.add_parser("init", help="Initialize project config")
    p_init.add_argument("--target", "-t", help="Target source directory")
    p_init.add_argument("--force", "-f", action="store_true", help="Overwrite existing config")
    
    # graph
    p_graph = subparsers.add_parser("graph", help="Build dependency graph")
    p_graph.add_argument("--target", "-t", help="Target directory (overrides config)")
    p_graph.add_argument("--save", "-s", action="store_true", help="Save to SQLite cache")
    p_graph.add_argument("--json", "-j", action="store_true", help="JSON output")
    
    # cone
    p_cone = subparsers.add_parser("cone", help="Show cone for a symbol")
    p_cone.add_argument("symbol", help="Symbol name to analyze")
    p_cone.add_argument("--json", "-j", action="store_true", help="JSON output")
    
    # run
    p_run = subparsers.add_parser("run", help="Run oracle loop on a task")
    p_run.add_argument("task", nargs="?", help="Task description")
    p_run.add_argument("--file", "-f", help="Read task from file")
    
    # watch
    p_watch = subparsers.add_parser("watch", help="Watch for changes (git hook)")
    p_watch.add_argument("--dry-run", "-n", action="store_true", help="Don't emit deltas")
    
    # dashboard
    p_dash = subparsers.add_parser("dashboard", help="Show cost dashboard")
    p_dash.add_argument("--json", "-j", action="store_true", help="JSON output")
    p_dash.add_argument("--run", "-r", help="Show specific run")
    p_dash.add_argument("--summary", "-s", action="store_true", help="Summary only")
    
    # test
    p_test = subparsers.add_parser("test", help="Run debugger tests")
    p_test.add_argument("--suite", "-s", help="Specific suite (correctness, cost_efficiency, reliability)")
    p_test.add_argument("--report", "-r", action="store_true", help="Generate markdown report")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 0
    
    # Dispatch to command
    commands = {
        "init": cmd_init,
        "graph": cmd_graph,
        "cone": cmd_cone,
        "run": cmd_run,
        "watch": cmd_watch,
        "dashboard": cmd_dashboard,
        "test": cmd_test,
    }
    
    return commands[args.command](args)

if __name__ == "__main__":
    sys.exit(main())
