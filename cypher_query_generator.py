"""
Cypher Query Generator
======================
Takes the structured output from extractor_classifier (intent + entities JSON)
and generates parameterized Neo4j Cypher queries ready for session.run().

Graph Schema: v3.0
──────────────────
Key nodes used here:
  Recipe         — title, recipe_type, total_time_minutes,
                   percent_calories_protein, percent_calories_fat, percent_calories_carbs
  Ingredient     — name, calories (per 100g), protein_g, total_fat_g, total_carbs_g,
                   dietary_fiber_g, total_sugars_g, sodium_mg, cholesterol_mg,
                   saturated_fat_g, polyunsaturated_fat_g, monounsaturated_fat_g,
                   vitamin_a_mcg, vitamin_c_mg, vitamin_d_mcg, vitamin_e_mg,
                   vitamin_k_mcg, calcium_mg, iron_mg, magnesium_mg, potassium_mg
  DietaryPreference — name
  NutrientDefinition — nutrient_name, unit_name
  IngredientNutritionValue — amount, unit, per_amount
  RecipeNutritionValue     — amount, unit, per_amount, data_source

Key relationships used here:
  (Recipe)      -[:USES_INGREDIENT]->    (Ingredient)
  (Recipe)      -[:SUITABLE_FOR_DIET]->  (DietaryPreference)
  (Ingredient)  -[:SUBSTITUTE_FOR]->     (Ingredient)
  (DietaryPreference) -[:FORBIDS]->      (Ingredient)
  (DietaryPreference) -[:ALLOWS]->       (Ingredient)
  (Ingredient)  -[:HAS_NUTRITION_VALUE]-> (IngredientNutritionValue)
  (IngredientNutritionValue) -[:OF_NUTRIENT]-> (NutrientDefinition)
  (Recipe)      -[:HAS_NUTRITION_VALUE]-> (RecipeNutritionValue)
  (RecipeNutritionValue)     -[:OF_NUTRIENT]-> (NutrientDefinition)

NOTE: There is NO Course node and no BELONGS_TO_COURSE relationship in v3.0.
      Recipe course/type is stored as the inline property `recipe_type`.
      Recipe does NOT have an inline `calories` property; calorie data lives
      in RecipeNutritionValue nodes (nutrient_name = "Energy").
"""

import json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OPERATOR_MAP = {"gt": ">=", "lt": "<="}


def _op(operator: str) -> str:
    return _OPERATOR_MAP.get(operator, ">=")


# ---------------------------------------------------------------------------
# Individual Cypher builders — each returns (cypher_string, params_dict)
# ---------------------------------------------------------------------------


