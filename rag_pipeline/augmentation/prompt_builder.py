from __future__ import annotations

from typing import Any

from rag_pipeline.augmentation.condense import (
    condense_for_llm,
    format_context_as_text,
    format_semantic_results_as_text,
)
from rag_pipeline.orchestrator.orchestrator import OrchestratorResult


SYSTEM_PROMPT = """You are a knowledgeable and empathetic nutrition assistant.
Your goal is to recommend recipes, answer food and nutrition questions, and suggest meal plans
based on the user's preferences, health conditions, dietary restrictions, and allergens.

Always respect the customer's hard constraints (allergens, dietary preferences, health conditions).
Never recommend recipes that contain allergens the customer is sensitive to.
Be concise, friendly, and practical in your responses."""


def build_augmented_prompt(
    result: OrchestratorResult,
    user_query: str,
    *,
    max_semantic: int = 5,
    max_structural: int = 7,
    max_cypher: int = 10,
) -> str:
    """
    Build the final augmented LLM prompt from all three retrieval results.

    Args:
        result: OrchestratorResult from orchestrate()
        user_query: Original user query
        max_semantic: Max semantic results to include
        max_structural: Max structural results to include
        max_cypher: Max cypher results to include

    Returns:
        Full augmented prompt string ready for LLM
    """
    sections: list[str] = []

    sections.append(f"[SYSTEM]\n{SYSTEM_PROMPT}")

    # ── Semantic context ──────────────────────────────────────────────────────
    if result.semantic_results:
        semantic_text = format_semantic_results_as_text(
            result.semantic_results,
            header="Semantically relevant results from knowledge graph:",
            max_items=max_semantic,
        )
        sections.append(f"[SEMANTIC CONTEXT]\n{semantic_text}")

    # ── Structural context ────────────────────────────────────────────────────
    expanded = result.structural_results.get("expanded_context", [])
    if expanded:
        condensed = condense_for_llm(expanded, max_items=max_structural)
        structural_text = format_context_as_text(
            condensed,
            header="Recipes liked by similar users:",
        )
        sections.append(f"[COLLABORATIVE CONTEXT]\n{structural_text}")

    # ── Cypher context ────────────────────────────────────────────────────────
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

    if intent in ("find_recipe", "find_recipe_by_pantry"):
        lines.append("Matching recipes from graph:")
        for i, row in enumerate(rows[:max_items], 1):
            title = row.get("r.title", row.get("title", "Unknown"))
            rtype = row.get("r.recipe_type", row.get("recipe_type", ""))
            time = row.get("r.total_time_minutes", row.get("total_time_minutes", ""))
            protein = row.get("r.percent_calories_protein", "")
            time_str = f", {time} min" if time else ""
            protein_str = f", {round(float(protein), 1)}% protein" if protein else ""
            lines.append(f"{i}. {title} [{rtype}{time_str}{protein_str}]")

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

    else:
        for i, row in enumerate(rows[:max_items], 1):
            lines.append(f"{i}. {row}")

    return "\n".join(lines)
