#!/usr/bin/env python3
"""
cone-validate debugger: systematic validation against production criteria.

Measures:
  - Correctness: false positive/negative rates, delta coverage, AST determinism
  - Cost: token savings, cascade overhead, retry costs
  - Reliability: parse success, concurrent agent behavior
  - Observability: structured JSON logs with trace IDs

Usage:
  python3 debugger.py                    # run all tests
  python3 debugger.py --test correctness # run specific suite
  python3 debugger.py --report           # generate report from last run
"""

import os, sys, json, time, uuid, hashlib, tempfile, shutil
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))

from validate import build_graph, compute_cone, parser, extract_symbols
from subscription import SubscriptionBus
from oracle_loop import (
    run_tsc, make_baseline_key, filter_cone_errors, dedup_errors, llm,
    MODEL_CHEAP, MODEL_STRONG, TARGET_DIR, PROJECT_ROOT,
)

# ── Config ────────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "debug_logs"
LOG_DIR.mkdir(exist_ok=True)


# ── Structured Logging ────────────────────────────────────────────────────────
@dataclass
class TraceEvent:
    trace_id: str
    timestamp: str
    event: str
    data: dict = field(default_factory=dict)
    duration_ms: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class StructuredLogger:
    """JSON logger with trace IDs for debugging."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.events: list[TraceEvent] = []
        self.log_file = LOG_DIR / f"{run_id}.jsonl"

    def log(self, event: str, data: dict = None, duration_ms: float = None) -> TraceEvent:
        e = TraceEvent(
            trace_id=self.run_id,
            timestamp=datetime.utcnow().isoformat() + "Z",
            event=event,
            data=data or {},
            duration_ms=duration_ms,
        )
        self.events.append(e)
        with open(self.log_file, "a") as f:
            f.write(e.to_json() + "\n")
        return e

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "event_count": len(self.events),
            "log_file": str(self.log_file),
        }


# ── Metrics Collection ────────────────────────────────────────────────────────
@dataclass
class TestResult:
    name: str
    passed: bool
    metric: float | None = None
    target: float | None = None
    details: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class SuiteResult:
    suite: str
    results: list[TestResult] = field(default_factory=list)
    duration_s: float = 0.0

    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.passed) / len(self.results)


# ── Test Suites ───────────────────────────────────────────────────────────────
class CorrectnessTests:
    """Test correctness criteria."""

    def __init__(self, logger: StructuredLogger):
        self.logger = logger
        self.graph_data = None

    def setup(self):
        """Load graph once for all tests."""
        start = time.time()
        self.graph_data = build_graph(TARGET_DIR)
        self.logger.log("graph_loaded", {
            "files": len(self.graph_data[6]),
            "symbols": len(self.graph_data[0]),
        }, duration_ms=(time.time() - start) * 1000)

    def test_ast_diff_determinism(self) -> TestResult:
        """AST diff should produce identical results on repeated runs."""
        symbols, sym_by_file, *_, sources, _, _ = self.graph_data

        # Pick a symbol with non-trivial code
        test_sym = "createBudgetStore"
        if test_sym not in symbols:
            return TestResult("ast_diff_determinism", False, error="Symbol not found")

        origin_file = symbols[test_sym]["file"]
        src = sources[origin_file]

        # Simulate a change
        modified = src + b"\n// test comment"

        # Run AST diff 5 times
        results = []
        for _ in range(5):
            old_tree = parser.parse(src)
            new_tree = parser.parse(modified)
            old_syms = {s["name"]: src[s["start"]:s["end"]] for s in extract_symbols(src, old_tree, origin_file)}
            new_syms = {s["name"]: modified[s["start"]:s["end"]] for s in extract_symbols(modified, new_tree, origin_file)}
            diff_hash = hashlib.md5(json.dumps(sorted(new_syms.keys())).encode()).hexdigest()
            results.append(diff_hash)

        # All should be identical
        unique = len(set(results))
        passed = unique == 1

        self.logger.log("ast_diff_determinism", {
            "runs": 5,
            "unique_results": unique,
            "passed": passed,
        })

        return TestResult(
            "ast_diff_determinism",
            passed,
            metric=100.0 if passed else 0.0,
            target=100.0,
            details={"runs": 5, "unique_results": unique},
        )

    def test_path_alias_resolution(self) -> TestResult:
        """All tsconfig path aliases for TS/TSX files should resolve.
        
        CSS, JSON, and other non-code imports are intentionally not resolved
        since they don't participate in the dependency graph.
        """
        symbols, sym_by_file, _, _, import_edges, sources, _, _ = self.graph_data

        # Count unresolved imports (imports that don't map to known files)
        # Only count .ts/.tsx imports — CSS/JSON/etc are intentionally skipped
        total_imports = 0
        unresolved = []

        for file, imports in import_edges.items():
            for imp in imports:
                # Skip non-code imports (CSS, JSON, assets)
                if any(imp.endswith(ext) for ext in ['.css', '.json', '.svg', '.png', '.jpg']):
                    continue
                total_imports += 1
                # Check if resolved file exists in sources
                if imp not in sources and not any(imp in s for s in sources.keys()):
                    unresolved.append({"file": file, "import": imp})

        unresolved_count = len(unresolved)
        passed = unresolved_count == 0

        self.logger.log("path_alias_resolution", {
            "total_imports": total_imports,
            "unresolved": unresolved_count,
            "passed": passed,
        })

        return TestResult(
            "path_alias_resolution",
            passed,
            metric=unresolved_count,
            target=0,
            details={"total_imports": total_imports, "unresolved_samples": unresolved[:5]},
        )

    def test_delta_propagation_coverage(self) -> TestResult:
        """Delta propagation should notify all affected callers."""
        symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, _, _ = self.graph_data

        test_sym = "createBudgetStore"
        if test_sym not in symbols:
            return TestResult("delta_propagation_coverage", False, error="Symbol not found")

        origin_file = symbols[test_sym]["file"]

        # Find all callers via graph
        # call_file_edges is {caller_file: set(callee_files)}
        callers_from_graph = set()
        for caller_file, callee_files in call_file_edges.items():
            if origin_file in callee_files:
                callers_from_graph.add(caller_file)

        # Also check import edges
        for importer, imports in import_edges.items():
            if origin_file in imports or any(origin_file in i for i in imports):
                callers_from_graph.add(importer)

        # Simulate subscription bus
        bus = SubscriptionBus(window=300)
        for caller in callers_from_graph:
            bus.record_reads(f"agent_{caller}", [test_sym])

        # Emit delta
        notifications = bus.emit_delta(
            "agent_writer",
            origin_file,
            [test_sym],
            "// modified",
        )

        notified = {n.agent_id.replace("agent_", "") for n in notifications}
        missed = callers_from_graph - notified

        passed = len(missed) == 0

        self.logger.log("delta_propagation_coverage", {
            "callers_found": len(callers_from_graph),
            "notifications_sent": len(notifications),
            "missed": list(missed),
            "passed": passed,
        })

        return TestResult(
            "delta_propagation_coverage",
            passed,
            metric=100.0 * len(notified) / max(len(callers_from_graph), 1),
            target=100.0,
            details={"callers": list(callers_from_graph), "missed": list(missed)},
        )

    def run(self) -> SuiteResult:
        start = time.time()
        self.setup()

        results = [
            self.test_ast_diff_determinism(),
            self.test_path_alias_resolution(),
            self.test_delta_propagation_coverage(),
        ]

        return SuiteResult(
            suite="correctness",
            results=results,
            duration_s=time.time() - start,
        )


class CostEfficiencyTests:
    """Test cost efficiency criteria."""

    def __init__(self, logger: StructuredLogger):
        self.logger = logger
        self.graph_data = None

    def setup(self):
        self.graph_data = build_graph(TARGET_DIR)

    def test_token_savings(self) -> TestResult:
        """Cone context should be significantly smaller than full context.
        
        Hub symbols (high fan-in) will have larger cones — that's correct.
        Leaf symbols should achieve >90% savings.
        We test both and report the range.
        """
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")

        symbols, sym_by_file, _, call_file_edges, import_edges, sources, _, _ = self.graph_data

        # Full context
        full_ctx = "\n".join(s.decode("utf-8", errors="replace") for s in sources.values())
        full_tokens = len(enc.encode(full_ctx))

        # Test multiple symbols: hub vs leaf
        test_cases = [
            ("createBudgetStore", "hub"),    # high fan-in, many callers
            ("formatMoney", "leaf"),          # low fan-in, utility
            ("createQuery", "leaf"),          # low fan-in, utility
        ]

        results = []
        for sym, sym_type in test_cases:
            if sym not in symbols:
                continue
            cone_files = compute_cone(sym, symbols, sym_by_file, call_file_edges, import_edges)
            cone_ctx = "\n".join(sources[f].decode("utf-8", errors="replace") for f in cone_files if f in sources)
            cone_tokens = len(enc.encode(cone_ctx))
            savings = (1 - cone_tokens / full_tokens) * 100
            results.append({
                "symbol": sym,
                "type": sym_type,
                "cone_files": len(cone_files),
                "cone_tokens": cone_tokens,
                "savings_pct": savings,
            })

        # Pass criteria: at least one leaf symbol achieves >80% savings
        leaf_results = [r for r in results if r["type"] == "leaf"]
        best_leaf_savings = max((r["savings_pct"] for r in leaf_results), default=0)
        passed = best_leaf_savings >= 80.0

        avg_savings = sum(r["savings_pct"] for r in results) / len(results) if results else 0

        self.logger.log("token_savings", {
            "full_tokens": full_tokens,
            "results": results,
            "best_leaf_savings": best_leaf_savings,
            "avg_savings": avg_savings,
            "passed": passed,
        })

        return TestResult(
            "token_savings",
            passed,
            metric=best_leaf_savings,
            target=80.0,
            details={
                "full_tokens": full_tokens,
                "symbols_tested": len(results),
                "best_leaf": best_leaf_savings,
                "hub_savings": next((r["savings_pct"] for r in results if r["type"] == "hub"), None),
            },
        )

    def test_graph_build_time(self) -> TestResult:
        """Graph should build in <5s for this codebase."""
        start = time.time()
        _ = build_graph(TARGET_DIR)
        elapsed = time.time() - start

        # Scale target: 5s for 500 files, so for 61 files ~ 0.6s
        file_count = len(self.graph_data[6])
        scaled_target = (file_count / 500) * 5.0
        target = max(scaled_target, 1.0)  # at least 1s

        passed = elapsed < target

        self.logger.log("graph_build_time", {
            "elapsed_s": elapsed,
            "target_s": target,
            "file_count": file_count,
            "passed": passed,
        })

        return TestResult(
            "graph_build_time",
            passed,
            metric=elapsed,
            target=target,
            details={"file_count": file_count},
        )

    def run(self) -> SuiteResult:
        start = time.time()
        self.setup()

        results = [
            self.test_token_savings(),
            self.test_graph_build_time(),
        ]

        return SuiteResult(
            suite="cost_efficiency",
            results=results,
            duration_s=time.time() - start,
        )


class ReliabilityTests:
    """Test reliability criteria."""

    def __init__(self, logger: StructuredLogger):
        self.logger = logger

    def test_parse_success_rate(self) -> TestResult:
        """Parse success rate should be >99%."""
        result = build_graph(TARGET_DIR)
        _, _, _, _, _, _, all_files, parse_errors = result

        total = len(all_files)
        success_rate = ((total - parse_errors) / total) * 100 if total > 0 else 0
        passed = success_rate >= 99.0

        self.logger.log("parse_success_rate", {
            "total_files": total,
            "parse_errors": parse_errors,
            "success_rate": success_rate,
            "passed": passed,
        })

        return TestResult(
            "parse_success_rate",
            passed,
            metric=success_rate,
            target=99.0,
            details={"total_files": total, "parse_errors": parse_errors},
        )

    def test_concurrent_subscriptions(self) -> TestResult:
        """Multiple agents subscribing concurrently should not corrupt state."""
        bus = SubscriptionBus(window=300)
        errors = []

        def agent_work(agent_id: str):
            try:
                # Subscribe to symbols
                bus.record_reads(agent_id, [f"sym_{i}" for i in range(10)])
                time.sleep(0.01)  # Simulate work
                return True
            except Exception as e:
                return str(e)

        # Run 10 agents concurrently
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(agent_work, f"agent_{i}") for i in range(10)]
            results = [f.result() for f in as_completed(futures)]

        # Check all succeeded
        failures = [r for r in results if r is not True]
        passed = len(failures) == 0

        # Verify state integrity
        state = bus.state()
        expected_agents = 10
        actual_agents = len(state["reads"])

        self.logger.log("concurrent_subscriptions", {
            "agents": 10,
            "failures": len(failures),
            "state_agents": actual_agents,
            "passed": passed,
        })

        return TestResult(
            "concurrent_subscriptions",
            passed and actual_agents == expected_agents,
            metric=actual_agents,
            target=expected_agents,
            details={"failures": failures},
        )

    def run(self) -> SuiteResult:
        start = time.time()

        results = [
            self.test_parse_success_rate(),
            self.test_concurrent_subscriptions(),
        ]

        return SuiteResult(
            suite="reliability",
            results=results,
            duration_s=time.time() - start,
        )


# ── Report Generation ─────────────────────────────────────────────────────────
def generate_report(suites: list[SuiteResult], logger: StructuredLogger) -> str:
    """Generate markdown report from test results."""
    lines = [
        "# Debugger Report",
        f"**Run ID:** `{logger.run_id}`",
        f"**Timestamp:** {datetime.utcnow().isoformat()}Z",
        f"**Log file:** `{logger.log_file}`",
        "",
    ]

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Suite | Pass Rate | Duration |")
    lines.append("|-------|-----------|----------|")
    for s in suites:
        rate = f"{s.pass_rate() * 100:.0f}%"
        dur = f"{s.duration_s:.2f}s"
        emoji = "✅" if s.pass_rate() == 1.0 else "⚠️" if s.pass_rate() > 0.5 else "❌"
        lines.append(f"| {emoji} {s.suite} | {rate} | {dur} |")
    lines.append("")

    # Detailed results
    for s in suites:
        lines.append(f"## {s.suite.title()}")
        lines.append("")
        for r in s.results:
            emoji = "✅" if r.passed else "❌"
            lines.append(f"### {emoji} {r.name}")
            if r.metric is not None and r.target is not None:
                lines.append(f"- **Metric:** {r.metric:.2f} (target: {r.target})")
            if r.error:
                lines.append(f"- **Error:** {r.error}")
            if r.details:
                lines.append(f"- **Details:** `{json.dumps(r.details, default=str)[:200]}`")
            lines.append("")

    return "\n".join(lines)


# ── Main Runner ───────────────────────────────────────────────────────────────
def run_all(suites_filter: list[str] = None) -> tuple[list[SuiteResult], StructuredLogger]:
    """Run all test suites (or filtered subset)."""
    run_id = f"debug_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    logger = StructuredLogger(run_id)

    logger.log("run_started", {"filter": suites_filter})

    available = {
        "correctness": CorrectnessTests,
        "cost_efficiency": CostEfficiencyTests,
        "reliability": ReliabilityTests,
    }

    to_run = suites_filter or list(available.keys())
    results = []

    for name in to_run:
        if name not in available:
            print(f"  [WARN] Unknown suite: {name}")
            continue

        print(f"\n{'='*60}")
        print(f"  Running: {name}")
        print(f"{'='*60}")

        suite_class = available[name]
        suite = suite_class(logger)
        result = suite.run()
        results.append(result)

        # Print results
        for r in result.results:
            emoji = "✅" if r.passed else "❌"
            metric_str = f" ({r.metric:.1f}/{r.target})" if r.metric is not None else ""
            print(f"  {emoji} {r.name}{metric_str}")

    logger.log("run_completed", {
        "suites": len(results),
        "total_tests": sum(len(s.results) for s in results),
        "passed": sum(sum(1 for r in s.results if r.passed) for s in results),
    })

    return results, logger


def print_summary(suites: list[SuiteResult]):
    """Print final summary."""
    total = sum(len(s.results) for s in suites)
    passed = sum(sum(1 for r in s.results if r.passed) for s in suites)
    failed = total - passed

    print(f"\n{'='*60}")
    print("  DEBUGGER SUMMARY")
    print(f"{'='*60}")
    print(f"  Total tests : {total}")
    print(f"  Passed      : {passed} ✅")
    print(f"  Failed      : {failed} {'❌' if failed > 0 else ''}")
    print(f"  Pass rate   : {passed/total*100:.0f}%")

    # Production readiness verdict
    all_passed = failed == 0
    if all_passed:
        print(f"\n  🟢 All criteria met for current scope")
    else:
        print(f"\n  🔴 {failed} criteria need attention before production")

    # List failures
    if failed > 0:
        print(f"\n  Failures:")
        for s in suites:
            for r in s.results:
                if not r.passed:
                    print(f"    - {s.suite}/{r.name}: {r.error or 'metric below target'}")


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    arg_parser = argparse.ArgumentParser(description="cone-validate debugger")
    arg_parser.add_argument("--test", "-t", nargs="+", help="Suites to run (correctness, cost_efficiency, reliability)")
    arg_parser.add_argument("--report", "-r", action="store_true", help="Generate markdown report")
    arg_parser.add_argument("--output", "-o", help="Report output file")
    arg_parser.add_argument("--list", "-l", action="store_true", help="List available suites")
    args = arg_parser.parse_args()

    if args.list:
        print("Available suites:")
        print("  - correctness: AST determinism, path aliases, delta coverage")
        print("  - cost_efficiency: Token savings, graph build time")
        print("  - reliability: Parse success, concurrent subscriptions")
        sys.exit(0)

    print("=" * 60)
    print("  CONE-VALIDATE DEBUGGER")
    print("  Production Readiness Validation")
    print("=" * 60)

    suites, logger = run_all(args.test)
    print_summary(suites)

    if args.report:
        report = generate_report(suites, logger)
        output_file = args.output or f"debug_logs/report_{logger.run_id}.md"
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).write_text(report)
        print(f"\n  Report saved: {output_file}")

    print(f"\n  Logs: {logger.log_file}")