def _build_find_recipe(entities: dict) -> tuple[str, dict]:
    """
    Dynamically build a find_recipe Cypher query.

    Supported entity keys
    ---------------------
    include_ingredient  List[str]  – ingredients that MUST be in the recipe
    exclude_ingredient  List[str]  – ingredients that MUST NOT be in the recipe
    diet                List[str]  – DietaryPreference names (SUITABLE_FOR_DIET)
    course              str        – maps to r.recipe_type (inline property)
    dish                str        – keyword match on r.title
    cal_upper_limit     int        – max calories via RecipeNutritionValue
    nutrient_threshold  dict       – {nutrient, operator, value}

    Graph notes
    -----------
    * recipe_type is an inline property on Recipe (no Course node in v3.0).
    * Recipe has no inline `calories` property; energy is in RecipeNutritionValue.
    * Recipe adds %_calories_protein/fat/carbs as v3.0 inline properties.
    """
    clauses: list[str] = ["MATCH (r:Recipe)"]
    where_parts: list[str] = []
    params: dict = {}

    # ── Ingredient inclusion ────────────────────────────────────────────────
    for idx, ing in enumerate(entities.get("include_ingredient", [])):
        param_key = f"include_ing_{idx}"
        where_parts.append(
            f"EXISTS {{ MATCH (r)-[:USES_INGREDIENT]->(inc_{idx}:Ingredient) "
            f"WHERE toLower(inc_{idx}.name) CONTAINS toLower(${param_key}) }}"
        )
        params[param_key] = ing

    # ── Ingredient exclusion ─────────────────────────────────────────────────
    for idx, ing in enumerate(entities.get("exclude_ingredient", [])):
        param_key = f"exclude_ing_{idx}"
        where_parts.append(
            f"NOT EXISTS {{ MATCH (r)-[:USES_INGREDIENT]->(exc_{idx}:Ingredient) "
            f"WHERE toLower(exc_{idx}.name) CONTAINS toLower(${param_key}) }}"
        )
        params[param_key] = ing

    # ── Diet filter — each diet requires its own MATCH ─────────────────────
    diets = entities.get("diet", [])
    for idx, diet in enumerate(diets):
        alias = f"dp_{idx}"
        clauses.append(
            f"MATCH (r)-[:SUITABLE_FOR_DIET]->({alias}:DietaryPreference "
            f"{{name: $diet_{idx}}})"
        )
        params[f"diet_{idx}"] = diet

    # ── Course / recipe_type ─────────────────────────────────────────────────
    course = entities.get("course")
    if course:
        where_parts.append("toLower(r.recipe_type) = toLower($course)")
        params["course"] = course

    # ── Dish / title keyword ──────────────────────────────────────────────────
    dish = entities.get("dish")
    if dish:
        where_parts.append("toLower(r.title) CONTAINS toLower($dish)")
        params["dish"] = dish

    # ── Calorie upper limit — via RecipeNutritionValue ────────────────────────
    cal_limit = entities.get("cal_upper_limit")
    if cal_limit is not None:
        where_parts.append(
            "EXISTS { "
            "MATCH (r)-[:HAS_NUTRITION_VALUE]->(rnv_cal:RecipeNutritionValue)"
            "-[:OF_NUTRIENT]->(nd_cal:NutrientDefinition) "
            "WHERE nd_cal.nutrient_name = 'Energy' "
            "AND rnv_cal.amount <= $cal_upper_limit }"
        )
        params["cal_upper_limit"] = cal_limit

    # ── Nutrient threshold — via RecipeNutritionValue ─────────────────────────
    threshold = entities.get("nutrient_threshold")
    if threshold and isinstance(threshold, dict):
        nutrient = threshold.get("nutrient", "Protein")
        op_sym = _op(threshold.get("operator", "gt"))
        value = threshold.get("value", 0)
        where_parts.append(
            "EXISTS { "
            "MATCH (r)-[:HAS_NUTRITION_VALUE]->(rnv_nt:RecipeNutritionValue)"
            "-[:OF_NUTRIENT]->(nd_nt:NutrientDefinition) "
            "WHERE nd_nt.nutrient_name = $threshold_nutrient "
            f"AND rnv_nt.amount {op_sym} $threshold_value }}"
        )
        params["threshold_nutrient"] = nutrient
        params["threshold_value"] = value

    # ── Assemble WHERE ────────────────────────────────────────────────────────
    if where_parts:
        clauses.append("WHERE " + "\n  AND ".join(where_parts))

    clauses.append(
        "RETURN r.title, r.recipe_type, r.total_time_minutes,\n"
        "       r.percent_calories_protein, r.percent_calories_fat, r.percent_calories_carbs"
    )
    clauses.append("LIMIT 10")

    return "\n".join(clauses), params


def _build_find_recipe_by_pantry(entities: dict) -> tuple[str, dict]:
    """
    Find recipes makeable from the user's available pantry.

    Supported entity keys
    ---------------------
    pantry_ingredients  List[str]  – ingredients the user has
    """
    pantry = entities.get("pantry_ingredients", [])

    cypher = (
        "WITH $pantry_list AS my_ingredients\n"
        "MATCH (r:Recipe)-[:USES_INGREDIENT]->(i:Ingredient)\n"
        "WITH r, COLLECT(toLower(i.name)) AS recipe_ingredients, my_ingredients\n"
        "WITH r,\n"
        "     [ing IN recipe_ingredients WHERE ing IN [x IN my_ingredients | toLower(x)]] "
        "AS have_ingredients,\n"
        "     SIZE(recipe_ingredients) AS total_needed\n"
        "WHERE SIZE(have_ingredients) >= (total_needed * 0.5)\n"
        "RETURN r.title, r.recipe_type,\n"
        "       SIZE(have_ingredients) AS matching_count,\n"
        "       total_needed\n"
        "ORDER BY matching_count DESC\n"
        "LIMIT 10"
    )
    params = {"pantry_list": pantry}
    return cypher, params


