"""
Two-tier NLU to minimize LLM costs:
- Tier 1: Regex/keyword patterns — instant, zero cost
- Tier 2: LLM extraction (extractor_classifier) — 200-500ms, costs tokens

B2B: Entity-only LLM fallback when rules miss allergens/diets/conditions.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from entity_codes import (
    ALLERGEN_KEYWORDS,
    ALLOWED_ALLERGENS,
    CONDITION_KEYWORDS,
    DIET_KEYWORDS,
    normalize_to_allergen,
    normalize_to_condition,
    normalize_to_diet,
)
from extractor_classifier import extract_intent, sanity_check

logger = logging.getLogger(__name__)


@dataclass
class NLUResult:
    """Intent and entity extraction result."""
    intent: str
    entities: dict[str, Any]
    source: str  # "rules" | "llm" | "fallback" — for debugging and cost tracking


# B2C diet/course maps (Title-case for graph compatibility)
_DIET_KEYWORDS_B2C: dict[str, str] = {
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
    # Nutrition — user's OWN logged intake (not recipe search with "calories below X")
    "nutrition_summary": r"(how('?s| is) my nutrition|my intake|how am i doing|(my|this week'?s?)\s+(protein|calories|nutrition))\b",
    # Recipe search (existing intents, for quick rule match when entities extractable)
    "find_recipe": r"(find|show|search|give me|suggest|recommend|get me)\b.*(recipe|meal|dish|food|dinner|lunch|breakfast)",
    "find_recipe_by_pantry": r"(what can i|cook with|make with|i have)\b.*(fridge|pantry|ingredients?)",
    # Grocery & preferences
    "grocery_list": r"(grocery|shopping|buy|shop)\b.*(list|items)",
    "set_preference": r"(i('?m| am) now|switch to|change to|set my diet)\b.*(keto|vegan|vegetarian|paleo|gluten)",
    # Out of domain
    "out_of_scope": r"\b(weather|news|stock|joke|code|program|politics|sports)\b",
}

# ── B2B patterns (vendor-scoped product/customer queries) ─────────────────────

B2B_RULE_PATTERNS: dict[str, str] = {
    "b2b_products_for_condition": (
        r"(products?|items?|goods?)\b.*(for|with|having)\b.*(customer|client).*"
        r"(diabet|hypertens|cholesterol|celiac|kidney|heart|lactose|ibs|gout|gerd)"
    ),
    "b2b_products_allergen_free": (
        r"(products?|items?)\b.*(free from|without|allergen.?free|no )\b.*"
        r"(peanut|dairy|gluten|soy|egg|wheat|shellfish|tree.?nut|milk|lactose|nuts?)"
    ),
    "b2b_products_for_diet": (
        r"(products?|items?|list)\b.*(keto|vegan|vegetarian|gluten.?free|paleo|"
        r"low.?carb|low.?fat|high.?protein)"
    ),
    "b2b_customers_for_product": (
        r"(which|what|find|show)\b.*(customer|client).*"
        r"(recommend|suitable|safe|match).*(product|item)"
    ),
    "b2b_customers_with_condition": (
        r"(list|show|find|how many)\b.*(customer|client).*"
        r"(with|have|having)\b.*(diabet|allerg|hypertens|intoleran|celiac)"
    ),
    "b2b_customer_recommendations": (
        r"(recommend|suggest|what product).*(for|to)\b.*[A-Za-z]"
    ),
    "b2b_analytics": (
        r"(how many|count|percentage|stats?|analytics?)\b.*(customer|client|product)"
    ),
    "b2b_product_compliance": (
        r"(is|are|check|verify)\b.*(product|item)\b.*"
        r"(safe|compliant|suitable|ok)\b.*(for|with)"
    ),
    "b2b_product_nutrition": (
        r"(nutrition|nutritional|macros)\b.*(product|item)"
    ),
    "b2b_generate_report": (
        r"(generate|create)\b.*(report|summary|matrix)"
    ),
}

def _needs_entity_llm_fallback(intent: str, entities: dict[str, Any], message: str) -> bool:
    """
    Return True if rule extraction missed compliance entities and message suggests they exist.
    """
    low = message.lower()

    # Allergen-related intents: need allergens, query has allergy words
    if intent in ("b2b_products_allergen_free", "b2b_customers_with_condition", "b2b_product_compliance"):
        has_allergy_words = any(w in low for w in ("allergy", "allergic", "allergen", "intolerance", "free from", "without", "no "))
        rule_allergens = entities.get("allergens") or entities.get("exclude_ingredient") or []
        if has_allergy_words and not rule_allergens:
            return True

    # Condition-related intents
    if intent in ("b2b_products_for_condition", "b2b_customers_with_condition", "b2b_product_compliance"):
        has_condition_words = any(w in low for w in (
            "diabetic", "diabetes", "hypertension", "celiac", "kidney", "lactose",
            "ibs", "gout", "gerd", "heart", "condition", "disease", "cholesterol",
        ))
        rule_conditions = entities.get("health_conditions") or []
        if has_condition_words and not rule_conditions:
            return True

    # Diet-related intents
    if intent == "b2b_products_for_diet":
        has_diet_words = any(w in low for w in ("keto", "vegan", "vegetarian", "gluten", "paleo", "diet", "low carb", "high protein"))
        rule_diets = entities.get("diet") or []
        if has_diet_words and not rule_diets:
            return True

    # Product name for compliance/nutrition
    if intent in ("b2b_product_compliance", "b2b_product_nutrition"):
        rule_product = (entities.get("product_name") or "").strip()
        has_product_ref = any(w in low for w in ("product", "item", "for ", "of "))
        if has_product_ref and len(rule_product) < 3:
            return True

    # Customer name for recommendations
    if intent == "b2b_customer_recommendations":
        rule_customer = (entities.get("customer_name") or "").strip()
        has_for_to = " for " in low or " to " in low
        if has_for_to and len(rule_customer) < 2:
            return True

    return False


def _normalize_llm_entities(entities: dict[str, Any], intent: str) -> dict[str, Any]:
    """
    Normalize LLM output to canonical codes. Accepts any user phrasing
    (spaces, dashes, mixed case, punctuation) and maps to DB codes.
    """
    out: dict[str, Any] = {}
    for key, val in entities.items():
        if val is None or val == "":
            continue
        if key == "allergens":
            items = [val] if isinstance(val, str) else (val if isinstance(val, list) else [])
            seen: set[str] = set()
            for x in items:
                canonical = normalize_to_allergen(str(x))
                if canonical and canonical not in seen:
                    seen.add(canonical)
                    out.setdefault("allergens", []).append(canonical)
        elif key == "health_conditions":
            items = [val] if isinstance(val, str) else (val if isinstance(val, list) else [])
            seen = set()
            for x in items:
                canonical = normalize_to_condition(str(x))
                if canonical and canonical not in seen:
                    seen.add(canonical)
                    out.setdefault("health_conditions", []).append(canonical)
        elif key == "diet":
            items = [val] if isinstance(val, str) else (val if isinstance(val, list) else [])
            seen = set()
            for x in items:
                canonical = normalize_to_diet(str(x))
                if canonical and canonical not in seen:
                    seen.add(canonical)
                    out.setdefault("diet", []).append(canonical)
        elif key in ("product_name", "customer_name") and isinstance(val, str) and len(val.strip()) > 1:
            out[key] = val.strip()
        elif key == "exclude_ingredient" and isinstance(val, list):
            normalized_excl: list[str] = []
            seen = set()
            for x in val:
                if not x:
                    continue
                canonical = normalize_to_allergen(str(x))
                if canonical and canonical not in seen:
                    seen.add(canonical)
                    normalized_excl.append(canonical)
            if normalized_excl:
                out["exclude_ingredient"] = normalized_excl
    return out


def _merge_b2b_entities(rules_entities: dict[str, Any], llm_entities: dict[str, Any]) -> dict[str, Any]:
    """
    Merge rule and LLM entities. Rules take precedence; LLM fills gaps.
    """
    merged = dict(rules_entities)
    for key in ("allergens", "health_conditions", "diet", "exclude_ingredient"):
        rule_val = merged.get(key)
        llm_val = llm_entities.get(key)
        if not rule_val and llm_val:
            merged[key] = llm_val
        elif rule_val and llm_val:
            combined = list(rule_val) if isinstance(rule_val, list) else [rule_val]
            for x in (llm_val if isinstance(llm_val, list) else [llm_val]):
                if x and x not in combined:
                    combined.append(x)
            merged[key] = combined
    for key in ("product_name", "customer_name"):
        if not (merged.get(key) or "").strip() and (llm_entities.get(key) or "").strip():
            merged[key] = llm_entities[key].strip()
    return merged


def _extract_b2b_entities_by_rules(message: str, intent: str) -> dict[str, Any] | None:
    """
    Extract B2B entities from message using keyword matching.
    Returns None if entities can't be extracted (signals LLM fallback).
    """
    low = message.lower()

    # No entities needed
    if intent == "b2b_analytics":
        return {}

    if intent == "b2b_generate_report":
        return {}

    # Allergen-free products
    if intent == "b2b_products_allergen_free":
        allergens: list[str] = []
        seen: set[str] = set()
        for kw, code in ALLERGEN_KEYWORDS.items():
            if kw in low and code not in seen:
                seen.add(code)
                allergens.append(code)
        # Also try "free from X" / "without X"
        for m in re.finditer(
            r"(?:free from|without|no )\s+([a-z][a-z\s\-]{1,20}?)(?:\s|$|,|and|or)",
            low,
        ):
            word = m.group(1).strip()
            if word and len(word) > 2:
                code = normalize_to_allergen(word)
                if code and code not in seen:
                    seen.add(code)
                    allergens.append(code)
        return {"allergens": allergens} if allergens else {"allergens": []}

    # Products for condition / diet
    if intent == "b2b_products_for_condition":
        conditions: list[str] = []
        for kw, code in CONDITION_KEYWORDS.items():
            if kw in low and code not in conditions:
                conditions.append(code)
        return {"health_conditions": conditions} if conditions else {}

    if intent == "b2b_products_for_diet":
        diets: list[str] = []
        for kw, label in DIET_KEYWORDS.items():
            if kw in low and label not in diets:
                diets.append(label)
        return {"diet": diets} if diets else {"diet": []}

    # Customers with condition
    if intent == "b2b_customers_with_condition":
        conditions = []
        allergens = []
        for kw, code in CONDITION_KEYWORDS.items():
            if kw in low and code not in conditions:
                conditions.append(code)
        for kw, code in ALLERGEN_KEYWORDS.items():
            if kw in low and code not in allergens:
                allergens.append(code)
        ents: dict[str, Any] = {}
        if conditions:
            ents["health_conditions"] = conditions
        if allergens:
            ents["allergens"] = allergens
        return ents if ents else {}

    # Customer recommendations (named customer)
    if intent == "b2b_customer_recommendations":
        for m in re.finditer(
            r"(?:for|to)\s+([A-Za-z][a-z]*(?:\s+[A-Za-z][a-z]*)?)", message, re.IGNORECASE
        ):
            name = m.group(1).strip()
            if len(name) > 1:
                return {"customer_name": name}
        return {}

    # Product compliance / nutrition — product name may be in query
    if intent in ("b2b_product_compliance", "b2b_product_nutrition"):
        # Simple heuristic: after "product" or "item" take the rest as product ref
        for prefix in ("product ", "item ", "for ", "of "):
            if prefix in low:
                idx = low.rfind(prefix) + len(prefix)
                rest = message[idx:].strip().rstrip("?").strip()
                if len(rest) > 2:
                    return {"product_name": rest}
        return {}

    # Customers for product — no entities from query alone
    if intent == "b2b_customers_for_product":
        return {}

    return None


def extract_hybrid_b2b(message: str, context: dict[str, Any] | None = None) -> NLUResult:
    """
    B2B-specific NLU: try B2B rule patterns first, then entity-only LLM fallback
    when rules miss allergens/diets/conditions, then full LLM intent extraction.
    """
    from extractor_classifier import extract_entities_only_b2b, extract_intent_b2b, sanity_check_b2b

    normalized = message.strip()
    if not normalized:
        return NLUResult(intent="unclear", entities={}, source="fallback")

    low = normalized.lower()
    source = "rules"

    # Tier 1: B2B rule-based matching
    for intent, pattern in B2B_RULE_PATTERNS.items():
        if re.search(pattern, low):
            entities = _extract_b2b_entities_by_rules(low, intent)
            if entities is not None:
                # Entity-only LLM fallback when rules miss compliance entities
                if _needs_entity_llm_fallback(intent, entities, message):
                    try:
                        llm_ents = extract_entities_only_b2b(message, intent)
                        if llm_ents:
                            llm_ents = _normalize_llm_entities(llm_ents, intent)
                            if llm_ents:
                                entities = _merge_b2b_entities(entities, llm_ents)
                                source = "rules+llm"
                    except Exception as e:
                        logger.warning("B2B entity LLM fallback failed: %s", e)
                return NLUResult(intent=intent, entities=entities, source=source)

    # Tier 2: Full LLM extraction (intent + entities)
    try:
        raw = extract_intent_b2b(message)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        check = sanity_check_b2b(parsed)
        if check is True:
            entities = parsed.get("entities", {})
            intent = parsed["intent"]
            if intent in (
                "b2b_products_allergen_free",
                "b2b_products_for_condition",
                "b2b_products_for_diet",
                "b2b_customers_with_condition",
                "b2b_product_compliance",
            ):
                entities = _normalize_llm_entities(entities, intent)
            return NLUResult(intent=intent, entities=entities, source="llm")
    except Exception:
        pass

    # Fallback: treat as generic product search
    return NLUResult(
        intent="b2b_products_for_diet",
        entities={"diet": [], "dish": message},
        source="fallback",
    )


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
        for kw, diet in _DIET_KEYWORDS_B2C.items():
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
        for kw, diet in _DIET_KEYWORDS_B2C.items():
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
