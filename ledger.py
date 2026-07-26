#!/usr/bin/env python3
"""
Token accounting ledger for cone-validate.

Tracks per-agent, per-task token usage with cost estimates.
Provides summary statistics and persistence via SQLite.

Cost rates (2024 Anthropic pricing):
  - claude-haiku-4.5: $0.25/1M input, $1.25/1M output
  - claude-sonnet-4:  $3.00/1M input, $15.00/1M output
"""

import json
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Cost rates per 1M tokens ──────────────────────────────────────────────────
COST_RATES = {
    # Haiku models
    "claude-haiku-4.5": {"input": 0.25, "output": 1.25},
    "haiku": {"input": 0.25, "output": 1.25},
    # Sonnet models
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4.6": {"input": 3.0, "output": 15.0},
    "kr/claude-sonnet-4.6": {"input": 3.0, "output": 15.0},
    "sonnet": {"input": 3.0, "output": 15.0},
    # Default fallback (assume sonnet pricing for safety)
    "default": {"input": 3.0, "output": 15.0},
}

DEFAULT_DB_PATH = Path(__file__).parent / "data" / "ledger.db"


def _ensure_db_dir(db_path: Path) -> None:
    """Create parent directory if it doesn't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)


def _get_cost_rate(model: str) -> dict:
    """Get cost rate for a model, with fallback to default."""
    # Check exact match first
    if model in COST_RATES:
        return COST_RATES[model]
    # Check if model name contains known keywords
    model_lower = model.lower()
    if "haiku" in model_lower:
        return COST_RATES["haiku"]
    if "sonnet" in model_lower:
        return COST_RATES["sonnet"]
    return COST_RATES["default"]


def estimate_cost(prompt_tokens: int, completion_tokens: int, model: str) -> float:
    """Estimate cost in USD for a single LLM call."""
    rate = _get_cost_rate(model)
    input_cost = (prompt_tokens / 1_000_000) * rate["input"]
    output_cost = (completion_tokens / 1_000_000) * rate["output"]
    return input_cost + output_cost


@dataclass
class TokenRecord:
    """Single token usage record."""
    id: int | None
    agent_id: str
    task_id: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    latency_ms: float
    cost_usd: float
    timestamp: float
    run_id: str | None = None
    passed: bool | None = None

    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class TokenLedger:
    """
    Token accounting ledger with SQLite persistence.
    
    Usage:
        ledger = TokenLedger()
        ledger.record("agent_a", "task_1", 1000, 500, "claude-haiku-4.5", 1500.0)
        print(ledger.summary())
        ledger.save()
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH, run_id: str = None):
        self.db_path = Path(db_path)
        self.run_id = run_id or f"run_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        self.records: list[TokenRecord] = []
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
                CREATE TABLE IF NOT EXISTS token_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    cost_usd REAL NOT NULL,
                    timestamp REAL NOT NULL,
                    passed INTEGER
                );
                
                CREATE INDEX IF NOT EXISTS idx_records_run ON token_records(run_id);
                CREATE INDEX IF NOT EXISTS idx_records_agent ON token_records(agent_id);
                CREATE INDEX IF NOT EXISTS idx_records_task ON token_records(task_id);
                CREATE INDEX IF NOT EXISTS idx_records_timestamp ON token_records(timestamp);
            """)

    def record(
        self,
        agent_id: str,
        task_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        latency_ms: float,
        passed: bool = None,
    ) -> TokenRecord:
        """Record a single LLM call's token usage."""
        cost = estimate_cost(prompt_tokens, completion_tokens, model)
        rec = TokenRecord(
            id=None,
            agent_id=agent_id,
            task_id=task_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            latency_ms=latency_ms,
            cost_usd=cost,
            timestamp=time.time(),
            run_id=self.run_id,
            passed=passed,
        )
        self.records.append(rec)
        return rec

    def save(self) -> None:
        """Persist all records to SQLite."""
        with self._connect() as conn:
            for rec in self.records:
                if rec.id is None:  # Only save new records
                    cursor = conn.execute(
                        """INSERT INTO token_records 
                           (run_id, agent_id, task_id, prompt_tokens, completion_tokens, 
                            model, latency_ms, cost_usd, timestamp, passed)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (rec.run_id, rec.agent_id, rec.task_id, rec.prompt_tokens,
                         rec.completion_tokens, rec.model, rec.latency_ms,
                         rec.cost_usd, rec.timestamp, 
                         1 if rec.passed else (0 if rec.passed is False else None)),
                    )
                    rec.id = cursor.lastrowid
            conn.commit()

    def summary(self) -> dict:
        """Generate summary statistics for current run."""
        if not self.records:
            return {
                "run_id": self.run_id,
                "total_records": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "by_agent": {},
                "by_task": {},
                "by_model": {},
            }

        by_agent: dict[str, dict] = {}
        by_task: dict[str, dict] = {}
        by_model: dict[str, dict] = {}

        total_prompt = 0
        total_completion = 0
        total_cost = 0.0

        for rec in self.records:
            total_prompt += rec.prompt_tokens
            total_completion += rec.completion_tokens
            total_cost += rec.cost_usd

            # By agent
            if rec.agent_id not in by_agent:
                by_agent[rec.agent_id] = {
                    "prompt_tokens": 0, "completion_tokens": 0, 
                    "cost_usd": 0.0, "calls": 0
                }
            by_agent[rec.agent_id]["prompt_tokens"] += rec.prompt_tokens
            by_agent[rec.agent_id]["completion_tokens"] += rec.completion_tokens
            by_agent[rec.agent_id]["cost_usd"] += rec.cost_usd
            by_agent[rec.agent_id]["calls"] += 1

            # By task
            if rec.task_id not in by_task:
                by_task[rec.task_id] = {
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "cost_usd": 0.0, "calls": 0
                }
            by_task[rec.task_id]["prompt_tokens"] += rec.prompt_tokens
            by_task[rec.task_id]["completion_tokens"] += rec.completion_tokens
            by_task[rec.task_id]["cost_usd"] += rec.cost_usd
            by_task[rec.task_id]["calls"] += 1

            # By model
            if rec.model not in by_model:
                by_model[rec.model] = {
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "cost_usd": 0.0, "calls": 0
                }
            by_model[rec.model]["prompt_tokens"] += rec.prompt_tokens
            by_model[rec.model]["completion_tokens"] += rec.completion_tokens
            by_model[rec.model]["cost_usd"] += rec.cost_usd
            by_model[rec.model]["calls"] += 1

        return {
            "run_id": self.run_id,
            "total_records": len(self.records),
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_cost_usd": total_cost,
            "by_agent": by_agent,
            "by_task": by_task,
            "by_model": by_model,
        }

    def to_json(self) -> str:
        """Serialize ledger to JSON for persistence/export."""
        return json.dumps({
            "run_id": self.run_id,
            "records": [asdict(r) for r in self.records],
            "summary": self.summary(),
        }, indent=2)

    @classmethod
    def load_run(cls, run_id: str, db_path: Path = DEFAULT_DB_PATH) -> "TokenLedger":
        """Load a specific run from the database."""
        ledger = cls(db_path=db_path, run_id=run_id)
        ledger.records = []
        
        with ledger._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM token_records WHERE run_id = ? ORDER BY timestamp""",
                (run_id,)
            ).fetchall()
            
            for row in rows:
                ledger.records.append(TokenRecord(
                    id=row["id"],
                    agent_id=row["agent_id"],
                    task_id=row["task_id"],
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    model=row["model"],
                    latency_ms=row["latency_ms"],
                    cost_usd=row["cost_usd"],
                    timestamp=row["timestamp"],
                    run_id=row["run_id"],
                    passed=None if row["passed"] is None else bool(row["passed"]),
                ))
        
        return ledger

    @classmethod
    def list_runs(cls, db_path: Path = DEFAULT_DB_PATH, limit: int = 20) -> list[dict]:
        """List recent runs with summary stats."""
        ledger = cls(db_path=db_path)
        runs = []
        
        with ledger._connect() as conn:
            rows = conn.execute("""
                SELECT 
                    run_id,
                    MIN(timestamp) as start_time,
                    MAX(timestamp) as end_time,
                    COUNT(*) as total_calls,
                    SUM(prompt_tokens) as total_prompt,
                    SUM(completion_tokens) as total_completion,
                    SUM(cost_usd) as total_cost,
                    COUNT(CASE WHEN passed = 1 THEN 1 END) as passed_count,
                    COUNT(CASE WHEN passed = 0 THEN 1 END) as failed_count
                FROM token_records
                GROUP BY run_id
                ORDER BY start_time DESC
                LIMIT ?
            """, (limit,)).fetchall()
            
            for row in rows:
                runs.append({
                    "run_id": row["run_id"],
                    "start_time": datetime.fromtimestamp(row["start_time"]).isoformat(),
                    "end_time": datetime.fromtimestamp(row["end_time"]).isoformat(),
                    "total_calls": row["total_calls"],
                    "total_prompt_tokens": row["total_prompt"],
                    "total_completion_tokens": row["total_completion"],
                    "total_tokens": row["total_prompt"] + row["total_completion"],
                    "total_cost_usd": row["total_cost"],
                    "passed": row["passed_count"],
                    "failed": row["failed_count"],
                })
        
        return runs


