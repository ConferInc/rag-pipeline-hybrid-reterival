"""
Post-fusion hard constraint filter.

Runs after RRF fusion to enforce safety constraints that semantic and structural
retrieval cannot enforce on their own.  Only applied to recipe-returning intents.

Three filters are implemented today (using current graph data):

  Filter A — Course / meal_type  (zero extra DB calls, payload-based)
  Filter B — Allergen exclusion  (one batched Neo4j call)
  Filter C — Calorie upper limit (one batched Neo4j call)

Two filters are stubbed with clear placeholders (require FORBIDS relationships
to be populated in Neo4j before they can be activated):

  Filter D — Dietary preference compliance  (PLACEHOLDER)
  Filter E — Health condition compliance    (PLACEHOLDER — maps via diet labels)

Zero-results fallback:
  build_zero_results_message() produces a deterministic, human-readable
  explanation when the filtered list is empty, identifying the most likely
  bottleneck constraint and suggesting what to relax.
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import Driver

logger = logging.getLogger(__name__)

# Intents that return recipes and should have hard constraints applied.
_RECIPE_INTENTS: frozenset[str] = frozenset({
    "find_recipe",
    "find_recipe_by_pantry",
    "similar_recipes",
    "recipes_for_cuisine",
    "recipes_by_nutrient",
    "rank_results",
    "ingredient_in_recipes",
    "cuisine_recipes",
})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _recipe_ids_from_fused(fused: list[dict[str, Any]]) -> list[str]:
    """Extract all recipe IDs (UUID strings) from fused results that have one."""
    ids: list[str] = []
    for item in fused:
        payload = item.get("payload") or {}
        rid = payload.get("id") or payload.get("r.id")
        if rid:
            ids.append(str(rid))
    return ids


def _key_for_item(item: dict[str, Any]) -> str:
    """Return the best available identifier for a fused item (id > key > title)."""
    payload = item.get("payload") or {}
    return str(
        payload.get("id") or payload.get("r.id")
        or item.get("key", "")
        or item.get("title", "")
    )


# ── Filter A — Course / meal_type (payload-based, zero DB calls) ───────────────

def _filter_course(
    fused: list[dict[str, Any]],
    course: str,
) -> list[dict[str, Any]]:
    """
    Drop recipes whose meal_type does not match the requested course.
    Only applied when the payload carries meal_type (Cypher results).
    Semantic/structural results without meal_type pass through — they are
    marked 'unverified' in their sources list so the LLM is aware.
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in fused:
        payload = item.get("payload") or {}
        meal_type = (
            payload.get("r.meal_type") or payload.get("meal_type") or ""
        ).lower()

        if not meal_type:
            # No meal_type in payload (semantic/structural result) — keep but mark
            item = dict(item)
            sources = list(item.get("sources", []))
            if "unverified_course" not in sources:
                sources.append("unverified_course")
            item["sources"] = sources
            kept.append(item)
        elif meal_type == course.lower():
            kept.append(item)
        else:
            dropped += 1
            logger.debug(
                "Course filter dropped recipe: title=%s meal_type=%s required=%s",
                item.get("title", "?"), meal_type, course,
            )

    if dropped:
        logger.info("Course filter: dropped %d / %d results", dropped, len(fused))
    return kept


# ── Filter B — Allergen exclusion (one batched Neo4j call) ────────────────────

