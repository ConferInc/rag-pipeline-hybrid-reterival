import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

from entity_codes import CONDITION_KEYWORDS, normalize_to_allergen
from rag_pipeline.nlu.intents import VALID_INTENTS, VALID_INTENTS_WITH_B2B
from openai import OpenAI

try:
    from rag_pipeline.llm_retry import with_retry
except ImportError:
    with_retry = None

try:
    from rag_pipeline.intent_cache import get_intent_cache
except ImportError:
    get_intent_cache = None

# ── Compact zero-shot prompt — used only when keyword filter returns None ──────
# Stripped of all examples and verbose descriptions (~212 tokens vs ~1,497 before)
SYSTEM_PROMPT = """NLU. Return JSON with keys: "intent", "entities", and "confidence". No markdown, no extra text.

Required: "intent", "entities", "confidence" (0.0–1.0)
confidence: 1.0 when very sure, 0.5–0.9 when somewhat unsure, <0.5 when guessing. Lower confidence for short/ambiguous queries (e.g. "recipes" alone, vague phrasing).

INTENTS (pick one): find_recipe | find_recipe_by_pantry | similar_recipes | recipes_for_cuisine | recipes_by_nutrient | rank_results | get_nutritional_info | nutrient_in_foods | nutrient_category | compare_foods | check_diet_compliance | check_substitution | get_substitution_suggestion | similar_ingredients | ingredient_in_recipes | ingredient_nutrients | find_product | product_nutrients | cuisine_recipes | cuisine_hierarchy | cross_reactive_allergens | general_nutrition | out_of_scope | greeting | help | farewell | plan_meals | show_meal_plan | log_meal | meal_history | nutrition_summary | swap_meal | grocery_list | set_preference | dietary_advice

ENTITIES (only if present): include_ingredient[]|exclude_ingredient[]|diet[]|course|dish|cal_upper_limit|nutrient_threshold{nutrient,operator:gt|lt,value}|pantry_ingredients[]|ingredient|nutrient|ingredients[]|original_ingredient|substitute_ingredient|criterion|target[]|cuisine|allergen|product|recipe_reference|meal_type|date|date_range|meals_per_day

diet values: Vegan|Vegetarian|Gluten-Free|Keto|Paleo|Dairy-Free|Nut-Free|High-Protein|Low-Fat|Low-Carb
course values: breakfast|lunch|dinner|dessert|appetizer|main_dish|side_dish|salad|soup|snack
criterion values: protein_to_calorie_ratio|lowest_fat|lowest_calories"""

# ── B2B-specific system prompt for LLM fallback ───────────────────────────────
SYSTEM_PROMPT_B2B = """NLU for B2B vendor platform. Return JSON with keys: "intent", "entities", and "confidence". No markdown, no extra text.

Required: "intent", "entities", "confidence" (0.0–1.0)

INTENTS (pick one): b2b_products_for_condition | b2b_products_allergen_free | b2b_products_for_diet | b2b_customers_for_product | b2b_customers_with_condition | b2b_customer_recommendations | b2b_analytics | b2b_product_compliance | b2b_product_nutrition | b2b_generate_report

ENTITIES (only if present): allergens[] | health_conditions[] | diet[] | product_name | customer_name | exclude_ingredient[]

Extract user phrases as-is; normalization handles any format (spaces, dashes, mixed case).
allergens/health_conditions/diet: map to canonical codes below.
allergens: seeds, other_legumes, egg, corn, sesame, buckwheat, alpha_gal_syndrome, tree_nuts, fish, spices_herbs, peanut, insect, soy, oral_allergy_syndrome, celery, wheat_gluten_cereals, gelatin, molluscs, shellfish, milk_dairy
health_conditions: food_allergy_other, diabetics_type_2, hyperlipidemia, kidney_disease, liver_disease, non_celiac_gluten_sensitivity, type_1_diabetics, celiac_diseases, hypertension, lactose_intolerance, irritable_bowel_syndrome, gout, heart_disease, oral_allergy_syndrome, gerd
diet: kosher, sesame_free, vegan, egg_free, renal_kidney_support, carnivore, flexitarian, halal, hindu_no_beef, high_protein, hyperlipidemia, low_carb, diabetes_friendly, alpha_gal_syndrome, oral_allergy_syndrome, low_fat, vegetarian_lacto_ovo, fish_free, whole_foods, low_fodmap, corn_free, shellfish_free, paleo, non_celiac_gluten_sensitivity, ketogenic, legume_free, mediterranean, pescatarian, dairy_free, strict_gluten_free, heart_healthy, peanut_tree_nut_free, soy_free, jain_vegetarian"""

# Entity-only extraction prompt (used when rules miss allergens/diets/conditions)
ENTITY_PROMPT_B2B = """Extract entities from the B2B query for intent "{intent}". Return JSON with only "entities" key. No markdown.

Query: "{query}"

Return format: {{"entities": {{"allergens": [], "health_conditions": [], "diet": [], "product_name": "", "customer_name": ""}}}}
Extract whatever the user said (any phrasing) and map to these codes. Normalization handles spaces, dashes, mixed case.

allergens: seeds, other_legumes, egg, corn, sesame, buckwheat, alpha_gal_syndrome, tree_nuts, fish, spices_herbs, peanut, insect, soy, oral_allergy_syndrome, celery, wheat_gluten_cereals, gelatin, molluscs, shellfish, milk_dairy
health_conditions: food_allergy_other, diabetics_type_2, hyperlipidemia, kidney_disease, liver_disease, non_celiac_gluten_sensitivity, type_1_diabetics, celiac_diseases, hypertension, lactose_intolerance, irritable_bowel_syndrome, gout, heart_disease, oral_allergy_syndrome, gerd
diet: kosher, sesame_free, vegan, egg_free, renal_kidney_support, carnivore, flexitarian, halal, hindu_no_beef, high_protein, hyperlipidemia, low_carb, diabetes_friendly, alpha_gal_syndrome, oral_allergy_syndrome, low_fat, vegetarian_lacto_ovo, fish_free, whole_foods, low_fodmap, corn_free, shellfish_free, paleo, non_celiac_gluten_sensitivity, ketogenic, legume_free, mediterranean, pescatarian, dairy_free, strict_gluten_free, heart_healthy, peanut_tree_nut_free, soy_free, jain_vegetarian
product_name, customer_name: plain text if present"""


def _keyword_result(intent: str, entities: dict[str, Any]) -> dict[str, Any]:
    """Build keyword-extractor result with confidence 1.0 (rules are deterministic)."""
    return {"intent": intent, "entities": entities, "confidence": 1.0}


# ── Keyword lookup tables ──────────────────────────────────────────────────────

