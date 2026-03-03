# Remaining Intents (Partial / Blocked)

This document lists evaluation intents that are **not yet fully implemented** in the RAG pipeline. They fall into two categories: **Partial** (workaround possible) and **Blocked** (schema changes required).

---

## Implemented Intents (23 total)

| Intent | Category | Status |
|--------|----------|--------|
| find_recipe | Recipe discovery | ✅ Implemented |
| find_recipe_by_pantry | Recipe discovery | ✅ Implemented |
| similar_recipes | Recipe discovery | ✅ Implemented (semantic) |
| recipes_for_cuisine | Recipe discovery | ✅ Implemented |
| recipes_by_nutrient | Recipe discovery | ✅ Implemented |
| rank_results | Recipe discovery | ✅ Implemented |
| get_nutritional_info | Nutrition | ✅ Implemented |
| nutrient_in_foods | Nutrition | ✅ Implemented |
| nutrient_category | Nutrition | ✅ Implemented |
| compare_foods | Nutrition | ✅ Implemented |
| check_diet_compliance | Diet & compliance | ✅ Implemented |
| check_substitution | Substitutions | ✅ Implemented |
| get_substitution_suggestion | Substitutions | ✅ Implemented |
| similar_ingredients | Ingredients | ✅ Implemented (semantic) |
| ingredient_in_recipes | Ingredients | ✅ Implemented |
| ingredient_nutrients | Ingredients | ✅ Implemented |
| find_product | Products | ✅ Implemented (semantic) |
| product_nutrients | Products | ✅ Implemented |
| cuisine_recipes | Cuisine & taxonomy | ✅ Implemented |
| cuisine_hierarchy | Cuisine & taxonomy | ✅ Implemented |
| cross_reactive_allergens | Allergen safety | ✅ Implemented |
| general_nutrition | General | ✅ Implemented (semantic/LLM) |
| out_of_scope | General | ✅ Implemented |

---

## Partial Intents (Workaround Possible)

These intents can be partially covered with semantic search or existing graph paths, but quality may be limited without schema changes.

| Intent | Issue | Workaround |
|--------|-------|------------|
| **recipes_for_diet** | Recipe–Dietary_Preferences link exists (SUITABLE_FOR_DIET) | Add Cypher builder using `Recipe -[:SUITABLE_FOR_DIET]-> Dietary_Preferences` |
| **diet_recipes** | Same as above | Same as recipes_for_diet |
| **recipes_for_allergen_safe** | Recipe–Allergen path via Ingredient | Filter recipes that avoid ingredients linked to specified allergen |
| **recipes_avoid_allergen** | Same as above | Same as recipes_for_allergen_safe |
| **ingredient_allergens** | Ingredient–Allergen link exists (CONTAINS_ALLERGEN) | Add Cypher builder for `Ingredient -[:CONTAINS_ALLERGEN]-> Allergens` |
| **product_for_diet** | Product–Dietary_Preferences link may exist | Semantic over product description; add Product–Diet link if available |
| **category_products** | Product has category_id | Requires Category node and Product -[:IN_CATEGORY]-> Category |

---

## Blocked Intents (Schema Change Required)

| Intent | Missing Schema Element | Action Needed |
|--------|------------------------|---------------|
| **plan_meal** | Aggregation / meal-planning logic | Can use LLM + recipe retrieval; no graph-specific blocker |
| **recipe_nutrition** | NutritionValue for entity_type=recipe | Ensure Recipe -[:HAS_NUTRITION]-> NutritionValue path exists |
| **product_nutrition** | — | ✅ Implemented (Product inline props) |
| **allergen_in_ingredient** | Ingredient -[:CONTAINS_ALLERGEN]-> Allergens | Add relationship if not present |
| **ingredient_in_products** | Product -[:CONTAINS_INGREDIENT]-> Ingredient | Add relationship |
| **product_ingredients** | Same as above | Same as ingredient_in_products |
| **condition_restricts** | Condition node + restrict rules | Add HealthCondition/Condition node with FORBIDS/restricts |
| **condition_recommends** | Same | Add Condition node with RECOMMENDS |
| **condition_nutrient_limits** | Same | Add Condition node with nutrient limits |
| **recipes_for_condition** | Same | Add Condition -[:RECOMMENDS]-> Recipe or similar |

---

## Schema Prerequisites (Per User Confirmation)

- **Ingredient–Allergen**: CONTAINS_ALLERGEN or equivalent ✅
- **Recipe–Dietary_Preferences**: SUITABLE_FOR_DIET ✅
- **Dietary_Preferences–Ingredient**: FORBIDS / ALLOWS ✅
- **Product–Ingredient**: Relationship exists ✅
- **Condition / health rules**: Condition node and rules exist ✅
- **Recipe nutrition**: NutritionValue path for entity_type=recipe ✅
- **Ingredient–SUBSTITUTE_FOR**: ❌ Not present (check_substitution / get_substitution_suggestion use this; may return empty)

---

## Next Steps

1. **Partial intents**: Add Cypher builders for recipes_for_diet, diet_recipes, recipes_for_allergen_safe, ingredient_allergens using existing relationships.
2. **Blocked intents**: Confirm exact schema for Condition, Product–Ingredient, and allergen links, then add builders.
3. **SUBSTITUTE_FOR**: If not in graph, consider semantic fallback for substitution intents or populate the relationship.
