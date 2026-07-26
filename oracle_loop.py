#!/usr/bin/env python3
"""
Oracle loop: tsc validation + retry + escalation.

Flow per attempt:
  1. Write Agent A's output to a temp file
  2. Run tsc --noEmit on the project
  3. Parse errors — filter to cone files only, deduplicate by root cause
  4. If errors: feed first root-cause error back to same model + retry
  5. After MAX_RETRIES: escalate to strong model (clean room — no draft)
  6. Log: attempt, model, tokens, error count, outcome

Dedup strategy (per architecture doc):
  - Sort errors by line number in the CHANGED file first
  - Feed only the first unique error message per changed symbol
  - Once definition error is fixed, call-site cascades often self-resolve
"""

import os, sys, re, json, time, shutil, subprocess, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import requests, tiktoken
from validate import build_graph, compute_cone
from single_agent_test import build_context
from ledger import TokenLedger

# ── Config ────────────────────────────────────────────────────────────────────
from config import (
    get_target_dir, get_project_root, get_gateway_url, get_api_key,
    get_model_cheap, get_model_strong, get_max_retries,
)

# Re-export config getters for backward compatibility with other scripts
TARGET_DIR = get_target_dir
PROJECT_ROOT = get_project_root
GATEWAY_URL = get_gateway_url
API_KEY = get_api_key
MODEL_CHEAP = get_model_cheap
MODEL_STRONG = get_model_strong
MAX_RETRIES = get_max_retries

enc = tiktoken.get_encoding("cl100k_base")

def tok(text: str) -> int:
    return len(enc.encode(text))


# ── LLM call ─────────────────────────────────────────────────────────────────
def llm(prompt: str, model: str, label: str, ledger: TokenLedger = None, 
        agent_id: str = "oracle_loop", task_id: str = "unknown") -> dict:
    print(f"\n  [{label}] model={model} prompt={tok(prompt):,} tok")
    payload = {
        "model": model, "max_tokens": 2048, "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Authorization": f"Bearer {get_api_key()}", "Content-Type": "application/json"}
    t0 = time.time()
    resp = requests.post(get_gateway_url(), json=payload, headers=headers, timeout=120)
    latency_s = time.time() - t0
    latency_ms = latency_s * 1000

    if resp.status_code != 200:
        print(f"  [ERROR] {resp.status_code}: {resp.text[:200]}")
        return {"reply": "", "prompt_tokens": tok(prompt), "completion_tokens": 0, 
                "latency": round(latency_s, 2), "latency_ms": latency_ms, "error": True}

    reply = ""
    usage = {}
    if resp.text.startswith("data:"):
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
    print(f"  [{label}] latency={latency_s:.2f}s output={ct:,} tok")
    
    # Record to ledger if provided
    if ledger is not None:
        ledger.record(agent_id, task_id, pt, ct, model, latency_ms)
    
    return {"reply": reply, "prompt_tokens": pt, "completion_tokens": ct, 
            "latency": round(latency_s, 2), "latency_ms": latency_ms, "error": False}


# ── tsc oracle ────────────────────────────────────────────────────────────────
def run_tsc(project_root: Path) -> list[dict]:
    """Run tsc --noEmit, return parsed error list."""
    result = subprocess.run(
        ["npx", "tsc", "--noEmit", "--pretty", "false"],
        cwd=project_root, capture_output=True, text=True, timeout=60
    )
    return parse_tsc_errors(result.stdout + result.stderr)


def parse_tsc_errors(output: str) -> list[dict]:
    """Parse tsc output into structured error dicts."""
    errors = []
    pattern = re.compile(r"^(.+?)\((\d+),(\d+)\):\s+error\s+(TS\d+):\s+(.+)$", re.MULTILINE)
    for m in pattern.finditer(output):
        errors.append({
            "file": m.group(1).strip(),
            "line": int(m.group(2)),
            "col":  int(m.group(3)),
            "code": m.group(4),
            "message": m.group(5).strip(),
        })
    return errors


def make_baseline_key(e: dict) -> tuple:
    """Baseline key by (file, code, message) — NOT line number.
    Line numbers shift when Agent A rewrites the file, so exact-line matching
    causes pre-existing errors to appear 'new' and triggers false retry loops."""
    return (e["file"], e["code"], e["message"])


def filter_cone_errors(errors: list[dict], cone_files: set[str], target_dir: Path) -> list[dict]:
    """Keep only errors in files within the cone."""
    result = []
    for e in errors:
        # normalize path to repo-relative
        try:
            rel = str(Path(e["file"]).resolve().relative_to(target_dir.resolve()))
        except ValueError:
            rel = e["file"]
        if any(rel.endswith(f) or f.endswith(rel) for f in cone_files):
            result.append({**e, "rel_file": rel})
    return result


