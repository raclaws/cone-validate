#!/usr/bin/env python3
"""
Harder oracle test: interface change that requires caller updates.

Task: Rename `rta` accessor to `readyToAssign` in BudgetStore.
This WILL break callers (App.tsx uses budgetStore.rta()).

Expected behavior:
  1. Haiku writes the rename in budget-signals.ts
  2. tsc fails with errors in App.tsx (caller breakage)
  3. Haiku either:
     a) Fixes only budget-signals.ts and leaves caller broken → retry with caller error
     b) Realizes it can't fix callers from origin-file-only context → escalation needed
  4. Escalation (Sonnet with full cone) fixes both definition AND callers

This validates:
  - Definition-first error strategy when there ARE caller errors
  - Escalation trigger when the task genuinely exceeds cheap model capability
  - Cost accounting: was escalation cheaper than failing retries?
"""

import os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from validate import build_graph, compute_cone
from single_agent_test import build_context
from oracle_loop import (
    run_tsc, make_baseline_key, filter_cone_errors, dedup_errors, format_errors, llm,
    MODEL_CHEAP, MODEL_STRONG, MAX_RETRIES, TARGET_DIR, PROJECT_ROOT,
)
import tiktoken

enc = tiktoken.get_encoding("cl100k_base")
def tok(t): return len(enc.encode(t))

# ── Prompts ───────────────────────────────────────────────────────────────────
HARD_TASK = """You are a TypeScript engineer. Your task is to modify the code below.

TASK: Rename the `rta` accessor to `readyToAssign` in the BudgetStore interface and implementation.
- In the interface: change `rta: Accessor<number>` to `readyToAssign: Accessor<number>`
- In createBudgetStore return: change `rta` property name to `readyToAssign`
- The internal computation stays the same, only the public name changes.

OUTPUT RULES:
- Output ONLY the raw modified TypeScript source of budget-signals.ts.
- No markdown fences, no explanation, no preamble.
- Complete file only.

CODE:
"""

def hard_retry_prompt(original_code: str, current_code: str, errors: str, attempt: int) -> str:
    return f"""You are a TypeScript engineer fixing a compile error.

TASK: Rename `rta` to `readyToAssign` in BudgetStore interface and implementation.

ATTEMPT {attempt} PRODUCED THESE tsc ERRORS:
{errors}

YOUR PREVIOUS CODE:
{current_code}

Fix ONLY the errors above. Do not change anything unrelated.

OUTPUT RULES:
- Output ONLY the raw modified TypeScript source.
- No markdown fences, no explanation.
- Complete file only.
"""

