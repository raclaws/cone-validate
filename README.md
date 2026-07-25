# cone-validate

Surface validation for the multi-agent coding architecture described in
`Multi-Agent Coding System: Context Management Architecture` (2026-07-26).

**Core question:** Does tree-sitter give us useful dependency cones, and are
those cones small enough to make cheap-model cascades viable?

---

## What this is

Complete surface validation for multi-agent coding architecture on a real
TypeScript codebase (`raclaws/twenty-dollar`):

| Script | Purpose |
|--------|---------|
| `validate.py` | Graph construction + tilde alias resolution + cone measurement |
| `single_agent_test.py` | Cone context vs full-lib context for one agent |
| `multi_agent_test.py` | Two-agent delta propagation via the graph layer |
| `oracle_loop.py` | tsc feedback loop with definition-first error dedup + escalation |
| `subscription.py` | SubscriptionBus — read tracking, delta routing, flush-on-complete |
| `git_hook.py` | Post-commit trigger — incremental graph update + delta emission |
| `e2e.py` | Full loop: write → validate → notify → verify |
| `hard_oracle_test.py` | Breaking change test (validates multi-agent coordination) |

Note: `validate.py` and `single_agent_test.py` target `frontend/src/lib` (lib
only, 26 files). Other scripts target the full `frontend/src` tree (61 files,
including `.tsx` components) to find real callers.

## Verified Results (2026-07-26)

| Claim | Result |
|-------|--------|
| Cones are small (avg 86% reduction) | ✅ |
| tsc oracle loop works | ✅ Haiku passed attempt 1 (670 tok) |
| AST diff is deterministic | ✅ No self-report needed |
| Delta-only handoff sufficient | ✅ 97.8% savings vs 2× full context |
| Git hook emits deltas on commit | ✅ Incremental graph update works |
| Breaking changes route correctly | ✅ Definition OK, callers flagged for Agent B |

---

## Setup

```bash
pip install tree-sitter==0.26.0 tree-sitter-typescript tiktoken requests
```

**Environment:**
- `single_agent_test.py` reads `GATEWAY_API_KEY` from the environment. Without
  it the LLM calls are skipped and only token counts are printed.
- `multi_agent_test.py` has a hardcoded Cloudflare gateway URL and API key
  (lines 30–31). Replace with your own credentials before running.
- `single_agent_test.py` points at `llm-gateway.selfhost.irfnmzk.work`
  (self-hosted). These are two different gateways with different auth.
- All three scripts hardcode `TARGET_DIR` to `/root/repos/twenty-dollar/...`.
  Edit that constant at the top of each script before running on a different
  machine.

---

## Scripts

### validate.py

Builds a symbol + dependency graph from all `.ts`/`.tsx` files in a directory,
then measures cone sizes for a sample of symbols.

**What it extracts:**
- Symbols: function declarations, arrow functions, classes, methods
- Call edges: which names are called in each file
- Import edges: relative `import` paths between files
- Call-file edges: resolves called names to their defining files (catches
  barrel re-exports that import edges miss)

**Cone computation (BFS):**
Starting from a target symbol's file, follow both call-file edges and import
edges transitively. The resulting file set is the "dependency cone" — the
minimum context needed to reason about that symbol.

```bash
python3 validate.py
```

**Key output:**
```
Symbol                  Kind   Cone files  Cone tok  Origin tok  % of repo
createBudgetStore    function          5     4,945         551      20.5%
initStore            function          5     8,091       4,342      33.5%
apiGet               function          1     1,461       1,461       6.1%
```

**Findings:**
- 61 files parsed (`.ts` + `.tsx`), 0 parse errors
- 105 symbols extracted
- Average cone: 14% of repo, 2.5 files
- Average reduction vs full repo: **86%**
- 12 cross-file call edges found (catches barrel re-exports)

---

### single_agent_test.py

Runs the same change-impact question against:
1. Cone-constrained context (5 files for `createBudgetStore`)
2. Full lib context (26 files)

Uses `anthropic/claude-haiku-4` (`MODEL` constant, line 24) via the configured
gateway. Note: `multi_agent_test.py` uses `claude-haiku-4.5` — two different
model identifiers for two different gateway routes.

**Question asked:** "I want to add a `reset()` method to `createBudgetStore`.
What files change? What are the minimal changes? Any immediate breakage?"

