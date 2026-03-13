from __future__ import annotations

from typing import Any

from rag_pipeline.augmentation.condense import (
    condense_for_llm,
    format_context_as_text,
    format_semantic_results_as_text,
)
from rag_pipeline.augmentation.fusion import format_fused_results_as_text
from rag_pipeline.orchestrator.orchestrator import OrchestratorResult


SYSTEM_PROMPT_BASE = """You are a Nutrition assistant. Recommend recipes and answer food/nutrition questions using ONLY the context below.

RULES: Use only recipes/ingredients from the context. If no suitable options in the context, say "I don't have matching recipes in my database" and suggest refining the query. Do NOT invent recipes or ingredients. Be concise and practical.

PERSONALIZATION: When [USER PROFILE] is provided: use the customer's name when greeting; respect diets, allergens, and health conditions; tailor suggestions to their health goal and activity level; reference recent meals when avoiding repetition helps."""

# Backward compatibility for cli.py and other consumers
SYSTEM_PROMPT = SYSTEM_PROMPT_BASE


def _build_constraint_instructions(profile: dict[str, Any]) -> str:
    """
    Build explicit constraint instructions for the LLM when profile has allergens,
    diets, or health conditions. Reduces hallucination of non-compliant ingredients.
    """
    lines: list[str] = []

    allergens = profile.get("allergens") or []
    if allergens:
        lines.append(f"NEVER suggest or mention these allergens: {', '.join(allergens)}. Never include them in recipes or substitutes.")

    diets = profile.get("diets") or []
    if diets:
        diet_rules: list[str] = []
        for d in diets:
            d_lower = (d or "").strip().lower()
            if "vegan" in d_lower:
                diet_rules.append("Vegan: no meat, fish, poultry, dairy, eggs, honey, or other animal products")
            elif "vegetarian" in d_lower:
                diet_rules.append("Vegetarian: no meat, fish, poultry")
            elif "keto" in d_lower or "ketogenic" in d_lower:
                diet_rules.append("Keto: no grains, sugar, high-carb ingredients (bread, pasta, rice)")
            elif "paleo" in d_lower:
                diet_rules.append("Paleo: no grains, legumes, dairy, refined sugar")
            elif "gluten" in d_lower:
                diet_rules.append("Gluten-Free: no wheat, barley, rye, or gluten-containing ingredients")
            elif "dairy" in d_lower:
                diet_rules.append("Dairy-Free: no milk, cheese, butter, yogurt, cream")
            elif "nut" in d_lower:
                diet_rules.append("Nut-Free: no peanuts, tree nuts")
            else:
                diet_rules.append(f"{d}: comply with standard {d} guidelines")
        if diet_rules:
            lines.append(f"ONLY suggest ingredients compliant with: {'; '.join(diet_rules)}")

    conditions = profile.get("health_conditions") or []
    if conditions:
        lines.append(f"Consider these health conditions: {', '.join(conditions)}. Avoid or warn about ingredients that could worsen them (e.g. high-sodium for hypertension, high-sugar for diabetes).")

    return " ".join(lines) if lines else ""


# Max recent recipes in profile to limit token cost
_PROFILE_RECENT_RECIPES_CAP = 5


def _build_profile_section(profile: dict[str, Any]) -> str:
    """
    Render the customer profile as a compact, readable block for the LLM prompt.
    Only includes fields that are non-empty so the section stays concise.
    """
    lines: list[str] = []

    name = profile.get("display_name")
    if name and isinstance(name, str) and name.strip():
        lines.append(f"Customer name: {name.strip()}")

    diets = profile.get("diets") or []
    if diets:
        lines.append(f"Dietary preferences: {', '.join(diets)}")

    allergens = profile.get("allergens") or []
    if allergens:
        lines.append(f"Allergens (NEVER include in recommendations): {', '.join(allergens)}")

    conditions = profile.get("health_conditions") or []
    if conditions:
        lines.append(f"Health conditions: {', '.join(conditions)}")

    goal = profile.get("health_goal")
    if goal:
        lines.append(f"Health goal: {goal.replace('_', ' ')}")

    activity = profile.get("activity_level")
    if activity:
        lines.append(f"Activity level: {activity}")

    recent = profile.get("recent_recipes") or []
    if recent:
        capped = recent[: _PROFILE_RECENT_RECIPES_CAP]
        lines.append(f"Recent meals: {', '.join(capped)}")

    household_type = profile.get("household_type")
    if household_type and isinstance(household_type, str):
        ht = household_type.strip().lower()
        if ht in ("individual", "couple", "family"):
            lines.append(f"Household type: {ht}")

    return "\n".join(lines) if lines else ""


