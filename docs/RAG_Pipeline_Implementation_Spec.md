# RAG Pipeline — Implementation Spec & Architecture

## 1. Overview

The RAG pipeline powers nutrition-related search and recommendations. It uses intent extraction, multi-source retrieval (semantic, structural, Cypher), RRF fusion, deterministic constraint enforcement, and optional LLM generation. Safety (allergens, diets, nutrient caps) is enforced deterministically after fusion.

---

## 2. Flow Overview

1. **Intent & entity extraction** — Parse query, detect intent, extract entities (diets, allergens, course, nutrient thresholds).
2. **Profile enrichment** — Merge customer profile (diets, allergens, health conditions) into entities.
3. **Entity validation** — Remove incompatible filters (e.g., vegan + meat).
4. **Parallel retrieval** — Run semantic, structural, and Cypher retrieval concurrently.
5. **RRF fusion** — Combine and rank results from all sources.
6. **Constraint filter** — Apply hard constraints (allergens, diet, course, calories).
7. **Zero-results handling** — Build explanation and suggestions when no results pass.
8. **LLM generation** (optional) — Generate a response from fused context.

---

## 3. Intent & Entity Schema

### 3.1 Intent Subsets

| Subset | Purpose | Examples |
|--------|---------|----------|
| **RECIPE_INTENTS** | Recipe-returning; hard constraints applied | find_recipe, find_recipe_by_pantry, similar_recipes, recipes_for_cuisine, recipes_by_nutrient, rank_results, ingredient_in_recipes, cuisine_recipes |
| **DATA_INTENTS_NEEDING_RETRIEVAL** | Retrieval + LLM generation | find_recipe, get_nutritional_info, compare_foods, check_diet_compliance, check_substitution, recipes_for_cuisine, nutrient_in_foods, etc. |
| **CHATBOT_DATA_INTENTS** | No retrieval; fixed Cypher | show_meal_plan, meal_history, nutrition_summary |

### 3.2 Entity Schema

| Field | Type | Example | Used For |
|-------|------|---------|----------|
| diet | list[str] | ["vegan", "keto"] | Diet filter (FORBIDS) |
| exclude_ingredient | list[str] | ["peanuts", "shellfish"] | Allergen filter |
| course | str | "lunch" | Course / meal_type filter |
| cal_upper_limit | int/float | 600 | Calorie cap |
| include_ingredient | list[str] | ["chicken"] | Pantry/query filters |
| nutrient_threshold | dict | {"nutrient": "protein_g", "operator": "gt", "value": 30} | Nutrient filter |
| cuisine | str | "italian" | Cuisine filter |

### 3.3 Extraction Examples

| Query | Intent | Entities |
|-------|--------|----------|
| "vegan lunch under 600 cal" | find_recipe | diet: [vegan], course: lunch, cal_upper_limit: 600 |
| "Nut-free recipes" | find_recipe | exclude_ingredient: [nut, nuts, peanuts] |
| "High-protein breakfast" | recipes_by_nutrient | nutrient_threshold: {nutrient: protein_g, operator: gt, value: ...}, course: breakfast |
| "I have chicken, tomatoes, onion" | find_recipe_by_pantry | include_ingredient: [chicken, tomatoes, onion] |

### 3.4 Validation

- `validate_entity_compatibility()` removes conflicting `include_ingredient` pairs (e.g., vegan + meat, keto + sugar, gluten-free + gluten).
- Confidence threshold 0.7; low confidence triggers fallback intent/entities.
- If LLM omits confidence, default 0.5.

---

## 4. Hard Constraint Rules (Explicit Filters)

### 4.1 Design Principles

- Constraints are enforced **deterministically**, not as soft signals.
- The **final candidate set** must always pass hard constraints; semantic/structural results that violate them are removed after fusion.
- For recipe intents, structured retrieval (Cypher) applies WHERE clauses; post-fusion filters enforce constraints on semantic and structural results.

### 4.2 Scope

Applied only to **RECIPE_INTENTS**, after RRF fusion, in this order: **course → allergens → calories → diet**.

### 4.3 Filter Rules

