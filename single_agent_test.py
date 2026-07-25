#!/usr/bin/env python3
"""
Single-agent test: cone-constrained context vs full-lib context.

Hypothesis: An agent given cone context answers change-impact questions
correctly while consuming significantly fewer tokens than full-lib context.

Target symbol: createBudgetStore (5 files, 20.5% of lib)
Question: signature change impact analysis
"""

import os, sys, json, time
import tiktoken
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate import build_graph, compute_cone

# ── Config ────────────────────────────────────────────────────────────────────
TARGET_DIR   = Path("/root/repos/twenty-dollar/frontend/src/lib")
TARGET_SYM   = "createBudgetStore"
GATEWAY_URL  = "https://llm-gateway.selfhost.irfnmzk.work/v1/chat/completions"
MODEL        = "anthropic/claude-haiku-4"   # cheap, fast — right model for the cascade test
API_KEY      = os.environ.get("GATEWAY_API_KEY", "")

enc = tiktoken.get_encoding("cl100k_base")

QUESTION = """You are a TypeScript code analyst.

I want to add a `reset()` method to the return type of `createBudgetStore`.
Given the code context below:

1. List every file that would need changes (with a one-line reason each).
2. For each file, describe the minimal change needed.
3. Are there any callers that would break immediately? Which ones?

Be specific — reference actual function names and types from the code.
"""

# ── Helpers ───────────────────────────────────────────────────────────────────
def tokens(text: str) -> int:
    return len(enc.encode(text))


def build_context(files: set[str], sources: dict[str, bytes]) -> str:
    parts = []
    for f in sorted(files):
        if f not in sources:
            continue
        src = sources[f].decode("utf-8", errors="replace")
        parts.append(f"// ── FILE: {f} ──\n{src}")
    return "\n\n".join(parts)


def call_llm(context: str, label: str) -> dict:
    prompt = QUESTION + "\n\n```typescript\n" + context + "\n```"
    prompt_tokens = tokens(prompt)

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  Context tokens: {prompt_tokens:,}")
    print(f"{'='*60}")

    if not API_KEY:
        print("  [SKIP] GATEWAY_API_KEY not set — printing context size only")
        return {"label": label, "prompt_tokens": prompt_tokens, "response": None, "latency": None}

    payload = {
        "model": MODEL,
        "max_tokens": 1024,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    t0 = time.time()
    resp = requests.post(GATEWAY_URL, json=payload, headers=headers, timeout=120)
    latency = round(time.time() - t0, 2)

    if resp.status_code != 200:
        print(f"  [ERROR] {resp.status_code}: {resp.text[:200]}")
        return {"label": label, "prompt_tokens": prompt_tokens, "response": None, "latency": latency}

    # Gateway may return SSE even with stream=False — handle both
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type or resp.text.startswith("data:"):
        # Parse SSE chunks
        reply = ""
        usage = {}
        for line in resp.text.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk in ("", "[DONE]"):
                continue
            try:
                obj = json.loads(chunk)
                delta = obj.get("choices", [{}])[0].get("delta", {})
                reply += delta.get("content", "")
                if "usage" in obj:
                    usage = obj["usage"]
            except json.JSONDecodeError:
                pass
        # estimate usage from tiktoken if not reported
        if not usage:
            usage = {"prompt_tokens": prompt_tokens, "completion_tokens": tokens(reply)}
    else:
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

    print(f"  Latency:        {latency}s")
    pt = usage.get('prompt_tokens', 0)
    ct = usage.get('completion_tokens', 0)
    print(f"  Prompt tokens:  {pt:,}" if pt else "  Prompt tokens:  ?")
    print(f"  Output tokens:  {ct:,}" if ct else "  Output tokens:  ?")
    print(f"\n  --- Response ---")
    print(reply)

    return {
        "label": label,
        "prompt_tokens": usage.get("prompt_tokens", prompt_tokens),
        "completion_tokens": usage.get("completion_tokens", 0),
        "latency": latency,
        "response": reply,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Building graph for {TARGET_DIR} ...")
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, errors = build_graph(TARGET_DIR)

    if TARGET_SYM not in symbols:
        print(f"Symbol '{TARGET_SYM}' not found. Available: {list(symbols.keys())[:10]}")
        sys.exit(1)

    # ── Cone context ─────────────────────────────────────────────────────────
    cone_files = compute_cone(TARGET_SYM, symbols, sym_by_file, call_file_edges, import_edges)
    cone_ctx   = build_context(cone_files, sources)
    cone_tok   = tokens(cone_ctx)

    # ── Full lib context ──────────────────────────────────────────────────────
    full_files = set(sources.keys())
    full_ctx   = build_context(full_files, sources)
    full_tok   = tokens(full_ctx)

    print(f"\nTarget symbol : {TARGET_SYM}")
    print(f"Cone files    : {len(cone_files)} / {len(full_files)}")
    print(f"Cone tokens   : {cone_tok:,}")
    print(f"Full tokens   : {full_tok:,}")
    print(f"Reduction     : {(1 - cone_tok/full_tok)*100:.1f}%")
    print(f"\nCone file list:")
    for f in sorted(cone_files):
        print(f"  {f}")

    # ── LLM calls ────────────────────────────────────────────────────────────
    cone_result = call_llm(cone_ctx, "CONE CONTEXT")
    full_result = call_llm(full_ctx, "FULL LIB CONTEXT")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    print(f"  {'':30} {'Cone':>10} {'Full':>10} {'Delta':>10}")
    print(f"  {'-'*62}")
    print(f"  {'Context tokens':<30} {cone_tok:>10,} {full_tok:>10,} {full_tok-cone_tok:>+10,}")
    if cone_result.get("prompt_tokens") and full_result.get("prompt_tokens"):
        ct = cone_result["prompt_tokens"]
        ft = full_result["prompt_tokens"]
        print(f"  {'Billed prompt tokens':<30} {ct:>10,} {ft:>10,} {ft-ct:>+10,}")
        print(f"  {'Token reduction':<30} {(1-ct/ft)*100:>9.1f}%")
    if cone_result.get("latency") and full_result.get("latency"):
        print(f"  {'Latency (s)':<30} {cone_result['latency']:>10} {full_result['latency']:>10}")
