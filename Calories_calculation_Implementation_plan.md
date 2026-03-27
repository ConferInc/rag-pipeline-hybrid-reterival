# Calories Calculation Implementation Plan

## Goal

Implement calorie handling using graph nutrition relationships (not `Recipe.calories` property), and apply it consistently across:

- Search endpoints
- Meal-candidate / meal-plan recommendation flow
- Chatbot retrieval flow

This supports queries like:

- "Dinner recipes below 500 kcal"
- "Meal plan should match profile calorie target"

## Current Gap

- Calories are currently read from recipe payload field (`payload.calories`) when present.
- In your graph model, calories are stored via:
  - `(Recipe)-[:HAS_NUTRITION]->(NutritionValue)-[:OF_NUTRIENT]->(NutrientDefinition)`
  - where nutrient is Energy/Calories and `NutritionValue.amount` holds value.

So calorie retrieval must come from this nutrition graph path.

## Detailed Implementation Steps

1. Add a shared calorie fetch helper in API layer:
   - Input: list of recipe UUIDs
   - Query:
     - Match `Recipe -> HAS_NUTRITION -> NutritionValue -> OF_NUTRIENT -> NutrientDefinition`
     - Filter nutrient names to canonical calorie aliases:
       - `Energy`
       - `Calories`
       - `Calories/Energy`
       - `Energy (kcal)`
       - `Energy, calories`
   - Output: `{recipe_id: calories_float}`

2. Add normalization rules:
   - Accept numeric `amount` only
   - Prefer kcal units; if unit is variant of kcal, normalize to float
   - If multiple rows exist for same recipe, pick deterministic one:
     - First prefer exact `Energy`/kcal match
     - Else max confidence alias
     - Else first valid row

3. Add fail-safe behavior:
   - If calorie row missing -> return `None` for that recipe
   - Never crash endpoint due to calorie fetch failures
   - Log structured warning counters

4. Update shared result-merging functions (single source of truth):
   - `_merge_results_with_profile(...)`
   - `_merge_results(...)`
   - Stop relying on `payload.calories` as primary source
   - Resolve recipe IDs, then batch fetch calories via new helper
   - Write resolved calories to metadata (`metadata.calories`)
   - Keep payload value only as fallback if graph value absent

5. Ensure this common path is used by:
   - `/search/hybrid`
   - `/recommend/feed`
   - `/recommend/meal-candidates`
   - chatbot recipe responses (via orchestrator result mapping)

6. Standardize entity extraction for calorie constraints:
   - Parse requests like:
     - "below 500 kcal"
     - "under 400 calories"
   - Map to `entities.cal_upper_limit`

7. In hard-constraint filtering:
   - Reuse graph-based calorie lookup (already conceptually present in constraint filter)
   - Ensure it checks graph `NutritionValue.amount` by nutrient alias
   - Apply for all recipe intents, including search and chatbot routes

8. Return explicit reason when filtering removes results:
   - "No recipes found under 500 kcal after constraints"

9. Add calorie audit metadata:
   - Compute and return:
     - `daily_calorie_target`
     - `selected_total_calories`
     - `calorie_delta`
     - `calorie_tolerance`
     - `calorie_compliance`

10. Add soft calorie-aware rerank:
   - Use per-meal target (`daily_target / meals_per_day`)
   - Boost candidates closer to per-meal calories

11. Add best-set selection for meal candidates:
   - From top-K candidates, choose `meals_per_day` combination whose sum is closest to target
   - Reorder selected set to top
   - Mark compliance based on tolerance

12. Keep rollout guarded with flags until validated:
   - `ENABLE_GRAPH_CALORIE_RESOLVER=1`
   - `ENABLE_CALORIE_RERANK=1`
   - `ENABLE_CALORIE_SET_SELECTION=1`

13. Backward compatibility defaults:
   - If graph calorie missing:
     - fallback to payload calorie when available
     - otherwise keep item but mark calorie unknown
   - Do not break existing response contracts; only add optional fields

14. Performance controls:
   - Batch recipe IDs per request (single query)
   - Add short-lived in-request cache for repeated IDs
   - Add timeout and safe fallback

15. Observability:
   - Track counters:
     - `calorie_graph_lookup_success_count`
     - `calorie_graph_lookup_missing_count`
     - `calorie_filter_dropped_count`
     - `calorie_set_selection_adequate_count`
   - Add sampled logs for query latency and missing nutrient mappings

16. Unit tests:
   - Calorie extraction from NutritionValue/NutrientDefinition aliases
   - Missing calorie rows -> `None`
   - Rerank and set-selection deterministic cases

17. Integration tests:
   - Search: "dinner below 500 kcal" returns only <=500 kcal items
   - Meal candidates: selected total close to profile target
   - Chatbot calorie query obeys same threshold logic

18. Regression checks:
   - Existing retrieval still works when calorie graph data is partial
   - USDA audit path remains intact
   - No latency spikes beyond acceptable SLO

## Non-disturbance Assurance

These enhancements can be made without disturbing current working behavior if:

- You gate each enhancement behind feature flags
- You keep fallback to old behavior when graph calorie data is missing
- You roll out endpoint by endpoint with monitoring

Recommended rollout order:

1. Enable graph calorie resolver in read-only metadata mode
2. Enable threshold filtering
3. Enable meal-candidate rerank
4. Enable meal-candidate set selection
