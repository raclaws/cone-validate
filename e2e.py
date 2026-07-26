#!/usr/bin/env python3
"""
Full end-to-end test: the complete loop.

  Agent A (Haiku, origin file) writes reset()
  → oracle validates (tsc, definition-first)
  → AST diff extracts changed symbols
  → SubscriptionBus routes delta to Agent B
  → Agent B (Haiku, caller file) verifies no breakage

This is the minimum viable proof that the architecture works end-to-end.
"""

import os, sys, re, json, time, subprocess, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import requests, tiktoken
from validate import build_graph, compute_cone, parser, extract_symbols
from single_agent_test import build_context
from oracle_loop import (
    run_tsc, make_baseline_key, filter_cone_errors,
    dedup_errors, format_errors, llm,
    WRITE_PROMPT, retry_prompt, escalation_prompt,
)
from config import (
    get_target_dir, get_project_root,
    get_model_cheap, get_model_strong, get_max_retries,
)
from subscription import SubscriptionBus

enc = tiktoken.get_encoding("cl100k_base")
def tok(t): return len(enc.encode(t))

# ── AST diff (inline, no extra dep) ──────────────────────────────────────────
def ast_diff_symbols(old_src: bytes, new_src: bytes, file_str: str) -> dict:
    old_tree = parser.parse(old_src)
    new_tree = parser.parse(new_src)
    old = {s["name"]: old_src[s["start"]:s["end"]] for s in extract_symbols(old_src, old_tree, file_str)}
    new = {s["name"]: new_src[s["start"]:s["end"]] for s in extract_symbols(new_src, new_tree, file_str)}
    return {
        "added":   sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(n for n in set(old) & set(new) if old[n] != new[n]),
    }