def build_augmented_prompt(
    result: OrchestratorResult,
    user_query: str,
    *,
    customer_profile: dict[str, Any] | None = None,
    max_semantic: int = 5,
    max_structural: int = 7,
    max_cypher: int = 10,
    max_fused: int = 15,
) -> str:
    """
    Build the final augmented LLM prompt from retrieval results.

    When fused_results is available (from RRF), uses a single RANKED CONTEXT section.
    Otherwise falls back to separate semantic, collaborative, and graph sections.

    Args:
        result: OrchestratorResult from orchestrate()
        user_query: Original user query
        customer_profile: Dict from fetch_customer_profile() — when provided, a
                          [USER PROFILE] section is injected so the LLM always
                          knows the customer's hard constraints (allergens, diets,
                          health conditions) when generating the response.
        max_semantic: Max semantic results (fallback only)
        max_structural: Max structural results (fallback only)
        max_cypher: Max cypher results (fallback only)
        max_fused: Max fused results when using RRF

    Returns:
        Full augmented prompt string ready for LLM
    """
    sections: list[str] = []

    # Build system prompt: base + optional constraint instructions
    system_prompt = SYSTEM_PROMPT_BASE
    if customer_profile:
        constraint_instructions = _build_constraint_instructions(customer_profile)
        if constraint_instructions:
            system_prompt = f"{system_prompt}\n\nHARD CONSTRAINTS: {constraint_instructions}"
    sections.append(f"[SYSTEM]\n{system_prompt}")

    # ── Customer profile (injected right after system prompt) ──────────────────
    if customer_profile:
        profile_text = _build_profile_section(customer_profile)
        if profile_text:
            sections.append(f"[USER PROFILE]\n{profile_text}")

    # ── Zero-results fallback (takes priority over context sections) ──────────
    # When post-fusion hard filters removed everything, inject the structured
    # explanation so the LLM can tell the user why and suggest alternatives.
    if result.fallback_message:
        sections.append(f"[NO RESULTS]\n{result.fallback_message}")
        # Still append the user query below so the LLM has full context
        sections.append(f"[USER QUERY]\n{user_query}")
        return "\n\n".join(sections)

    # ── Fused RRF context (primary when available) ─────────────────────────────
    if result.fused_results:
        fused_text = format_fused_results_as_text(
            result.fused_results,
            header="Ranked results (semantic + collaborative + graph):",
            max_items=max_fused,
        )
        sections.append(f"[RANKED CONTEXT]\n{fused_text}")
    else:
        # Fallback: separate sections
        if result.semantic_results:
            semantic_text = format_semantic_results_as_text(
                result.semantic_results,
                header="Semantically relevant results from knowledge graph:",
                max_items=max_semantic,
            )
            sections.append(f"[SEMANTIC CONTEXT]\n{semantic_text}")

        expanded = result.structural_results.get("expanded_context", [])
        if expanded:
            condensed = condense_for_llm(expanded, max_items=max_structural)
            structural_text = format_context_as_text(
                condensed,
                header="Recipes liked by similar users:",
            )
            sections.append(f"[COLLABORATIVE CONTEXT]\n{structural_text}")

        if result.cypher_results:
            cypher_text = _format_cypher_results(
                result.intent,
                result.cypher_results,
                max_items=max_cypher,
            )
            sections.append(f"[GRAPH FACTS]\n{cypher_text}")

    # ── Errors (if any) ───────────────────────────────────────────────────────
    if result.errors:
        sections.append(f"[WARNINGS]\n" + "\n".join(f"- {e}" for e in result.errors))

    # ── User query ────────────────────────────────────────────────────────────
    sections.append(f"[USER QUERY]\n{user_query}")

    return "\n\n".join(sections)


