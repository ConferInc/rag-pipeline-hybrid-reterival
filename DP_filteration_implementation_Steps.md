# Dietary preferences (DP) filtration — implementation steps

This document describes how to implement **diet-aware behavior** without storing diet tags on `Recipe` nodes. The approach uses a **derived canonical field** (e.g. `dietary_compatibility`) computed from graph relationships, plus optional hard filtering.

**Graph model (reference):** `Dietary_Preferences` → `FORBIDDEN` / `ALLOWS` → `Ingredient`; `Recipe` → `USES_INGREDIENT` → `Ingredient`.

---

## 1) Define the contract

**Canonical recipe payload (add one field):**

- `dietary_compatibility: list[str]` — Diets from the **current request context** (`entities["diet"]`) for which this recipe **passes** your rules (no forbidden ingredients, etc.).
- If no diets in context: use `[]` or omit consistently (pick one and stick to it).

**Do not** put user profile diets into the recipe node; only reflect **graph-derived** compatibility for the diets you are evaluating.

---

## 2) Decide semantics (FORBIDDEN vs ALLOWS)

### Minimum viable (recommended first)

For each diet `D` and recipe `R`:

- **Incompatible** if there exists `(D)-[:FORBIDDEN]->(i)` and `(R)-[:USES_INGREDIENT]->(i)`.
- **Compatible** otherwise.

### Stricter (optional later)

If you rely on `ALLOWS`, define explicit rules (e.g. “must have at least one allowed ingredient” or “all ingredients must be allowed”). This is easy to get wrong; defer until you need it.

Document the chosen rule in code comments next to the Cypher.

---

## 3) Where to compute it (single place)

**Primary:** `rag_pipeline/orchestrator/constraint_filter.py`

- After fusion, you already have recipe UUIDs and diet-related logic (`_filter_diet_compliance`, etc.).
- Add a phase: **enrich** fused items with `payload["dietary_compatibility"]`, or compute once and attach during/after diet filter.

**Why not in retrievers:** Semantic/structural paths do not naturally carry the full diet list without duplicating Cypher; one batched Neo4j query is cleaner.

---

## 4) Implementation steps (code)

### Step A — Normalize diet names

In `constraint_filter.py` (or a small helper), normalize `entities["diet"]` to a list of non-empty strings (same pattern as elsewhere in the pipeline).

### Step B — Batch compatibility query

Add something like:

`_fetch_recipe_diet_compatibility(driver, recipe_ids: list[str], diets: list[str], database) -> dict[str, list[str]]`

**Cypher sketch (FORBIDDEN-only):**

- `UNWIND $recipe_ids AS rid`
- `MATCH (r:Recipe {id: rid})`
- `UNWIND $diets AS diet_name`
- `MATCH (dp:Dietary_Preferences)` where name matches `diet_name`
- `OPTIONAL MATCH (dp)-[:FORBIDDEN]->(i:Ingredient)<-[:USES_INGREDIENT]-(r)`
- Return `rid`, `diet_name`, `count(i) AS violations`
- In Python: for each row, if `violations == 0`, append diet to that recipe’s compatibility list.

Handle both UUID `r.id` and elementId if fused lists still use mixed IDs (reuse patterns from existing calorie/diet filter helpers).

### Step C — Attach to fused payload

For each item in `fused`:

- Resolve recipe id (reuse `_rid_for_item` / `_recipe_ids_from_fused` logic).
- `item["payload"]["dietary_compatibility"] = compat_map.get(rid, [])`

Use `dict(item)` if you mutate in place to avoid unintended side effects.

### Step D — Fusion canonical payload

In `rag_pipeline/augmentation/fusion.py`, extend `_canonical_recipe_payload` to include:

`"dietary_compatibility": payload.get("dietary_compatibility") or []`

**Important:** Fusion may strip unknown keys. You must either:

- add `dietary_compatibility` to `_canonical_recipe_payload`, **or**
- run enrichment **after** fusion and **before** any second canonicalization (or avoid stripping this field).

**Simplest:** add the key to `_canonical_recipe_payload` once enrichment exists, or enrich after filters and ensure nothing strips it afterward.

### Step E — Optional: hard filter

If the product requirement is “never show non-keto when user asked keto”:

- In `_filter_diet_compliance` or a new `_filter_diet_graph`, **drop** recipes that violate FORBIDDEN for any requested diet (you may already be close with `_fetch_diet_violating_ids`).

Keep enrichment for **explainability** even when filtering.

### Step F — API / reasons (optional)

In `api/app.py`, `_build_reasons`: if `diet` is in entities and `dietary_compatibility` intersects, add a short reason string.

### Step G — Prompt builder (optional)

In `rag_pipeline/augmentation/prompt_builder.py`, when formatting fused results for the LLM, mention compatible diets if present.

---

## 5) Files likely to change (checklist)

| Area | File | Change |
|------|------|--------|
| Compute + attach | `rag_pipeline/orchestrator/constraint_filter.py` | New helper + call from `apply_hard_constraints` or a dedicated enrich step |
| Canonical shape | `rag_pipeline/augmentation/fusion.py` | Add `dietary_compatibility` to `_canonical_recipe_payload` if fusion strips fields |
| Docs | `standardized_Payload_Implementation_Steps.md` | Document new optional field (optional) |
| UX copy | `api/app.py` | Optional reasons |
| LLM context | `rag_pipeline/augmentation/prompt_builder.py` | Optional display |

---

## 6) Testing / acceptance

- User entities: `diet: ["Vegan"]` — compatible recipes have `dietary_compatibility` containing `"Vegan"`; violating recipes are dropped or listed without it, per product choice.
- Empty `diet` — no extra DB work or return empty list consistently.
- Performance — one batched query per request, not per recipe.

---

## 7) Common pitfalls

- **Relationship name drift:** align Cypher with the actual Neo4j relationship type (`FORBIDDEN` vs variants like `FORBIDS`).
- **Diet name matching:** use the same normalization as `_fetch_diet_violating_ids` (e.g. `toLower(trim(dp.name))`).
- **Fusion stripping:** if you do not add the field to `_canonical_recipe_payload`, enrichment can disappear on the next canonical pass.

---

## 8) Product choice (before coding)

Decide explicitly:

- **Soft only:** enrichment + optional reasons (recipes may still appear if other filters allow).
- **Hard filter:** drop violating recipes in addition to enrichment.

Both can coexist: filter for compliance, enrich for UI/API clarity.