def dedup_errors(errors: list[dict], changed_file: str) -> list[dict]:
    """
    Root-cause dedup:
    1. Errors in the changed file first (definition errors)
    2. One error per unique (file, code) pair
    3. Return at most 3 to avoid overwhelming the model
    """
    # sort: changed file first, then by line
    def sort_key(e):
        is_changed = 0 if (changed_file in e.get("rel_file", e["file"])) else 1
        return (is_changed, e["line"])

    sorted_errors = sorted(errors, key=sort_key)

    # dedup by (file, error_code)
    seen = set()
    deduped = []
    for e in sorted_errors:
        key = (e.get("rel_file", e["file"]), e["code"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    return deduped[:3]  # max 3 per architecture doc


def format_errors(errors: list[dict]) -> str:
    lines = []
    for e in errors:
        lines.append(f"  {e.get('rel_file', e['file'])}:{e['line']}:{e['col']} {e['code']}: {e['message']}")
    return "\n".join(lines)


# ── Prompts ───────────────────────────────────────────────────────────────────
WRITE_PROMPT = """You are a TypeScript engineer. Your task is to modify the code below.

TASK: Add a `reset()` method to the `BudgetStore` interface and implement it in `createBudgetStore`.
- Add `reset: () => void` to the interface.
- Add `reset: () => categoryMemos.clear()` to the returned object.

OUTPUT RULES:
- Output ONLY the raw modified TypeScript source of budget-signals.ts.
- No markdown fences, no explanation, no preamble.
- Output the complete file, character-for-character identical to the input except for the two additions.

CODE:
"""

def retry_prompt(original_code: str, current_code: str, errors: str, attempt: int) -> str:
    return f"""You are a TypeScript engineer fixing a compile error.

ORIGINAL TASK: Add `reset: () => void` to BudgetStore interface and `reset: () => categoryMemos.clear()` to the return object.

ATTEMPT {attempt} PRODUCED THESE tsc ERRORS:
{errors}

YOUR PREVIOUS CODE:
{current_code}

Fix ONLY the errors above. Do not change anything else.

OUTPUT RULES:
- Output ONLY the raw modified TypeScript source.
- No markdown fences, no explanation, no preamble.
- Complete file only.
"""

def escalation_prompt(original_code: str, cone_ctx: str, errors: str) -> str:
    return f"""You are a senior TypeScript engineer. A junior model attempted a code change and failed after {MAX_RETRIES} retries.

ORIGINAL TASK: Add a `reset()` method to `createBudgetStore` in budget-signals.ts.
- Add `reset: () => void` to the BudgetStore interface
- Add `reset: () => categoryMemos.clear()` to the returned object

COMPILER ERRORS FROM LAST ATTEMPT:
{errors}

FULL CODEBASE CONTEXT (dependency cone):
{cone_ctx}

Do NOT use the failed code as a starting point. Reason from the errors and the original context.

OUTPUT RULES:
- Output ONLY the raw modified TypeScript source of budget-signals.ts.
- No markdown fences, no explanation, no preamble.
- Complete file only.
"""


# ── Main oracle loop ──────────────────────────────────────────────────────────
def oracle_loop(symbols, sym_by_file, call_file_edges, import_edges, sources, 
                ledger: TokenLedger = None):
    target_sym  = "createBudgetStore"
    origin_file = symbols[target_sym]["file"]  # lib/budget-signals.ts
    origin_path = get_target_dir() / origin_file
    origin_src  = sources[origin_file].decode("utf-8", errors="replace")

    cone_files = compute_cone(target_sym, symbols, sym_by_file, call_file_edges, import_edges)
    cone_ctx   = build_context(cone_files, sources)

    # baseline tsc errors (pre-existing, filter out before loop)
    print("Running baseline tsc check ...")
    baseline_errors = run_tsc(get_project_root())
    baseline_keys   = {make_baseline_key(e) for e in baseline_errors}
    print(f"  Baseline errors: {len(baseline_errors)}")

    log = []
    current_code = origin_src
    model = get_model_cheap()
    last_errors = []

    for attempt in range(1, get_max_retries() + 2):  # +1 for escalation slot
        is_escalation = attempt > get_max_retries()

        if attempt == 1:
            prompt = WRITE_PROMPT + origin_src
            label  = f"ATTEMPT {attempt} (write)"
            task_id = "write"
        elif is_escalation:
            prompt = escalation_prompt(origin_src, cone_ctx, format_errors(last_errors))
            model  = get_model_strong()
            label  = f"ESCALATION (clean room, {get_model_strong()})"
            task_id = "escalation"
        else:
            prompt = retry_prompt(origin_src, current_code, format_errors(last_errors), attempt)
            label  = f"ATTEMPT {attempt} (retry)"
            task_id = f"retry_{attempt}"

        result = llm(prompt, model, label, ledger=ledger, 
                     agent_id="oracle_loop", task_id=task_id)
        if result.get("error"):
            print(f"  LLM call failed, aborting.")
            break

        # strip accidental markdown fences
        new_code = result["reply"]
        if "```" in new_code:
            lines = new_code.splitlines()
            new_code = "\n".join(l for l in lines if not l.startswith("```"))

        # write to temp file, run tsc
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ts", dir=origin_path.parent,
            prefix=".tmp_oracle_", delete=False
        ) as f:
            tmp_path = Path(f.name)
            f.write(new_code)

        # rename over original for tsc check
        backup = origin_path.read_bytes()
        try:
            origin_path.write_text(new_code)
            all_errors  = run_tsc(get_project_root())
            # subtract baseline — match by (file, code, message), not line number
            new_errors  = [e for e in all_errors
                           if make_baseline_key(e) not in baseline_keys]
            # Phase 1: only errors in the CHANGED FILE (definition errors first)
            # Once definition passes, call-site cascades in the cone self-resolve.
            # Only escalate to full cone errors if changed file is clean.
            changed_errors = [e for e in new_errors
                              if origin_file in e["file"] or e["file"].endswith(origin_file)]
            cone_errors    = filter_cone_errors(new_errors, cone_files, get_target_dir()) if not changed_errors else changed_errors
            deduped        = dedup_errors(changed_errors if changed_errors else cone_errors, origin_file)
        finally:
            origin_path.write_bytes(backup)  # always restore
            tmp_path.unlink(missing_ok=True)

        entry = {
            "attempt": attempt,
            "model": model,
            "prompt_tokens": result["prompt_tokens"],
            "output_tokens": result.get("completion_tokens", 0),
            "latency": result["latency"],
            "total_errors": len(new_errors),
            "cone_errors": len(cone_errors),
            "deduped_errors": len(deduped),
            "passed": len(cone_errors) == 0,
        }
        log.append(entry)

        status = "✅ PASS" if entry["passed"] else f"❌ {len(cone_errors)} errors"
        print(f"  tsc result: {status}")
        if cone_errors:
            print(f"  Top errors (deduped):\n{format_errors(deduped)}")

        if entry["passed"]:
            print(f"\n  ✅ Passed on attempt {attempt} with {model}")
            current_code = new_code
            break

        last_errors = deduped
        current_code = new_code

        if is_escalation:
            print(f"\n  ❌ Escalation also failed. Oracle loop exhausted.")
            break

    return log, current_code


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from ledger import print_cost_summary
    
    print("Building graph ...")
    symbols, sym_by_file, call_edges, call_file_edges, import_edges, sources, all_files, errors = build_graph(get_target_dir())
    print(f"  {len(all_files)} files, {len(symbols)} symbols\n")

    # Create ledger for this run
    ledger = TokenLedger()
    
    log, final_code = oracle_loop(symbols, sym_by_file, call_file_edges, import_edges, sources, ledger=ledger)

    print(f"\n{'='*60}")
    print("  ORACLE LOOP SUMMARY")
    print(f"{'='*60}")
    print(f"  {'Attempt':<12} {'Model':<30} {'P.Tokens':>9} {'Errors':>7} {'Result':>8}")
    print(f"  {'-'*68}")
    for e in log:
        result_str = "PASS" if e["passed"] else f"FAIL({e['cone_errors']})"
        print(f"  {e['attempt']:<12} {e['model']:<30} {e['prompt_tokens']:>9,} {e['cone_errors']:>7} {result_str:>8}")

    total_tokens = sum(e["prompt_tokens"] for e in log)
    passed = any(e["passed"] for e in log)
    print(f"\n  Total prompt tokens : {total_tokens:,}")
    print(f"  Outcome             : {'✅ PASSED' if passed else '❌ FAILED'}")
    if passed:
        winning = next(e for e in log if e["passed"])
        print(f"  Won on attempt      : {winning['attempt']} ({winning['model']})")
    
    # Print cost summary and save ledger
    print_cost_summary(ledger)
    ledger.save()
    print(f"  Ledger saved to: {ledger.db_path}")