# Maps health condition keywords (from user queries) to diet labels.
# Condition names match B2C_Customer_Health_Conditions nodes in the graph.
# Diet labels map to existing Dietary_Preferences nodes via FORBIDS/ALLOWS edges.
_HEALTH_TO_DIET_MAP: dict[str, list[str]] = {
    # Type 1 Diabetes / Type 2 Diabetes
    "diabetic":          ["Low-Carb", "Low-Fat"],
    "diabetes":          ["Low-Carb", "Low-Fat"],
    "type 1 diabetes":   ["Low-Carb", "Low-Fat"],
    "type 2 diabetes":   ["Low-Carb", "Low-Fat"],
    "type 1":            ["Low-Carb", "Low-Fat"],
    "type 2":            ["Low-Carb", "Low-Fat"],
    # Hyperlipidemia / High Cholesterol
    "hyperlipidemia":    ["Low-Fat"],
    "high cholesterol":  ["Low-Fat"],
    "cholesterol":       ["Low-Fat"],
    # Kidney Disease (Chronic Kidney Disease)
    "kidney disease":    ["Low-Fat", "Low-Carb"],
    "chronic kidney":    ["Low-Fat", "Low-Carb"],
    "ckd":               ["Low-Fat", "Low-Carb"],
    "kidney":            ["Low-Fat", "Low-Carb"],
    # Liver Disease
    "liver disease":     ["Low-Fat"],
    "liver":             ["Low-Fat"],
    # Non-Celiac Gluten Sensitivity
    "gluten sensitivity":     ["Gluten-Free"],
    "non-celiac":             ["Gluten-Free"],
    "non celiac":             ["Gluten-Free"],
    "gluten sensitive":       ["Gluten-Free"],
    "gluten intolerant":      ["Gluten-Free"],
    "gluten intolerance":     ["Gluten-Free"],
    # Celiac Disease
    "celiac":            ["Gluten-Free"],
    "celiac disease":    ["Gluten-Free"],
    "coeliac":           ["Gluten-Free"],
    # Hypertension (High Blood Pressure)
    "hypertension":           ["Low-Carb"],
    "high blood pressure":    ["Low-Carb"],
    "blood pressure":         ["Low-Carb"],
    "hypertensive":           ["Low-Carb"],
    # Lactose Intolerance
    "lactose intolerance":    ["Dairy-Free"],
    "lactose intolerant":     ["Dairy-Free"],
    "lactose":                ["Dairy-Free"],
    # Irritable Bowel Syndrome (IBS)
    "ibs":               ["Gluten-Free"],
    "irritable bowel":   ["Gluten-Free"],
    "irritable bowel syndrome": ["Gluten-Free"],
    # Gout
    "gout":              ["Low-Fat", "Low-Carb"],
    # Heart Disease / Cardiovascular Disease
    "heart disease":          ["Low-Fat"],
    "cardiovascular":         ["Low-Fat"],
    "cardiovascular disease": ["Low-Fat"],
    "heart condition":        ["Low-Fat"],
    "cardiac":                ["Low-Fat"],
    # Oral Allergy Syndrome (OAS)
    "oral allergy":           [],   # no direct diet label mapping — falls to LLM
    "oas":                    [],
    # GERD / Acid Reflux
    "gerd":              ["Low-Fat"],
    "acid reflux":       ["Low-Fat"],
    "reflux":            ["Low-Fat"],
    "heartburn":         ["Low-Fat"],
    # Food Allergy (Other) — generic, no specific diet label
    "food allergy":      [],        # falls to LLM for specifics
}

# All health-related words that should be recognised as food-context words
# (prevents them from triggering out_of_scope)
_HEALTH_WORDS: set[str] = {
    "diabetic", "diabetes", "cholesterol", "hyperlipidemia", "kidney",
    "liver", "celiac", "coeliac", "hypertension", "hypertensive",
    "lactose", "ibs", "gout", "cardiac", "cardiovascular", "gerd",
    "reflux", "heartburn", "allergy", "allergic", "condition", "patient",
    "patients", "disease", "syndrome", "intolerance", "intolerant",
    "sensitive", "sensitivity",
} | set(CONDITION_KEYWORDS.keys())

_DIET_MAP: dict[str, str] = {
    "vegan": "Vegan",
    "vegetarian": "Vegetarian",
    "veggie": "Vegetarian",
    "veg": "Vegetarian",
    "keto": "Keto",
    "ketogenic": "Keto",
    "paleo": "Paleo",
    "gluten-free": "Gluten-Free",
    "gluten free": "Gluten-Free",
    "dairy-free": "Dairy-Free",
    "dairy free": "Dairy-Free",
    "no dairy": "Dairy-Free",
    "nut-free": "Nut-Free",
    "nut free": "Nut-Free",
    "no nuts": "Nut-Free",
    "high-protein": "High-Protein",
    "high protein": "High-Protein",
    "low-fat": "Low-Fat",
    "low fat": "Low-Fat",
    "low-carb": "Low-Carb",
    "low carb": "Low-Carb",
}

_COURSE_MAP: dict[str, str] = {
    "breakfast": "breakfast",
    "brakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "dessert": "dessert",
    "desserts": "dessert",
    "snack": "snack",
    "snacks": "snack",
    "appetizer": "appetizer",
    "starter": "appetizer",
    "main course": "main_dish",
    "main dish": "main_dish",
    "main meal": "main_dish",
    "side dish": "side_dish",
    "side": "side_dish",
    "salad": "salad",
    "soup": "soup",
}

_CUISINE_SET: set[str] = {
    "italian", "mexican", "indian", "mediterranean", "asian", "chinese",
    "japanese", "thai", "french", "greek", "american", "middle eastern",
    "spanish", "korean", "vietnamese", "turkish", "moroccan", "lebanese",
    "persian", "ethiopian", "caribbean", "brazilian", "german", "british",
    "swedish", "danish", "polish", "russian", "hungarian", "norwegian",
}

_RECIPE_TRIGGERS: set[str] = {
    "recipe", "recipes", "meal", "meals", "dish", "dishes",
    "food", "cook", "cooking", "make", "prepare", "bake", "baking",
}

_NUTRIENT_MAP: dict[str, str] = {
    "protein": "Protein",
    "fat": "Total Fat",
    "carb": "Carbohydrate",
    "carbs": "Carbohydrate",
    "carbohydrate": "Carbohydrate",
    "carbohydrates": "Carbohydrate",
    "fiber": "Dietary Fiber",
    "fibre": "Dietary Fiber",
    "sugar": "Total Sugars",
    "sugars": "Total Sugars",
    "sodium": "Sodium",
    "salt": "Sodium",
    "calories": "Energy",
    "calorie": "Energy",
    "energy": "Energy",
    "iron": "iron",
    "calcium": "calcium",
    "vitamin": "vitamin",
    "zinc": "zinc",
    "magnesium": "magnesium",
    "potassium": "potassium",
    "omega": "omega",
}

_PRODUCT_TYPES: set[str] = {
    "bread", "milk", "butter", "cheese", "yogurt", "yoghurt", "cream",
    "flour", "oil", "sauce", "pasta", "cereal", "bar", "powder",
    "supplement", "drink", "beverage", "snack", "chips", "crackers",
}

