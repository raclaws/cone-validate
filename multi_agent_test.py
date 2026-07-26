#!/usr/bin/env python3
"""
Multi-agent test: delta propagation via the graph layer.

Flow:
  1. Agent A  — cone context for createBudgetStore → writes modified file (adds reset())
  2. AST diff — tree-sitter parse before/after → extract changed symbols
  3. Graph    — query callers of changed symbols → targeted invalidation set
  4. Agent B  — receives ONLY delta + its own caller cone → "does your code need updating?"

Measures:
  - Does Agent B get the right invalidation signal without seeing Agent A's full context?
  - Token cost of delta-only handoff vs full-context handoff
"""

import os, sys, json, time, copy
sys.path.insert(0, ".")

import tiktoken
import requests
from pathlib import Path
from validate import (
    build_graph, compute_cone,
    parse_file, extract_symbols, extract_calls, parser,
)
from single_agent_test import build_context

# ── Config ────────────────────────────────────────────────────────────────────
try:
    from config import get_target_dir, get_gateway_url, get_api_key
    TARGET_DIR = get_target_dir()
    GATEWAY_URL = get_gateway_url()
    API_KEY = get_api_key()
except (ImportError, ValueError):
    TARGET_DIR  = Path("/root/repos/twenty-dollar/frontend/src")  # Legacy fallback
    GATEWAY_URL = ""
    API_KEY = os.environ.get("GATEWAY_API_KEY", "")
MODEL       = "claude-haiku-4.5"

enc = tiktoken.get_encoding("cl100k_base")

def tok(text: str) -> int:
    return len(enc.encode(text))