def _build_get_nutritional_info(entities: dict) -> tuple[str, dict]:
    """
    Get nutritional data for an ingredient.

    Supported entity keys
    ---------------------
    ingredient  str   – Ingredient.name
    nutrient    str   – NutrientDefinition.nutrient_name (optional)

    If `nutrient` is provided → deep traversal through IngredientNutritionValue.
    If omitted → fast inline macro properties returned directly from Ingredient.
    """
    ingredient = entities.get("ingredient", "")
    nutrient = entities.get("nutrient")
    params = {"ingredient_name": ingredient}

    if nutrient:
        cypher = (
            "MATCH (i:Ingredient)\n"
            "WHERE toLower(i.name) = toLower($ingredient_name)\n"
            "MATCH (i)-[:HAS_NUTRITION_VALUE]->(inv:IngredientNutritionValue)"
            "-[:OF_NUTRIENT]->(nd:NutrientDefinition)\n"
            "WHERE toLower(nd.nutrient_name) = toLower($nutrient_name)\n"
            "RETURN i.name AS ingredient,\n"
            "       nd.nutrient_name AS nutrient,\n"
            "       inv.amount AS amount,\n"
            "       inv.unit AS unit,\n"
            "       inv.per_amount AS per_amount,\n"
            "       nd.unit_name AS standard_unit"
        )
        params["nutrient_name"] = nutrient
    else:
        # Return all 20 inline macro properties (v3.0 Ingredient schema)
        cypher = (
            "MATCH (i:Ingredient)\n"
            "WHERE toLower(i.name) = toLower($ingredient_name)\n"
            "RETURN i.name AS ingredient,\n"
            "       i.calories,\n"
            "       i.protein_g,\n"
            "       i.total_fat_g,\n"
            "       i.saturated_fat_g,\n"
            "       i.polyunsaturated_fat_g,\n"
            "       i.monounsaturated_fat_g,\n"
            "       i.cholesterol_mg,\n"
            "       i.sodium_mg,\n"
            "       i.total_carbs_g,\n"
            "       i.dietary_fiber_g,\n"
            "       i.total_sugars_g,\n"
            "       i.vitamin_a_mcg,\n"
            "       i.vitamin_c_mg,\n"
            "       i.vitamin_d_mcg,\n"
            "       i.vitamin_e_mg,\n"
            "       i.vitamin_k_mcg,\n"
            "       i.calcium_mg,\n"
            "       i.iron_mg,\n"
            "       i.magnesium_mg,\n"
            "       i.potassium_mg"
        )

    return cypher, params


def _build_compare_foods(entities: dict) -> tuple[str, dict]:
    """
    Compare nutritional values of two or more ingredients.

    Supported entity keys
    ---------------------
    ingredients  List[str]  – food names to compare (min 2)
    nutrient     str        – optional: narrow to one NutrientDefinition

    Graph note: Ingredient has 20 inline nutrition properties (per 100g).
    When a specific nutrient is requested we traverse IngredientNutritionValue
    so we can return the exact data-source and unit from the graph.
    """
    foods = entities.get("ingredients", [])
    nutrient = entities.get("nutrient")
    params = {"food_list": foods}

    if nutrient:
        cypher = (
            "MATCH (i:Ingredient)\n"
            "WHERE toLower(i.name) IN [x IN $food_list | toLower(x)]\n"
            "MATCH (i)-[:HAS_NUTRITION_VALUE]->(inv:IngredientNutritionValue)"
            "-[:OF_NUTRIENT]->(nd:NutrientDefinition)\n"
            "WHERE toLower(nd.nutrient_name) = toLower($nutrient_name)\n"
            "  AND inv.per_amount = '100g'\n"
            "RETURN i.name AS ingredient,\n"
            "       nd.nutrient_name AS nutrient,\n"
            "       inv.amount AS amount,\n"
            "       inv.unit AS unit\n"
            "ORDER BY inv.amount DESC"
        )
        params["nutrient_name"] = nutrient
    else:
        # Return all 20 inline v3.0 macro properties
        cypher = (
            "MATCH (i:Ingredient)\n"
            "WHERE toLower(i.name) IN [x IN $food_list | toLower(x)]\n"
            "RETURN i.name AS ingredient,\n"
            "       i.calories,\n"
            "       i.protein_g,\n"
            "       i.total_fat_g,\n"
            "       i.saturated_fat_g,\n"
            "       i.total_carbs_g,\n"
            "       i.dietary_fiber_g,\n"
            "       i.total_sugars_g,\n"
            "       i.sodium_mg,\n"
            "       i.cholesterol_mg,\n"
            "       i.calcium_mg,\n"
            "       i.iron_mg,\n"
            "       i.potassium_mg\n"
            "ORDER BY i.name"
        )

    return cypher, params