```bash
python3 single_agent_test.py
```

**Findings:**

| | Cone | Full |
|---|---|---|
| Prompt tokens | 5,104 | 24,579 |
| Reduction | — | **79.2%** |
| Latency | ~0.1s* | 6.97s |
| Correctness | ✅ | ✅ |

*cone call was served from cache on second run

Quality difference: cone context gave the correct minimal answer
(`budget-signals.ts` only). Full context additionally surfaced `store.ts` as
a *potential* call site to update — valid, but optional. Cone = what must
change; full = what might want to change.

---

### multi_agent_test.py

Two-agent coordination through the deterministic layer only. Agents never
share context directly.

**Flow:**

```
1. Agent A  — origin file only (budget-signals.ts, 690 tok)
              note: cone computed but Agent A receives origin_src directly
              task: add reset() to createBudgetStore
              output: modified file source

2. AST diff — tree-sitter parse before/after
              result: {changed: ['createBudgetStore']}
              (deterministic, no self-report)

3. Graph    — query: who calls createBudgetStore?
              result: App.tsx, BudgetView.tsx, CoverDialog.tsx

4. Agent B  — receives: delta (new symbol impl) + its own file (App.tsx)
              task: does your code need updating?
              output: correct analysis, no Agent A context needed
```

```bash
python3 multi_agent_test.py
```

**Findings:**

| Metric | Value |
|---|---|
| Agent A prompt tokens | 690 |
| Agent B prompt tokens | 3,527 |
| Combined | 4,217 |
| 2× full-context baseline | 189,832 |
| **Savings** | **97.8%** |
| AST diff symbols changed | 1 (correct) |
| Callers found | 3 (all correct) |
| Agent B answer quality | Correct, specific, no hallucination |

---

## Architecture notes

### Why call-file edges matter

Import edges alone miss barrel re-exports. Example: `BudgetView.tsx` imports
`createBudgetStore` via `~/lib/budget-signals` (tilde alias), which the naive
path resolver doesn't match. It was found via the call-edge layer instead.
Always run both.

### TSX support

The original `validate.py` only scanned `*.ts`. Real callers lived in `*.tsx`
components. Adding `.tsx` to the glob is mandatory for any TS project with
React/Solid components.

### Path alias gap

Tilde aliases (`~/lib/...`) are not resolved — only relative paths (`./lib/...`).
In practice this means some import edges are missing for the `src/views/` tree.
The call-edge layer compensates, but a proper implementation needs
`tsconfig.json` path alias resolution.

### AST diff reliability

tree-sitter parses even syntactically broken output — it produces error nodes
rather than failing. The diff compares byte spans of matched symbols, so a
symbol is "changed" if its source bytes differ. This is conservative (a
whitespace-only change counts) but correct for the prototype.

### Clean Room Escalation

Agent B in the multi-agent test receives:
- The changed symbol's new implementation
- Its own file only

It does NOT receive Agent A's full context or chain-of-thought. This is
intentional — the architecture doc's "Clean Room Escalation" principle applied
to the handoff. Agent B's answer was complete and correct without it.

---

## Validated hypotheses

| Hypothesis | Result |
|---|---|
| tree-sitter extracts TS/TSX symbols reliably | ✅ 61 files, 0 errors |
| Cone << full repo tokens | ✅ avg 86% reduction |
| Cones stratify by symbol complexity | ✅ 6%–34% range visible |
| Call edges catch what imports miss | ✅ barrel re-exports found |
| Cone context = correct LLM answers | ✅ haiku-4.5 answered correctly |
| AST diff works deterministically | ✅ no self-report needed |
| Graph finds .tsx callers | ✅ after adding .tsx glob |
| Delta-only handoff sufficient for Agent B | ✅ 97.8% token savings |

## Known gaps

1. **Path alias resolution** — tilde aliases not followed in import edges
2. **Semantic boundaries** — tree-sitter is syntactic; dynamic dispatch,
   generics, and conditional types are invisible to the graph
3. **Scope declaration** — agents don't emit a symbol manifest during
   generation; subscription is inferred from query history (acceptable for
   prototype, needs explicit contract for production)
4. **Test coverage as oracle** — not validated here; correctness of Agent B's
   answer was manually assessed, not verified by a test suite
