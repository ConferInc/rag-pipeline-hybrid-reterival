"""Entity enrichment: add missing entities from query keywords (config-driven heuristics)."""

from __future__ import annotations

import re
from typing import Any


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

    return result
