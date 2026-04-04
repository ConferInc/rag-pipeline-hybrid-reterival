"""
Cypher Query Generator
======================
Takes the structured output from extractor_classifier (intent + entities JSON)
and generates parameterized Neo4j Cypher queries ready for session.run().

Graph Schema: v3.0
──────────────────
Key nodes used here:
  Recipe         — id, title, meal_type, difficulty, total_time_minutes,
                   prep_time_minutes, cook_time_minutes, servings, image_url,
                   percent_calories_protein, percent_calories_fat, percent_calories_carbs
  Ingredient     — name, calories (per 100g), protein_g, total_fat_g, total_carbs_g,
                   dietary_fiber_g, total_sugars_g, sodium_mg, cholesterol_mg,
                   saturated_fat_g, polyunsaturated_fat_g, monounsaturated_fat_g,
                   vitamin_a_mcg, vitamin_c_mg, vitamin_d_mcg, vitamin_e_mg,
                   vitamin_k_mcg, calcium_mg, iron_mg, magnesium_mg, potassium_mg
  Dietary_Preferences — name
  NutrientDefinition  — nutrient_name, unit_name
  NutritionValue      — amount, unit, per_amount
  Allergens           — name, code, cross_reactive_with, common_names

Key relationships used here:
  (Recipe)             -[:USES_INGREDIENT]->  (Ingredient)
  (Recipe)             -[:BELONGS_TO_CUSINE]-> (Cuisine)
  (B2C_Customer)       -[:FOLLOWS_DIET]->      (Dietary_Preferences)
  (Ingredient)         -[:SUBSTITUTE_FOR]->    (Ingredient)
  (Dietary_Preferences)-[:FORBIDS]->           (Ingredient)
  (Dietary_Preferences)-[:ALLOWS]->            (Ingredient)
  (Ingredient)         -[:HAS_NUTRITION]->     (NutritionValue)
  (NutritionValue)     -[:OF_NUTRIENT]->       (NutrientDefinition)
  (B2C_Customer)       -[:IS_ALLERGIC]->       (Allergens)

NOTE: There is NO Course node and no BELONGS_TO_COURSE relationship.
      Recipe course/type is stored as the inline property `meal_type`.
      Recipe does NOT have an inline `calories` property; calorie data lives
      in NutritionValue nodes linked via HAS_NUTRITION → OF_NUTRIENT.
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


def _build_find_recipe(entities: dict, limit: int = 50) -> tuple[str, dict]:
    """
    Dynamically build a find_recipe Cypher query.

    Supported entity keys
    ---------------------
    include_ingredient  List[str]  – ingredients that MUST be in the recipe
    exclude_ingredient  List[str]  – ingredients that MUST NOT be in the recipe
    diet                List[str]  – Dietary_Preferences names (FORBIDS/ALLOWS)
    course              str        – maps to r.meal_type (inline property)
    dish                str        – keyword match on r.title
    cal_upper_limit     int        – max calories via NutritionValue (HAS_NUTRITION)
    nutrient_threshold  dict       – {nutrient, operator, value}
    cuisine_preference  List[str]  – preferred cuisines (context); filter via BELONGS_TO_CUSINE

    Graph notes
    -----------
    * meal_type is an inline property on Recipe (no Course node).
    * Recipe has no inline `calories` property; energy is in NutritionValue
      linked via (Recipe)-[:HAS_NUTRITION]->(NutritionValue)-[:OF_NUTRIENT]->(NutrientDefinition).
    * Recipe has percent_calories_protein/fat/carbs as inline properties.
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

    # ── Diet filter — collaborative scoring via B2C_Customer FOLLOWS_DIET
    #
    # Strategy: two-tier scoring
    #   Tier 1 (Primary)  — Collaborative: recipes popular with users who share
    #                        the same diets float to the top via collab_score.
    #   Tier 2 (Secondary) — Direct profile boost applied in contextual_rerank
    #                        (cuisine, meal_type, health_goal).
    #
    # OPTIONAL MATCH means every recipe is a candidate regardless of whether
    # collaborative data exists. collab_score = 0 for uncovered recipes; they
    # still appear, just ranked lower. This replaces the previous hard-JOIN
    # pattern that produced near-zero results on a sparse graph.
    diets = entities.get("diet", [])
    if diets:
        clauses.append(
            "OPTIONAL MATCH (cu_diet:B2C_Customer)-[:FOLLOWS_DIET]->(dp_diet:Dietary_Preferences)"
        )
        where_parts.append("dp_diet.name IN $diets")
        where_parts.append("EXISTS { MATCH (cu_diet)-[:SAVED|VIEWED]->(r) }")
        params["diets"] = [str(d) for d in diets]

    # ── Cuisine preference (PRD-33 context) ───────────────────────────────────
    # Recipe must belong to at least one preferred cuisine via BELONGS_TO_CUSINE.
    cuisine_prefs = entities.get("cuisine_preference", [])
    if isinstance(cuisine_prefs, list) and cuisine_prefs:
        prefs = [str(p).strip() for p in cuisine_prefs if p]
    elif cuisine_prefs:
        prefs = [str(cuisine_prefs).strip()]
    else:
        prefs = []
    if prefs:
        where_parts.append(
            "EXISTS { MATCH (r)-[:BELONGS_TO_CUSINE]->(c_cuis:Cuisine) "
            "WHERE ANY(pref IN $cuisine_preference "
            "WHERE toLower(c_cuis.name) CONTAINS toLower(pref) "
            "OR toLower(c_cuis.code) CONTAINS toLower(pref)) }"
        )
        params["cuisine_preference"] = prefs

    # ── Course / meal_type ───────────────────────────────────────────────────
    course = entities.get("course")
    if course:
        where_parts.append("toLower(r.meal_type) = toLower($course)")
        params["course"] = course

    # ── Dish / title keyword ──────────────────────────────────────────────────
    dish = entities.get("dish")
    if dish:
        where_parts.append("toLower(r.title) CONTAINS toLower($dish)")
        params["dish"] = dish

    # ── Calorie upper limit — via NutritionValue (HAS_NUTRITION) ─────────────
    cal_limit = entities.get("cal_upper_limit")
    if cal_limit is not None:
        where_parts.append(
            "EXISTS { "
            "MATCH (r)-[:HAS_NUTRITION]->(rnv_cal:NutritionValue)"
            "-[:OF_NUTRIENT]->(nd_cal:NutrientDefinition) "
            "WHERE nd_cal.nutrient_name IN ['Energy', 'Calories/Energy'] "
            "AND rnv_cal.amount <= $cal_upper_limit }"
        )
        params["cal_upper_limit"] = cal_limit

    # ── Nutrient threshold — percent_calories_* for protein/fat/carbs ─────────
    # Extractor passes percentage values; Recipe has percent_calories_protein/fat/carbs.
    threshold = entities.get("nutrient_threshold")
    if threshold and isinstance(threshold, dict):
        nutrient = (threshold.get("nutrient") or "Protein").lower()
        op_sym = _op(threshold.get("operator", "gt"))
        value = threshold.get("value", 0)
        if "protein" in nutrient:
            prop = "r.percent_calories_protein"
        elif "fat" in nutrient:
            prop = "r.percent_calories_fat"
        elif "carb" in nutrient:
            prop = "r.percent_calories_carbs"
        else:
            # Fiber, sodium, etc.: use HAS_NUTRITION (grams/mg)
            prop = None
        if prop:
            where_parts.append(f"{prop} IS NOT NULL AND {prop} {op_sym} $threshold_value")
            params["threshold_value"] = value
        else:
            where_parts.append(
                "EXISTS { "
                "MATCH (r)-[:HAS_NUTRITION]->(rnv_nt:NutritionValue)"
                "-[:OF_NUTRIENT]->(nd_nt:NutrientDefinition) "
                "WHERE nd_nt.nutrient_name = $threshold_nutrient "
                f"AND rnv_nt.amount {op_sym} $threshold_value }}"
            )
            params["threshold_nutrient"] = threshold.get("nutrient", "Protein")
            params["threshold_value"] = value

    # ── Assemble WHERE ────────────────────────────────────────────────────────
    if where_parts:
        clauses.append("WHERE " + "\n  AND ".join(where_parts))

    # ── Aggregate collab_score, then RETURN ───────────────────────────────────
    # WITH groups by recipe so count(DISTINCT cu_diet) gives the number of
    # diet-matching users who saved/viewed each recipe. When no diets are
    # supplied (cu_diet never bound), count() returns 0 for all recipes.
    # Recipes with no collaborative data still appear — just ranked lower.
    clauses.append(
        "WITH r, count(DISTINCT cu_diet) AS collab_score"
    )
    clauses.append(
        "RETURN r.id AS id, r.title AS title, r.meal_type AS meal_type,\n"
        "       r.total_time_minutes AS total_time_minutes,\n"
        "       r.percent_calories_protein AS percent_calories_protein,\n"
        "       r.percent_calories_fat AS percent_calories_fat,\n"
        "       r.percent_calories_carbs AS percent_calories_carbs,\n"
        "       collab_score"
    )
    clauses.append("ORDER BY collab_score DESC, id")
    clauses.append(f"LIMIT {limit}")

    return "\n".join(clauses), params