def _build_check_diet_compliance(entities: dict) -> tuple[str, dict]:
    """
    Check whether an ingredient is allowed on a given diet.

    Supported entity keys
    ---------------------
    ingredient  str        – Ingredient.name
    diet        List[str]  – DietaryPreference.name (first entry used)

    Graph relationships used:
      (DietaryPreference) -[:FORBIDS]-> (Ingredient)
      (DietaryPreference) -[:ALLOWS]->  (Ingredient)
    """
    ingredient = entities.get("ingredient", "")
    diets = entities.get("diet", [])
    diet_name = diets[0] if diets else ""
    params = {"ingredient_name": ingredient}

    if diet_name:
        cypher = (
            "MATCH (ing:Ingredient)\n"
            "WHERE toLower(ing.name) = toLower($ingredient_name)\n"
            "MATCH (diet:DietaryPreference {name: $diet_name})\n"
            "OPTIONAL MATCH (diet)-[forbidden:FORBIDS]->(ing)\n"
            "OPTIONAL MATCH (diet)-[allowed:ALLOWS]->(ing)\n"
            "RETURN\n"
            "  ing.name AS ingredient,\n"
            "  diet.name AS diet,\n"
            "  CASE\n"
            '    WHEN forbidden IS NOT NULL THEN "NOT_ALLOWED"\n'
            '    WHEN allowed IS NOT NULL THEN "EXPLICITLY_ALLOWED"\n'
            '    ELSE "UNKNOWN/NEUTRAL"\n'
            "  END AS compliance_status"
        )
        params["diet_name"] = diet_name
    else:
        cypher = (
            "MATCH (ing:Ingredient)\n"
            "WHERE toLower(ing.name) = toLower($ingredient_name)\n"
            "RETURN ing.name AS ingredient, 'NO_DIET_PROVIDED' AS compliance_status"
        )

    return cypher, params


def _build_check_substitution(entities: dict) -> tuple[str, dict]:
    """
    Check whether ingredient A can substitute for ingredient B.

    Supported entity keys
    ---------------------
    original_ingredient   str – the ingredient being replaced
    substitute_ingredient str – the proposed replacement

    Graph relationship:
      (Ingredient) -[:SUBSTITUTE_FOR]-> (Ingredient)
    """
    substitute = entities.get("substitute_ingredient", "")
    original = entities.get("original_ingredient", "")

    cypher = (
        "MATCH (sub:Ingredient)\n"
        "WHERE toLower(sub.name) = toLower($substitute_name)\n"
        "MATCH (orig:Ingredient)\n"
        "WHERE toLower(orig.name) = toLower($original_name)\n"
        "OPTIONAL MATCH (sub)-[r:SUBSTITUTE_FOR]->(orig)\n"
        "RETURN sub.name AS substitute,\n"
        "       orig.name AS original,\n"
        "       r IS NOT NULL AS is_direct_substitute,\n"
        "       r.notes AS notes,\n"
        "       r.ratio AS ratio"
    )
    params = {
        "substitute_name": substitute,
        "original_name": original,
    }
    return cypher, params


