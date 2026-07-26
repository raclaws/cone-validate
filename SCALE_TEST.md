# Scale Test: Excalidraw

**Date:** 2026-07-26  
**Codebase:** [Excalidraw](https://github.com/excalidraw/excalidraw)  
**Description:** Collaborative whiteboard React app (monorepo with core library + app)

## Codebase Metrics

| Metric | Value |
|--------|-------|
| TypeScript files | 571 |
| Symbols extracted | 1,889 |
| Files with calls | 487 |
| Cross-file call edges | 1,959 |
| Total repo tokens | 2,436,617 |

## Path Aliases

Excalidraw uses TypeScript path aliases for its monorepo packages:
- `@excalidraw/common` → `./packages/common/src/`
- `@excalidraw/excalidraw` → `./packages/excalidraw/`
- `@excalidraw/element` → `./packages/element/src/`
- `@excalidraw/math` → `./packages/math/src/`
- `@excalidraw/utils` → `./packages/utils/src/`
- Plus: `fractional-indexing`, `laser-pointer`

## Validation Results

### Parse Success Rate

| Metric | Value |
|--------|-------|
| Files parsed | 571 / 571 |
| Parse errors | 0 |
| **Success rate** | **100%** ✅ |

### Graph Build Time

| Metric | Value | Target |
|--------|-------|--------|
| Total time | 11.9s | <5s for 500 files |
| Files/second | 48 | ~100 |

**Note:** Build time exceeds target. Contributing factors:
- 1,889 symbols (vs 331 in twenty-dollar)
- 1,959 cross-file edges requiring resolution
- Path alias resolution for 7 internal packages

### Cone Reduction

| Metric | Value |
|--------|-------|
| Average cone size | 18.8% of repo |
| Average cone files | 151 files |
| **Average reduction** | **81.2%** ✅ |

## Sample Cone Analysis

### Small Cones (Leaf Functions)
```
hasBackground          →   1 file,    459 tokens (0.0% of repo)
hasStrokeColor         →   1 file,    459 tokens (0.0% of repo)
toolIsArrow            →   1 file,    459 tokens (0.0% of repo)
```

### Large Cones (Core Functions)
```
bindElementsToFramesAfterDuplication → 251 files, 765,963 tokens (31.4%)
isElementIntersectingFrame           → 251 files, 765,963 tokens (31.4%)
getElementsCompletelyInFrame         → 251 files, 765,963 tokens (31.4%)
```

These frame-related functions have large cones because they're used throughout the codebase for element manipulation.

## Comparison with twenty-dollar

| Metric | twenty-dollar | Excalidraw | Scale Factor |
|--------|---------------|------------|--------------|
| Files | 61 | 571 | 9.4x |
| Symbols | 331 | 1,889 | 5.7x |
| Parse success | 100% | 100% | ✅ |
| Build time | <1s | 11.9s | ~12x |
| Avg reduction | 86% | 81.2% | Similar |

## Edge Cases & Observations

### 1. Monorepo Structure
Excalidraw's monorepo required scanning `packages/` subdirectory specifically:
```bash
CONE_TARGET_DIR=/root/repos/excalidraw/packages
CONE_PROJECT_ROOT=/root/repos/excalidraw
```

### 2. Path Alias Resolution
All 7 internal package aliases resolved correctly. The validator properly maps:
- `@excalidraw/math` → `packages/math/src/index.ts`
- Wildcard patterns like `@excalidraw/element/*`

### 3. Dense Dependency Clusters
Frame-related functions form a dense cluster (251 files share the same cone), indicating tight coupling in the frame/element subsystem.

### 4. No Parse Failures
Despite complex TypeScript patterns (generics, decorators, JSX), tree-sitter-typescript handled everything.

## Performance Optimization Opportunities

1. **Parallel parsing** - Current sequential parse could be parallelized
2. **Incremental graph builds** - Cache symbol table between runs
3. **Lazy cone computation** - Compute on-demand rather than full matrix

## Verdict

| Criterion | Status |
|-----------|--------|
| Parse success >99% | ✅ **100%** |
| Graph builds <5s for 500 files | ⚠️ **11.9s** (needs optimization) |
| Meaningful cone reduction | ✅ **81.2% average** |
| Path alias support | ✅ **Working** |

**Overall:** The architecture scales to 500+ file codebases with 100% parse success and meaningful cone reduction. Build time optimization needed for production use on large repos.

---

## Test Commands

```bash
# Clone
git clone --depth 1 https://github.com/excalidraw/excalidraw.git /root/repos/excalidraw

# Validate
cd /root/cone-validate
CONE_TARGET_DIR=/root/repos/excalidraw/packages \
CONE_PROJECT_ROOT=/root/repos/excalidraw \
python3 validate.py
```