def _build_find_recipe_by_pantry(entities: dict, limit: int = 50) -> tuple[str, dict]:
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
        "RETURN r.id, r.title, r.meal_type,\n"
        "       SIZE(have_ingredients) AS matching_count,\n"
        "       total_needed\n"
        "ORDER BY matching_count DESC\n"
        f"LIMIT {limit}"
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

    If `nutrient` is provided → deep traversal through NutritionValue.
    If omitted → fast inline macro properties returned directly from Ingredient.
    """
    ingredient = entities.get("ingredient", "")
    nutrient = entities.get("nutrient")
    params = {"ingredient_name": ingredient}

    if nutrient:
        cypher = (
            "MATCH (i:Ingredient)\n"
            "WHERE toLower(i.name) = toLower($ingredient_name)\n"
            "MATCH (i)-[:HAS_NUTRITION]->(inv:NutritionValue)"
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
    When a specific nutrient is requested we traverse NutritionValue
    so we can return the exact data-source and unit from the graph.
    """
    foods = entities.get("ingredients", [])
    nutrient = entities.get("nutrient")
    params = {"food_list": foods}

    if nutrient:
        cypher = (
            "MATCH (i:Ingredient)\n"
            "WHERE toLower(i.name) IN [x IN $food_list | toLower(x)]\n"
            "MATCH (i)-[:HAS_NUTRITION]->(inv:NutritionValue)"
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
    diet        List[str]  – Dietary_Preferences.name (first entry used)

    Graph relationships used:
      (Dietary_Preferences) -[:FORBIDDEN]-> (Ingredient)
      (Dietary_Preferences) -[:ALLOWS]->   (Ingredient)  # if present
    """
    ingredient = entities.get("ingredient", "")
    diets = entities.get("diet", [])
    diet_name = diets[0] if diets else ""
    params = {"ingredient_name": ingredient}

    if diet_name:
        cypher = (
            "MATCH (ing:Ingredient)\n"
            "WHERE toLower(ing.name) = toLower($ingredient_name)\n"
            "MATCH (diet:Dietary_Preferences {name: $diet_name})\n"
            "OPTIONAL MATCH (diet)-[forbidden:FORBIDDEN]->(ing)\n"
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
      (Ingredient) -[:SUBSTITUTE_FOR]->     (Ingredient)
      (Dietary_Preferences) -[:FORBIDDEN]-> (Ingredient)
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
            "MATCH (diet:Dietary_Preferences {name: $diet_context})",
            "WHERE NOT EXISTS {",
            "  MATCH (diet)-[:FORBIDDEN]->(candidate)",
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

    Graph note: Recipe has percent_calories_protein/fat/carbs as inline props.
                Calorie data lives in NutritionValue nodes linked via HAS_NUTRITION → OF_NUTRIENT.
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
            "MATCH (r)-[:HAS_NUTRITION]->(rnv:NutritionValue)"
            "-[:OF_NUTRIENT]->(nd:NutrientDefinition)\n"
            "WHERE nd.nutrient_name IN ['Energy', 'Calories/Energy']\n"
            "RETURN r.id, r.title, r.meal_type, rnv.amount AS calories, rnv.unit\n"
            "ORDER BY calories ASC"
        )
    else:
        prop, direction = criterion_map.get(
            criterion, ("r.percent_calories_protein", "DESC")
        )
        cypher = (
            "MATCH (r:Recipe)\n"
            "WHERE r.id IN $recipe_ids\n"
            f"RETURN r.id, r.title, r.meal_type, {prop} AS sort_value\n"
            f"ORDER BY sort_value {direction}"
        )

    params = {"recipe_ids": recipe_ids}
    return cypher, params


def _build_recipes_for_cuisine(entities: dict, limit: int = 50) -> tuple[str, dict]:
    """
    Find recipes from a specific cuisine.
    Recipe -[:BELONGS_TO_CUSINE]-> Cuisine.
    """
    cuisine = entities.get("cuisine", "")
    include_ing = entities.get("include_ingredient", [])
    params = {"cuisine_name": cuisine or ""}

    clauses = [
        "MATCH (r:Recipe)-[:BELONGS_TO_CUSINE]->(c:Cuisine)",
        "WHERE toLower(c.name) CONTAINS toLower($cuisine_name) OR toLower(c.code) CONTAINS toLower($cuisine_name)",
    ]

    for idx, ing in enumerate(include_ing):
        param_key = f"include_ing_{idx}"
        clauses.append(
            f"MATCH (r)-[:USES_INGREDIENT]->(i{idx}:Ingredient) WHERE toLower(i{idx}.name) CONTAINS toLower(${param_key})"
        )
        params[param_key] = ing

    clauses.append(
        "RETURN DISTINCT r.id, r.title, r.meal_type, r.total_time_minutes, "
        "r.percent_calories_protein, c.name AS cuisine_name"
    )
    clauses.append(f"LIMIT {limit}")
    cypher = "\n".join(clauses)
    return cypher, params


def _build_recipes_by_nutrient(entities: dict, limit: int = 50) -> tuple[str, dict]:
    """
    Recipes filtered by nutrient (e.g. high-protein).
    Uses Recipe percent_calories_* or nutrient_threshold.
    """
    course = entities.get("course", "")
    threshold = entities.get("nutrient_threshold", {})
    params = {}

    clauses = ["MATCH (r:Recipe)"]
    where = []
    if course:
        where.append("toLower(r.meal_type) = toLower($course)")
        params["course"] = course
    if threshold and isinstance(threshold, dict):
        nutrient = threshold.get("nutrient", "Protein")
        op = threshold.get("operator", "gt")
        val = threshold.get("value", 20)
        if "protein" in nutrient.lower():
            prop = "r.percent_calories_protein"
        elif "fat" in nutrient.lower():
            prop = "r.percent_calories_fat"
        elif "carb" in nutrient.lower():
            prop = "r.percent_calories_carbs"
        else:
            prop = "r.percent_calories_protein"
        op_sym = ">=" if op == "gt" else "<="
        where.append(f"{prop} {op_sym} $threshold_val")
        params["threshold_val"] = val
    if where:
        clauses.append("WHERE " + " AND ".join(where))
    clauses.append(
        "RETURN r.id, r.title, r.meal_type, r.total_time_minutes, "
        "r.percent_calories_protein, r.percent_calories_fat, r.percent_calories_carbs"
    )
    clauses.append("ORDER BY r.percent_calories_protein DESC")
    clauses.append(f"LIMIT {limit}")
    return "\n".join(clauses), params


def _build_nutrient_in_foods(entities: dict) -> tuple[str, dict]:
    """
    Foods/ingredients high or low in a nutrient.
    Uses Ingredient inline props (iron_mg, protein_g, etc) or NutritionValue path.
    """
    nutrient = entities.get("nutrient", "iron")
    nutrient_lower = nutrient.lower()

    prop_map = {
        "iron": ("i.iron_mg", "iron_mg"),
        "protein": ("i.protein_g", "protein_g"),
        "calcium": ("i.calcium_mg", "calcium_mg"),
        "fiber": ("i.dietary_fiber_g", "dietary_fiber_g"),
        "sodium": ("i.sodium_mg", "sodium_mg"),
        "potassium": ("i.potassium_mg", "potassium_mg"),
        "vitamin c": ("i.vitamin_c_mg", "vitamin_c_mg"),
        "vitamin d": ("i.vitamin_d_mcg", "vitamin_d_mcg"),
    }
    prop_expr = None
    prop_alias = None
    for k, (expr, alias) in prop_map.items():
        if k in nutrient_lower:
            prop_expr = expr
            prop_alias = alias
            break
    if prop_expr:
        cypher = (
            f"MATCH (i:Ingredient)\n"
            f"WHERE {prop_expr} IS NOT NULL AND {prop_expr} > 0\n"
            f"RETURN i.name AS ingredient, {prop_expr} AS amount\n"
            f"ORDER BY {prop_expr} DESC\n"
            "LIMIT 15"
        )
        return cypher, {}
    cypher = (
        "MATCH (i:Ingredient)-[:HAS_NUTRITION]->(nv:NutritionValue)"
        "-[:OF_NUTRIENT]->(nd:NutrientDefinition)\n"
        "WHERE toLower(nd.nutrient_name) CONTAINS toLower($nutrient_name)\n"
        "RETURN i.name AS ingredient, nd.nutrient_name AS nutrient, nv.amount AS amount, nv.unit AS unit\n"
        "ORDER BY nv.amount DESC\n"
        "LIMIT 15"
    )
    return cypher, {"nutrient_name": nutrient}


def _build_nutrient_category(entities: dict) -> tuple[str, dict]:
    """
    Nutrient categories / hierarchy.
    NutritionCategory has parent_category_id, category_name.
    """
    cypher = (
        "MATCH (nc:NutritionCategory)\n"
        "OPTIONAL MATCH (parent:NutritionCategory) WHERE nc.parent_category_id = parent.id\n"
        "RETURN nc.category_name, nc.subcategory_name, nc.display_name, parent.category_name AS parent_category\n"
        "ORDER BY nc.sort_order, nc.category_name\n"
        "LIMIT 50"
    )
    return cypher, {}


def _build_ingredient_in_recipes(entities: dict, limit: int = 50) -> tuple[str, dict]:
    """Recipes containing a specific ingredient."""
    ingredient = entities.get("ingredient", "")
    params = {"ingredient_pattern": ingredient}
    cypher = (
        "MATCH (r:Recipe)-[:USES_INGREDIENT]->(i:Ingredient)\n"
        "WHERE toLower(i.name) CONTAINS toLower($ingredient_name)\n"
        "RETURN r.id, r.title, r.meal_type, r.total_time_minutes, i.name AS ingredient\n"
        f"LIMIT {limit}"
    )
    params["ingredient_name"] = ingredient
    return cypher, params


def _build_ingredient_nutrients(entities: dict) -> tuple[str, dict]:
    """Alias for get_nutritional_info - same logic."""
    return _build_get_nutritional_info(entities)


def _build_product_nutrients(entities: dict) -> tuple[str, dict]:
    """
    Product nutrition from inline Product properties.
    """
    product = entities.get("product", "")
    nutrient = entities.get("nutrient", "")
    params = {"product_pattern": product}
    if nutrient and "protein" in nutrient.lower():
        cols = "p.name AS product, p.protein_g AS amount, 'g' AS unit"
    elif nutrient and "cal" in nutrient.lower():
        cols = "p.name AS product, p.calories AS amount, 'kcal' AS unit"
    else:
        cols = (
            "p.name AS product, p.calories, p.protein_g, p.total_fat_g, p.total_carbs_g, "
            "p.dietary_fiber_g, p.sodium_mg, p.iron_mg, p.calcium_mg"
        )
    cypher = (
        "MATCH (p:Product)\n"
        "WHERE toLower(p.name) CONTAINS toLower($product_name)\n"
        f"RETURN {cols}\n"
        "LIMIT 5"
    )
    params["product_name"] = product
    return cypher, params


def _build_cuisine_hierarchy(entities: dict) -> tuple[str, dict]:
    """Cuisine taxonomy via parent_cuisine_id."""
    cypher = (
        "MATCH (c:Cuisine)\n"
        "OPTIONAL MATCH (parent:Cuisine) WHERE c.parent_cuisine_id = parent.id\n"
        "RETURN c.name, c.code, c.region, parent.name AS parent_cuisine\n"
        "ORDER BY c.name\n"
        "LIMIT 50"
    )
    return cypher, {}


def _build_cross_reactive_allergens(entities: dict) -> tuple[str, dict]:
    """Allergens cross-reactive with a given allergen (e.g. latex)."""
    allergen = entities.get("allergen", "")
    params = {"allergen_name": allergen}
    cypher = (
        "MATCH (a:Allergens)\n"
        "WHERE toLower(a.name) CONTAINS toLower($allergen_name) "
        "OR toLower(a.code) CONTAINS toLower($allergen_name)\n"
        "RETURN a.name, a.code, a.cross_reactive_with, a.common_names\n"
        "LIMIT 10"
    )
    return cypher, params


def _build_noop_cypher(entities: dict) -> tuple[str, dict]:
    """No-op for semantic-only intents (similar_recipes, find_product, general_nutrition, out_of_scope)."""
    return "MATCH (r:Recipe) WHERE 1 = 0 RETURN r.title LIMIT 0", {}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_INTENT_BUILDERS = {
    "find_recipe":                  _build_find_recipe,
    "find_recipe_by_pantry":        _build_find_recipe_by_pantry,
    "similar_recipes":              _build_noop_cypher,
    "recipes_for_cuisine":          _build_recipes_for_cuisine,
    "recipes_by_nutrient":          _build_recipes_by_nutrient,
    "get_nutritional_info":         _build_get_nutritional_info,
    "nutrient_in_foods":            _build_nutrient_in_foods,
    "nutrient_category":            _build_nutrient_category,
    "compare_foods":                _build_compare_foods,
    "check_diet_compliance":        _build_check_diet_compliance,
    "check_substitution":           _build_check_substitution,
    "get_substitution_suggestion":  _build_get_substitution_suggestion,
    "similar_ingredients":          _build_noop_cypher,
    "ingredient_in_recipes":        _build_ingredient_in_recipes,
    "ingredient_nutrients":         _build_ingredient_nutrients,
    "find_product":                 _build_noop_cypher,
    "product_nutrients":            _build_product_nutrients,
    "cuisine_recipes":              _build_recipes_for_cuisine,
    "cuisine_hierarchy":            _build_cuisine_hierarchy,
    "cross_reactive_allergens":     _build_cross_reactive_allergens,
    "general_nutrition":            _build_noop_cypher,
    "out_of_scope":                 _build_noop_cypher,
}


# Recipe-returning intents whose builders accept a `limit` parameter.
_RECIPE_INTENT_BUILDERS: set[str] = {
    "find_recipe",
    "find_recipe_by_pantry",
    "recipes_for_cuisine",
    "cuisine_recipes",
    "recipes_by_nutrient",
    "ingredient_in_recipes",
}


def generate_cypher_query(
    intent: str,
    entities: dict,
    limit: int = 50,
) -> tuple[str, dict]:
    """
    Dispatch to the appropriate Cypher builder based on intent.

    Parameters
    ----------
    intent   : str   — supported intent from extractor_classifier
    entities : dict  — entity dict from extractor_classifier output
    limit    : int   — maximum rows to return for recipe-returning intents (default 50)

    Returns
    -------
    (cypher_string, params_dict)
        Ready for neo4j_session.run(cypher_string, **params_dict)
    """
    builder = _INTENT_BUILDERS.get(intent)
    if builder is None:
        raise ValueError(
            f"Unknown intent '{intent}'. "
            f"Supported intents: {sorted(_INTENT_BUILDERS.keys())}"
        )
    if intent in _RECIPE_INTENT_BUILDERS:
        return builder(entities, limit=limit)
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
            "label": "rank_results — lowest_calories (uses NutritionValue via HAS_NUTRITION)",
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