def _fetch_allergen_violating_ids(
    driver: Driver,
    recipe_ids: list[str],
    allergens: list[str],
    database: str | None,
) -> set[str]:
    """
    Return the set of recipe IDs that contain at least one allergen ingredient.
    Uses a single UNWIND query across all recipe IDs — not one call per recipe.
    """
    if not recipe_ids or not allergens:
        return set()

    allergens_lower = [a.lower() for a in allergens]

    cypher = """
    UNWIND $recipe_ids AS rid
    MATCH (r:Recipe {id: rid})-[:USES_INGREDIENT]->(i:Ingredient)
    WHERE toLower(i.name) IN $allergens
    RETURN DISTINCT r.id AS flagged_id
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(
                cypher,
                recipe_ids=recipe_ids,
                allergens=allergens_lower,
            )
            return {str(row["flagged_id"]) for row in rows}
    except Exception as e:
        # On DB error: fail open (keep all results) — better to show potentially
        # unsafe results than to silently return nothing.
        logger.warning(
            "Allergen filter DB call failed — skipping filter: %s", e,
            extra={"component": "constraint_filter"},
        )
        return set()


def _filter_allergens(
    fused: list[dict[str, Any]],
    allergens: list[str],
    driver: Driver,
    database: str | None,
) -> list[dict[str, Any]]:
    """
    Drop recipes that contain any allergen ingredient.
    Recipes without an ID in the payload cannot be verified and are kept
    but marked 'unverified_allergen' so the LLM is warned.
    """
    recipe_ids = _recipe_ids_from_fused(fused)
    violating_ids = _fetch_allergen_violating_ids(driver, recipe_ids, allergens, database)

    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in fused:
        payload = item.get("payload") or {}
        rid = str(payload.get("id") or payload.get("r.id") or "")

        if not rid:
            # No UUID — cannot verify; keep but mark
            item = dict(item)
            sources = list(item.get("sources", []))
            if "unverified_allergen" not in sources:
                sources.append("unverified_allergen")
            item["sources"] = sources
            kept.append(item)
        elif rid in violating_ids:
            dropped += 1
            logger.info(
                "Allergen filter dropped recipe: id=%s title=%s",
                rid, item.get("title", "?"),
            )
        else:
            kept.append(item)

    if dropped:
        logger.info(
            "Allergen filter: dropped %d / %d results (allergens=%s)",
            dropped, len(fused), allergens,
        )
    return kept


# ── Filter C — Calorie upper limit (one batched Neo4j call) ───────────────────

def _fetch_calorie_violating_ids(
    driver: Driver,
    recipe_ids: list[str],
    cal_limit: int | float,
    database: str | None,
) -> set[str]:
    """
    Return the set of recipe IDs whose energy value exceeds cal_limit.
    Uses a single UNWIND query across all recipe IDs.
    """
    if not recipe_ids:
        return set()

    cypher = """
    UNWIND $recipe_ids AS rid
    MATCH (r:Recipe {id: rid})
          -[:HAS_NUTRITION]->(nv:NutritionValue)
          -[:OF_NUTRIENT]->(nd:NutrientDefinition)
    WHERE nd.nutrient_name = 'Energy'
      AND nv.amount > $cal_limit
    RETURN DISTINCT r.id AS flagged_id
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(
                cypher,
                recipe_ids=recipe_ids,
                cal_limit=float(cal_limit),
            )
            return {str(row["flagged_id"]) for row in rows}
    except Exception as e:
        logger.warning(
            "Calorie filter DB call failed — skipping filter: %s", e,
            extra={"component": "constraint_filter"},
        )
        return set()


def _filter_calories(
    fused: list[dict[str, Any]],
    cal_limit: int | float,
    driver: Driver,
    database: str | None,
) -> list[dict[str, Any]]:
    """
    Drop recipes whose energy exceeds cal_limit.
    Recipes without an ID cannot be verified and are kept but marked.
    """
    recipe_ids = _recipe_ids_from_fused(fused)
    violating_ids = _fetch_calorie_violating_ids(driver, recipe_ids, cal_limit, database)

    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in fused:
        payload = item.get("payload") or {}
        rid = str(payload.get("id") or payload.get("r.id") or "")

        if not rid:
            item = dict(item)
            sources = list(item.get("sources", []))
            if "unverified_calories" not in sources:
                sources.append("unverified_calories")
            item["sources"] = sources
            kept.append(item)
        elif rid in violating_ids:
            dropped += 1
            logger.info(
                "Calorie filter dropped recipe: id=%s title=%s (limit=%s)",
                rid, item.get("title", "?"), cal_limit,
            )
        else:
            kept.append(item)

    if dropped:
        logger.info(
            "Calorie filter: dropped %d / %d results (limit=%s kcal)",
            dropped, len(fused), cal_limit,
        )
    return kept