# ── Agent B prompt ────────────────────────────────────────────────────────────
def agent_b_prompt(changed_file: str, changed_symbols: list[str],
                   new_src: str, caller_file: str, caller_src: str) -> str:
    sym_list = ", ".join(f"`{s}`" for s in changed_symbols)
    return f"""You are a TypeScript engineer reviewing a change notification from the graph layer.

CHANGE NOTIFICATION:
  File changed    : {changed_file}
  Symbols changed : {sym_list}
  New source:
---
{new_src}
---

YOUR FILE: {caller_file}
---
{caller_src}
---

Questions:
1. Does your file need any changes as a result of this notification?
2. If yes: list each change with line reference and what to change.
3. If no: explain why it is unaffected.

Be specific. Reference actual function names and types from the code.
"""


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 62)
    print("  END-TO-END TEST: write → validate → notify → verify")
    print("=" * 62)

    # ── Graph ─────────────────────────────────────────────────────────────────
    print("\n[1/5] Building graph ...")
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, errors = build_graph(get_target_dir())
    print(f"      {len(all_files)} files, {len(symbols)} symbols")

    target_sym  = "createBudgetStore"
    origin_file = symbols[target_sym]["file"]
    origin_path = get_target_dir() / origin_file
    origin_src  = sources[origin_file].decode("utf-8", errors="replace")
    cone_files  = compute_cone(target_sym, symbols, sym_by_file, call_file_edges, import_edges)
    cone_ctx    = build_context(cone_files, sources)

    # ── Subscription: Agent B pre-registers reads ─────────────────────────────
    print("\n[2/5] Agent B registers symbol reads (simulates context build) ...")
    bus = SubscriptionBus(window=300)
    # Agent B (App.tsx) would have queried createBudgetStore when building context
    bus.record_reads("agent_b", [target_sym, "BudgetStore"])
    print(f"      Agent B subscribed to: {[target_sym, 'BudgetStore']}")

    # ── Oracle loop: Agent A writes + tsc validates ───────────────────────────
    print("\n[3/5] Oracle loop: Agent A writes reset(), tsc validates ...")
    baseline_errors = run_tsc(get_project_root())
    baseline_keys   = {make_baseline_key(e) for e in baseline_errors}
    print(f"      Baseline tsc errors: {len(baseline_errors)}")

    log = []
    current_code = origin_src
    model = get_model_cheap()
    last_errors = []
    final_code = None

    for attempt in range(1, get_max_retries() + 2):
        is_escalation = attempt > get_max_retries()
        if attempt == 1:
            prompt = WRITE_PROMPT + origin_src
            label  = f"Agent A attempt {attempt}"
        elif is_escalation:
            prompt = escalation_prompt(origin_src, cone_ctx, format_errors(last_errors))
            model  = get_model_strong()
            label  = f"Agent A ESCALATION ({get_model_strong()})"
        else:
            prompt = retry_prompt(origin_src, current_code, format_errors(last_errors), attempt)
            label  = f"Agent A retry {attempt}"

        result = llm(prompt, model, label)
        if result.get("error"):
            break

        new_code = result["reply"]
        if "```" in new_code:
            new_code = "\n".join(l for l in new_code.splitlines() if not l.startswith("```"))

        backup = origin_path.read_bytes()
        try:
            origin_path.write_text(new_code)
            all_err   = run_tsc(get_project_root())
            new_err   = [e for e in all_err if make_baseline_key(e) not in baseline_keys]
            chg_err   = [e for e in new_err if origin_file in e["file"] or e["file"].endswith(origin_file)]
            cone_err  = filter_cone_errors(new_err, cone_files, get_target_dir()) if not chg_err else chg_err
            deduped   = dedup_errors(chg_err if chg_err else cone_err, origin_file)
        finally:
            origin_path.write_bytes(backup)

        passed = len(chg_err) == 0
        entry  = {"attempt": attempt, "model": model,
                  "prompt_tokens": result["prompt_tokens"],
                  "passed": passed, "errors": len(chg_err)}
        log.append(entry)
        print(f"      Attempt {attempt} ({model}): {'✅ PASS' if passed else f'❌ {len(chg_err)} errors'}")

        if passed:
            final_code = new_code
            break

        last_errors  = deduped
        current_code = new_code
        if is_escalation:
            print("      Oracle loop exhausted.")
            break

    if not final_code:
        print("\n❌ Could not produce valid code. Aborting.")
        sys.exit(1)

    # ── AST diff ──────────────────────────────────────────────────────────────
    print("\n[4/5] AST diff: extracting changed symbols ...")
    diff = ast_diff_symbols(origin_src.encode(), final_code.encode(), origin_file)
    all_changed = diff["added"] + diff["changed"]
    print(f"      Added  : {diff['added']}")
    print(f"      Changed: {diff['changed']}")

    # ── Subscription: emit delta, notify Agent B ──────────────────────────────
    notifications = bus.emit_delta(
        emitting_agent="agent_a",
        changed_file=origin_file,
        changed_symbols=all_changed if all_changed else [target_sym],
        new_src=final_code,
    )
    print(f"\n      Delta emitted. Notifications queued: {len(notifications)}")
    for n in notifications:
        print(f"      → {n.summary()}")

    # ── Agent B verifies ──────────────────────────────────────────────────────
    print("\n[5/5] Agent B flushes notifications and verifies ...")
    flushed = bus.flush("agent_b")

    # pick App.tsx as Agent B's file
    b_file = "App.tsx"
    b_src  = sources.get(b_file, b"").decode("utf-8", errors="replace")

    for notif in flushed:
        b_prompt = agent_b_prompt(
            notif.changed_file, notif.changed_symbols,
            notif.new_src, b_file, b_src,
        )
        b_result = llm(b_prompt, MODEL_CHEAP, f"Agent B verify ({b_file})")
        print(f"\n  Agent B response:\n")
        print(b_result["reply"])

    # ── Summary ───────────────────────────────────────────────────────────────
    total_a = sum(e["prompt_tokens"] for e in log)
    total_b = b_result.get("prompt_tokens", 0) if flushed else 0
    full_baseline = tok(build_context(set(sources.keys()), sources))

    print(f"\n{'='*62}")
    print("  END-TO-END SUMMARY")
    print(f"{'='*62}")
    print(f"  Oracle attempts          : {len(log)}")
    print(f"  Winning model            : {next(e['model'] for e in log if e['passed'])}")
    print(f"  Agent A total tokens     : {total_a:,}")
    print(f"  Agent B tokens           : {total_b:,}")
    print(f"  Combined (A+B)           : {total_a + total_b:,}")
    print(f"  2× full-context baseline : {2 * full_baseline:,}")
    print(f"  Savings                  : {(1-(total_a+total_b)/(2*full_baseline))*100:.1f}%")
    print(f"  Notifications routed     : {len(notifications)}")
    print(f"  tsc oracle result        : ✅ PASS")
