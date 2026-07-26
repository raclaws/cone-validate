#!/usr/bin/env python3
"""
cone-validate MCP Server — exposes dependency cones to Claude Code / Cline / etc.

Install:
  1. pip install mcp
  2. Add to claude_desktop_config.json:
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

Tools exposed:
  - get_cone: Get dependency cone for a symbol
  - get_file_context: Get cone context for editing a file
  - list_symbols: List all symbols in the codebase
  - validate_change: Run tsc on a proposed change
"""

import json
import sys
from pathlib import Path

# Add cone-validate to path
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from validate import build_graph, compute_cone
from config import get_target_dir, get_project_root

# Global state
_graph_cache = None
_current_target = None
_current_project = None

def get_graph():
    """Lazy-load and cache the dependency graph."""
    global _graph_cache, _current_target
    import os
    
    # Read directly from env to avoid config module caching
    target_dir = os.environ.get("CONE_TARGET_DIR")
    if not target_dir:
        raise ValueError("CONE_TARGET_DIR not set")
    target = Path(target_dir)
    
    # Rebuild if target changed
    if _graph_cache is None or _current_target != str(target):
        _graph_cache = build_graph(target)
        _current_target = str(target)
    return _graph_cache

def set_project(target_dir: str, project_root: str = None):
    """Change the target project at runtime."""
    global _graph_cache, _current_target, _current_project
    import os
    
    os.environ["CONE_TARGET_DIR"] = target_dir
    if project_root:
        os.environ["CONE_PROJECT_ROOT"] = project_root
    else:
        # Default project_root to parent of target_dir
        os.environ["CONE_PROJECT_ROOT"] = str(Path(target_dir).parent)
    
    # Clear cache to force rebuild
    _graph_cache = None
    _current_target = None
    _current_project = project_root or str(Path(target_dir).parent)