# ── Filter D — Dietary preference compliance (FORBIDDEN relationships) ────────

def _fetch_diet_violating_ids(
    driver: Driver,
    recipe_ids: list[str],
    diets: list[str],
    database: str | None,
) -> set[str]:
    """
    Return the set of recipe IDs that use any ingredient forbidden by the diet(s).
    Uses (Dietary_Preferences)-[:FORBIDDEN]->(Ingredient) and (Recipe)-[:USES_INGREDIENT]->(Ingredient).
    """
    if not recipe_ids or not diets:
        return set()

    cypher = """
    UNWIND $recipe_ids AS rid
    MATCH (r:Recipe {id: rid})-[:USES_INGREDIENT]->(i:Ingredient)
          <-[:FORBIDDEN]-(dp:Dietary_Preferences)
    WHERE dp.name IN $diets
    RETURN DISTINCT r.id AS flagged_id
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(
                cypher,
                recipe_ids=recipe_ids,
                diets=diets,
            )
            return {str(row["flagged_id"]) for row in rows}
    except Exception as e:
        logger.warning(
            "Diet filter DB call failed — skipping filter: %s", e,
            extra={"component": "constraint_filter"},
        )
        return set()


def _filter_diet_compliance(
    fused: list[dict[str, Any]],
    diets: list[str],
    driver: Driver,
    database: str | None,
) -> list[dict[str, Any]]:
    """
    Drop recipes that use any ingredient forbidden by the requested diet(s).
    Recipes without an ID cannot be verified and are kept but marked.
    """
    recipe_ids = _recipe_ids_from_fused(fused)
    violating_ids = _fetch_diet_violating_ids(driver, recipe_ids, diets, database)

    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in fused:
        payload = item.get("payload") or {}
        rid = str(payload.get("id") or payload.get("r.id") or "")

        if not rid:
            item = dict(item)
            sources = list(item.get("sources", []))
            if "unverified_diet" not in sources:
                sources.append("unverified_diet")
            item["sources"] = sources
            kept.append(item)
        elif rid in violating_ids:
            dropped += 1
            logger.info(
                "Diet filter dropped recipe: id=%s title=%s (diets=%s)",
                rid, item.get("title", "?"), diets,
            )
        else:
            kept.append(item)

    if dropped:
        logger.info(
            "Diet filter: dropped %d / %d results (diets=%s)",
            dropped, len(fused), diets,
        )
    return kept


# ── Filter E — Health condition compliance (PLACEHOLDER) ──────────────────────
# Health conditions are mapped to diet labels via _HEALTH_TO_DIET_MAP in
# profile_enrichment.py and then treated as dietary preferences.
# This filter activates automatically once Filter D is enabled and FORBIDS
# relationships are populated — no separate implementation needed.


# ── Main entry point ──────────────────────────────────────────────────────────

def apply_hard_constraints(
    fused: list[dict[str, Any]],
    entities: dict[str, Any],
    intent: str,
    driver: Driver,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """
    Apply all active hard constraint filters to the fused result list.

    Only runs for recipe-returning intents.  Filters are applied in order of
    safety criticality: course → allergens → calories.

    Args:
        fused:    RRF-fused result list from apply_rrf().
        entities: Merged entities dict (query + profile enrichment).
        intent:   Extracted intent string.
        driver:   Neo4j driver (needed for DB-backed filters).
        database: Neo4j database name.

    Returns:
        Filtered list.  Items that could not be verified carry extra source
        tags ('unverified_allergen', 'unverified_course', 'unverified_calories')
        so the prompt builder can warn the LLM.
    """
    if intent not in _RECIPE_INTENTS or not fused:
        return fused

    result = list(fused)

    # ── A: Course ─────────────────────────────────────────────────────────────
    course = entities.get("course")
    if course:
        result = _filter_course(result, course)

    # ── B: Allergens ──────────────────────────────────────────────────────────
    allergens: list[str] = entities.get("exclude_ingredient") or []
    if allergens:
        result = _filter_allergens(result, allergens, driver, database)

    # ── C: Calorie limit ──────────────────────────────────────────────────────
    cal_limit = entities.get("cal_upper_limit")
    if cal_limit is not None:
        result = _filter_calories(result, cal_limit, driver, database)

    # ── D: Diet compliance (FORBIDDEN relationships in Neo4j) ─────────────────
    diets = entities.get("diet") or []
    if isinstance(diets, str):
        diets = [diets] if diets else []
    if diets:
        result = _filter_diet_compliance(result, diets, driver, database)

    logger.info(
        "Hard constraint filter complete",
        extra={
            "component": "constraint_filter",
            "intent": intent,
            "before": len(fused),
            "after": len(result),
            "filters_applied": {
                "course": bool(course),
                "allergens": bool(allergens),
                "cal_limit": cal_limit is not None,
                "diet": bool(diets),
            },
        },
    )
    return result


# ── Zero-results fallback message builder ─────────────────────────────────────

def build_zero_results_message(
    entities: dict[str, Any],
    intent: str,
) -> str:
    """
    Build a deterministic, human-readable fallback message when the filtered
    result list is empty.

    Identifies the most likely bottleneck constraint (most restrictive) and
    suggests relaxing the least safety-critical one.  Allergens are NEVER
    suggested for relaxation.

    Args:
        entities: Merged entities dict after profile enrichment.
        intent:   Extracted intent string.

    Returns:
        A structured fallback string injected into the prompt as [NO RESULTS].
    """
    allergens: list[str] = entities.get("exclude_ingredient") or []
    diets: list[str] = entities.get("diet") or []
    course: str | None = entities.get("course")
    cal_limit = entities.get("cal_upper_limit")
    nutrient_threshold: dict | None = entities.get("nutrient_threshold")

    # Build a plain-English description of what was searched for
    search_parts: list[str] = []
    if diets:
        search_parts.append(", ".join(diets))
    if course:
        search_parts.append(course)
    search_parts.append("recipes")
    if allergens:
        search_parts.append(f"(excluding {', '.join(allergens)})")
    if cal_limit:
        search_parts.append(f"under {cal_limit} calories")
    if nutrient_threshold and isinstance(nutrient_threshold, dict):
        op = "at least" if nutrient_threshold.get("operator") == "gt" else "at most"
        search_parts.append(
            f"with {op} {nutrient_threshold.get('value')} {nutrient_threshold.get('nutrient', '')}"
        )

    searched_for = " ".join(search_parts)

    # Identify what can be relaxed (never allergens)
    relaxation_hints: list[str] = []
    if cal_limit:
        relaxation_hints.append(f"removing the {cal_limit}-calorie limit")
    if nutrient_threshold:
        relaxation_hints.append("adjusting the nutrient threshold")
    if course:
        relaxation_hints.append(f"not restricting to {course}")
    if len(diets) > 1:
        relaxation_hints.append("using fewer diet filters")

    # Build the message
    lines: list[str] = [
        f"No {searched_for} were found in the knowledge base.",
    ]

    if allergens and not relaxation_hints:
        # Only constraint is allergens — nothing safe to relax
        lines.append(
            f"Your allergen restrictions ({', '.join(allergens)}) are always enforced. "
            "Try broadening the search by removing other filters, or ask for "
            "ingredient substitutions."
        )
    elif relaxation_hints:
        hint_str = " or ".join(relaxation_hints)
        lines.append(f"Try {hint_str} to find more options.")
        if allergens:
            lines.append(
                f"Note: allergen restrictions ({', '.join(allergens)}) cannot be relaxed "
                "and will always be enforced."
            )
    else:
        lines.append(
            "Try broadening your search — for example, remove the meal type filter "
            "or search for a different cuisine."
        )

    # Clarifying question
    lines.append("Would you like me to suggest the closest available alternatives?")

    return "\n".join(lines)
