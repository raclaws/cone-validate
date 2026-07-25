# Validation Report: Multi-Agent Coding Architecture
**Date:** 2026-07-26
**Codebase:** `raclaws/twenty-dollar` — `frontend/src` (SolidJS budget app)
**Tooling:** tree-sitter 0.26, tiktoken cl100k_base, claude-haiku-4.5
**Scope:** Surface validation only — not a prototype build

---

## Executive Summary

All load-bearing hypotheses in the architecture doc validated on real code.

The core claim — that agents can coordinate through a deterministic graph layer
instead of through each other — holds. AST diff correctly identified changed
symbols without LLM self-report. The graph found all real callers across
`.ts` and `.tsx` files. Agent B produced a correct, specific answer receiving
only the delta and its own file — 97.8% fewer tokens than a naive two-agent
full-context approach.

One structural gap surfaced: path alias resolution (`~/lib/...`) is missing,
which means some import edges in the `src/views/` tree are absent. The
call-edge layer compensated in this test, but this needs to be addressed
before the graph can be trusted as the sole routing signal.

**Recommendation:** Proceed to next validation phase — beginning with the
oracle loop (tsc / tests) before expanding to multi-caller and
concurrent-writer scenarios. The three load-bearing assumptions hold; two
caveats apply (n=1 on Agent B coverage, oracle loop unexercised).

---

## Validation Pass 1: Graph Construction + Cone Measurement

**Hypothesis:** tree-sitter can reliably extract symbol → dependency graphs
from a real TypeScript codebase, and resulting cones are significantly smaller
than full-repo context.

### Method

1. Parse all `*.ts` + `*.tsx` files in `twenty-dollar/frontend/src` with
   tree-sitter
2. Extract: function declarations, arrow functions, classes, methods
3. Build two edge types:
   - Import edges: relative `import` paths
   - Call-file edges: resolve called names to defining files
4. BFS from target symbol over both edge types → dependency cone
5. Measure cone token count vs full-repo token count (tiktoken cl100k_base)

### Results

**Graph stats:**
- Files parsed: 61 / 61 (0 errors)
- Symbols extracted: 105
- Files with call edges: 23
- Cross-file call edges resolved: 12

**Cone size sample (lib/ subset, 26 files, 24,127 total tokens):**

| Symbol | Kind | Cone files | Cone tokens | % of repo |
|---|---|---|---|---|
| `apiGet` / `apiPost` | function | 1 | 1,461 | 6.1% |
| `createQuery` | function | 2 | 711 | 2.9% |
| `computeBudget` | function | 2 | 3,683 | 15.3% |
| `createBudgetStore` | function | 5 | 4,945 | 20.5% |
| `initStore` | function | 5 | 8,091 | 33.5% |

**Averages:** 14% of repo, 2.5 files, 86% reduction
*(averages are from the full 20-symbol internal run, not the 5-row table above;
recalculating from the displayed table gives ~15.7%, 3.0 files, 84.3% — the
displayed sample skews toward larger cones)*

**Verdict:** ✅ Hypothesis confirmed. Cones are correctly computed and
meaningfully stratified. The `apiGet` family (leaf functions, no interface
change) vs `createBudgetStore` (hub, crosses module boundary) vs `initStore`
(core state, largest blast radius) maps exactly to the cheap/escalate routing
signal described in the architecture doc.

### Finding: call edges catch what imports miss

The import resolver uses naive relative path matching. `BudgetView.tsx` imports
`createBudgetStore` via `~/lib/budget-signals` (tilde alias). The import edge
was missed. The call-file edge layer found it because `createBudgetStore`
appears as a called name. Without call edges, 3 callers would have been
invisible.

---

## Validation Pass 2: Single-Agent Context Efficiency

**Hypothesis:** An LLM given cone-constrained context answers change-impact
questions correctly while consuming significantly fewer tokens than full-lib
context.

### Method

Target symbol: `createBudgetStore` (5 files, 20.5% of lib)

Question:
> "I want to add a `reset()` method to `createBudgetStore`. List every file
> that needs changes, describe the minimal change, and list any callers that
> would break immediately."

Run `claude-haiku-4.5` twice — once with cone context, once with full lib
context. Compare token cost and answer quality.

### Results

| Metric | Cone | Full |
|---|---|---|
| Context files | 5 | 26 |
| Prompt tokens (billed) | 5,104 | 24,579 |
| Token reduction | — | **79.2%** |
| Latency | ~0.1s (cached) | 6.97s |

**Cone response (summary):** Correctly identified `budget-signals.ts` as the
only required change. Gave exact interface diff (`reset: () => void`) and
correct implementation (`categoryMemos.clear()`). Correctly assessed no
immediate breakage (additive change).

**Full response (summary):** Same answer, plus mentioned `store.ts` as a
*potential* update site. Valid — `store.ts` is a downstream consumer visible
in full context. The cone correctly excludes it because it's a caller, not a
dependency.

**Quality delta:** Cone = minimal sufficient answer. Full = minimal sufficient
+ optional forward-looking note. For a cascade where Agent B reviews callers
separately, cone is correct default behavior.

**Verdict:** ✅ Hypothesis confirmed. 79% token reduction with no correctness
loss on the primary question.

---

## Validation Pass 3: Multi-Agent Delta Propagation

**Hypothesis:** Two agents can coordinate correctly through the deterministic
graph layer alone, with neither agent seeing the other's full context.

### Method

```
Agent A  → origin file only (budget-signals.ts, 690 tok)
           (cone computed but origin_src passed directly)
           task: write the reset() change
           output: modified file source

AST diff → tree-sitter parse before/after modified file
           output: {changed: ['createBudgetStore']}

Graph    → query callers of changed symbols
           output: App.tsx, BudgetView.tsx, CoverDialog.tsx

Agent B  → delta (new symbol impl) + own file (App.tsx) only
           task: does your code need updating?
```

