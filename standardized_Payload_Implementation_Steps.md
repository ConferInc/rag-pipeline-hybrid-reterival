# Standardized Payload Implementation Steps

## Goal

Standardize recipe candidate payload shape across all retrieval paths (semantic, structural, and Cypher) so downstream fusion, filtering, and response mapping are deterministic and meal type filtering is reliable.

---

## Canonical Recipe Payload Contract

Every retrieval path must return recipe candidates using this shape before fusion:

```python
{
  "id": str,                     # Recipe UUID
  "title": str,
  "meal_type": str,              # breakfast | lunch | dinner | snack
  "total_time_minutes": int | None,
  "source": str,                 # semantic | structural | cypher
  "score_raw": float,            # retriever-native score
  "payload": {                   # optional canonical copy for downstream consumers
    "id": str,
    "title": str,
    "meal_type": str,
    "total_time_minutes": int | None
  }
}
```

Optional fields (if available):

- `cuisine: str | None`
- `calories: float | None`

## Hard rules

- `id`, `title`, `meal_type`, `source`, and `score_raw` are mandatory.
- Do not emit prefixed keys like `r.id`, `r.meal_type` in final output.
- Do not emit nested mixed payload shapes (for example `payload.payload.id`).
- If `meal_type` is missing, treat as contract violation and drop the item with a warning log.

---

## Implementation Steps (File-by-File)

## 1) Semantic retrieval standardization

**File:** `rag_pipeline/retrieval/service.py`

### Changes

- Identify the function that maps graph/vector hits into semantic result objects.
- Update mapper to return canonical keys for every recipe candidate.
- Ensure recipe `meal_type` is included in mapped output.
- Set `source = "semantic"` and map retriever score to `score_raw`.
- Ensure `id` is recipe UUID, not elementId.

### Acceptance criteria

- Every semantic recipe result contains mandatory canonical fields.
- No legacy keys (`r.meal_type`, nested payload keys) in returned semantic records.

---

## 2) Structural retrieval standardization

**File:** `rag_pipeline/retrieval/structural.py`

### Changes

- Update candidate builder in expansion output to canonical shape.
- Include `meal_type` in every recipe candidate payload.
- Set `source = "structural"` and use structural score as `score_raw`.
- Ensure `id` is UUID. If only elementId is available, resolve UUID before returning.

### Acceptance criteria

- Structural recipe candidates always contain canonical mandatory fields.
- `meal_type` is present in all returned structural recipe candidates.

---

## 3) Cypher retrieval output standardization

**Files:** `rag_pipeline/orchestrator/cypher_runner.py`, `cypher_query_generator.py`

### Changes

- Confirm all recipe-returning Cypher queries return `r.id`, `r.title`, `r.meal_type`.
- In `run_cypher_retrieval`, map rows into canonical output keys:
  - `id <- r.id`
  - `title <- r.title`
  - `meal_type <- r.meal_type`
  - `source <- "cypher"`
  - `score_raw <- deterministic default or ranking score`
- Remove final-output reliance on prefixed keys.

### Acceptance criteria

- Cypher retrieval returns canonical shape identical to other retrievers.
- Downstream code can read only canonical keys for Cypher rows.

---

## 4) Fusion input/output contract update

**File:** `rag_pipeline/augmentation/fusion.py`

### Changes

- Update fusion readers to consume only canonical input fields.
- Use canonical `id` as deduplication key.
- Preserve canonical recipe fields in fused item payload.
- Keep `sources` and `rrf_score` behavior unchanged.
- During migration only, optionally keep old payload under debug-only `raw_payload`.

### Acceptance criteria

- Fused results expose canonical payload fields consistently.
- No fusion logic depends on retriever-specific legacy key variants.

---

## 5) Hard constraint filter update (meal_type enforcement)

**File:** `rag_pipeline/orchestrator/constraint_filter.py`

### Changes

- Update course filter to read `payload["meal_type"]` only.
- Remove fallback chains (`payload.get("r.meal_type")`, etc.) after migration.
- If `course` is present and item `meal_type` is missing, drop item and log contract violation.
- Keep strict equality comparison for meal type (`meal_type == course.lower()`).

### Acceptance criteria

- For `course=lunch`, no non-lunch recipes pass the course filter.
- Missing `meal_type` no longer passes as `unverified_course`.

---

## 6) API result merge helpers simplification

**File:** `api/app.py`

### Functions to update

- `_resolve_id`
- `_resolve_id_with_lookup`
- `_merge_results`
- `_merge_results_with_profile`
- Any reason builders reading payload fields

### Changes

- Read canonical payload keys first and primarily (`id`, `title`, `meal_type`).
- Remove long legacy fallback chains once migration is complete.
- Keep temporary compatibility fallbacks only during phase-wise rollout.

### Acceptance criteria

- Response mapping logic is canonical-key based.
- Legacy key paths are removed after rollout completion.

---

## 7) Search request deterministic meal filter wiring

**File:** `api/app.py`

### Changes

Choose one:

1. Add `meal_type` directly to `SearchRequest` and map to `entities["course"]`, or
2. Map `req.filters["meal_type"]` to `entities["course"]` before orchestrate.

### Acceptance criteria

- Search endpoint can enforce meal type from backend/UI deterministically, not only from NLU/context.

---

## 8) Prompt and rerank consumers alignment

**Files:** `rag_pipeline/augmentation/prompt_builder.py`, `rag_pipeline/orchestrator/constraint_filter.py`

### Changes

- Ensure all payload reads use canonical keys.
- Remove prefixed/nested fallback reads once canonical rollout is complete.

### Acceptance criteria

- Prompt/rerank logic works with one payload shape across all retrieval sources.

---

## Rollout Plan (Safe Migration)

## Phase A: Canonical write, dual read

- Standardize retriever outputs first.
- Keep downstream fallback readers temporarily for compatibility.
- Add logging for legacy key usage.

## Phase B: Enforce contract

- Add validation at orchestrator boundary before fusion.
- Drop malformed candidates and log contract violations.

## Phase C: Remove legacy fallbacks

- Remove all fallback chains for `r.*` and nested payload variants.
- Keep canonical-only reads across fusion/filter/api layers.

---

## Validation and Testing Checklist

## Unit tests

- Semantic mapper returns canonical mandatory fields.
- Structural mapper returns canonical mandatory fields.
- Cypher runner mapper returns canonical mandatory fields.

## Integration tests

- Mixed retriever results fuse correctly with canonical shape.
- `course=lunch` never returns snack/dinner/breakfast in final ranked output.
- Search endpoint meal type input is propagated into `entities["course"]`.

## Regression tests

- Existing endpoints (`/recommend/feed`, `/recommend/meal-candidates`, `/search/hybrid`, chat retrieval path) keep expected response schema.
- Zero-results fallback still behaves correctly when strict meal filter removes all results.

---

## Logging and Observability

Add temporary metrics/log counters during migration:

- `payload_contract_violation_count` by source
- `missing_meal_type_count` by source
- `legacy_key_read_count` by file/function (temporary)

Remove `legacy_key_read_count` after Phase C cleanup.

---

## Definition of Done

- All retrievers produce canonical payload shape.
- Fusion/filter/api consume canonical payload keys only.
- Meal type filtering is deterministic and strict across all sources.
- Legacy payload key fallbacks are removed.
- Tests and migration logs confirm no contract violations in normal traffic.