_FOOD_WORDS: set[str] = (
    _RECIPE_TRIGGERS
    | set(_NUTRIENT_MAP.keys())
    | set(_CUISINE_SET)
    | set(_DIET_MAP.keys())
    | set(_COURSE_MAP.keys())
    | _PRODUCT_TYPES
    | _HEALTH_WORDS
    | {
        "ingredient", "ingredients", "food", "foods", "nutrition", "nutritional",
        "nutrient", "nutrients", "calorie", "calories", "diet", "allergen",
        "allergy", "allergic", "substitute", "substitution", "replace",
        "cuisine", "cook", "cooking", "eat", "eating", "healthy", "health",
        "cross-reactive", "cross", "reactive", "cross-reactivity",
        "macronutrients", "macronutrient", "micronutrients", "micronutrient",
        "glycemic", "antioxidants", "probiotics", "prebiotics", "cholesterol",
        "alternatives", "alternative", "similar", "compare", "comparison",
        "vitamin", "vitamins", "mineral", "minerals", "omega",
        "hungry", "hunger", "crave", "craving", "starving",
    }
)


def _extract_health_diets(low: str) -> list[str]:
    """
    Scan *low* (lowercased query) for health-condition keywords and return
    the union of their mapped diet labels (deduped, preserving order).
    Multi-word phrases are checked before single words to avoid partial matches.
    Returns an empty list when no condition is found or when the condition maps
    to no diet label (those queries fall through to the LLM).
    """
    # Sort keys longest-first so multi-word phrases are matched before substrings
    sorted_keys = sorted(_HEALTH_TO_DIET_MAP.keys(), key=len, reverse=True)
    seen: set[str] = set()
    result: list[str] = []
    for key in sorted_keys:
        if key in low:
            for diet in _HEALTH_TO_DIET_MAP[key]:
                if diet not in seen:
                    seen.add(diet)
                    result.append(diet)
    return result