def print_cost_summary(ledger: TokenLedger) -> None:
    """Print a formatted cost summary to stdout."""
    summary = ledger.summary()
    
    print(f"\n{'='*60}")
    print("  TOKEN USAGE & COST SUMMARY")
    print(f"{'='*60}")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Total calls: {summary['total_records']}")
    print(f"  Total tokens: {summary['total_tokens']:,}")
    print(f"    - Prompt:     {summary['total_prompt_tokens']:,}")
    print(f"    - Completion: {summary['total_completion_tokens']:,}")
    print(f"  Estimated cost: ${summary['total_cost_usd']:.4f}")
    
    if summary["by_model"]:
        print(f"\n  By Model:")
        print(f"  {'-'*56}")
        print(f"  {'Model':<30} {'Tokens':>10} {'Cost':>10}")
        print(f"  {'-'*56}")
        for model, stats in summary["by_model"].items():
            total_tok = stats["prompt_tokens"] + stats["completion_tokens"]
            print(f"  {model:<30} {total_tok:>10,} ${stats['cost_usd']:>9.4f}")
    
    if summary["by_task"]:
        print(f"\n  By Task:")
        print(f"  {'-'*56}")
        print(f"  {'Task':<30} {'Tokens':>10} {'Cost':>10}")
        print(f"  {'-'*56}")
        for task, stats in summary["by_task"].items():
            total_tok = stats["prompt_tokens"] + stats["completion_tokens"]
            print(f"  {task:<30} {total_tok:>10,} ${stats['cost_usd']:>9.4f}")
    
    print(f"{'='*60}\n")


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing TokenLedger...")
    
    # Create test ledger
    ledger = TokenLedger(run_id="test_run")
    
    # Record some test data
    ledger.record("oracle_loop", "write_attempt_1", 5000, 1500, "claude-haiku-4.5", 2500.0)
    ledger.record("oracle_loop", "retry_attempt_2", 6000, 1800, "claude-haiku-4.5", 2800.0)
    ledger.record("oracle_loop", "escalation", 15000, 3000, "kr/claude-sonnet-4.6", 5000.0, passed=True)
    
    # Print summary
    print_cost_summary(ledger)
    
    # Save to DB
    ledger.save()
    print(f"Saved to: {ledger.db_path}")
    
    # Test JSON export
    print("\nJSON export (truncated):")
    print(ledger.to_json()[:500] + "...")
    
    # Test loading
    loaded = TokenLedger.load_run("test_run")
    print(f"\nLoaded {len(loaded.records)} records from DB")
    
    print("\n✅ TokenLedger test passed")
