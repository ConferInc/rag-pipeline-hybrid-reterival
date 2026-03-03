"""
Profile-based entity enrichment.

Merges a logged-in customer's stored profile (diets, allergens, health conditions)
into the entities dict that was produced by the intent extractor.  This runs after
intent extraction so the Cypher generator and prompt builder always receive the full
personalised picture — the user never has to repeat their constraints in every query.
"""

from __future__ import annotations

import sys
import os
from typing import Any

# _HEALTH_TO_DIET_MAP lives in extractor_classifier.py at the repo root.
# Insert the root so we can import it without circular deps.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from extractor_classifier import _HEALTH_TO_DIET_MAP  # noqa: E402


def _health_conditions_to_diets(conditions: list[str]) -> list[str]:
    """
    Map a list of stored health-condition names (as they appear in
    B2C_Customer_Health_Conditions nodes) to diet labels understood by the
    Cypher generator (e.g. "Low-Carb", "Gluten-Free").

    Uses longest-key-first matching so "Type 2 Diabetes" is matched before
    the substring "diabetes".  Returns a deduplicated list preserving order.
    """
    sorted_keys = sorted(_HEALTH_TO_DIET_MAP.keys(), key=len, reverse=True)
    seen: set[str] = set()
    result: list[str] = []
    for condition in conditions:
        condition_lower = condition.lower()
        for key in sorted_keys:
            if key in condition_lower:
                for diet in _HEALTH_TO_DIET_MAP[key]:
                    if diet not in seen:
                        seen.add(diet)
                        result.append(diet)
                break  # one condition → one map entry is enough
    return result


def merge_profile_into_entities(
    entities: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """
    Return a new entities dict enriched with the customer's stored profile.

    Rules (never overwrites, only adds):
    - profile["diets"]             → merged into entities["diet"]
    - profile["health_conditions"] → converted to diet labels via
                                     _HEALTH_TO_DIET_MAP, merged into entities["diet"]
    - profile["allergens"]         → merged into entities["exclude_ingredient"]
                                     (hard safety constraint — always enforced)

    Args:
        entities: Entities dict produced by the intent extractor (not mutated).
        profile:  Dict returned by fetch_customer_profile() — keys: diets,
                  allergens, health_conditions, health_goal, activity_level,
                  recent_recipes.

    Returns:
        New dict with profile constraints merged in.
    """
    result = dict(entities)

    # ── 1. Merge stored diet preferences ─────────────────────────────────────
    profile_diets: list[str] = list(profile.get("diets") or [])

    # ── 2. Convert health conditions → diet labels ────────────────────────────
    condition_diets = _health_conditions_to_diets(
        list(profile.get("health_conditions") or [])
    )

    # Union: query-extracted diets + profile diets + condition-derived diets
    existing_diets: list[str] = result.get("diet") or []
    if not isinstance(existing_diets, list):
        existing_diets = [existing_diets] if existing_diets else []

    seen_diets: set[str] = {d.lower() for d in existing_diets}
    merged_diets: list[str] = list(existing_diets)

    for diet in profile_diets + condition_diets:
        if diet.lower() not in seen_diets:
            seen_diets.add(diet.lower())
            merged_diets.append(diet)

    if merged_diets:
        result["diet"] = merged_diets

    # ── 3. Merge allergens → exclude_ingredient (safety constraint) ───────────
    profile_allergens: list[str] = list(profile.get("allergens") or [])
    if profile_allergens:
        existing_excl: list[str] = result.get("exclude_ingredient") or []
        if not isinstance(existing_excl, list):
            existing_excl = [existing_excl] if existing_excl else []

        seen_excl: set[str] = {a.lower() for a in existing_excl}
        merged_excl: list[str] = list(existing_excl)

        for allergen in profile_allergens:
            if allergen.lower() not in seen_excl:
                seen_excl.add(allergen.lower())
                merged_excl.append(allergen)

        result["exclude_ingredient"] = merged_excl

    return result
