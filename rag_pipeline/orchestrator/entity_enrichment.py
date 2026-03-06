"""Entity enrichment: add missing entities from query keywords (config-driven heuristics)."""

from __future__ import annotations

import re
from typing import Any


def _extract_exclude_ingredients_from_query(query_lower: str) -> list[str]:
    """
    Extract exclude_ingredient from patterns like "without X", "no X", "avoid X",
    "allergic to X". Used to enrich entities when LLM omits them.
    """
    found: list[str] = []
    seen: set[str] = set()
    diet_words = {"vegan", "vegetarian", "keto", "paleo", "gluten", "dairy", "nut", "protein", "fat", "carb"}

    def _add(ing: str) -> None:
        ing = ing.strip().rstrip(".,?!")
        if ing and len(ing) > 1 and ing not in seen:
            if any(d in ing for d in diet_words):
                return
            seen.add(ing)
            found.append(ing)

    patterns = [
        (r"\bwithout\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", 1, 2),
        (r"\bno\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", 1, 2),
        (r"\bavoid(?:ing)?\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", 1, 2),
        (r"\ballerg(?:ic|y)\s+to\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", 1, 2),
        (r"\b(?:don't|dont|do\s+not)\s+want\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", 1, 2),
        (r"\bfree\s+of\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", 1, 2),
        (r"\bexcluding\s+([a-z][\w\s\-]{1,25}?)(?:\s+(?:and|or)\s+([a-z][\w\s\-]{1,25}?))?(?:\s|$|\?|,)", 1, 2),
    ]
    for pat, g1, g2 in patterns:
        for m in re.finditer(pat, query_lower):
            _add(m.group(g1))
            if g2 and m.lastindex >= g2 and m.group(g2):
                _add(m.group(g2))
    return found


def enrich_entities(
    raw_query: str,
    entities: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """
    Add missing entities inferred from query keywords using config mappings.

    Only adds when a keyword is in the query and the corresponding entity is
    missing or empty. Does not overwrite existing values.

    Args:
        raw_query: User query text
        entities: Parsed entities from extractor
        config: intent_extraction (entity_enrichment_enabled and entity_fallbacks) from embedding_config.yaml

    Returns:
        New dict with enriched entities (does not mutate input)
    """
    result = dict(entities)
    if not config.get("entity_enrichment_enabled", False):
        return result
    fallbacks = config.get("entity_fallbacks") or {}
    if not fallbacks:
        return result

    query_lower = raw_query.lower()

    # Diet keywords -> entities["diet"]
    diet_map: dict[str, list[str]] = fallbacks.get("diet_keywords") or {}
    if diet_map:
        current_diet = result.get("diet") or []
        if not isinstance(current_diet, list):
            current_diet = [current_diet] if current_diet else []
        existing = {d.lower() for d in current_diet}
        for keyword, diets in diet_map.items():
            if keyword in query_lower:
                for d in diets:
                    if d.lower() not in existing:
                        current_diet.append(d)
                        existing.add(d.lower())
        if current_diet:
            result["diet"] = current_diet

    # Course keywords -> entities["course"]
    course_map: dict[str, str] = fallbacks.get("course_keywords") or {}
    if course_map and not result.get("course"):
        for keyword, course_val in course_map.items():
            if re.search(rf"\b{re.escape(keyword)}\b", query_lower):
                result["course"] = course_val
                break

    # Exclude ingredients: "without X", "no X", "avoid X", "allergic to X"
    exclude_from_query = _extract_exclude_ingredients_from_query(query_lower)
    if exclude_from_query:
        current = result.get("exclude_ingredient") or []
        if not isinstance(current, list):
            current = [current] if current else []
        existing_lower = {str(x).lower() for x in current}
        for ing in exclude_from_query:
            if ing.lower() not in existing_lower:
                current.append(ing)
                existing_lower.add(ing.lower())
        result["exclude_ingredient"] = current

    return result
