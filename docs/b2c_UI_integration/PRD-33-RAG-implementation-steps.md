# PRD-33: Contextual Recommendations — RAG Implementation Steps

> **PRD:** [PRD-33-contextual-recommendations.md](./PRD-33-contextual-recommendations.md)  
> **Scope:** RAG API only (`rag-pipeline-hybrid-reterival`)  
> **Assumption:** B2C backend already sends `context` to the RAG API.

---

## Final Implementation Steps

### 1. Add `context` to request schemas

**File:** `api/app.py`

Add optional `context` field to request models:

- `FeedRequest`: `context: dict[str, Any] | None = Field(None, description="RecommendationContext from B2C")`
- `SearchRequest`: same field
- `ChatProcessRequest`: same field

**Notes:** Keep optional so backward compatibility is preserved. All downstream logic must treat `None` or empty dict as "no context."

---

### 2. Extend `profile_enrichment.py` — consume context

**File:** `rag_pipeline/orchestrator/profile_enrichment.py`

In `merge_profile_into_entities`, after existing diet/allergen/condition logic:

- Read `context = profile.get("context") or {}`
- Add to `result` when present:
  - `cuisine_preference` ← `context["cuisinePreferences"]`
  - `region` ← `context["country"]`
  - `sub_region` ← `context["state"]`
  - `meal_time` ← `context["mealTimeSlot"]`
  - `season` ← `context["season"]`
  - `calorie_target` ← `context["targetCalories"]`
  - `protein_target_g` ← `context["targetProteinG"]`
  - `exclude_recipe_ids` ← `context["recentMealIds"]`
- Map `meal_time` → `course` when `course` not already set:
  - `morning` → `breakfast`
  - `afternoon` → `lunch`
  - `evening` → `dinner`
  - `late_night` → `snack`
- If `context` is absent, perform no additional changes.

---

### 3. Attach context to profile in handlers

**File:** `api/app.py`

After resolving `profile` in each handler:

- **`recommend_feed`:** If `req.context` present, set `profile["context"] = req.context` before merge
- **`search_hybrid`:** Same for the profile passed to `orchestrate`
- **`chat_process` (DATA_INTENTS_NEEDING_RETRIEVAL path):** Same for the profile passed to `orchestrate`

---

### 4. Wire feed to use context and profile merge

**File:** `api/app.py` — `recommend_feed`

- After building initial `entities`, call `merge_profile_into_entities(entities, profile)` (ensure profile has context from Step 3).
- Set `entities["course"]` from `meal_time` when `req.meal_type` is absent.
- Use `exclude_recipe_ids` from entities for post-filter (by recipe ID) when present; fallback to title-based `recent_recipes` filter.
- Ensure all context-dependent logic is null-safe.

---

### 5. Extend `build_feed_query_text` with context

**File:** `api/app.py` — `build_feed_query_text`

- Add optional `entities: dict | None = None`.
- When `entities` is provided, include in the synthetic query:
  - Cuisine from `entities.get("cuisine_preference", [])`
  - Season from `entities.get("season")`
  - Course/meal_type from `entities.get("course")` or `meal_time`
  - Region from `entities.get("region")`
- When `entities` is omitted, use existing behavior.

---

### 6. Add `augment_query_with_context` and use it

**File:** `rag_pipeline/orchestrator/orchestrator.py`

- Implement `augment_query_with_context(query, entities)` that:
  - Maps `meal_time` → meal terms: `morning`→`breakfast morning`, `afternoon`→`lunch midday`, `evening`→`dinner supper`, `late_night`→`snack`
  - Maps `season` → seasonal terms: `summer`→`fresh light cool refreshing salad`, `winter`→`warm hearty comfort soup stew`
  - Appends cuisine terms from `cuisine_preference` (first 3)
- Call it before semantic retrieval when entities are available.
- No-op when entities lack relevant fields.

---

### 7. Add `contextual_rerank` with fused-item shape handling

**File:** `rag_pipeline/orchestrator/constraint_filter.py` (or new module in `orchestrator/`)

- Implement `contextual_rerank(fused_results, entities)`:
  - Extract `recipe_id` from `item.get("key")` or `item.get("payload", {}).get("id")`
  - Extract `calories` and `cuisine` from payload (handle missing gracefully)
  - Use `item.get("rrf_score", item.get("score", 0.5))` as base score
  - Apply PRD logic: penalize recent meals (×0.3), penalize high-calorie recipes (×0.7), boost cuisine match (×1.3)
  - Update score field, preserve all other item fields
  - Return list sorted by adjusted score descending
- Call after `apply_hard_constraints` in both `recommend_feed` and `orchestrate`.
- No-op when entities have no context signals.

---

### 8. Add cuisine filter to Cypher `find_recipe`

**File:** `cypher_query_generator.py` — `_build_find_recipe`

- When `entities.get("cuisine_preference")` is non-empty, add cuisine filter via `BELONGS_TO_CUSINE` (or actual schema relationship).
- Confirm relationship name in Neo4j schema before implementing.

---

### 9. Inject context into LLM prompt

**File:** `rag_pipeline/augmentation/prompt_builder.py`

- Add `_build_context_section(entities)` that returns lines for: meal_time, season, region, cuisine_preference, calorie_target.
- In `build_augmented_prompt`, add a `[USER CONTEXT]` section when any context fields are present.
- Ensure chat flows pass `orch_result.entities` into the prompt builder.
- Omit section when no context fields exist.

---

### 10. Pass context through search and chat flows

**File:** `api/app.py`

- **`search_hybrid`:** Attach `req.context` to `customer_profile` before calling `orchestrate`.
- **`chat_process` (DATA_INTENTS_NEEDING_RETRIEVAL):** Same.
- Ensure `build_augmented_prompt` receives `orch_result.entities` so the context section is rendered.

---

## Notes

| Topic | Detail |
|-------|--------|
| **Graceful degradation** | Context is optional. Use `profile.get("context") or {}` and check for non-empty values before using. |
| **Endpoint path** | RAG exposes `POST /search/hybrid`; B2C backend is assumed to proxy to this when calling search. |
| **Fused item structure** | `contextual_rerank` must adapt to `key`, `payload.id`, `rrf_score`; missing fields should skip adjustments, not crash. |
| **Calorie handling** | PRD uses soft penalization in rerank only; no hard filter derived from `calorie_target`. |
| **Cuisine in payload** | Cypher returns cuisine; semantic/structural may not. Rerank cuisine boost applies only when cuisine is present. |
