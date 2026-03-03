"""
Two-tier NLU to minimize LLM costs:
- Tier 1: Regex/keyword patterns — instant, zero cost
- Tier 2: LLM extraction (extractor_classifier) — 200-500ms, costs tokens

WHY HYBRID:
At scale, calling the LLM for every "hi" or "show my meal plan" wastes money.
Simple intents can be matched with regex. Only ambiguous/complex queries
need the LLM. This approach handles ~60% of messages without any LLM call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from extractor_classifier import extract_intent, sanity_check


@dataclass
class NLUResult:
    """Intent and entity extraction result."""
    intent: str
    entities: dict[str, Any]
    source: str  # "rules" | "llm" | "fallback" — for debugging and cost tracking


# Minimal maps for rule-based entity extraction (chatbot-specific)
_DIET_KEYWORDS: dict[str, str] = {
    "vegan": "Vegan",
    "vegetarian": "Vegetarian",
    "keto": "Keto",
    "ketogenic": "Keto",
    "paleo": "Paleo",
    "gluten-free": "Gluten-Free",
    "gluten free": "Gluten-Free",
    "dairy-free": "Dairy-Free",
    "dairy free": "Dairy-Free",
    "nut-free": "Nut-Free",
    "nut free": "Nut-Free",
    "high-protein": "High-Protein",
    "high protein": "High-Protein",
    "low-fat": "Low-Fat",
    "low fat": "Low-Fat",
    "low-carb": "Low-Carb",
    "low carb": "Low-Carb",
}

_COURSE_KEYWORDS: dict[str, str] = {
    "breakfast": "breakfast",
    "lunch": "lunch",
    "dinner": "dinner",
    "dessert": "dessert",
    "snack": "snack",
    "snacks": "snack",
    "tonight": "dinner",
    "this morning": "breakfast",
    "noon": "lunch",
}

# Order matters: more specific patterns first
RULE_PATTERNS: dict[str, str] = {
    # Conversational (no entities)
    "greeting": r"^(hi|hello|hey|good morning|good evening|good afternoon|sup|yo|howdy)\b",
    "help": r"^(what can you do|how do i|help|what are you)\b",
    "farewell": r"^(bye|goodbye|thanks|thank you|thx|see you|later)\b",
    # Meal plan & logging
    "show_meal_plan": r"(show|view|see|what'?s|display)\b.*(meal plan|my plan|this week'?s plan)",
    "plan_meals": r"(plan|create|generate|make|draw|schedule)\b.*(meal|week|menu|eating|diet)",
    "log_meal": r"(i (had|ate|eaten|just)|log|record|track)\b.*(breakfast|lunch|dinner|snack|meal)",
    "meal_history": r"(what did i eat|what i ate|ate today|my meals today|what have i eaten)",
    "swap_meal": r"(swap|replace|change|switch)\b.*(dinner|lunch|breakfast|tonight|today)",
    # Nutrition
    "nutrition_summary": r"(how('?s| is) my nutrition|my intake|protein|calories)\b.*(doing|week|today|summary)?",
    # Recipe search (existing intents, for quick rule match when entities extractable)
    "find_recipe": r"(find|show|search|give me|suggest|recommend|get me)\b.*(recipe|meal|dish|food|dinner|lunch|breakfast)",
    "find_recipe_by_pantry": r"(what can i|cook with|make with|i have)\b.*(fridge|pantry|ingredients?)",
    # Grocery & preferences
    "grocery_list": r"(grocery|shopping|buy|shop)\b.*(list|items)",
    "set_preference": r"(i('?m| am) now|switch to|change to|set my diet)\b.*(keto|vegan|vegetarian|paleo|gluten)",
    # Out of domain
    "out_of_scope": r"\b(weather|news|stock|joke|code|program|politics|sports)\b",
}


def extract_hybrid(message: str, context: dict[str, Any] | None = None) -> NLUResult:
    """
    Try rule-based extraction first. If no match or entities can't be extracted,
    fall back to LLM-based extraction via extractor_classifier.

    Args:
        message: User message
        context: Optional context (e.g. session history) — reserved for future use

    Returns:
        NLUResult with intent, entities, and source ("rules" | "llm" | "fallback")
    """
    normalized = message.strip()
    if not normalized:
        return NLUResult(intent="unclear", entities={}, source="fallback")

    low = normalized.lower()

    # Tier 1: Rule-based matching
    for intent, pattern in RULE_PATTERNS.items():
        if re.search(pattern, low):
            entities = _extract_entities_by_rules(low, intent)
            if entities is not None:
                return NLUResult(intent=intent, entities=entities, source="rules")

    # Tier 2: LLM extraction (for complex/ambiguous queries)
    try:
        raw = extract_intent(message)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        check = sanity_check(parsed)
        if check is True:
            return NLUResult(
                intent=parsed["intent"],
                entities=parsed.get("entities", {}),
                source="llm",
            )
    except Exception:
        pass

    # Fallback: treat as generic recipe search
    return NLUResult(intent="find_recipe", entities={"dish": message}, source="fallback")


def _extract_entities_by_rules(message: str, intent: str) -> dict[str, Any] | None:
    """
    Extract entities using simple keyword matching.
    Returns None if entities can't be reliably extracted (signals LLM fallback).
    """
    # No entities needed
    if intent in (
        "greeting",
        "help",
        "farewell",
        "out_of_scope",
        "show_meal_plan",
        "nutrition_summary",
        "grocery_list",
    ):
        return {}

    # plan_meals: optional date/range — for now empty, LLM can enrich
    if intent == "plan_meals":
        return {}

    # meal_history: no structured entities
    if intent == "meal_history":
        return {}

    # swap_meal: try to extract meal_type
    if intent == "swap_meal":
        for kw, course in _COURSE_KEYWORDS.items():
            if kw in message:
                return {"meal_type": course}
        return {"meal_type": "dinner"}  # default for "swap tonight's dinner"

    # set_preference: try to extract diet
    if intent == "set_preference":
        for kw, diet in _DIET_KEYWORDS.items():
            if kw in message:
                return {"diet": [diet]}
        return {}

    # log_meal: try to extract meal_type; recipe is harder, return None for complex
    if intent == "log_meal":
        entities: dict[str, Any] = {}
        for kw, course in _COURSE_KEYWORDS.items():
            if kw in message:
                entities["meal_type"] = course
                break
        # If we found meal_type, try to get recipe from "I had X for lunch" pattern
        # Strip common prefixes to get dish name
        for prefix in ("i had ", "i ate ", "i just had ", "log ", "record "):
            if prefix in message:
                rest = message.split(prefix, 1)[-1]
                for suffix in (" for breakfast", " for lunch", " for dinner", " for snack"):
                    rest = rest.replace(suffix, "")
                rest = rest.strip()
                if len(rest) > 2:
                    entities["recipe_reference"] = rest
                break
        return entities if entities else None

    # find_recipe: try diet + course
    if intent == "find_recipe":
        entities = {}
        for kw, diet in _DIET_KEYWORDS.items():
            if kw in message:
                entities.setdefault("diet", []).append(diet)
        for kw, course in _COURSE_KEYWORDS.items():
            if kw in message:
                entities["course"] = course
                break
        # If we found something useful, return; else fall to LLM for dish/query
        if entities:
            return entities
        # No diet/course — use full message as dish for simple "find pasta"
        return {"dish": message}

    # find_recipe_by_pantry: needs ingredients — return None for LLM
    if intent == "find_recipe_by_pantry":
        return None

    return None