def _keyword_extract(text: str) -> dict[str, Any] | None:
    """
    Fast keyword/pattern-based intent + entity extraction.

    Returns a fully-formed {intent, entities} dict when confident,
    or None to signal that the LLM fallback should be used.

    Deliberately conservative: only returns a result when the signal
    is unambiguous. Anything complex or multi-constraint falls through
    to the LLM.
    """
    q = text.lower().strip()
    words = set(re.findall(r"[a-z][\w\-]*", q))

    # ── Calorie upper limit: "below/under X kcal" / "recipes under 120 calories" ─
    # Must run before generic numeric fallthrough so we extract cal_upper_limit.
    cal_limit_match = re.search(
        r"\b(?:below|under|less\s+than|max|maximum|at\s+most)\s+(\d+)\s*(?:kcal|cal|calories?)\b",
        q,
    )
    if cal_limit_match and (words & _RECIPE_TRIGGERS or any(w in q for w in ["recipe", "recipes", "meal", "meals", "dish", "food"])):
        cal_val = int(cal_limit_match.group(1))
        entities: dict[str, Any] = {"cal_upper_limit": cal_val}
        course = _extract_course(q)
        if course:
            entities["course"] = course
        diets = _extract_diets(q)
        if diets:
            entities["diet"] = diets
        exclude_ingredients = _extract_exclude_ingredients(q)
        if exclude_ingredients:
            entities["exclude_ingredient"] = exclude_ingredients
        # Also extract nutrient threshold for "high protein under 100 kcal"
        nutrient_recipe = re.search(
            r"(?:high|low|rich\s+in)\s*[-\s]?([a-z]+)\s+(?:recipe|recipes|meal|meals|dish|dishes|food|foods|dinner|lunch|breakfast|snack)?",
            q,
        )
        if nutrient_recipe:
            nword = nutrient_recipe.group(1).strip()
            nutrient = _NUTRIENT_MAP.get(nword)
            is_low = "low" in (nutrient_recipe.group(0).split()[0] if nutrient_recipe else "")
            op = "lt" if is_low else "gt"
            nutrient_name_for_graph = nutrient or "Protein"
            if "protein" in (nword or ""):
                default_val = 10 if is_low else 25
            elif "fat" in (nword or ""):
                default_val = 30 if is_low else 50
            elif "carb" in (nword or ""):
                default_val = 20 if is_low else 40
            elif "fiber" in (nword or ""):
                default_val = 2 if is_low else 5
            else:
                default_val = 20 if is_low else 25
            entities["nutrient_threshold"] = {
                "nutrient": nutrient_name_for_graph,
                "operator": op,
                "value": default_val,
            }
        return _keyword_result("find_recipe", entities)

    # ── Queries with other numeric thresholds → fall through to LLM ───────────
    if re.search(r"\b\d+\s*(?:cal|kcal|g|mg|calories?|grams?)\b", q):
        return None

    # ── similar_recipes / similar_ingredients — checked before out_of_scope ───
    # "something like biryani" has no food words but is clearly a recipe query.
    sim_recipe_early = re.search(
        r"(?:recipes?\s+like\s+|similar\s+(?:recipes?\s+)?to\s+|dishes?\s+like\s+|something\s+like\s+)(.+?)(?:\?|$)",
        q,
    )
    if sim_recipe_early:
        ref = sim_recipe_early.group(1).strip()
        return _keyword_result("similar_recipes", {"recipe_reference": ref})

    sim_ingr_early = re.search(
        r"(?:ingredients?\s+like\s+|similar\s+ingredients?\s+to\s+)(.+?)(?:\?|$)", q
    )
    if sim_ingr_early:
        ingr = sim_ingr_early.group(1).strip()
        return _keyword_result("similar_ingredients", {"ingredient": ingr})

    # ── out_of_scope: no food/nutrition words at all ───────────────────────────
    if not words & _FOOD_WORDS:
        return _keyword_result("out_of_scope", {})

    # ── cross_reactive_allergens ───────────────────────────────────────────────
    if "cross-reactive" in q or "cross reactive" in q or "cross-reactivity" in q:
        allergen = _extract_after(q, ["cross-reactive with", "cross reactive with",
                                      "cross-reactivity with", "reactive with"])
        entities = {}
        if allergen:
            entities["allergen"] = allergen
        return _keyword_result("cross_reactive_allergens", entities)

    # ── rank_results ───────────────────────────────────────────────────────────
    if re.search(r"\b(rank|sort|order)\s+by\b", q):
        criterion = None
        if any(w in q for w in ["protein", "protein-to-calorie", "protein to calorie"]):
            criterion = "protein_to_calorie_ratio"
        elif any(w in q for w in ["lowest fat", "low fat", "least fat"]):
            criterion = "lowest_fat"
        elif any(w in q for w in ["lowest calorie", "lowest calories", "fewest calories", "least calories"]):
            criterion = "lowest_calories"
        entities = {}
        if criterion:
            entities["criterion"] = criterion
        return _keyword_result("rank_results", entities)

    # ── cuisine_hierarchy ──────────────────────────────────────────────────────
    if re.search(r"\b(types?|subtypes?|kinds?|categories|taxonomy|hierarchy)\s+of\b.*(cuisine|food)", q) \
            or "cuisine taxonomy" in q or "cuisine hierarchy" in q:
        cuisine = _extract_cuisine(q)
        entities = {}
        if cuisine:
            entities["cuisine"] = cuisine
        return _keyword_result("cuisine_hierarchy", entities)

    # ── compare_foods ──────────────────────────────────────────────────────────
    # "X vs Y" — stop before nutrition/protein/carb/? to avoid capturing trailing words
    vs_match = re.search(r"([a-z][\w\s]{1,15}?)\s+vs\.?\s+([a-z][\w]{1,15})(?:\s+nutrition|\s+protein|\s+carb|\?|$)", q)
    compare_match = re.search(r"compare\s+(.+?)\s+and\s+(.+?)(?:\s+nutrition|\s+protein|\s+carb|$)", q)
    which_match = re.search(r"which\s+has\s+more\s+(\w+)[,\s]+(.+?)\s+or\s+(.+?)(?:\?|$)", q)
    if vs_match or compare_match or which_match:
        foods: list[str] = []
        nutrient = None
        if vs_match:
            foods = [vs_match.group(1).strip(), vs_match.group(2).strip()]
        elif compare_match:
            foods = [compare_match.group(1).strip(), compare_match.group(2).strip()]
        elif which_match:
            nutrient_word = which_match.group(1).strip()
            nutrient = _NUTRIENT_MAP.get(nutrient_word)
            foods = [which_match.group(2).strip(), which_match.group(3).strip()]
        if len(foods) >= 2:
            entities = {"ingredients": foods}
            if not nutrient:
                for nw, nv in _NUTRIENT_MAP.items():
                    if nw in q:
                        nutrient = nv
                        break
            if nutrient:
                entities["nutrient"] = nutrient
            return _keyword_result("compare_foods", entities)

    # ── check_substitution: "can I substitute X with Y" / "use X instead of Y" ─
    sub_with = re.search(
        r"(?:substitute|replace|swap)\s+(.+?)\s+(?:with|for)\s+(.+?)(?:\?|$|\s+in\b)", q
    )
    # "use X instead of Y" → X is the substitute, Y is the original
    instead_of = re.search(r"(?:use\s+)?(.+?)\s+instead\s+of\s+(.+?)(?:\?|$)", q)
    if sub_with or instead_of:
        if sub_with:
            orig = sub_with.group(1).strip()
            sub = sub_with.group(2).strip()
        else:
            sub = instead_of.group(1).strip()
            orig = instead_of.group(2).strip()
            # Strip leading "use " if present
            sub = re.sub(r"^use\s+", "", sub).strip()
        return _keyword_result("check_substitution", {"original_ingredient": orig, "substitute_ingredient": sub})

    # ── get_substitution_suggestion: "alternatives to X" / "what can I replace X with" ─
    alt_match = re.search(
        r"(?:alternatives?\s+(?:to|for)\s+|substitute\s+for\s+|what\s+can\s+i\s+(?:use|replace)\s+(?:instead\s+of\s+)?)(.+?)(?:\?|$|\s+in\b|\s+with\b)",
        q,
    )
    replace_with = re.search(r"(?:replace|swap)\s+(.+?)\s+with(?:\s+what|\?|$)", q)
    if alt_match or replace_with:
        ingredient = (replace_with.group(1) if replace_with else alt_match.group(1)).strip()
        entities: dict[str, Any] = {"ingredient": ingredient}
        diets = _extract_diets(q)
        if diets:
            entities["diet"] = diets
        return _keyword_result("get_substitution_suggestion", entities)

    # ── check_diet_compliance: "is X vegan/keto/..." / "can vegans eat X" ──────
    compliance_match = re.search(
        r"(?:is\s+(.+?)\s+(?:vegan|vegetarian|keto|paleo|gluten.free|dairy.free|halal|kosher|healthy)(?:\?|$))"
        r"|(?:can\s+(?:a\s+)?(?:vegan|vegetarian|keto|paleo)\s+(?:person\s+)?eat\s+(.+?)(?:\?|$))"
        r"|(?:(?:vegan|vegetarian|keto|paleo|gluten.free|dairy.free).friendly\s+(.+?)(?:\?|$))",
        q,
    )
    if compliance_match:
        ingredient = (
            compliance_match.group(1)
            or compliance_match.group(2)
            or compliance_match.group(3)
            or ""
        ).strip()
        diets = _extract_diets(q)
        entities = {}
        if ingredient:
            entities["ingredient"] = ingredient
        if diets:
            entities["diet"] = diets
        return _keyword_result("check_diet_compliance", entities)

    # ── find_recipe_by_pantry: "I have X,Y,Z what can I cook/make" ────────────
    pantry_match = re.search(
        r"(?:i\s+have\s+|using\s+|with\s+)(.+?)(?:\s+what\s+can\s+i\s+(?:cook|make|prepare|bake)|\s+recipe|\?|$)",
        q,
    )
    have_and_cook = re.search(
        r"what\s+can\s+i\s+(?:cook|make|prepare|bake)\s+with\s+(.+?)(?:\?|$)", q
    )
    if pantry_match or have_and_cook:
        raw = (pantry_match.group(1) if pantry_match else have_and_cook.group(1)).strip()
        # Split on commas, "and", "or"
        items = [i.strip() for i in re.split(r",\s*|\s+and\s+|\s+or\s+", raw) if i.strip()]
        if len(items) >= 2:
            entities = {"pantry_ingredients": items}
            excludes = _extract_exclude_ingredients(q)
            if excludes:
                entities["exclude_ingredient"] = excludes
            return _keyword_result("find_recipe_by_pantry", entities)

    # ── similar_ingredients: "alternatives to X" (ingredient context) ────────
    # Note: "recipes like X" / "something like X" already handled before out_of_scope check.
    sim_ingr = re.search(
        r"(?:alternatives?\s+to\s+)(.+?)(?:\?|$)", q
    )
    if sim_ingr:
        ingr = sim_ingr.group(1).strip()
        return _keyword_result("similar_ingredients", {"ingredient": ingr})

    # ── nutrient_category: "types of vitamins/minerals/macronutrients" ─────────
    if re.search(r"\b(macronutrients?|micronutrients?|nutrient\s+categor|types?\s+of\s+(?:vitamins?|minerals?|nutrients?)|what\s+are\s+(?:vitamins?|minerals?|macronutrients?|micronutrients?))\b", q):
        return _keyword_result("nutrient_category", {})

    # ── nutrient_in_foods: "foods high/rich in X" / "sources of X" ────────────
    nutrient_in_foods = re.search(
        r"(?:foods?\s+(?:high|rich|low)\s+in\s+|foods?\s+with\s+(?:high|low)\s+|sources?\s+of\s+|high\s+in\s+|rich\s+in\s+)([a-z][\w\s]{1,20}?)(?:\?|$|\s+food|\s+source)",
        q,
    )
    if nutrient_in_foods:
        nword = nutrient_in_foods.group(1).strip()
        nutrient = _NUTRIENT_MAP.get(nword, nword)
        return _keyword_result("nutrient_in_foods", {"nutrient": nutrient})

    # ── get_nutritional_info: "how much X in Y" / "calories in X" / "nutrition of X" ─
    how_much = re.search(
        r"how\s+(?:much|many)\s+([a-z][\w\s]{0,20}?)\s+(?:is\s+in|in|does)\s+(.+?)(?:\?|$|\s+have|\s+contain)",
        q,
    )
    calories_in = re.search(r"(?:calories?|nutrition(?:al\s+info)?|macros?)\s+(?:in|of|for)\s+(.+?)(?:\?|$)", q)
    x_content = re.search(r"([a-z]+)\s+content\s+(?:of|in)\s+(.+?)(?:\?|$)", q)
    if how_much:
        nword = how_much.group(1).strip()
        ingredient = how_much.group(2).strip()
        nutrient = _NUTRIENT_MAP.get(nword)
        entities = {"ingredient": ingredient}
        if nutrient:
            entities["nutrient"] = nutrient
        return _keyword_result("get_nutritional_info", entities)
    if calories_in:
        ingredient = calories_in.group(1).strip()
        return _keyword_result("get_nutritional_info", {"ingredient": ingredient})
    if x_content:
        nword = x_content.group(1).strip()
        ingredient = x_content.group(2).strip()
        nutrient = _NUTRIENT_MAP.get(nword)
        entities = {"ingredient": ingredient}
        if nutrient:
            entities["nutrient"] = nutrient
        return _keyword_result("get_nutritional_info", entities)

    # ── ingredient_nutrients: "nutrients in X" / "what nutrients does X have" ──
    ingr_nutrients = re.search(
        r"(?:nutrients?\s+(?:in|of)\s+|what\s+nutrients?\s+(?:does|do|is\s+in)\s+|nutritional\s+(?:value|content)\s+of\s+)(.+?)(?:\?|$|\s+have|\s+contain)",
        q,
    )
    if ingr_nutrients:
        ingredient = ingr_nutrients.group(1).strip()
        return _keyword_result("ingredient_nutrients", {"ingredient": ingredient})

    # ── product_nutrients: "nutrition in <product>" when product type word present ─
    prod_nutrition = re.search(
        r"(?:nutrition(?:al\s+info)?|calories?|macros?|protein)\s+(?:in|of|for)\s+(.+?)(?:\?|$)", q
    )
    if prod_nutrition:
        candidate = prod_nutrition.group(1).strip()
        if any(pt in candidate for pt in _PRODUCT_TYPES):
            return _keyword_result("product_nutrients", {"product": candidate})

    # ── ingredient_in_recipes: "recipes with/containing/using X" ──────────────
    ingr_in_recipe = re.search(
        r"(?:recipes?\s+(?:with|containing|using|that\s+(?:use|have|include))\s+|dishes?\s+(?:with|using)\s+|meals?\s+(?:with|using)\s+)(.+?)(?:\?|$)",
        q,
    )
    if ingr_in_recipe:
        ingredient = ingr_in_recipe.group(1).strip()
        # Exclude if it looks like a diet/course modifier (those go to find_recipe)
        if ingredient not in _DIET_MAP and ingredient not in _COURSE_MAP:
            return _keyword_result("ingredient_in_recipes", {"ingredient": ingredient})

    # ── find_product: diet modifier + product type word, but NOT when recipe triggers present ─
    # e.g. "gluten-free bread" → find_product, but "gluten-free pasta dishes" → find_recipe
    diets = _extract_diets(q)
    if diets and words & _PRODUCT_TYPES and not (words & _RECIPE_TRIGGERS):
        product_word = next(w for w in words if w in _PRODUCT_TYPES)
        product = f"{' '.join(d.lower() for d in diets)} {product_word}".strip()
        return _keyword_result("find_product", {"product": product, "diet": diets})

    # ── recipes_for_cuisine / cuisine_recipes ──────────────────────────────────
    cuisine = _extract_cuisine(q)
    if cuisine and (words & _RECIPE_TRIGGERS or any(w in q for w in ["food", "dish", "dishes", "cuisine"])):
        entities = {"cuisine": cuisine}
        course = _extract_course(q)
        if course:
            entities["course"] = course
        diets = _extract_diets(q)
        if diets:
            entities["diet"] = diets
        # Pick up any ingredient mentioned alongside cuisine
        include = _extract_include_ingredients(q, exclude_words={cuisine.lower()})
        if include:
            entities["include_ingredient"] = include
        return _keyword_result("recipes_for_cuisine", entities)

    # ── general_nutrition: "what is X" / "explain X" / "define X" ─────────────
    # Only when X is a nutrition concept, not a food item
    general = re.search(r"(?:what\s+is\s+|what\s+are\s+|explain\s+|define\s+|tell\s+me\s+about\s+)(.+?)(?:\?|$)", q)
    if general:
        concept = general.group(1).strip()
        # If the concept is a known nutrient concept (not a food), treat as general_nutrition
        nutrition_concepts = {
            "fiber", "fibre", "protein", "carbohydrate", "carbohydrates", "fat", "fats",
            "calories", "macronutrients", "micronutrients", "vitamins", "minerals",
            "antioxidants", "probiotics", "prebiotics", "glycemic index", "omega-3",
            "omega 3", "cholesterol", "saturated fat", "trans fat", "sodium",
        }
        if concept in nutrition_concepts or any(nc in concept for nc in nutrition_concepts):
            return _keyword_result("general_nutrition", {"nutrient": concept})

    # ── recipes_by_nutrient: "high-protein recipes" / "low-fat dinner" ─────────
    # Fires when the primary signal is a nutrient modifier (high/low + nutrient)
    # with a recipe trigger. Skipped when:
    #   - the matched word is also a diet label keyword (e.g. "carb" → Low-Carb)
    #     AND an explicit course is present → prefer find_recipe
    #   - a non-nutrient diet label is also present alongside a course
    _NUTRIENT_DIET_WORDS = {"protein", "fat", "carb", "carbs"}  # words that double as diet labels
    nutrient_recipe = re.search(
        r"(?:high|low|rich\s+in)\s*[-\s]?([a-z]+)\s+(?:recipe|recipes|meal|meals|dish|dishes|food|foods|dinner|lunch|breakfast|snack)",
        q,
    )
    if nutrient_recipe:
        nword = nutrient_recipe.group(1).strip()
        nutrient = _NUTRIENT_MAP.get(nword)
        course = _extract_course(q)
        diets_check = _extract_diets(q)
        nutrient_diets = {"High-Protein", "Low-Fat", "Low-Carb"}
        non_nutrient_diets = [d for d in diets_check if d not in nutrient_diets]
        # Fall through to find_recipe when:
        #   - a non-nutrient diet label is present alongside a course, OR
        #   - the nutrient word is "carb"/"carbs" (maps to Low-Carb diet) AND a course is present
        #     (e.g. "low-carb lunch" → Low-Carb diet + course → find_recipe)
        #   Note: "low-fat dinner" stays as recipes_by_nutrient (fat is a nutrient, not a diet label
        #   that changes the query type when combined with a course)
        is_carb_word = nword in {"carb", "carbs", "carbohydrate", "carbohydrates"}
        if (non_nutrient_diets and course) or (is_carb_word and course):
            pass  # fall through to find_recipe
        else:
            entities = {}
            # Build nutrient_threshold for Cypher/constraint filter
            # "high"/"rich in" -> gt (>=), "low" -> lt (<=)
            # Recipe uses percent_calories_* for protein/fat/carbs; others use grams via HAS_NUTRITION
            is_low = "low" in (nutrient_recipe.group(0).split()[0] if nutrient_recipe else "")
            op = "lt" if is_low else "gt"
            nutrient_name_for_graph = nutrient or "Protein"
            if "protein" in (nword or ""):
                default_val = 10 if is_low else 25  # % of calories from protein
            elif "fat" in (nword or ""):
                default_val = 30 if is_low else 50  # low fat <= 30%, high fat >= 50%
            elif "carb" in (nword or ""):
                default_val = 20 if is_low else 40  # % of calories
            elif "fiber" in (nword or ""):
                default_val = 2 if is_low else 5   # grams
            else:
                default_val = 20 if is_low else 25
            entities["nutrient_threshold"] = {
                "nutrient": nutrient_name_for_graph,
                "operator": op,
                "value": default_val,
            }
            if course:
                entities["course"] = course
            if non_nutrient_diets:
                entities["diet"] = non_nutrient_diets
            include = _extract_include_ingredients(q)
            if include:
                entities["include_ingredient"] = include
            exclude_ingredients = _extract_exclude_ingredients(q)
            if exclude_ingredients:
                entities["exclude_ingredient"] = exclude_ingredients
            return _keyword_result("recipes_by_nutrient", entities)

    # ── find_recipe: diet and/or course keyword + recipe trigger ──────────────
    # Only fire when we have at least one clear signal (diet OR course OR health condition)
    # to avoid swallowing ambiguous queries that should go to LLM.
    diets = _extract_diets(q)
    health_diets = _extract_health_diets(q)
    # Merge health-condition-derived diets with explicit diet labels (no duplicates)
    all_diets = diets + [d for d in health_diets if d not in diets]
    course = _extract_course(q)
    has_recipe_trigger = bool(words & _RECIPE_TRIGGERS)
    exclude_ingredients = _extract_exclude_ingredients(q)
    has_food_context = any(w in q for w in ["recipe", "recipes", "meal", "meals", "dish", "dishes",
                                             "food", "foods", "dinner", "lunch", "breakfast",
                                             "dessert", "desserts", "appetizer", "soup", "salad",
                                             "snack", "cook", "make", "prepare", "eat", "eating",
                                             "healthy", "ideas", "patient", "patients",
                                             "hungry", "hunger", "crave", "craving", "starving"])

    # Health condition with no diet mapping → let LLM handle it (unless we extracted exclude_ingredient)
    has_health_word = bool(words & _HEALTH_WORDS)
    health_maps_empty = has_health_word and health_diets == [] and not diets and not exclude_ingredients

    if health_maps_empty:
        return None  # e.g. "food allergy" or "oral allergy" — LLM handles specifics

    if (all_diets or course or exclude_ingredients) and (has_recipe_trigger or has_food_context):
        entities = {}
        if all_diets:
            entities["diet"] = all_diets
        if course:
            entities["course"] = course
        if exclude_ingredients:
            entities["exclude_ingredient"] = exclude_ingredients
        include = _extract_include_ingredients(q)
        if include:
            entities["include_ingredient"] = include
        return _keyword_result("find_recipe", entities)

    # ── Ambiguous / complex → fall through to LLM ─────────────────────────────
    return None