def _build_get_substitution_suggestion(entities: dict) -> tuple[str, dict]:
    """
    Suggest substitutes for an ingredient, optionally filtered by diet.

    Supported entity keys
    ---------------------
    ingredient  str        – the ingredient needing a replacement
    diet        List[str]  – optional diet context (first entry used)

    Graph relationships:
      (Ingredient) -[:SUBSTITUTE_FOR]-> (Ingredient)
      (DietaryPreference) -[:FORBIDS]-> (Ingredient)
    """
    ingredient = entities.get("ingredient", "")
    diets = entities.get("diet", [])
    diet_context = diets[0] if diets else ""
    params = {"ingredient_name": ingredient}

    cypher_lines = [
        "MATCH (orig:Ingredient)",
        "WHERE toLower(orig.name) = toLower($ingredient_name)",
        "MATCH (candidate:Ingredient)-[:SUBSTITUTE_FOR]->(orig)",
    ]

    if diet_context:
        cypher_lines.extend([
            "MATCH (diet:DietaryPreference {name: $diet_context})",
            "WHERE NOT EXISTS {",
            "  MATCH (diet)-[:FORBIDS]->(candidate)",
            "}",
        ])
        params["diet_context"] = diet_context

    cypher_lines.extend([
        "RETURN candidate.name AS suggested_substitute,",
        "       candidate.calories AS calories_per_100g,",
        "       candidate.protein_g AS protein_g_per_100g",
        "ORDER BY candidate.name",
        "LIMIT 10",
    ])

    return "\n".join(cypher_lines), params


def _build_rank_results(entities: dict) -> tuple[str, dict]:
    """
    Rank a set of Recipe IDs by a given criterion.

    Supported entity keys
    ---------------------
    criterion  str        – "protein_to_calorie_ratio" | "lowest_fat" | "lowest_calories"
    target     List[str]  – Recipe IDs to rank

    Graph note: Recipe v3.0 adds percent_calories_protein/fat/carbs as inline props.
                Calorie data lives in RecipeNutritionValue (no inline `calories` on Recipe).
    """
    criterion = entities.get("criterion", "protein_to_calorie_ratio")
    recipe_ids = entities.get("target", [])

    # Map criteria to expression + sort direction
    criterion_map = {
        "protein_to_calorie_ratio": ("r.percent_calories_protein", "DESC"),
        "lowest_fat":               ("r.percent_calories_fat",     "ASC"),
        "lowest_calories": None,  # handled specially below (needs NutritionValue join)
    }

    if criterion == "lowest_calories":
        cypher = (
            "MATCH (r:Recipe)\n"
            "WHERE r.id IN $recipe_ids\n"
            "MATCH (r)-[:HAS_NUTRITION_VALUE]->(rnv:RecipeNutritionValue)"
            "-[:OF_NUTRIENT]->(nd:NutrientDefinition {nutrient_name: 'Energy'})\n"
            "RETURN r.title, rnv.amount AS calories, rnv.unit\n"
            "ORDER BY calories ASC"
        )
    else:
        prop, direction = criterion_map.get(
            criterion, ("r.percent_calories_protein", "DESC")
        )
        cypher = (
            "MATCH (r:Recipe)\n"
            "WHERE r.id IN $recipe_ids\n"
            f"RETURN r.title, {prop} AS sort_value\n"
            f"ORDER BY sort_value {direction}"
        )

    params = {"recipe_ids": recipe_ids}
    return cypher, params


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_INTENT_BUILDERS = {
    "find_recipe":                  _build_find_recipe,
    "find_recipe_by_pantry":        _build_find_recipe_by_pantry,
    "get_nutritional_info":         _build_get_nutritional_info,
    "compare_foods":                _build_compare_foods,
    "check_diet_compliance":        _build_check_diet_compliance,
    "check_substitution":           _build_check_substitution,
    "get_substitution_suggestion":  _build_get_substitution_suggestion,
    "rank_results":                 _build_rank_results,
}


def generate_cypher_query(intent: str, entities: dict) -> tuple[str, dict]:
    """
    Dispatch to the appropriate Cypher builder based on intent.

    Parameters
    ----------
    intent   : str   — one of the 8 supported intents
    entities : dict  — entity dict from extractor_classifier output

    Returns
    -------
    (cypher_string, params_dict)
        Ready for  neo4j_session.run(cypher_string, **params_dict)
    """
    builder = _INTENT_BUILDERS.get(intent)
    if builder is None:
        raise ValueError(
            f"Unknown intent '{intent}'. "
            f"Supported intents: {sorted(_INTENT_BUILDERS.keys())}"
        )
    return builder(entities)


# ---------------------------------------------------------------------------
# End-to-end pipeline (uncomment to use with extractor_classifier)
# ---------------------------------------------------------------------------

