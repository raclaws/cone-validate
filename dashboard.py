#!/usr/bin/env python3
"""
Cost dashboard for cone-validate.

CLI tool to view token usage and cost data across runs.

Usage:
  python3 dashboard.py              # Show recent runs
  python3 dashboard.py --json       # JSON output
  python3 dashboard.py --run <id>   # Details for specific run
  python3 dashboard.py --summary    # Aggregate summary
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ledger import TokenLedger, DEFAULT_DB_PATH


def format_cost(cost: float) -> str:
    """Format cost in USD with appropriate precision."""
    if cost < 0.01:
        return f"${cost:.6f}"
    elif cost < 1.0:
        return f"${cost:.4f}"
    else:
        return f"${cost:.2f}"


def format_tokens(tokens: int) -> str:
    """Format token count with commas."""
    return f"{tokens:,}"


def print_runs_table(runs: list[dict]) -> None:
    """Print a formatted table of recent runs."""
    if not runs:
        print("No runs recorded yet.")
        return

    print(f"\n{'='*80}")
    print("  RECENT RUNS")
    print(f"{'='*80}")
    print(f"  {'Timestamp':<20} {'Run ID':<25} {'Calls':>6} {'Tokens':>12} {'Cost':>10} {'Pass/Fail':>10}")
    print(f"  {'-'*76}")
    
    for run in runs:
        ts = run['start_time'][:19].replace('T', ' ')  # Truncate to seconds
        run_id = run['run_id'][:24] + '.' if len(run['run_id']) > 24 else run['run_id']
        calls = run['total_calls']
        tokens = format_tokens(run['total_tokens'])
        cost = format_cost(run['total_cost_usd'])
        
        passed = run.get('passed', 0)
        failed = run.get('failed', 0)
        if passed or failed:
            pf = f"{passed}✓/{failed}✗"
        else:
            pf = "-"
        
        print(f"  {ts:<20} {run_id:<25} {calls:>6} {tokens:>12} {cost:>10} {pf:>10}")
    
    print(f"  {'-'*76}")
    total_cost = sum(r['total_cost_usd'] for r in runs)
    total_tokens = sum(r['total_tokens'] for r in runs)
    print(f"  {'TOTAL':<20} {'':<25} {'':<6} {format_tokens(total_tokens):>12} {format_cost(total_cost):>10}")
    print(f"{'='*80}\n")


def print_run_details(run_id: str) -> None:
    """Print detailed breakdown for a specific run."""
    ledger = TokenLedger.load_run(run_id)
    
    if not ledger.records:
        print(f"No records found for run: {run_id}")
        return
    
    summary = ledger.summary()
    
    print(f"\n{'='*80}")
    print(f"  RUN DETAILS: {run_id}")
    print(f"{'='*80}")
    
    # Records table
    print(f"\n  Individual Calls:")
    print(f"  {'-'*74}")
    print(f"  {'Time':<12} {'Agent':<15} {'Task':<15} {'Model':<20} {'Tokens':>8} {'Cost':>8}")
    print(f"  {'-'*74}")
    
    for rec in ledger.records:
        ts = datetime.fromtimestamp(rec.timestamp).strftime('%H:%M:%S')
        agent = rec.agent_id[:14]
        task = rec.task_id[:14]
        model = rec.model[:19]
        tokens = rec.total_tokens()
        cost = format_cost(rec.cost_usd)
        
        print(f"  {ts:<12} {agent:<15} {task:<15} {model:<20} {tokens:>8,} {cost:>8}")
    
    # By model breakdown
    if summary['by_model']:
        print(f"\n  By Model:")
        print(f"  {'-'*50}")
        for model, stats in summary['by_model'].items():
            total = stats['prompt_tokens'] + stats['completion_tokens']
            print(f"    {model:<25} {total:>10,} tok  {format_cost(stats['cost_usd']):>10}")
    
    # By task breakdown
    if summary['by_task']:
        print(f"\n  By Task:")
        print(f"  {'-'*50}")
        for task, stats in summary['by_task'].items():
            total = stats['prompt_tokens'] + stats['completion_tokens']
            print(f"    {task:<25} {total:>10,} tok  {format_cost(stats['cost_usd']):>10}")
    
    # Summary
    print(f"\n  Summary:")
    print(f"  {'-'*50}")
    print(f"    Total calls:      {summary['total_records']}")
    print(f"    Prompt tokens:    {summary['total_prompt_tokens']:,}")
    print(f"    Completion tokens:{summary['total_completion_tokens']:,}")
    print(f"    Total tokens:     {summary['total_tokens']:,}")
    print(f"    Estimated cost:   {format_cost(summary['total_cost_usd'])}")
    print(f"{'='*80}\n")


def print_aggregate_summary() -> None:
    """Print aggregate summary across all runs."""
    runs = TokenLedger.list_runs(limit=100)
    
    if not runs:
        print("No runs recorded yet.")
        return
    
    total_calls = sum(r['total_calls'] for r in runs)
    total_prompt = sum(r['total_prompt_tokens'] for r in runs)
    total_completion = sum(r['total_completion_tokens'] for r in runs)
    total_cost = sum(r['total_cost_usd'] for r in runs)
    total_passed = sum(r.get('passed', 0) for r in runs)
    total_failed = sum(r.get('failed', 0) for r in runs)
    
    print(f"\n{'='*60}")
    print("  AGGREGATE SUMMARY")
    print(f"{'='*60}")
    print(f"  Total runs:         {len(runs)}")
    print(f"  Total LLM calls:    {total_calls:,}")
    print(f"  Prompt tokens:      {total_prompt:,}")
    print(f"  Completion tokens:  {total_completion:,}")
    print(f"  Total tokens:       {total_prompt + total_completion:,}")
    print(f"  Total cost:         {format_cost(total_cost)}")
    if total_passed or total_failed:
        print(f"  Pass rate:          {total_passed}/{total_passed+total_failed} ({100*total_passed/(total_passed+total_failed):.1f}%)")
    
    # Cost per 1K tokens
    total_tokens = total_prompt + total_completion
    if total_tokens > 0:
        cost_per_1k = (total_cost / total_tokens) * 1000
        print(f"  Avg cost/1K tokens: {format_cost(cost_per_1k)}")
    
    print(f"{'='*60}\n")


def output_json(runs: list[dict] = None, run_id: str = None, summary: bool = False) -> None:
    """Output data as JSON."""
    if run_id:
        ledger = TokenLedger.load_run(run_id)
        data = {
            "run_id": run_id,
            "records": [
                {
                    "timestamp": rec.timestamp,
                    "agent_id": rec.agent_id,
                    "task_id": rec.task_id,
                    "model": rec.model,
                    "prompt_tokens": rec.prompt_tokens,
                    "completion_tokens": rec.completion_tokens,
                    "total_tokens": rec.total_tokens(),
                    "cost_usd": rec.cost_usd,
                    "latency_ms": rec.latency_ms,
                    "passed": rec.passed,
                }
                for rec in ledger.records
            ],
            "summary": ledger.summary(),
        }
    elif summary:
        runs = TokenLedger.list_runs(limit=100)
        data = {
            "runs": runs,
            "aggregate": {
                "total_runs": len(runs),
                "total_calls": sum(r['total_calls'] for r in runs),
                "total_prompt_tokens": sum(r['total_prompt_tokens'] for r in runs),
                "total_completion_tokens": sum(r['total_completion_tokens'] for r in runs),
                "total_cost_usd": sum(r['total_cost_usd'] for r in runs),
            }
        }
    else:
        runs = runs or TokenLedger.list_runs()
        data = {"runs": runs}
    
    print(json.dumps(data, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(
        description="Cost dashboard for cone-validate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 dashboard.py              # Show recent runs
  python3 dashboard.py --json       # JSON output for scripting
  python3 dashboard.py --run run_20240101_120000  # Details for specific run
  python3 dashboard.py --summary    # Aggregate statistics
        """
    )
    parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")
    parser.add_argument("--run", "-r", help="Show details for specific run ID")
    parser.add_argument("--summary", "-s", action="store_true", help="Show aggregate summary")
    parser.add_argument("--limit", "-n", type=int, default=20, help="Number of recent runs to show")
    parser.add_argument("--db", help=f"Path to ledger database (default: {DEFAULT_DB_PATH})")
    
    args = parser.parse_args()
    
    # Check if database exists
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        if args.json:
            print(json.dumps({"error": "No ledger database found", "path": str(db_path)}))
        else:
            print(f"No ledger database found at: {db_path}")
            print("Run oracle_loop.py to generate cost data.")
        return 1
    
    # Handle JSON output
    if args.json:
        output_json(run_id=args.run, summary=args.summary)
        return 0
    
    # Handle specific run
    if args.run:
        print_run_details(args.run)
        return 0
    
    # Handle summary
    if args.summary:
        print_aggregate_summary()
        return 0
    
    # Default: show recent runs
    runs = TokenLedger.list_runs(limit=args.limit)
    print_runs_table(runs)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