# ── Entity extraction helpers ──────────────────────────────────────────────────

def _extract_diets(q: str) -> list[str]:
    """Extract all diet labels present in the query."""
    found: list[str] = []
    for keyword, label in _DIET_MAP.items():
        if keyword in q and label not in found:
            found.append(label)
    return found


def _extract_course(q: str) -> str | None:
    """Extract meal course from query. Multi-word phrases checked first."""
    for phrase in sorted(_COURSE_MAP.keys(), key=len, reverse=True):
        if phrase in q:
            return _COURSE_MAP[phrase]
    return None


def _extract_cuisine(q: str) -> str | None:
    """Extract cuisine name from query. Multi-word cuisines checked first."""
    for cuisine in sorted(_CUISINE_SET, key=len, reverse=True):
        if cuisine in q:
            return cuisine.title()
    return None


def _extract_after(q: str, phrases: list[str]) -> str | None:
    """Extract the word/phrase immediately after any of the given trigger phrases."""
    for phrase in phrases:
        idx = q.find(phrase)
        if idx != -1:
            rest = q[idx + len(phrase):].strip().rstrip("?").strip()
            if rest:
                return rest.split()[0] if " " in rest else rest
    return None


def _extract_exclude_ingredients(q: str) -> list[str]:
    """
    Extract ingredients to exclude (allergens, avoidances) from patterns like:
    - "without strawberries", "without X and Y"
    - "no nuts", "no X or Y"
    - "avoid dairy", "avoid X"
    - "allergic to peanuts", "allergy to X"
    - "don't want strawberries"
    - "free of nuts" (but not diet labels like nut-free, gluten-free)
    - "excluding X"
    Returns a deduplicated list of ingredient names.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(ing: str) -> None:
        ing = ing.strip().rstrip(".,?!")
        if ing and len(ing) > 1:
            low = ing.lower()
            # Skip diet labels (nut-free, gluten-free are diets, not ingredient exclusions)
            if low in _DIET_MAP or low.replace("-", " ") in _DIET_MAP:
                return
            # Normalize to allergen code when possible; else keep raw
            code = normalize_to_allergen(ing)
            val = code if code else ing.replace(" ", "_")
            if val not in seen:
                seen.add(val)
                found.append(val)

    # "without X" / "without X and Y"
    for m in re.finditer(r"\bwithout\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", q):
        _add(m.group(1))
        if m.group(2):
            _add(m.group(2))

    # "no X" / "no X or Y"
    for m in re.finditer(r"\bno\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", q):
        _add(m.group(1))
        if m.group(2):
            _add(m.group(2))

    # "avoid X"
    for m in re.finditer(r"\bavoid(?:ing)?\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", q):
        _add(m.group(1))
        if m.group(2):
            _add(m.group(2))

    # "allergic to X" / "allergy to X"
    for m in re.finditer(r"\ballerg(?:ic|y)\s+to\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", q):
        _add(m.group(1))
        if m.group(2):
            _add(m.group(2))

    # "don't want X" / "do not want X"
    for m in re.finditer(r"\b(?:don't|dont|do\s+not)\s+want\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", q):
        _add(m.group(1))
        if m.group(2):
            _add(m.group(2))

    # "free of X" (but not "nut-free", "gluten-free" which are diets)
    for m in re.finditer(r"\bfree\s+of\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", q):
        _add(m.group(1))
        if m.group(2):
            _add(m.group(2))

    # "excluding X"
    for m in re.finditer(r"\bexcluding\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", q):
        _add(m.group(1))
        if m.group(2):
            _add(m.group(2))

    return found


def _extract_include_ingredients(q: str, exclude_words: set[str] | None = None) -> list[str]:
    """
    Extract 'with X' or 'and X' ingredient mentions from a recipe query.
    Returns a list only when the signal is unambiguous (single ingredient after 'with').
    """
    exclude_words = exclude_words or set()
    match = re.search(r"\bwith\s+([a-z][\w\s]{1,20}?)(?:\s+and\s+([a-z][\w\s]{1,20}))?(?:\?|$|\s+recipe|\s+dish)", q)
    if match:
        results = []
        for grp in [match.group(1), match.group(2)]:
            if grp:
                w = grp.strip()
                if w and w not in exclude_words and w not in _COURSE_MAP and w not in _DIET_MAP:
                    results.append(w)
        return results
    return []


def _get_client() -> OpenAI:
    timeout = float(os.environ.get("LLM_TIMEOUT", "30"))
    return OpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
        timeout=timeout,
    )


def _load_llm_retry_config(config_path: str | Path = "embedding_config.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return raw.get("llm_retry", {}) or {}
    except Exception:
        return {}


def extract_intent(
    text: str,
    *,
    model: str | None = None,
    config_path: str | Path = "embedding_config.yaml",
) -> str:
    """
    Extract intent + entities from free-text user query.

    First tries the zero-cost keyword pre-filter (_keyword_extract). If it
    returns a confident result, that is serialised to JSON and returned
    immediately — no LLM call is made. Only ambiguous or complex queries
    fall through to the LLM (using the compact zero-shot SYSTEM_PROMPT).

    Args:
        text: User query
        model: LLM model name (defaults to INTENT_MODEL env var or gpt-4o-mini)
        config_path: Path to embedding_config.yaml for llm_retry settings

    Returns:
        Raw JSON string (either from keyword filter or LLM)
    """
    # ── Layer 1: keyword pre-filter (zero LLM cost) ───────────────────────────
    keyword_result = _keyword_extract(text)
    if keyword_result is not None:
        return json.dumps(keyword_result)

    # ── Layer 2: LLM fallback with compact prompt ─────────────────────────────
    client = _get_client()
    model = model or os.environ.get("INTENT_MODEL", "gpt-4o-mini")

    def _call():
        return client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )

    retry_cfg = _load_llm_retry_config(config_path)
    if with_retry and retry_cfg:
        response = with_retry(
            _call,
            max_attempts=retry_cfg.get("max_attempts", 3),
            initial_delay_ms=float(retry_cfg.get("initial_delay_ms", 1000)),
            max_delay_ms=float(retry_cfg.get("max_delay_ms", 30000)),
            jitter=retry_cfg.get("jitter", True),
        )
    else:
        response = _call()

    return response.choices[0].message.content


def extract_intent_b2b(
    text: str,
    *,
    model: str | None = None,
    config_path: str | Path = "embedding_config.yaml",
) -> str:
    """
    Extract B2B intent + entities. Uses B2B-specific system prompt.
    No keyword pre-filter — goes straight to LLM (call from extract_hybrid_b2b
    after rule patterns have been tried).
    """
    client = _get_client()
    model = model or os.environ.get("INTENT_MODEL", "gpt-4o-mini")

    def _call():
        return client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_B2B},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )

    retry_cfg = _load_llm_retry_config(config_path)
    if with_retry and retry_cfg:
        response = with_retry(
            _call,
            max_attempts=retry_cfg.get("max_attempts", 3),
            initial_delay_ms=float(retry_cfg.get("initial_delay_ms", 1000)),
            max_delay_ms=float(retry_cfg.get("max_delay_ms", 30000)),
            jitter=retry_cfg.get("jitter", True),
        )
    else:
        response = _call()

    return response.choices[0].message.content


def extract_entities_only_b2b(
    text: str,
    intent: str,
    *,
    model: str | None = None,
    config_path: str | Path = "embedding_config.yaml",
) -> dict[str, Any] | None:
    """
    Entity-only LLM extraction when rule-based extraction misses allergens/diets/conditions.
    Returns parsed entities dict or None on failure. Caller normalizes and merges.
    """
    client = _get_client()
    model = model or os.environ.get("INTENT_MODEL", "gpt-4o-mini")
    prompt = ENTITY_PROMPT_B2B.format(intent=intent, query=text)

    def _call():
        return client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )

    try:
        retry_cfg = _load_llm_retry_config(config_path)
        if with_retry and retry_cfg:
            response = with_retry(
                _call,
                max_attempts=retry_cfg.get("max_attempts", 2),
                initial_delay_ms=float(retry_cfg.get("initial_delay_ms", 1000)),
                max_delay_ms=float(retry_cfg.get("max_delay_ms", 10000)),
                jitter=retry_cfg.get("jitter", True),
            )
        else:
            response = _call()
        raw = response.choices[0].message.content
        parsed = parse_extractor_output(raw) if isinstance(raw, str) else raw
        if parsed and isinstance(parsed.get("entities"), dict):
            return parsed["entities"]
    except Exception as e:
        logger.warning("extract_entities_only_b2b failed: %s", e)
    return None


RETRY_USER_MESSAGE = "Return only valid JSON with keys 'intent' and 'entities'. No markdown."


def parse_extractor_output(raw: str) -> dict[str, Any] | None:
    """
    Parse LLM extractor output. Tries json.loads first, then repair (strip markdown, fix trailing commas).

    Returns:
        Parsed dict or None if unparseable
    """
    text = raw.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try repairing common issues
    try:
        fixed = re.sub(r",\s*}", "}", text)
        fixed = re.sub(r",\s*]", "]", fixed)
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    return None


def extract_intent_with_retry(
    text: str,
    *,
    model: str | None = None,
    max_retries: int = 1,
    retry_message: str = RETRY_USER_MESSAGE,
    config_path: str | Path = "embedding_config.yaml",
) -> tuple[str, bool]:
    """
    Extract intent + entities with optional retry on parse failure.

    Keyword pre-filter is checked first; if it matches, the result is
    returned immediately without any LLM call or retry logic.

    Args:
        text: User query
        model: LLM model name
        max_retries: Max extra calls after first parse failure
        retry_message: Message to add on retry when output was invalid JSON

    Returns:
        (raw_json_string, was_parse_successful)
    """
    # Intent cache: if hit, skip keyword and LLM
    if get_intent_cache:
        cache = get_intent_cache(config_path)
        if cache:
            cached = cache.get(text)
            if cached is not None:
                return cached, True

    # Keyword pre-filter: if confident, skip LLM entirely
    keyword_result = _keyword_extract(text)
    if keyword_result is not None:
        raw = json.dumps(keyword_result)
        if get_intent_cache:
            c = get_intent_cache(config_path)
            if c:
                c.put(text, raw)
        return raw, True

    raw = extract_intent(text, model=model, config_path=config_path)
    parsed = parse_extractor_output(raw)
    if parsed is not None:
        if get_intent_cache:
            c = get_intent_cache(config_path)
            if c:
                c.put(text, raw)
        return raw, True
    for _ in range(max_retries):
        client = _get_client()
        m = model or os.environ.get("INTENT_MODEL", "gpt-4o-mini")
        def _retry_call():
            return client.chat.completions.create(
                model=m,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{retry_message}"},
                    {"role": "user", "content": text},
                ],
                temperature=0,
            )
        retry_cfg = _load_llm_retry_config(config_path)
        if with_retry and retry_cfg:
            response = with_retry(
                _retry_call,
                max_attempts=retry_cfg.get("max_attempts", 3),
                initial_delay_ms=float(retry_cfg.get("initial_delay_ms", 1000)),
                max_delay_ms=float(retry_cfg.get("max_delay_ms", 30000)),
                jitter=retry_cfg.get("jitter", True),
            )
        else:
            response = _retry_call()
        raw = response.choices[0].message.content
        parsed = parse_extractor_output(raw)
        if parsed is not None:
            if get_intent_cache:
                c = get_intent_cache(config_path)
                if c:
                    c.put(text, raw)
            return raw, True
    return raw, False


def sanity_check(output_json):
    """
    Validates the structure and basic logic of the extractor output.
    Returns True on success, or (False, reason_string) on failure.
    """
    required_keys = {"intent", "entities"}

    if not isinstance(output_json, dict):
        return False, "Output is not a dictionary"

    if not required_keys.issubset(output_json.keys()):
        return False, "Missing required top-level keys: 'intent' and/or 'entities'"

    if not isinstance(output_json["intent"], str):
        return False, "Intent is not a string"

    if output_json["intent"] not in VALID_INTENTS:
        return False, f"Unknown intent: '{output_json['intent']}'. Valid: {sorted(VALID_INTENTS)}"

    if not isinstance(output_json["entities"], dict):
        return False, "Entities is not a dictionary"

    entities = output_json["entities"]

    # --- include/exclude ingredient overlap check ---
    include = entities.get("include_ingredient", [])
    exclude = entities.get("exclude_ingredient", [])
    if isinstance(include, list) and isinstance(exclude, list):
        overlap = set(i.lower() for i in include) & set(e.lower() for e in exclude)
        if overlap:
            return False, f"Ingredient(s) in both include and exclude: {list(overlap)}"

    # --- nutrient_threshold structure check ---
    threshold = entities.get("nutrient_threshold")
    if threshold is not None:
        if not isinstance(threshold, dict):
            return False, "nutrient_threshold must be a dictionary"
        for required_field in ("nutrient", "operator", "value"):
            if required_field not in threshold:
                return False, f"nutrient_threshold missing field: '{required_field}'"
        if threshold.get("operator") not in ("gt", "lt"):
            return False, "nutrient_threshold.operator must be 'gt' or 'lt'"
        if not isinstance(threshold.get("value"), (int, float)):
            return False, "nutrient_threshold.value must be a number"

    # --- compare_foods needs at least 2 ingredients ---
    if output_json["intent"] == "compare_foods":
        foods = entities.get("ingredients", [])
        if not isinstance(foods, list) or len(foods) < 2:
            return False, "compare_foods requires at least 2 items in 'ingredients'"

    # --- Step 7: optional confidence validation (warn and strip if invalid, don't fail) ---
    conf = output_json.get("confidence")
    if conf is not None:
        if not isinstance(conf, (int, float)):
            logger.warning("sanity_check: confidence must be a number, ignoring value %r", conf)
            output_json.pop("confidence", None)
        elif not (0 <= conf <= 1):
            logger.warning("sanity_check: confidence must be 0–1, ignoring value %r", conf)
            output_json.pop("confidence", None)

    return True


def sanity_check_b2b(output_json) -> bool | tuple[bool, str]:
    """
    Validates B2B extractor output. Uses VALID_INTENTS_WITH_B2B so B2B intents pass.
    Returns True on success, or (False, reason_string) on failure.
    """
    required_keys = {"intent", "entities"}
    if not isinstance(output_json, dict):
        return False, "Output is not a dictionary"
    if not required_keys.issubset(output_json.keys()):
        return False, "Missing required top-level keys: 'intent' and/or 'entities'"
    if not isinstance(output_json["intent"], str):
        return False, "Intent is not a string"
    if output_json["intent"] not in VALID_INTENTS_WITH_B2B:
        return False, f"Unknown intent: '{output_json['intent']}'. Valid B2B intents included."
    if not isinstance(output_json["entities"], dict):
        return False, "Entities is not a dictionary"
    return True


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    # Usage:
    #   python3 extractor_classifier.py                        → interactive mode
    #   python3 extractor_classifier.py "vegan dinner recipes" → single query
    #   python3 extractor_classifier.py --keyword-only "..."   → skip LLM, keyword filter only

    keyword_only = "--keyword-only" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    def _run(query: str) -> None:
        print(f"\nQuery: {query!r}")
        print("-" * 60)

        # Always show keyword filter result first
        kw = _keyword_extract(query)
        if kw is not None:
            print(f"[KEYWORD HIT — 0 LLM tokens]")
            print(json.dumps(kw, indent=2))
            check = sanity_check(kw)
            print(f"Sanity check: {'OK' if check is True else check[1]}")
            return

        print("[KEYWORD MISS — calling LLM fallback]")
        if keyword_only:
            print("(skipping LLM, --keyword-only mode)")
            return

        response = extract_intent(query)
        try:
            response_json = json.loads(response)
        except json.JSONDecodeError:
            response_json = {"error": "Invalid JSON response", "raw": response}

        print(json.dumps(response_json, indent=2))
        check = sanity_check(response_json)
        print(f"Sanity check: {'OK' if check is True else check[1]}")

    if args:
        # Single query passed as argument
        _run(" ".join(args))
    else:
        # Interactive mode — keep prompting until Ctrl+C or empty input
        print("Intent Extractor — interactive mode  (Ctrl+C or empty input to quit)")
        print("Tip: run with --keyword-only to skip LLM calls\n")
        while True:
            try:
                query = input("Query> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nBye!")
                break
            if not query:
                break
            _run(query)
