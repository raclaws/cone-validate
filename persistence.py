#!/usr/bin/env python3
"""
SQLite persistence layer for cone-validate crash recovery.

Provides:
  - GraphStore: save/load dependency graph (symbols, edges)
  - SubscriptionStore: save/load subscription reads and pending notifications

Design: SQLite with JSON columns for complex nested data.
Target: <100ms load time for 500-symbol graph.
"""

import os
import json
import sqlite3
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any

DEFAULT_DB_PATH = Path(__file__).parent / "data" / "cone.db"


def _ensure_db_dir(db_path: Path) -> None:
    """Create parent directory if it doesn't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)


class GraphStore:
    """
    Persist dependency graph to SQLite.
    
    Tables:
      - symbols: name, file, kind, start, end
      - edges: edge_type, from_key, to_key, data_json
      - metadata: key, value (for sources, file lists, etc.)
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        _ensure_db_dir(self.db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS symbols (
                    name TEXT PRIMARY KEY,
                    file TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    start INTEGER NOT NULL,
                    end INTEGER NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS sym_by_file (
                    file TEXT NOT NULL,
                    symbol_name TEXT NOT NULL,
                    PRIMARY KEY (file, symbol_name)
                );
                
                CREATE TABLE IF NOT EXISTS call_edges (
                    caller_file TEXT NOT NULL,
                    called_name TEXT NOT NULL,
                    PRIMARY KEY (caller_file, called_name)
                );
                
                CREATE TABLE IF NOT EXISTS call_file_edges (
                    caller_file TEXT NOT NULL,
                    callee_file TEXT NOT NULL,
                    PRIMARY KEY (caller_file, callee_file)
                );
                
                CREATE TABLE IF NOT EXISTS import_edges (
                    importer_file TEXT NOT NULL,
                    imported_file TEXT NOT NULL,
                    PRIMARY KEY (importer_file, imported_file)
                );
                
                CREATE TABLE IF NOT EXISTS sources (
                    file TEXT PRIMARY KEY,
                    content BLOB NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                
                CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
                CREATE INDEX IF NOT EXISTS idx_sym_by_file_file ON sym_by_file(file);
            """)

    def save(self, symbols: dict, sym_by_file: dict, call_edges: dict,
             call_file_edges: dict, import_edges: dict, sources: dict,
             all_files: list, parse_errors: int) -> float:
        """
        Save complete graph state. Returns time taken in ms.
        """
        start = time.time()
        
        with self._connect() as conn:
            # Clear existing data
            conn.executescript("""
                DELETE FROM symbols;
                DELETE FROM sym_by_file;
                DELETE FROM call_edges;
                DELETE FROM call_file_edges;
                DELETE FROM import_edges;
                DELETE FROM sources;
                DELETE FROM metadata;
            """)
            
            # Insert symbols
            conn.executemany(
                "INSERT INTO symbols (name, file, kind, start, end) VALUES (?, ?, ?, ?, ?)",
                [(name, s["file"], s["kind"], s["start"], s["end"]) for name, s in symbols.items()]
            )
            
            # Insert sym_by_file (deduplicate)
            rows = []
            seen = set()
            for file, syms in sym_by_file.items():
                for sym in syms:
                    key = (file, sym)
                    if key not in seen:
                        seen.add(key)
                        rows.append(key)
            conn.executemany(
                "INSERT INTO sym_by_file (file, symbol_name) VALUES (?, ?)",
                rows
            )
            
            # Insert call_edges
            rows = []
            for caller_file, called_names in call_edges.items():
                for name in called_names:
                    rows.append((caller_file, name))
            conn.executemany(
                "INSERT INTO call_edges (caller_file, called_name) VALUES (?, ?)",
                rows
            )
            
            # Insert call_file_edges
            rows = []
            for caller_file, callee_files in call_file_edges.items():
                for callee_file in callee_files:
                    rows.append((caller_file, callee_file))
            conn.executemany(
                "INSERT INTO call_file_edges (caller_file, callee_file) VALUES (?, ?)",
                rows
            )
            
            # Insert import_edges (deduplicate since list may have duplicates)
            rows = []
            seen = set()
            for importer, imported_files in import_edges.items():
                for imported in imported_files:
                    key = (importer, imported)
                    if key not in seen:
                        rows.append(key)
                        seen.add(key)
            conn.executemany(
                "INSERT INTO import_edges (importer_file, imported_file) VALUES (?, ?)",
                rows
            )
            
            # Insert sources
            conn.executemany(
                "INSERT INTO sources (file, content) VALUES (?, ?)",
                [(f, content) for f, content in sources.items()]
            )
            
            # Insert metadata
            conn.execute(
                "INSERT INTO metadata (key, value_json) VALUES (?, ?)",
                ("all_files", json.dumps([str(f) for f in all_files]))
            )
            conn.execute(
                "INSERT INTO metadata (key, value_json) VALUES (?, ?)",
                ("parse_errors", json.dumps(parse_errors))
            )
            conn.execute(
                "INSERT INTO metadata (key, value_json) VALUES (?, ?)",
                ("saved_at", json.dumps(time.time()))
            )
            
            conn.commit()
        
        return (time.time() - start) * 1000

    def load(self) -> tuple | None:
        """
        Load graph state. Returns tuple matching build_graph() output,
        or None if no saved state exists.
        
        Returns: (symbols, sym_by_file, call_edges, call_file_edges, 
                  import_edges, sources, all_files, parse_errors)
        """
        start = time.time()
        
        if not self.db_path.exists():
            return None
        
        with self._connect() as conn:
            # Check if we have data
            row = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()
            if row[0] == 0:
                return None
            
            # Load symbols
            symbols = {}
            for row in conn.execute("SELECT name, file, kind, start, end FROM symbols"):
                symbols[row["name"]] = {
                    "name": row["name"],
                    "file": row["file"],
                    "kind": row["kind"],
                    "start": row["start"],
                    "end": row["end"],
                }
            
            # Load sym_by_file
            from collections import defaultdict
            sym_by_file = defaultdict(list)
            for row in conn.execute("SELECT file, symbol_name FROM sym_by_file"):
                sym_by_file[row["file"]].append(row["symbol_name"])
            
            # Load call_edges
            call_edges = defaultdict(set)
            for row in conn.execute("SELECT caller_file, called_name FROM call_edges"):
                call_edges[row["caller_file"]].add(row["called_name"])
            
            # Load call_file_edges
            call_file_edges = defaultdict(set)
            for row in conn.execute("SELECT caller_file, callee_file FROM call_file_edges"):
                call_file_edges[row["caller_file"]].add(row["callee_file"])
            
            # Load import_edges
            import_edges = defaultdict(list)
            for row in conn.execute("SELECT importer_file, imported_file FROM import_edges"):
                import_edges[row["importer_file"]].append(row["imported_file"])
            
            # Load sources
            sources = {}
            for row in conn.execute("SELECT file, content FROM sources"):
                sources[row["file"]] = row["content"]
            
            # Load metadata
            all_files_row = conn.execute(
                "SELECT value_json FROM metadata WHERE key = ?", ("all_files",)
            ).fetchone()
            all_files = [Path(p) for p in json.loads(all_files_row["value_json"])] if all_files_row else []
            
            parse_errors_row = conn.execute(
                "SELECT value_json FROM metadata WHERE key = ?", ("parse_errors",)
            ).fetchone()
            parse_errors = json.loads(parse_errors_row["value_json"]) if parse_errors_row else 0
        
        load_time_ms = (time.time() - start) * 1000
        return (symbols, dict(sym_by_file), dict(call_edges), dict(call_file_edges),
                dict(import_edges), sources, all_files, parse_errors, load_time_ms)

    def clear(self) -> None:
        """Clear all persisted data."""
        if self.db_path.exists():
            self.db_path.unlink()
        self._init_schema()


class SubscriptionStore:
    """
    Persist subscription bus state to SQLite.
    
    Tables:
      - reads: agent_id, symbol, timestamp
      - pending: agent_id, notification_json
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        _ensure_db_dir(self.db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS subscription_reads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS subscription_pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    notification_json TEXT NOT NULL
                );
                
                CREATE INDEX IF NOT EXISTS idx_reads_agent ON subscription_reads(agent_id);
                CREATE INDEX IF NOT EXISTS idx_pending_agent ON subscription_pending(agent_id);
            """)

    def save_reads(self, reads: dict[str, list[tuple[str, float]]]) -> None:
        """Save the reads dictionary. Replaces existing data."""
        with self._connect() as conn:
            conn.execute("DELETE FROM subscription_reads")
            rows = []
            for agent_id, read_list in reads.items():
                for symbol, timestamp in read_list:
                    rows.append((agent_id, symbol, timestamp))
            conn.executemany(
                "INSERT INTO subscription_reads (agent_id, symbol, timestamp) VALUES (?, ?, ?)",
                rows
            )
            conn.commit()

    def load_reads(self) -> dict[str, list[tuple[str, float]]]:
        """Load the reads dictionary."""
        from collections import defaultdict
        reads = defaultdict(list)
        
        if not self.db_path.exists():
            return dict(reads)
        
        with self._connect() as conn:
            for row in conn.execute("SELECT agent_id, symbol, timestamp FROM subscription_reads"):
                reads[row["agent_id"]].append((row["symbol"], row["timestamp"]))
        
        return dict(reads)

    def save_pending(self, pending: dict[str, list]) -> None:
        """Save the pending notifications. Replaces existing data."""
        with self._connect() as conn:
            conn.execute("DELETE FROM subscription_pending")
            rows = []
            for agent_id, notifications in pending.items():
                for notif in notifications:
                    # Serialize Notification dataclass to JSON
                    notif_dict = {
                        "agent_id": notif.agent_id,
                        "changed_file": notif.changed_file,
                        "changed_symbols": notif.changed_symbols,
                        "new_src": notif.new_src,
                        "timestamp": notif.timestamp,
                    }
                    rows.append((agent_id, json.dumps(notif_dict)))
            conn.executemany(
                "INSERT INTO subscription_pending (agent_id, notification_json) VALUES (?, ?)",
                rows
            )
            conn.commit()

    def load_pending(self, notification_class) -> dict[str, list]:
        """Load the pending notifications. Requires Notification class for reconstruction."""
        from collections import defaultdict
        pending = defaultdict(list)
        
        if not self.db_path.exists():
            return dict(pending)
        
        with self._connect() as conn:
            for row in conn.execute("SELECT agent_id, notification_json FROM subscription_pending"):
                data = json.loads(row["notification_json"])
                notif = notification_class(
                    agent_id=data["agent_id"],
                    changed_file=data["changed_file"],
                    changed_symbols=data["changed_symbols"],
                    new_src=data["new_src"],
                    timestamp=data["timestamp"],
                )
                pending[row["agent_id"]].append(notif)
        
        return dict(pending)

    def clear(self) -> None:
        """Clear subscription data only."""
        with self._connect() as conn:
            conn.execute("DELETE FROM subscription_reads")
            conn.execute("DELETE FROM subscription_pending")
            conn.commit()


# ── Convenience functions ─────────────────────────────────────────────────────
def get_graph_store(db_path: Path = DEFAULT_DB_PATH) -> GraphStore:
    """Get a GraphStore instance."""
    return GraphStore(db_path)


def get_subscription_store(db_path: Path = DEFAULT_DB_PATH) -> SubscriptionStore:
    """Get a SubscriptionStore instance."""
    return SubscriptionStore(db_path)


# ── Smoke test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    
    print("Testing GraphStore...")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        store = GraphStore(db_path)
        
        # Mock data
        symbols = {
            "foo": {"name": "foo", "file": "a.ts", "kind": "function", "start": 0, "end": 100},
            "bar": {"name": "bar", "file": "b.ts", "kind": "arrow", "start": 0, "end": 50},
        }
        sym_by_file = {"a.ts": ["foo"], "b.ts": ["bar"]}
        call_edges = {"a.ts": {"bar"}}
        call_file_edges = {"a.ts": {"b.ts"}}
        import_edges = {"a.ts": ["b.ts"]}
        sources = {"a.ts": b"// a.ts", "b.ts": b"// b.ts"}
        all_files = [Path("a.ts"), Path("b.ts")]
        
        save_time = store.save(symbols, sym_by_file, call_edges, call_file_edges,
                               import_edges, sources, all_files, 0)
        print(f"  Save time: {save_time:.1f}ms")
        
        loaded = store.load()
        load_time = loaded[-1]  # Last element is load_time_ms
        print(f"  Load time: {load_time:.1f}ms")
        
        # Verify
        assert loaded[0] == symbols, "Symbols mismatch"
        assert loaded[1] == sym_by_file, "sym_by_file mismatch"
        print("  ✅ GraphStore round-trip passed")
    
    print("\nTesting SubscriptionStore...")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        store = SubscriptionStore(db_path)
        
        # Mock data
        reads = {
            "agent_a": [("foo", 1000.0), ("bar", 1001.0)],
            "agent_b": [("baz", 1002.0)],
        }
        store.save_reads(reads)
        loaded_reads = store.load_reads()
        assert loaded_reads == reads, "Reads mismatch"
        print("  ✅ SubscriptionStore reads round-trip passed")
    
    print("\n✅ All persistence tests passed")