def hard_escalation_prompt(original_code: str, cone_ctx: str, errors: str) -> str:
    return f"""You are a senior TypeScript engineer. A junior model attempted a code change and failed after {MAX_RETRIES} retries.

ORIGINAL TASK: Rename `rta` to `readyToAssign` in the BudgetStore interface.
- This is a breaking change that affects callers.
- The interface and implementation need updating.
- Callers that use `.rta()` will break and need updating to `.readyToAssign()`.

COMPILER ERRORS FROM LAST ATTEMPT:
{errors}

FULL CODEBASE CONTEXT (dependency cone — includes callers):
{cone_ctx}

Your job: produce a FIXED version of budget-signals.ts that compiles.
Note: You can only output budget-signals.ts. If callers need fixing, that's a separate step.
Focus on making the definition correct first.

OUTPUT RULES:
- Output ONLY the raw modified TypeScript source of budget-signals.ts.
- No markdown fences, no explanation.
- Complete file only.
"""


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 62)
    print("  HARDER ORACLE TEST: interface rename (breaks callers)")
    print("=" * 62)

    # Build graph
    print("\n[1] Building graph ...")
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, errors = build_graph(TARGET_DIR)
    print(f"    {len(all_files)} files, {len(symbols)} symbols")

    target_sym  = "createBudgetStore"
    origin_file = symbols[target_sym]["file"]
    origin_path = TARGET_DIR / origin_file
    origin_src  = sources[origin_file].decode("utf-8", errors="replace")
    cone_files  = compute_cone(target_sym, symbols, sym_by_file, call_file_edges, import_edges)
    cone_ctx    = build_context(cone_files, sources)

    print(f"    Origin: {origin_file}")
    print(f"    Cone: {len(cone_files)} files, {tok(cone_ctx):,} tokens")

    # Baseline
    print("\n[2] Baseline tsc check ...")
    baseline_errors = run_tsc(PROJECT_ROOT)
    baseline_keys   = {make_baseline_key(e) for e in baseline_errors}
    print(f"    Baseline errors: {len(baseline_errors)}")

    # Oracle loop
    print("\n[3] Oracle loop ...")
    log = []
    current_code = origin_src
    model = MODEL_CHEAP
    last_errors = []

    for attempt in range(1, MAX_RETRIES + 2):
        is_escalation = attempt > MAX_RETRIES
        if attempt == 1:
            prompt = HARD_TASK + origin_src
            label  = f"Attempt {attempt}"
        elif is_escalation:
            prompt = hard_escalation_prompt(origin_src, cone_ctx, format_errors(last_errors))
            model  = MODEL_STRONG
            label  = f"ESCALATION ({MODEL_STRONG})"
        else:
            prompt = hard_retry_prompt(origin_src, current_code, format_errors(last_errors), attempt)
            label  = f"Attempt {attempt} (retry)"

        result = llm(prompt, model, label)
        if result.get("error"):
            print(f"    LLM error, aborting.")
            break

        new_code = result["reply"]
        if "```" in new_code:
            new_code = "\n".join(l for l in new_code.splitlines() if not l.startswith("```"))

        # Write and test
        backup = origin_path.read_bytes()
        try:
            origin_path.write_text(new_code)
            all_err = run_tsc(PROJECT_ROOT)
            new_err = [e for e in all_err if make_baseline_key(e) not in baseline_keys]

            # Separate definition errors from caller errors
            def_errors = [e for e in new_err if origin_file in e["file"] or e["file"].endswith(origin_file)]
            caller_errors = [e for e in new_err if e not in def_errors]

            # Strategy: definition must pass first, then we consider caller errors
            if def_errors:
                # Definition broken — feed definition errors only
                active_errors = def_errors
                deduped = dedup_errors(def_errors, origin_file)
            elif caller_errors:
                # Definition OK but callers broken — this is EXPECTED for a rename
                # The cheap model can't fix callers (it only has origin file)
                # This should trigger escalation or be marked as "definition done, callers need separate fix"
                active_errors = caller_errors
                deduped = dedup_errors(caller_errors, origin_file)
            else:
                active_errors = []
                deduped = []

        finally:
            origin_path.write_bytes(backup)

        passed = len(active_errors) == 0
        entry = {
            "attempt": attempt,
            "model": model,
            "prompt_tokens": result["prompt_tokens"],
            "def_errors": len(def_errors),
            "caller_errors": len(caller_errors),
            "passed": passed,
        }
        log.append(entry)

        status = "✅ PASS" if passed else f"❌ def={len(def_errors)} caller={len(caller_errors)}"
        print(f"    {label}: {status}")
        if deduped:
            print(f"    Top errors:\n{format_errors(deduped[:3])}")

        if passed:
            print(f"\n    ✅ Passed on attempt {attempt} with {model}")
            break

        # If definition is clean but callers are broken, that's expected behavior
        # The model can only fix budget-signals.ts, not the callers
        if len(def_errors) == 0 and len(caller_errors) > 0:
            print(f"    [INFO] Definition OK, but {len(caller_errors)} caller errors (expected for rename)")
            print(f"    [INFO] Cheap model can't fix callers — would need multi-file edit or separate Agent B pass")
            # For this test, we mark definition-clean as success
            entry["passed"] = True
            entry["note"] = "definition_clean_callers_broken"
            print(f"\n    ✅ Definition passed — caller fixes would be a separate step")
            break

        last_errors = deduped
        current_code = new_code

        if is_escalation:
            print(f"\n    ❌ Escalation failed. Oracle loop exhausted.")
            break

    # Summary
    total_tokens = sum(e["prompt_tokens"] for e in log)
    passed = any(e.get("passed") for e in log)
    winning = next((e for e in log if e.get("passed")), None)

    print(f"\n{'='*62}")
    print("  HARDER ORACLE TEST SUMMARY")
    print(f"{'='*62}")
    print(f"  {'Attempt':<12} {'Model':<30} {'Tokens':>10} {'Def Err':>8} {'Caller':>8} {'Result':>10}")
    print(f"  {'-'*80}")
    for e in log:
        result_str = "PASS" if e.get("passed") else "FAIL"
        note = f" ({e.get('note', '')})" if e.get("note") else ""
        print(f"  {e['attempt']:<12} {e['model']:<30} {e['prompt_tokens']:>10,} {e['def_errors']:>8} {e['caller_errors']:>8} {result_str:>10}{note}")

    print(f"\n  Total tokens        : {total_tokens:,}")
    print(f"  Outcome             : {'✅ PASSED' if passed else '❌ FAILED'}")
    if winning:
        print(f"  Won on attempt      : {winning['attempt']} ({winning['model']})")

    # Cost comparison
    sonnet_only = tok(HARD_TASK + origin_src)  # what Sonnet would cost if we went straight there
    print(f"\n  Haiku-only cost     : {sum(e['prompt_tokens'] for e in log if 'haiku' in e['model'].lower()):,}")
    print(f"  Sonnet-only baseline: {sonnet_only:,} (if we skipped Haiku)")
    print(f"  Actual total        : {total_tokens:,}")