# from extractor_classifier import gemini_api_call, sanity_check
#
# def process_query(free_text: str) -> dict:
#     raw_response = gemini_api_call(free_text)
#     parsed = json.loads(raw_response)
#
#     check = sanity_check(parsed)
#     if check is not True:
#         ok, reason = check
#         raise RuntimeError(f"Sanity check failed: {reason}")
#
#     intent = parsed["intent"]
#     entities = parsed["entities"]
#     cypher, params = generate_cypher_query(intent, entities)
#
#     return {"intent": intent, "entities": entities, "cypher": cypher, "params": params}


# ---------------------------------------------------------------------------
# Demo / offline test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    demos = [
        {
            "label": "find_recipe — Keto main dish with Chicken, no dairy, <600 kcal",
            'intent': 'find_recipe', 'entities': {'include_ingredient': ['olive oil']},
        },
        {
            "label": "find_recipe — High-protein breakfast with nutrient_threshold",
            "intent": "find_recipe",
            "entities": {
                "course": "breakfast",
                "diet": ["High-Protein"],
                "nutrient_threshold": {"nutrient": "Protein", "operator": "gt", "value": 25},
            },
        },
        {
            "label": "find_recipe — Dish keyword (Moussaka) + gluten-free",
            "intent": "find_recipe",
            "entities": {
                "dish": "Moussaka",
                "diet": ["Gluten-Free"],
            },
        },
        {
            "label": "find_recipe_by_pantry — Eggs, Cheese, Spinach",
            "intent": "find_recipe_by_pantry",
            "entities": {
                "pantry_ingredients": ["Eggs", "Cheese", "Spinach"],
            },
        },
        {
            "label": "get_nutritional_info — Protein in Quinoa",
            "intent": "get_nutritional_info",
            "entities": {
                "ingredient": "Quinoa",
                "nutrient": "Protein",
            },
        },
        {
            "label": "get_nutritional_info — All macros for Broccoli",
            "intent": "get_nutritional_info",
            "entities": {
                "ingredient": "Broccoli",
            },
        },
        {
            "label": "compare_foods — Carbohydrate: Rice vs Pasta",
            "intent": "compare_foods",
            "entities": {
                "ingredients": ["Rice", "Pasta"],
                "nutrient": "Carbohydrate",
            },
        },
        {
            "label": "compare_foods — All macros: Apple vs Banana vs Orange",
            "intent": "compare_foods",
            "entities": {
                "ingredients": ["Apple", "Banana", "Orange"],
            },
        },
        {
            "label": "check_diet_compliance — Honey on Vegan diet",
            "intent": "check_diet_compliance",
            "entities": {
                "ingredient": "Honey",
                "diet": ["Vegan"],
            },
        },
        {
            "label": "check_diet_compliance — No diet provided",
            "intent": "check_diet_compliance",
            "entities": {
                "ingredient": "Almond Milk",
            },
        },
        {
            "label": "check_substitution — Rice Flour for Wheat Flour",
            "intent": "check_substitution",
            "entities": {
                "original_ingredient": "Wheat Flour",
                "substitute_ingredient": "Rice Flour",
            },
        },
        {
            "label": "get_substitution_suggestion — Butter, Vegan context",
            "intent": "get_substitution_suggestion",
            "entities": {
                "ingredient": "Butter",
                "diet": ["Vegan"],
            },
        },
        {
            "label": "get_substitution_suggestion — Pasta, no diet context",
            "intent": "get_substitution_suggestion",
            "entities": {
                "ingredient": "Pasta",
            },
        },
        {
            "label": "rank_results — protein_to_calorie_ratio",
            "intent": "rank_results",
            "entities": {
                "criterion": "protein_to_calorie_ratio",
                "target": ["recipe-uuid-001", "recipe-uuid-002", "recipe-uuid-003"],
            },
        },
        {
            "label": "rank_results — lowest_calories (uses RecipeNutritionValue)",
            "intent": "rank_results",
            "entities": {
                "criterion": "lowest_calories",
                "target": ["recipe-uuid-001", "recipe-uuid-002"],
            },
        },
    ]

    separator = "=" * 72
    for demo in demos:
        print(separator)
        print(f"▸ {demo['label']}")
        print(separator)
        cypher, params = generate_cypher_query(demo["intent"], demo["entities"])
        print(cypher)
        print(f"\nParams: {json.dumps(params, indent=2)}\n")