Agent B receives zero context from Agent A beyond the changed symbol's new
implementation. Clean Room Escalation in practice.

### Results

**AST diff accuracy:**
- Changed symbols detected: `createBudgetStore` — correct, 1 symbol
- Added: none. Removed: none.
- No false positives. No self-report from Agent A needed.

**Graph caller detection:**
- Found: `App.tsx`, `BudgetView.tsx`, `CoverDialog.tsx` — all 3 real callers
- Miss: 0
- Note: callers found via call-edge layer; import edges missed them (tilde
  alias gap)

**Agent B answer quality:**

Agent B correctly identified:
- No changes required to `App.tsx`
- Exact call site (`line 134, Sidebar component`)
- Actual properties used (`budget()`, `rta()`) — not `reset()`
- Correctly classified change as additive/non-breaking
- Noted forward case: "if a future feature needs to clear memos, this is
  where you'd call `reset()`"

No hallucination. No phantom changes suggested. Answer was specific to the
actual code, not generic reasoning.

**Token efficiency:**

| Metric | Tokens |
|---|---|
| Agent A prompt | 690 |
| Agent B prompt | 3,527 |
| **Combined** | **4,217** |
| 2× full-context baseline | 189,832 |
| **Savings** | **97.8%** |

**Scope note:** The 189,832-token baseline is 2× full `src/` context (61
files). Pass 2 used `src/lib` (26 files, 24,579 tokens). Against the same
`src/lib` scope, the apples-to-apples multi-agent saving is
`(1 − 4,217 / (2 × 24,579)) × 100 = **91.4%**` — still significant, but
6.4 pp lower than the headline figure. The 97.8% is not wrong; its baseline
is the broader scope that includes the `.tsx` caller files that Pass 2 did
not scan.

**Verdict:** ✅ Hypothesis confirmed. Delta propagation worked correctly.
Agents coordinated through the graph layer without sharing context. Token
savings at 97.8% vs naive approach.

---

## Gaps Found During Validation

### 1. Path alias resolution (medium priority)

**Observed:** Tilde aliases (`~/lib/budget-signals`) are not followed by the
import edge resolver. Affects `src/views/` tree which uses `~` aliases
throughout.

**Impact:** Import edges are missing for those files. Call-edge layer
compensated in this test (all callers were still found), but this is a
coincidence of the test case — a symbol that is imported but not called would
be missed entirely.

**Fix:** Parse `tsconfig.json` `paths` config and resolve aliases before
building import edges.

### 2. TSX files initially excluded (fixed during validation)

**Observed:** Original `validate.py` only scanned `*.ts`. Real callers
(`App.tsx`, `BudgetView.tsx`, `CoverDialog.tsx`) were invisible until `.tsx`
was added to the glob.

**Impact:** The graph is incomplete for any TS project with framework
components (React, Solid, Vue with TSX). This is a common case, not an edge
case.

**Fix:** Applied during validation — `rglob("*.ts") + rglob("*.tsx")`.

### 3. Scope subscription mechanism unspecified

**Observed:** The architecture doc notes agents need to "subscribe to symbols
they're holding." In the test, subscription was implicit — the agent's file
was known a priori, and call edges were used to find it.

**In production:** Agents generate code without emitting a symbol manifest.
Two options remain open:
- Read-time tracking: log graph queries as implicit subscriptions
- Pre-task scope declaration: require a planning step before generation

Neither was validated. This is the biggest unresolved design question for a
real multi-agent system.

### 4. Correctness oracle not exercised

**Observed:** Agent B's answer was assessed manually, not by running tests or
a type-checker. The architecture doc relies on `tsc --noEmit` and the test
suite as the oracle for validating Agent A's output.

**Impact:** We validated the context management layer; we did not validate the
full loop including deterministic verification.

**Fix:** Next step — run `tsc --noEmit` on Agent A's output and feed compile
errors back as context for a retry.

---

## Summary Table

| Hypothesis | Result | Evidence |
|---|---|---|
| tree-sitter extracts TS/TSX symbols | ✅ | 61 files, 0 errors, 105 symbols |
| Cone << full repo | ✅ | avg 86% reduction |
| Cones stratify by complexity | ✅ | 6%–34% range, routing signal clear |
| Call edges catch tilde alias imports | ✅ | 12 cross-file edges; tilde alias compensated (not barrel re-export) |
| Cone context = correct LLM answers | ✅ | haiku-4.5, 79.2% token reduction |
| AST diff is deterministic | ✅ | 1 changed symbol, no false positives |
| Graph finds .tsx callers | ✅ | 3/3 callers found after .tsx glob fix |
| Delta-only handoff sufficient | ✅ | Agent B correct, no Agent A context |
| 97.8% token savings multi-agent | ✅ | 4,217 vs 189,832 tokens |
| Path alias resolution | ❌ | tilde aliases not followed |
| Correctness oracle (tsc / tests) | ⬜ | not exercised in this pass |
| Scope subscription mechanism | ⬜ | design question open |

---

## Recommendation

**Build the prototype.**

The three load-bearing assumptions in the architecture doc all hold on real
code with a real model:
1. Cones are small enough to make cheap models viable
2. AST diff removes the need for LLM self-report
3. Delta-only handoff is sufficient for correct downstream reasoning

Priority order for the prototype:
1. **tsconfig path alias resolution** — required for correctness on any real
   project, not optional
2. **tsc / test oracle loop** — the validate → feedback → retry cycle is the
   part that makes cascades reliable, needs to be exercised before claiming
   the system works end-to-end
3. **Scope subscription** — pick read-time tracking as the lazy default;
   re-evaluate when you have two concurrent writers