| Filter | Entity Field | Logic |
|--------|--------------|-------|
| **Allergen** | exclude_ingredient | (1) Title-based: drop if title contains any exclude term (with variant expansion). (2) Graph: drop if recipe uses any ingredient whose name CONTAINS an exclude term. |
| **Diet** | diet | (1) Title-based: drop if title has meat/fish terms and diet is vegan/vegetarian. (2) Graph: drop if recipe uses ingredient linked via (Dietary_Preferences)-[:FORBIDS]->(Ingredient). |
| **Course** | course | Drop if payload meal_type exists and does not match requested course. Items without meal_type kept but tagged `unverified_course`. |
| **Calories** | cal_upper_limit | Drop if recipe Energy > cal_upper_limit via HAS_NUTRITION → OF_NUTRIENT. |

### 4.4 Zero-Results Behavior

When no results pass constraints:

- **(a) Explain** — Deterministic message describing constraints (diet, course, allergens, calories).
- **(b) Suggest** — Suggest relaxing calorie limit, course, or diet; **never allergens**.
- **(c) Clarify** — Ask: "Would you like me to suggest the closest available alternatives?"

---

## 5. Retrieval

### 5.1 Retrieval Paths

| Path | Purpose | Method |
|------|---------|--------|
| **Semantic** | Similarity search | Vector index per label; label inferred from query. Uses `label_text_rules` (e.g., Recipe: id, title, description, difficulty, cuisine_code, total_time_minutes). |
| **Structural** | Graph-based / collaborative | GraphSAGE similar nodes + 1-hop expansion. Intent-specific seed label and expansion (e.g., B2C_Customer → Recipe via SAVED, VIEWED). Skipped when customer_node_id missing or intent not in STRUCTURAL_INTENTS. |
| **Cypher** | Structured queries | Intent-specific Cypher for diets, allergens, cuisine, nutrients, etc. |

### 5.2 Structured Retrieval as Filter vs Ranking

- **Structured retrieval** — Used as a filter: Cypher applies WHERE clauses; constraint filters remove violators.
- **Structural retrieval** — Provides additional context (similar users); results still go through constraint filters.

### 5.3 Parallelism and Timeouts

- All three paths run in parallel via `asyncio.gather` and `asyncio.to_thread`.
- Each path has a timeout (`retrieval_guardrails.timeout_ms`, default 15s).
- On timeout, that path returns empty; others continue (best-effort fallback).

---

## 6. RRF Parameters & Top-N Flow

### 6.1 RRF Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| k | 60 | RRF constant: score += 1 / (k + rank). |
| max_items | 10 | Max number of items after fusion. |

### 6.2 Top-N Flow

1. Semantic retrieval → up to `top_k` per label (default 5).
2. Structural retrieval → GraphSAGE `top_k` seeds + 1-hop expansion.
3. Cypher retrieval → up to `max_rows` (50).
4. RRF fusion over semantic, structural, and Cypher.
5. Truncate to `max_items` (10).
6. Apply hard constraints (allergen, diet, course, calories).
7. Final top-N: constrained list (no further reranking).

### 6.3 Weights and Sources

- Sources are unweighted; no per-source weights in config.
- No post-fusion reranker (cross-encoder or LLM).

---

## 7. Latency Budget & Fallback Behavior

### 7.1 Per-Path Timeout

| Setting | Default | Behavior |
|---------|---------|----------|
| retrieval_guardrails.timeout_ms | 15000 | Per-path timeout (semantic, structural, Cypher). On timeout: path returns empty; others continue. |

### 7.2 Fallback Behavior

| Scenario | Behavior |
|----------|----------|
| Path timeout | Path returns empty; others run; fusion uses available results. |
| Low intent confidence | Use fallback intent/entities. |
| All retrieval empty | Zero-results message; no LLM generation if configured. |
| DB error (allergen/calorie filter) | Fail open: skip filter, keep results (log warning). |

### 7.3 Caching (Latency Reduction)

| Cache | Config | Effect |
|-------|--------|--------|
| Embedding | embedding_cache.enabled, max_size: 500 | Reduces embedding API calls. |
| Intent | intent_cache.enabled | Skips LLM when retry mode enabled. |
| Label | label_cache.enabled | Skips label inference for cached queries. |

