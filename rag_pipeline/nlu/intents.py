"""
Expected intents — single source of truth for the RAG pipeline.

All NLU output (keyword extractor, LLM extractor, chatbot rules) must produce
an intent from VALID_INTENTS. Used by sanity_check, constraint_filter,
orchestrator, and API routing.
"""

from __future__ import annotations

# ── Full enum of valid intents ────────────────────────────────────────────────

VALID_INTENTS: frozenset[str] = frozenset({
    # Recipe search & discovery
    "find_recipe",
    "find_recipe_by_pantry",
    "similar_recipes",
    "recipes_for_cuisine",
    "recipes_by_nutrient",
    "rank_results",
    "ingredient_in_recipes",
    "cuisine_recipes",
    # Ingredient & nutrition
    "get_nutritional_info",
    "nutrient_in_foods",
    "nutrient_category",
    "compare_foods",
    "check_diet_compliance",
    "ingredient_nutrients",
    # Substitution
    "check_substitution",
    "get_substitution_suggestion",
    "similar_ingredients",
    # Product
    "find_product",
    "product_nutrients",
    # Cuisine & allergens
    "cuisine_hierarchy",
    "cross_reactive_allergens",
    # General
    "general_nutrition",
    "out_of_scope",
    # Conversational (chatbot)
    "greeting",
    "help",
    "farewell",
    # Chatbot data intents
    "plan_meals",
    "show_meal_plan",
    "log_meal",
    "meal_history",
    "nutrition_summary",
    "swap_meal",
    "grocery_list",
    "set_preference",
    "dietary_advice",
    # Fallback (empty/invalid input)
    "unclear",
})

# ── Subsets for pipeline logic ────────────────────────────────────────────────

# Intents that return recipes and should have hard constraints (allergens, diets, calories) applied
RECIPE_INTENTS: frozenset[str] = frozenset({
    "find_recipe",
    "find_recipe_by_pantry",
    "similar_recipes",
    "recipes_for_cuisine",
    "recipes_by_nutrient",
    "rank_results",
    "ingredient_in_recipes",
    "cuisine_recipes",
})

# Intents that run retrieval (semantic + structural + cypher) + LLM generation
DATA_INTENTS_NEEDING_RETRIEVAL: frozenset[str] = frozenset({
    "find_recipe",
    "find_recipe_by_pantry",
    "get_nutritional_info",
    "compare_foods",
    "check_diet_compliance",
    "check_substitution",
    "get_substitution_suggestion",
    "similar_recipes",
    "recipes_for_cuisine",
    "recipes_by_nutrient",
    "nutrient_in_foods",
    "nutrient_category",
    "ingredient_in_recipes",
    "ingredient_nutrients",
    "find_product",
    "product_nutrients",
    "cuisine_recipes",
    "cuisine_hierarchy",
    "cross_reactive_allergens",
    "general_nutrition",
})

# Deterministic chatbot intents: fixed Cypher, no retrieval or LLM generation
CHATBOT_DATA_INTENTS: frozenset[str] = frozenset({
    "show_meal_plan",
    "meal_history",
    "nutrition_summary",
})

# ── B2B intents (vendor-scoped product/customer queries) ──────────────────────

B2B_INTENTS: frozenset[str] = frozenset({
    "b2b_products_for_condition",
    "b2b_products_allergen_free",
    "b2b_products_for_diet",
    "b2b_customers_for_product",
    "b2b_customers_with_condition",
    "b2b_customer_recommendations",
    "b2b_analytics",
    "b2b_product_compliance",
    "b2b_product_nutrition",
    "b2b_generate_report",
})

# B2B intents that run Cypher retrieval (route to b2b_cypher)
B2B_DATA_INTENTS: frozenset[str] = frozenset({
    "b2b_products_for_condition",
    "b2b_products_allergen_free",
    "b2b_products_for_diet",
    "b2b_customers_for_product",
    "b2b_customers_with_condition",
    "b2b_customer_recommendations",
    "b2b_analytics",
    "b2b_product_compliance",
    "b2b_product_nutrition",
    "b2b_generate_report",
})

# All valid intents including B2B (for sanity_check when in B2B context)
VALID_INTENTS_WITH_B2B: frozenset[str] = VALID_INTENTS | B2B_INTENTS