# ── LLM call (SSE-aware) ──────────────────────────────────────────────────────
def llm(prompt: str, label: str, max_tokens: int = 2048) -> dict:
    print(f"\n{'─'*60}")
    print(f"  {label}  [{tok(prompt):,} prompt tokens]")
    print(f"{'─'*60}")

    payload = {
        "model": MODEL, "max_tokens": max_tokens, "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    t0 = time.time()
    resp = requests.post(GATEWAY_URL, json=payload, headers=headers, timeout=120)
    latency = round(time.time() - t0, 2)

    if resp.status_code != 200:
        print(f"  [ERROR] {resp.status_code}: {resp.text[:300]}")
        return {"prompt_tokens": tok(prompt), "reply": "", "latency": latency}

    reply = ""
    usage = {}
    if "text/event-stream" in resp.headers.get("content-type", "") or resp.text.startswith("data:"):
        for line in resp.text.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk in ("", "[DONE]"):
                continue
            try:
                obj = json.loads(chunk)
                reply += obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if "usage" in obj:
                    usage = obj["usage"]
            except json.JSONDecodeError:
                pass
        if not usage:
            usage = {"prompt_tokens": tok(prompt), "completion_tokens": tok(reply)}
    else:
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

    pt = usage.get("prompt_tokens", tok(prompt))
    ct = usage.get("completion_tokens", tok(reply))
    print(f"  Latency: {latency}s | prompt: {pt:,} | output: {ct:,}")
    return {"prompt_tokens": pt, "completion_tokens": ct, "latency": latency, "reply": reply}


# ── AST diff ──────────────────────────────────────────────────────────────────
def ast_diff(old_src: bytes, new_src: bytes, file_str: str) -> dict:
    """Compare symbol sets before/after. Returns added/removed/changed."""
    _, old_tree = parse_file.__wrapped__(old_src) if hasattr(parse_file, '__wrapped__') else (None, None)
    from validate import parser  # reuse configured parser
    old_tree = parser.parse(old_src)
    new_tree = parser.parse(new_src)

    old_syms = {s["name"]: old_src[s["start"]:s["end"]] for s in extract_symbols(old_src, old_tree, file_str)}
    new_syms = {s["name"]: new_src[s["start"]:s["end"]] for s in extract_symbols(new_src, new_tree, file_str)}

    added   = set(new_syms) - set(old_syms)
    removed = set(old_syms) - set(new_syms)
    changed = {n for n in set(old_syms) & set(new_syms) if old_syms[n] != new_syms[n]}

    return {
        "added": sorted(added),
        "removed": sorted(removed),
        "changed": sorted(changed),
        "new_src": new_src,
    }


def find_callers(changed_symbols: list[str], sym_by_file: dict, call_edges: dict) -> dict[str, list[str]]:
    """Return {file: [symbols_it_calls_that_changed]}."""
    result = {}
    for file, calls in call_edges.items():
        hits = [s for s in changed_symbols if s in calls]
        if hits:
            result[file] = hits
    return result


# ── Agent A prompt ────────────────────────────────────────────────────────────
AGENT_A_TASK = """You are a TypeScript engineer. Your task is to modify the code below.

TASK: Add a `reset()` method to the `BudgetStore` interface and implement it in `createBudgetStore`.
- The method should clear the internal `categoryMemos` map.
- Add `reset: () => void` to the interface.
- Add `reset: () => categoryMemos.clear()` to the returned object.

OUTPUT RULES (strictly follow):
- Output ONLY the raw modified TypeScript source of `budget-signals.ts`.
- No markdown fences, no explanation, no preamble.
- Output the complete file, character-for-character identical to the input except for the two additions above.

CODE:
"""

# ── Agent B prompt ────────────────────────────────────────────────────────────
def agent_b_prompt(delta: dict, caller_file: str, caller_src: str, changed_syms: list[str]) -> str:
    changed_list = ", ".join(f"`{s}`" for s in changed_syms)
    new_content = delta["new_src"].decode("utf-8", errors="replace")
    return f"""You are a TypeScript engineer reviewing a code change notification.

CHANGE NOTIFICATION (from the graph layer):
  File changed : budget-signals.ts
  Symbols changed: {changed_list}
  New implementation of changed symbols:
---
{new_content}
---

YOUR FILE to review: {caller_file}
---
{caller_src}
---

QUESTIONS:
1. Does your file need any changes as a result of this notification?
2. If yes: list each change with file + line reference + what to change.
3. If no: explain why it is unaffected.

Be specific. Reference actual symbol names and types from the code.
"""


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Building graph ...")
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, errors = build_graph(TARGET_DIR)

    # ── Step 1: Agent A ───────────────────────────────────────────────────────
    a_sym   = "createBudgetStore"
    a_file  = symbols[a_sym]["file"]
    a_cone  = compute_cone(a_sym, symbols, sym_by_file, call_file_edges, import_edges)
    a_ctx   = build_context(a_cone, sources)

    # Find budget-signals.ts specifically (origin file)
    origin_src = sources[a_file]

    a_result = llm(AGENT_A_TASK + origin_src.decode("utf-8", errors="replace"), "AGENT A — write createBudgetStore")
    print(f"\n  Agent A output (first 300 chars):\n  {a_result['reply'][:300]!r}")

    # ── Step 2: AST diff ──────────────────────────────────────────────────────
    new_src = a_result["reply"].encode("utf-8")
    # strip accidental markdown fences if model disobeyed
    if new_src.startswith(b"```"):
        lines = new_src.splitlines()
        new_src = b"\n".join(l for l in lines if not l.startswith(b"```"))

    diff = ast_diff(origin_src, new_src, a_file)
    all_changed = diff["added"] + diff["changed"]
    print(f"\nAST diff on {a_file}:")
    print(f"  Added   : {diff['added']}")
    print(f"  Removed : {diff['removed']}")
    print(f"  Changed : {diff['changed']}")

    # ── Step 3: Graph query — find callers ────────────────────────────────────
    callers = find_callers(all_changed if all_changed else [a_sym], sym_by_file, call_edges)
    print(f"\nCallers affected by delta {all_changed or [a_sym]}:")
    for f, syms in callers.items():
        print(f"  {f}  (calls: {syms})")

    if not callers:
        # fallback: anything that imports the origin file
        callers = {
            f: [a_sym] for f, imps in import_edges.items()
            if any(a_file.replace(".ts","") in imp for imp in imps)
        }
        print(f"  (no call-edge callers; using import callers: {list(callers.keys())})")

    # ── Step 4: Agent B ───────────────────────────────────────────────────────
    # Pick the most interesting caller (most calls to changed symbols)
    b_file = max(callers, key=lambda f: len(callers[f])) if callers else None

    if not b_file:
        print("\nNo callers found — cannot run Agent B.")
        sys.exit(0)

    b_src = sources.get(b_file, b"").decode("utf-8", errors="replace")
    b_prompt = agent_b_prompt(diff, b_file, b_src, callers[b_file])

    b_result = llm(b_prompt, f"AGENT B — review caller {b_file}")

    print(f"\n  Agent B response:\n")
    print(b_result["reply"])

    # ── Summary ───────────────────────────────────────────────────────────────
    full_ctx_tok = tok(build_context(set(sources.keys()), sources))
    delta_tok    = tok(diff["new_src"].decode("utf-8", errors="replace"))
    b_cone       = compute_cone(b_file.split("/")[-1].replace(".ts",""), symbols, sym_by_file, call_file_edges, import_edges)
    b_cone_tok   = tok(build_context(b_cone, sources)) if b_cone else tok(b_src)

    print(f"\n{'='*60}")
    print("  MULTI-AGENT SUMMARY")
    print(f"{'='*60}")
    print(f"  Agent A cone tokens          : {a_result['prompt_tokens']:>8,}")
    print(f"  Agent B delta+caller tokens  : {b_result['prompt_tokens']:>8,}")
    print(f"  Full-context baseline        : {full_ctx_tok:>8,}")
    print(f"  Combined (A+B) actual        : {a_result['prompt_tokens'] + b_result['prompt_tokens']:>8,}")
    combined = a_result['prompt_tokens'] + b_result['prompt_tokens']
    print(f"  Savings vs 2× full context   : {(1 - combined/(2*full_ctx_tok))*100:>7.1f}%")
    print(f"\n  AST diff symbols changed     : {len(all_changed)}")
    print(f"  Callers notified             : {len(callers)}")
    print(f"  Agent A latency              : {a_result['latency']}s")
    print(f"  Agent B latency              : {b_result['latency']}s")
