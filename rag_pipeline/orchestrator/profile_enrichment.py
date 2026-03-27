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

# Soft buffer on per-meal cap derived from daily target (aligns with B2C-003 gap analysis).
_CAL_UPPER_LIMIT_BUFFER = 1.1
_DEFAULT_MEALS_PER_DAY = 3


def _normalize_recipe_id_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    out: list[str] = []
    for x in raw:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def _merge_recipe_id_lists(*parts: Any) -> list[str]:
    """Union ID lists; dedupe case-insensitively; first occurrence wins."""
    seen: set[str] = set()
    merged: list[str] = []
    for part in parts:
        for rid in _normalize_recipe_id_list(part):
            key = rid.lower()
            if key not in seen:
                seen.add(key)
                merged.append(rid)
    return merged


def _derive_cal_upper_limit_from_daily(
    daily_calories: Any,
    *,
    meals_per_day: Any = None,
) -> int | None:
    """
    Map daily calorie target to a per-recipe cap for Cypher / hard filters.

    Uses per_meal = daily / meals_per_day (default 3), then × buffer (10%).
    Returns None if daily is missing or not positive.
    """
    try:
        daily = float(daily_calories)
    except (TypeError, ValueError):
        return None
    if daily <= 0:
        return None
    m = meals_per_day
    try:
        n = int(m) if m is not None else _DEFAULT_MEALS_PER_DAY
    except (TypeError, ValueError):
        n = _DEFAULT_MEALS_PER_DAY
    n = max(1, n)
    per_meal = daily / float(n)
    return int(round(per_meal * _CAL_UPPER_LIMIT_BUFFER))


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
    - profile["context"] (B2C)     → meal slot, cuisine, targets, etc.; see PRD-33.
      recentMealIds union with existing exclude_recipe_ids; targetCalories sets
      calorie_target and derives cal_upper_limit when not already set from the query.

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

    # ── 4. Consume context (PRD-33) ─────────────────────────────────────────────
    context = profile.get("context") or {}
    if context:
        if context.get("cuisinePreferences"):
            result["cuisine_preference"] = context["cuisinePreferences"]
        if context.get("country") is not None and str(context["country"]).strip():
            result["region"] = context["country"]
        if context.get("state") is not None and str(context["state"]).strip():
            result["sub_region"] = context["state"]
        if context.get("mealTimeSlot"):
            result["meal_time"] = context["mealTimeSlot"]
            if not result.get("course"):
                _MT_TO_COURSE = {
                    "morning": "breakfast",
                    "afternoon": "lunch",
                    "evening": "dinner",
                    "late_night": "snack",
                }
                result["course"] = _MT_TO_COURSE.get(
                    context["mealTimeSlot"], context["mealTimeSlot"]
                )
        if context.get("season"):
            result["season"] = context["season"]
        if context.get("targetCalories") is not None:
            result["calorie_target"] = context["targetCalories"]
            # Derive per-recipe cap for Cypher / apply_hard_constraints when the user
            # did not ask for an explicit cal limit in the query (cal_upper_limit).
            if result.get("cal_upper_limit") is None:
                meals_raw = (
                    context.get("mealsPerDay")
                    or context.get("meals_per_day")
                    or profile.get("meals_per_day")
                )
                cap = _derive_cal_upper_limit_from_daily(
                    context["targetCalories"],
                    meals_per_day=meals_raw,
                )
                if cap is not None:
                    result["cal_upper_limit"] = cap
        if context.get("targetProteinG") is not None:
            result["protein_target_g"] = context["targetProteinG"]
        if "recentMealIds" in context:
            merged_ids = _merge_recipe_id_lists(
                result.get("exclude_recipe_ids"),
                context["recentMealIds"],
            )
            if merged_ids:
                result["exclude_recipe_ids"] = merged_ids

    return result