def _format_cypher_results(
    intent: str,
    rows: list[dict[str, Any]],
    *,
    max_items: int = 10,
) -> str:
    """Format Cypher result rows as readable text based on intent."""
    if not rows:
        return "No results found."

    lines: list[str] = []

    if intent in ("find_recipe", "find_recipe_by_pantry", "recipes_for_cuisine", "recipes_by_nutrient", "ingredient_in_recipes", "cuisine_recipes"):
        lines.append("Matching recipes from graph:")
        for i, row in enumerate(rows[:max_items], 1):
            title = row.get("r.title", row.get("title", "Unknown"))
            rtype = row.get("r.meal_type", row.get("r.recipe_type", row.get("meal_type", row.get("recipe_type", ""))))
            time = row.get("r.total_time_minutes", row.get("total_time_minutes", ""))
            protein = row.get("r.percent_calories_protein", "")
            cuisine = row.get("cuisine_name", row.get("c.name", ""))
            time_str = f", {time} min" if time else ""
            protein_str = f", {round(float(protein), 1)}% protein" if protein and protein != "" else ""
            cuisine_str = f", {cuisine}" if cuisine else ""
            lines.append(f"{i}. {title} [{rtype}{time_str}{protein_str}{cuisine_str}]")

    elif intent == "get_nutritional_info":
        lines.append("Nutritional information:")
        for row in rows[:max_items]:
            ingredient = row.get("ingredient", "")
            nutrient = row.get("nutrient", "")
            amount = row.get("amount", "")
            unit = row.get("unit", "")
            if nutrient:
                lines.append(f"- {ingredient}: {nutrient} = {amount} {unit}")
            else:
                filtered = {k: v for k, v in row.items() if v is not None and k != "ingredient"}
                lines.append(f"- {ingredient}: {filtered}")

    elif intent == "compare_foods":
        lines.append("Nutritional comparison:")
        for row in rows[:max_items]:
            ingredient = row.get("ingredient", "")
            nutrient = row.get("nutrient", "")
            amount = row.get("amount", "")
            unit = row.get("unit", "")
            if nutrient:
                lines.append(f"- {ingredient}: {amount} {unit} of {nutrient}")
            else:
                filtered = {k: v for k, v in row.items() if v is not None}
                lines.append(f"- {filtered}")

    elif intent == "check_diet_compliance":
        for row in rows[:max_items]:
            ingredient = row.get("ingredient", "")
            diet = row.get("diet", "")
            status = row.get("compliance_status", "")
            lines.append(f"- {ingredient} on {diet} diet: {status}")

    elif intent in ("check_substitution", "get_substitution_suggestion"):
        lines.append("Substitution options:")
        for row in rows[:max_items]:
            sub = row.get("suggested_substitute", row.get("substitute", ""))
            original = row.get("original", "")
            is_direct = row.get("is_direct_substitute", "")
            notes = row.get("notes", "")
            if original:
                lines.append(f"- {sub} can replace {original} (direct: {is_direct}) {notes or ''}")
            else:
                lines.append(f"- {sub}")

    elif intent == "nutrient_in_foods":
        lines.append("Foods high in this nutrient:")
        for row in rows[:max_items]:
            ingredient = row.get("ingredient", "")
            amount = row.get("amount", "")
            unit = row.get("unit", "")
            nutrient = row.get("nutrient", "")
            if nutrient:
                lines.append(f"- {ingredient}: {amount} {unit} of {nutrient}")
            else:
                lines.append(f"- {ingredient}: {amount}")

    elif intent == "nutrient_category":
        lines.append("Nutrient categories:")
        for row in rows[:max_items]:
            cat = row.get("nc.category_name", row.get("category_name", row.get("display_name", "")))
            sub = row.get("nc.subcategory_name", row.get("subcategory_name", ""))
            parent = row.get("parent_category", "")
            parts = [cat]
            if sub:
                parts.append(f"({sub})")
            if parent:
                parts.append(f"under {parent}")
            lines.append(f"- {' '.join(parts)}")

    elif intent == "product_nutrients":
        lines.append("Product nutrition:")
        for row in rows[:max_items]:
            product = row.get("product", row.get("p.name", ""))
            if "amount" in row and row["amount"] is not None:
                lines.append(f"- {product}: {row.get('amount')} {row.get('unit', '')}")
            else:
                filtered = {k: v for k, v in row.items() if v is not None and k not in ("product",)}
                lines.append(f"- {product}: {filtered}")

    elif intent == "cuisine_hierarchy":
        lines.append("Cuisine taxonomy:")
        for row in rows[:max_items]:
            name = row.get("c.name", row.get("name", ""))
            code = row.get("c.code", row.get("code", ""))
            region = row.get("c.region", row.get("region", ""))
            parent = row.get("parent_cuisine", "")
            parts = [name]
            if code:
                parts.append(f"[{code}]")
            if region:
                parts.append(f"({region})")
            if parent:
                parts.append(f"← {parent}")
            lines.append(f"- {' '.join(parts)}")

    elif intent == "cross_reactive_allergens":
        lines.append("Cross-reactive allergens:")
        for row in rows[:max_items]:
            name = row.get("a.name", row.get("name", ""))
            cross = row.get("a.cross_reactive_with", row.get("cross_reactive_with", ""))
            common = row.get("a.common_names", row.get("common_names", ""))
            lines.append(f"- {name}: cross-reactive with {cross or 'N/A'} | common names: {common or 'N/A'}")

    elif intent == "ingredient_nutrients":
        lines.append("Ingredient nutrition:")
        for row in rows[:max_items]:
            ingredient = row.get("ingredient", "")
            nutrient = row.get("nutrient", "")
            amount = row.get("amount", "")
            unit = row.get("unit", "")
            if nutrient:
                lines.append(f"- {ingredient}: {nutrient} = {amount} {unit}")
            else:
                filtered = {k: v for k, v in row.items() if v is not None and k != "ingredient"}
                lines.append(f"- {ingredient}: {filtered}")

    else:
        for i, row in enumerate(rows[:max_items], 1):
            lines.append(f"{i}. {row}")

    return "\n".join(lines)