# Create MCP server
server = Server("cone-validate")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_cone",
            description="Get the dependency cone for a symbol. Returns all files that depend on or are depended by this symbol — the minimal context needed to safely modify it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name (function, class, variable, type)"
                    },
                    "include_source": {
                        "type": "boolean",
                        "description": "Include file contents in response (default: true)",
                        "default": True
                    }
                },
                "required": ["symbol"]
            }
        ),
        Tool(
            name="get_file_context",
            description="Get the combined dependency cone for all symbols in a file. Use this before editing a file to understand its dependencies and dependents.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File path (relative to target_dir)"
                    },
                    "include_source": {
                        "type": "boolean",
                        "description": "Include file contents in response (default: true)",
                        "default": True
                    }
                },
                "required": ["file"]
            }
        ),
        Tool(
            name="list_symbols",
            description="List all symbols in the codebase, optionally filtered by file or kind.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Filter to symbols in this file"
                    },
                    "kind": {
                        "type": "string",
                        "description": "Filter by kind (function, class, variable, type, interface)"
                    },
                    "query": {
                        "type": "string",
                        "description": "Filter by name substring"
                    }
                }
            }
        ),
        Tool(
            name="validate_change",
            description="Validate a proposed code change by running tsc. Returns any type errors introduced.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File path to validate"
                    },
                    "content": {
                        "type": "string",
                        "description": "Proposed new content for the file"
                    }
                },
                "required": ["file", "content"]
            }
        ),
        Tool(
            name="set_project",
            description="Change the target TypeScript project at runtime. Clears the graph cache and rebuilds on next tool call. Use this to switch between projects without restarting.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_dir": {
                        "type": "string",
                        "description": "Path to the source directory (e.g., /path/to/project/src)"
                    },
                    "project_root": {
                        "type": "string",
                        "description": "Path to the project root with tsconfig.json (default: parent of target_dir)"
                    }
                },
                "required": ["target_dir"]
            }
        ),
        Tool(
            name="get_project",
            description="Get the current project configuration (target_dir and project_root).",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, _ = get_graph()
    
    if name == "get_cone":
        symbol_name = arguments["symbol"]
        include_source = arguments.get("include_source", True)
        
        if symbol_name not in symbols:
            # Fuzzy match
            matches = [s for s in symbols if symbol_name.lower() in s.lower()]
            if matches:
                return [TextContent(
                    type="text",
                    text=f"Symbol '{symbol_name}' not found. Did you mean: {', '.join(matches[:10])}"
                )]
            return [TextContent(type="text", text=f"Symbol '{symbol_name}' not found")]
        
        cone_files = compute_cone(symbol_name, symbols, sym_by_file, call_file_edges, import_edges)
        info = symbols[symbol_name]
        
        result = {
            "symbol": symbol_name,
            "kind": info["kind"],
            "origin_file": info["file"],
            "cone_files": sorted(cone_files),
            "cone_size": len(cone_files),
            "total_files": len(all_files)
        }
        
        if include_source:
            result["sources"] = {}
            for f in cone_files:
                src = sources.get(f, b"")
                if isinstance(src, bytes):
                    src = src.decode("utf-8", errors="replace")
                result["sources"][f] = src
        
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    
    elif name == "get_file_context":
        file_path = arguments["file"]
        include_source = arguments.get("include_source", True)
        
        # Normalize path
        if file_path.startswith("/"):
            file_path = str(Path(file_path).relative_to(get_target_dir()))
        
        if file_path not in sym_by_file:
            return [TextContent(type="text", text=f"File '{file_path}' not found or has no symbols")]
        
        # Union of cones for all symbols in the file
        combined_cone = set()
        file_symbols = sym_by_file[file_path]
        
        for sym_name in file_symbols:
            cone = compute_cone(sym_name, symbols, sym_by_file, call_file_edges, import_edges)
            combined_cone.update(cone)
        
        result = {
            "file": file_path,
            "symbols_in_file": file_symbols,
            "cone_files": sorted(combined_cone),
            "cone_size": len(combined_cone),
            "total_files": len(all_files)
        }
        
        if include_source:
            result["sources"] = {}
            for f in combined_cone:
                src = sources.get(f, b"")
                if isinstance(src, bytes):
                    src = src.decode("utf-8", errors="replace")
                result["sources"][f] = src
        
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    
    elif name == "list_symbols":
        file_filter = arguments.get("file")
        kind_filter = arguments.get("kind")
        query_filter = arguments.get("query", "").lower()
        
        results = []
        for name, info in symbols.items():
            if file_filter and info["file"] != file_filter:
                continue
            if kind_filter and info["kind"] != kind_filter:
                continue
            if query_filter and query_filter not in name.lower():
                continue
            results.append({
                "name": name,
                "kind": info["kind"],
                "file": info["file"]
            })
        
        return [TextContent(type="text", text=json.dumps(results[:100], indent=2))]
    
    elif name == "validate_change":
        import subprocess
        import tempfile
        
        file_path = arguments["file"]
        content = arguments["content"]
        project_root = get_project_root()
        
        # Resolve full path
        if not file_path.startswith("/"):
            full_path = Path(get_target_dir()) / file_path
        else:
            full_path = Path(file_path)
        
        # Backup original
        original = full_path.read_text() if full_path.exists() else None
        
        try:
            # Write proposed change
            full_path.write_text(content)
            
            # Run tsc
            result = subprocess.run(
                ["npx", "tsc", "--noEmit", "--pretty", "false"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            errors = []
            for line in result.stdout.split("\n"):
                if file_path in line and "error" in line.lower():
                    errors.append(line.strip())
            
            return [TextContent(type="text", text=json.dumps({
                "valid": len(errors) == 0,
                "errors": errors,
                "raw_output": result.stdout[:2000] if errors else ""
            }, indent=2))]
            
        finally:
            # Restore original
            if original is not None:
                full_path.write_text(original)
            elif full_path.exists():
                full_path.unlink()
    
    elif name == "set_project":
        target_dir = arguments["target_dir"]
        project_root = arguments.get("project_root")
        
        # Validate path exists
        if not Path(target_dir).exists():
            return [TextContent(type="text", text=json.dumps({
                "error": f"Target directory does not exist: {target_dir}"
            }))]
        
        set_project(target_dir, project_root)
        
        return [TextContent(type="text", text=json.dumps({
            "success": True,
            "target_dir": target_dir,
            "project_root": project_root or str(Path(target_dir).parent),
            "message": "Project changed. Graph will rebuild on next tool call."
        }, indent=2))]
    
    elif name == "get_project":
        import os
        target = os.environ.get("CONE_TARGET_DIR")
        project = os.environ.get("CONE_PROJECT_ROOT")
        
        if not target:
            return [TextContent(type="text", text=json.dumps({
                "configured": False,
                "error": "No project configured. Use set_project to configure."
            }))]
        
        return [TextContent(type="text", text=json.dumps({
            "configured": True,
            "target_dir": target,
            "project_root": project or str(Path(target).parent),
            "graph_loaded": _graph_cache is not None,
            "symbols": len(_graph_cache[0]) if _graph_cache else 0
        }, indent=2))]
    
    return [TextContent(type="text", text=f"Unknown tool: {name}")]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