### 7.4 Latency Budget (Targets)

- Per-path retrieval: max 15s per path.
- No formal p95/p99 SLA.
- Logged: extraction, retrieval, fusion, filter latency (ms).

---

## 8. Data Model (KG and Indices)

### 8.1 Main Node Labels

Recipe, Ingredient, Product, B2C_Customer, Cuisine, NutrientDefinition, Allergens, Dietary_Preferences, B2C_Customer_Health_Profiles, B2C_Customer_Health_Conditions, Household.

### 8.2 Main Relationship Types

HAS_PROFILE, HAS_CONDITION, IS_ALLERGIC, FOLLOWS_DIET, BELONGS_TO, VIEWED, RATED, SAVED, REJECTED, USES_INGREDIENT, BELONGS_TO_CUSINE, HAS_NUTRITION, OF_NUTRIENT, FORBIDS (for diet compliance).

### 8.3 Semantic Search Text

Per-label `label_text_rules` define which fields are concatenated for embeddings:

- **Recipe** — id, title, description, difficulty, cuisine_code, total_time_minutes.
- **Ingredient** — id, name, category.
- **Product** — id, name, brand, status.
- **B2C_Customer** — id, email, full_name, gender.
- **Cuisine** — id, code.

### 8.4 Vector Indexes

- Separate vector indexes per label (e.g., vec_recipe_semanticembedding, vec_ingredient_semanticembedding).
- Dimensions: 1536 (OpenAI text-embedding-3-small).

---

## 9. Evaluation Set Outline

### 9.1 Metrics

| Metric | Type | Description |
|--------|------|-------------|
| Relevance | LLM judge (0–1) | Does the answer match the query? |
| Faithfulness | LLM judge (0–1) | Is the answer grounded in context? |
| Safety compliance | Deterministic (0–1) | Any allergen/diet/course/calorie violations? |

### 9.2 Gold Regression Set (10–20 queries)

| # | Category | Query | Expected Intent | Safety Focus |
|---|----------|-------|-----------------|--------------|
| 1 | Recipe | Nut-free recipes | find_recipe | Allergen |
| 2 | Recipe | Shellfish-free recipes | find_recipe | Allergen |
| 3 | Recipe | Vegan dessert recipes | find_recipe | Diet |
| 4 | Recipe | vegan lunch under 600 cal | find_recipe | Diet + course + calories |
| 5 | Recipe | High-protein breakfast | recipes_by_nutrient | Nutrient |
| 6 | Recipe | Italian pasta recipes | recipes_for_cuisine | — |
| 7 | Recipe | I have chicken, tomatoes, onion | find_recipe_by_pantry | — |
| 8 | Nutrition | How much protein in quinoa? | get_nutritional_info | — |
| 9 | Diet | Is honey vegan? | check_diet_compliance | — |
| 10 | Allergen | Peanut-free dinner ideas | find_recipe | Allergen |
| 11 | Substitution | Substitute for butter (vegan) | get_substitution_suggestion | — |
| 12 | Out-of-scope | Weather today | out_of_scope | — |

### 9.3 Online Feedback

- Not yet implemented: saves, skips, substitutions.
- Future: feedback endpoint and event schema to track these actions.

---

## 10. API Endpoints (Summary)

| Endpoint | Purpose |
|----------|---------|
| POST /search/hybrid | Natural-language search via orchestrator |
| POST /recommend/feed | Personalized feed from profile (no query) |
| POST /recommend/meal-candidates | Meal-plan candidates; excludes meal history |
| POST /chat | Chatbot with intent routing |
| POST /ingredient-substitution | Ingredient substitution suggestions |

---

## 11. Configuration Reference

Key config paths in `embedding_config.yaml`:

- `embedding_cache`, `intent_cache`, `label_cache` — Caching.
- `retrieval_guardrails.timeout_ms` — Per-path timeout.
- `retrieval_guardrails.rrf.k`, `retrieval_guardrails.rrf.max_items` — RRF.
- `retrieval_guardrails.cypher.max_rows` — Cypher limit.
- `intent_semantic_labels` — Intent → semantic label.
- `intent_structural` — Intent → structural expansion config.
- `label_text_rules` — Semantic text per label.
