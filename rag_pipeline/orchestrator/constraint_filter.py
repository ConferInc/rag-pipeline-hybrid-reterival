"""
Post-fusion hard constraint filter.

Runs after RRF fusion to enforce safety constraints that semantic and structural
retrieval cannot enforce on their own.  Only applied to recipe-returning intents.

Three filters are implemented today (using current graph data):

  Filter A — Course / meal_type  (zero extra DB calls, payload-based)
  Filter B — Allergen exclusion  (one batched Neo4j call)
  Filter C — Calorie upper limit (one batched Neo4j call)

Two filters are stubbed with clear placeholders (require FORBIDDEN relationships
to be populated in Neo4j before they can be activated):

  Filter D — Dietary preference compliance  (PLACEHOLDER)
  Filter E — Health condition compliance    (PLACEHOLDER — maps via diet labels)

Zero-results fallback:
  build_zero_results_message() produces a deterministic, human-readable
  explanation when the filtered list is empty, identifying the most likely
  bottleneck constraint and suggesting what to relax.
"""

from __future__ import annotations

import re
import logging
import os
from typing import Any

from neo4j import Driver

from rag_pipeline.nlu.intents import RECIPE_INTENTS

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _recipe_ids_from_fused(fused: list[dict[str, Any]]) -> list[str]:
    """
    Extract all recipe IDs from fused results.
    Checks payload.id, payload.r.id, nested payload, item.key (if UUID), connected_id.
    """
    _uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    ids: list[str] = []
    seen: set[str] = set()
    for item in fused:
        payload = item.get("payload") or {}
        nested = payload.get("payload") or {}
        rid = (
            payload.get("id")
            or payload.get("r.id")
            or nested.get("id")
            or nested.get("r.id")
            or (item.get("key") if item.get("key") and _uuid_re.match(str(item.get("key"))) else None)
            or payload.get("connected_id")
            or item.get("connected_id")
        )
        if rid:
            s = str(rid).strip()
            if s and s not in seen:
                seen.add(s)
                ids.append(s)
    return ids


def _key_for_item(item: dict[str, Any]) -> str:
    """Return the best available identifier for a fused item (id > key > title)."""
    payload = item.get("payload") or {}
    nested = payload.get("payload") or {}
    return str(
        payload.get("id")
        or payload.get("r.id")
        or nested.get("id")
        or nested.get("r.id")
        or item.get("key", "")
        or item.get("title", "")
    )


# ── Filter A — Course / meal_type (payload-based, zero DB calls) ───────────────

# Only these are valid meal_type values in the graph — if the NLU extracts something
# else (e.g. "soup", "salad", "sandwich"), it's a dish category, NOT a meal type,
# and the filter should be skipped to avoid dropping all results.
_VALID_MEAL_TYPES: set[str] = {"breakfast", "lunch", "dinner", "snack", "dessert"}

def _filter_course(
    fused: list[dict[str, Any]],
    course: str,
) -> list[dict[str, Any]]:
    """
    Drop recipes whose meal_type does not match the requested course.
    Uses canonical payload["meal_type"] only.

    IMPORTANT: Only filters when course is a valid meal_type (breakfast, lunch,
    dinner, snack, dessert). Non-standard values like "soup" or "curry" are
    dish categories — filtering on them drops all results because no recipe has
    meal_type="soup". In that case we skip the filter entirely.

    Semantic/structural results without meal_type pass through — they are
    marked 'unverified' in their sources list so the LLM is aware.
    Recipes with missing meal_type are KEPT (benefit of the doubt) rather than
    dropped, to avoid zero-result scenarios when graph data is incomplete.
    """
    course_lower = course.strip().lower()

    # Skip filter for non-standard course values (dish categories, not meal types)
    if course_lower not in _VALID_MEAL_TYPES:
        logger.info(
            "Course filter skipped: '%s' is not a valid meal_type (valid: %s)",
            course, ", ".join(sorted(_VALID_MEAL_TYPES)),
        )
        return fused

    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in fused:
        payload = item.get("payload") or {}
        meal_type_raw = payload.get("meal_type")
        meal_type = str(meal_type_raw).strip().lower() if meal_type_raw is not None else ""

        if not meal_type:
            # Keep recipes with missing meal_type (benefit of the doubt) but mark them
            item = dict(item)
            sources = list(item.get("sources", []))
            if "unverified_course" not in sources:
                sources.append("unverified_course")
            item["sources"] = sources
            kept.append(item)
        elif meal_type == course_lower:
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
    Return the set of recipe IDs that contain at least one allergen/exclude ingredient.
    Uses CONTAINS (not exact match) so "strawberries" matches "Strawberry" etc.
    """
    if not recipe_ids or not allergens:
        return set()

    allergens_lower = [a.lower() for a in allergens]

    # ANY(a IN $allergens WHERE toLower(i.name) CONTAINS a) — flexible matching
    cypher = """
    UNWIND $recipe_ids AS rid
    MATCH (r:Recipe {id: rid})-[:USES_INGREDIENT]->(i:Ingredient)
    WHERE ANY(a IN $allergens WHERE toLower(i.name) CONTAINS a)
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


def _expand_exclude_term_variants(term: str) -> set[str]:
    """
    Expand an exclude term into variants for matching (typos, plural/singular).
    E.g. "banannas" -> {"banannas", "bananna", "bananas", "banana"}
    """
    t = term.lower().strip()
    variants = {t}
    # Common typos
    _TYPO_MAP = {"bananna": "banana", "banannas": "banana", "strawberrys": "strawberries"}
    if t in _TYPO_MAP:
        variants.add(_TYPO_MAP[t])
    # Plural -> singular
    if t.endswith("ies") and len(t) > 3:
        variants.add(t[:-3] + "y")
    elif t.endswith("es") and len(t) > 2:
        variants.add(t[:-2])
    elif t.endswith("s") and len(t) > 1:
        variants.add(t[:-1])
    return variants


def _filter_exclude_by_title(
    fused: list[dict[str, Any]],
    exclude_terms: list[str],
) -> list[dict[str, Any]]:
    """
    Drop recipes whose title contains any exclude term (case-insensitive).
    Uses variant expansion (typos, plural/singular) so "banannas" catches "banana bread".
    """
    if not exclude_terms:
        return fused
    all_variants: set[str] = set()
    for t in exclude_terms:
        all_variants.update(_expand_exclude_term_variants(t))
    terms_to_check = [v for v in all_variants if len(v) >= 3]
    kept = []
    for item in fused:
        payload = item.get("payload") or {}
        nested = payload.get("payload") or {}
        title = (
            item.get("title")
            or payload.get("title")
            or payload.get("r.title")
            or nested.get("title")
            or nested.get("name")
        )
        if not title:
            kept.append(item)
            continue
        title_lower = str(title).lower()
        if any(term in title_lower for term in terms_to_check):
            logger.debug(
                "Exclude-by-title dropped: title=%s (exclude=%s)",
                title[:60], exclude_terms,
            )
            continue
        kept.append(item)
    return kept


def _filter_allergens(
    fused: list[dict[str, Any]],
    allergens: list[str],
    driver: Driver,
    database: str | None,
) -> list[dict[str, Any]]:
    """
    Drop recipes that contain any allergen/exclude ingredient.
    Uses (1) title with variant expansion (typos, plural), (2) graph CONTAINS match.
    """
    # Expand terms so "banannas" matches "banana", "bananas" matches "banana bread"
    expanded = list({v for t in allergens for v in _expand_exclude_term_variants(t)})

    # 1. Title-based exclusion first (no DB call)
    result = _filter_exclude_by_title(fused, expanded)

    # 2. Graph-based: drop recipes that use the ingredient (use expanded for CONTAINS)
    recipe_ids = _recipe_ids_from_fused(result)
    violating_ids = _fetch_allergen_violating_ids(driver, recipe_ids, expanded, database)

    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in result:
        payload = item.get("payload") or {}
        nested = payload.get("payload") or {}
        rid = str(
            payload.get("id")
            or payload.get("r.id")
            or nested.get("id")
            or nested.get("r.id")
            or ""
        )

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
            dropped, len(result), allergens,
        )
    return kept


# ── Filter C — Calorie upper limit ────────────────────────────────────────────
# Tries multiple Neo4j patterns. For strict mode, recipes with no calorie data
# in Neo4j are treated as violating (cannot verify they meet the limit).


def _fetch_calorie_violating_ids(
    driver: Driver,
    recipe_ids: list[str],
    cal_limit: int | float,
    database: str | None,
) -> set[str]:
    """
    Return recipe IDs that exceed cal_limit or lack verifiable calorie data.

    Uses (Recipe)-[:HAS_NUTRITION]->(NutritionValue)-[:OF_NUTRIENT]->(NutrientDefinition)
    where nutrient_name in common variants (Energy, Calories/Energy, Calories, etc.).
    Matches recipes by r.id (UUID) or elementId(r) for structural results.
    """
    if not recipe_ids:
        return set()

    _CALORIE_NUTRIENT_NAMES = [
        "Energy", "Calories/Energy", "Calories", "Energy (kcal)", "Energy, calories",
    ]
    cal_names_lc = [n.lower() for n in _CALORIE_NUTRIENT_NAMES]
    _uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    uuid_ids = [x for x in recipe_ids if x and _uuid_re.match(str(x))]
    elem_ids = [x for x in recipe_ids if x and ":" in str(x) and not _uuid_re.match(str(x))]

    violating: set[str] = set()
    verified_ok: set[str] = set()
    cal_f = float(cal_limit)
    recipe_set = set(recipe_ids)

    def _run_rows(q: str, params: dict) -> list[dict]:
        try:
            with driver.session(database=database) as session:
                rows = session.run(q, **params)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.debug("Calorie query failed: %s", e)
        return []

    cal_params = {"cal_limit": cal_f, "cal_names_lc": cal_names_lc}
    # 1. Exceeds via HAS_NUTRITION (match by r.id for UUIDs)
    if uuid_ids:
        q_exceed = """
        MATCH (r:Recipe)-[:HAS_NUTRITION]->(nv:NutritionValue)-[:OF_NUTRIENT]->(nd:NutrientDefinition)
        WHERE r.id IN $recipe_ids
          AND toLower(coalesce(nd.nutrient_name, nd.name, "")) IN $cal_names_lc
          AND toFloat(nv.amount) > $cal_limit
        RETURN r.id AS id, elementId(r) AS elem_id
        """
        for row in _run_rows(q_exceed, {**cal_params, "recipe_ids": uuid_ids}):
            if row.get("id"):
                violating.add(str(row["id"]))
            if row.get("elem_id"):
                violating.add(str(row["elem_id"]))

    # 2. Exceeds (match by elementId) — add both id and elem_id so either matches
    if elem_ids:
        q_exceed_elem = """
        UNWIND $elem_ids AS eid
        MATCH (r:Recipe)-[:HAS_NUTRITION]->(nv:NutritionValue)-[:OF_NUTRIENT]->(nd:NutrientDefinition)
        WHERE elementId(r) = eid
          AND toLower(coalesce(nd.nutrient_name, nd.name, "")) IN $cal_names_lc
          AND toFloat(nv.amount) > $cal_limit
        RETURN r.id AS id, elementId(r) AS elem_id
        """
        for row in _run_rows(q_exceed_elem, {**cal_params, "elem_ids": elem_ids}):
            if row.get("id"):
                violating.add(str(row["id"]))
            if row.get("elem_id"):
                violating.add(str(row["elem_id"]))

    # 3. Verified OK (match by r.id)
    if uuid_ids:
        q_ok = """
        MATCH (r:Recipe)-[:HAS_NUTRITION]->(nv:NutritionValue)-[:OF_NUTRIENT]->(nd:NutrientDefinition)
        WHERE r.id IN $recipe_ids
          AND toLower(coalesce(nd.nutrient_name, nd.name, "")) IN $cal_names_lc
          AND toFloat(nv.amount) <= $cal_limit
        RETURN r.id AS id, elementId(r) AS elem_id
        """
        for row in _run_rows(q_ok, {**cal_params, "recipe_ids": uuid_ids}):
            if row.get("id"):
                verified_ok.add(str(row["id"]))
            if row.get("elem_id"):
                verified_ok.add(str(row["elem_id"]))

    # 4. Verified OK (match by elementId)
    if elem_ids:
        q_ok_elem = """
        UNWIND $elem_ids AS eid
        MATCH (r:Recipe)-[:HAS_NUTRITION]->(nv:NutritionValue)-[:OF_NUTRIENT]->(nd:NutrientDefinition)
        WHERE elementId(r) = eid
          AND toLower(coalesce(nd.nutrient_name, nd.name, "")) IN $cal_names_lc
          AND toFloat(nv.amount) <= $cal_limit
        RETURN r.id AS id, elementId(r) AS elem_id
        """
        for row in _run_rows(q_ok_elem, {**cal_params, "elem_ids": elem_ids}):
            if row.get("id"):
                verified_ok.add(str(row["id"]))
            if row.get("elem_id"):
                verified_ok.add(str(row["elem_id"]))

    # Strict: recipes with no calorie data → violating (drop them)
    # Exception: if we matched zero recipes (verified_ok and violating both empty),
    # recipe IDs may not match graph format — keep results to avoid empty search.
    no_data = recipe_set - verified_ok - violating
    if no_data:
        if verified_ok or violating:
            # We matched some recipes; drop the unverifiable ones (strict)
            violating.update(no_data)
            logger.info(
                "Calorie filter: dropped %d recipes lacking calorie data (limit=%s)",
                len(no_data), cal_limit,
                extra={"component": "constraint_filter"},
            )
        else:
            # Queries matched nothing — possible ID format mismatch; keep all
            logger.warning(
                "Calorie filter: no recipes matched in Neo4j (verified=%d, violating=%d). "
                "Check Recipe.id format matches retrieval payload. Keeping results.",
                len(verified_ok), len(violating),
                extra={"component": "constraint_filter", "recipe_count": len(recipe_set)},
            )
    return violating


def _rid_for_item(item: dict[str, Any]) -> str:
    """Extract recipe ID from fused item (same logic as _recipe_ids_from_fused)."""
    payload = item.get("payload") or {}
    nested = payload.get("payload") or {}
    _uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    rid = (
        payload.get("id")
        or payload.get("r.id")
        or nested.get("id")
        or nested.get("r.id")
        or (item.get("key") if item.get("key") and _uuid_re.match(str(item.get("key"))) else None)
        or payload.get("connected_id")
        or item.get("connected_id")
    )
    return str(rid).strip() if rid else ""


def _filter_calories(
    fused: list[dict[str, Any]],
    cal_limit: int | float,
    driver: Driver,
    database: str | None,
) -> list[dict[str, Any]]:
    """
    Drop recipes whose energy exceeds cal_limit.
    Strict: recipes without an extractable ID are dropped (cannot verify they meet limit).
    """
    recipe_ids = _recipe_ids_from_fused(fused)
    violating_ids = _fetch_calorie_violating_ids(driver, recipe_ids, cal_limit, database)

    cal_f = float(cal_limit)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in fused:
        rid = _rid_for_item(item)

        if not rid:
            # No ID — cannot verify; drop for strict calorie filtering
            dropped += 1
            logger.debug(
                "Calorie filter dropped (no ID): title=%s (limit=%s)",
                item.get("title", "?"), cal_limit,
            )
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
            extra={
                "component": "constraint_filter",
                "counter": "calorie_filter_dropped_count",
                "value": dropped,
            },
        )
    return kept


# ── Filter D — Dietary preference compliance (FORBIDDEN relationships) ──────
# Schema: (Dietary_Preferences)-[:FORBIDDEN]->(Ingredient), (Recipe)-[:USES_INGREDIENT]->(Ingredient)

def _fetch_diet_violating_ids(
    driver: Driver,
    recipe_ids: list[str],
    diets: list[str],
    database: str | None,
) -> set[str]:
    """
    Return the set of recipe IDs that use any ingredient forbidden by the diet(s).
    Uses (Dietary_Preferences)-[:FORBIDDEN]->(Ingredient) and (Recipe)-[:USES_INGREDIENT]->(Ingredient).
    Supports both r.id (UUID) and elementId(r). Case-insensitive diet name match.
    """
    if not recipe_ids or not diets:
        return set()

    diets_norm = [d.strip() for d in diets if d and isinstance(d, str)]
    if not diets_norm:
        return set()

    _uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    uuid_ids = [x for x in recipe_ids if x and _uuid_re.match(str(x))]
    elem_ids = [x for x in recipe_ids if x and ":" in str(x) and not _uuid_re.match(str(x))]

    violating: set[str] = set()

    def _run(q: str, params: dict) -> list[dict]:
        try:
            with driver.session(database=database) as session:
                return [dict(row) for row in session.run(q, **params)]
        except Exception as e:
            logger.warning("Diet filter DB call failed: %s", e, extra={"component": "constraint_filter"})
        return []

    # Match by r.id (UUID)
    if uuid_ids:
        q = """
        UNWIND $recipe_ids AS rid
        MATCH (r:Recipe)-[:USES_INGREDIENT]->(i:Ingredient)<-[:FORBIDDEN]-(dp:Dietary_Preferences)
        WHERE r.id = rid AND toLower(trim(dp.name)) IN $diets_lower
        RETURN DISTINCT r.id AS id, elementId(r) AS elem_id
        """
        diets_lower = [d.lower() for d in diets_norm]
        for row in _run(q, {"recipe_ids": uuid_ids, "diets_lower": diets_lower}):
            if row.get("id"):
                violating.add(str(row["id"]))
            if row.get("elem_id"):
                violating.add(str(row["elem_id"]))

    # Match by elementId
    if elem_ids:
        q = """
        UNWIND $elem_ids AS eid
        MATCH (r:Recipe)-[:USES_INGREDIENT]->(i:Ingredient)<-[:FORBIDDEN]-(dp:Dietary_Preferences)
        WHERE elementId(r) = eid AND toLower(trim(dp.name)) IN $diets_lower
        RETURN DISTINCT r.id AS id, elementId(r) AS elem_id
        """
        diets_lower = [d.lower() for d in diets_norm]
        for row in _run(q, {"elem_ids": elem_ids, "diets_lower": diets_lower}):
            if row.get("id"):
                violating.add(str(row["id"]))
            if row.get("elem_id"):
                violating.add(str(row["elem_id"]))

    return violating


# Meat/fish terms — violates both Vegan and Vegetarian
_MEAT_FISH_TERMS: frozenset[str] = frozenset({
    "duck", "steak", "beef", "pork", "bacon", "ham", "sausage", "venison",
    "lamb", "chicken", "turkey", "fish", "salmon", "tuna", "shrimp", "lobster",
    "crab", "scallop", "meat", "seafood",
})
# Extra terms for Vegan only (eggs, broths, etc. — vegetarian allows eggs/dairy)
_VEGAN_EXTRA_TERMS: frozenset[str] = frozenset({
    "egg", "eggs", "chicken broth", "beef broth", "fish broth", "bone broth",
    "anchovy", "anchovies", "gelatin", "honey",
})


def _filter_diet_by_title(
    fused: list[dict[str, Any]],
    diets: list[str],
) -> list[dict[str, Any]]:
    """
    Drop recipes whose title or description contains obvious meat/fish/egg terms
    when diet is Vegan/Vegetarian. Belt-and-suspenders alongside graph FORBIDDEN.
    """
    diet_set = {d.strip().lower() for d in diets if d and isinstance(d, str)}
    if not diet_set & {"vegan", "vegetarian", "vegetarian_lacto_ovo"}:
        return fused

    is_vegan = "vegan" in diet_set
    blocklist = _MEAT_FISH_TERMS | _VEGAN_EXTRA_TERMS if is_vegan else _MEAT_FISH_TERMS

    kept = []
    for item in fused:
        payload = item.get("payload") or {}
        nested = payload.get("payload") or {}
        title = (
            item.get("title")
            or payload.get("title")
            or payload.get("r.title")
            or nested.get("title")
            or nested.get("name")
        )
        desc = payload.get("description") or nested.get("description") or ""
        text_to_check = f"{title or ''} {desc}".lower()

        if not text_to_check.strip():
            kept.append(item)
            continue
        if any(term in text_to_check for term in blocklist):
            logger.debug(
                "Diet-by-title dropped: title=%s (diets=%s)",
                str(title or "?")[:60], list(diet_set),
            )
            continue
        kept.append(item)
    return kept


def _filter_diet_compliance(
    fused: list[dict[str, Any]],
    diets: list[str],
    driver: Driver,
    database: str | None,
) -> list[dict[str, Any]]:
    """
    Drop recipes that use any ingredient forbidden by the requested diet(s).
    Uses (1) title/description blocklist for Vegan/Vegetarian, (2) graph FORBIDDEN.
    Strict: recipes without an extractable ID are dropped (cannot verify).
    """
    diets = [d for d in diets if d and isinstance(d, str)]
    if not diets:
        return fused

    # 1. Title/description blocklist (eggs, chicken broth, meat, etc.)
    result = _filter_diet_by_title(fused, diets)

    # 2. Graph-based: FORBIDDEN relationships
    recipe_ids = _recipe_ids_from_fused(result)
    violating_ids = _fetch_diet_violating_ids(driver, recipe_ids, diets, database)

    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in result:
        rid = _rid_for_item(item)

        if not rid:
            dropped += 1
            logger.debug(
                "Diet filter dropped (no ID): title=%s (diets=%s)",
                item.get("title", "?"), diets,
            )
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


# ── Filter E — Nutrient threshold (protein, fat, etc.) ────────────────────────

# Nutrient names that use Recipe.percent_calories_* (percentage of calories).
# Extractor passes values as percentages for these; Cypher layer matches.
_PERCENT_CALORIE_NUTRIENTS: frozenset[str] = frozenset({
    "protein", "total fat", "fat",
    "carbohydrate", "carbohydrates", "carb", "carbs",
})


def _fetch_nutrient_threshold_violating_ids(
    driver: Driver,
    recipe_ids: list[str],
    nutrient: str,
    operator: str,
    value: int | float,
    database: str | None,
) -> set[str]:
    """
    Return recipe IDs that violate the nutrient threshold.

    For protein/fat/carbs: uses Recipe.percent_calories_protein/fat/carbs
    (percentage of calories). Matches extractor and Cypher layer.

    For fiber, sodium, etc.: uses HAS_NUTRITION (amount in grams/mg).
    """
    if not recipe_ids:
        return set()

    nutrient_lower = (nutrient or "").strip().lower()

    # Use Recipe percent_calories_* for protein/fat/carbs (extractor passes % values)
    if nutrient_lower in _PERCENT_CALORIE_NUTRIENTS:
        prop_map = {
            "protein": "r.percent_calories_protein",
            "total fat": "r.percent_calories_fat",
            "fat": "r.percent_calories_fat",
            "carbohydrate": "r.percent_calories_carbs",
            "carbohydrates": "r.percent_calories_carbs",
            "carb": "r.percent_calories_carbs",
            "carbs": "r.percent_calories_carbs",
        }
        prop = prop_map.get(nutrient_lower, "r.percent_calories_protein")
        if operator == "gt":
            # Violating = prop < value (does not meet "at least")
            cypher = f"""
            UNWIND $recipe_ids AS rid
            MATCH (r:Recipe {{id: rid}})
            WHERE {prop} IS NULL OR {prop} < $threshold_value
            RETURN DISTINCT r.id AS flagged_id
            """
        else:
            # operator 'lt': violating = prop > value (exceeds "at most")
            cypher = f"""
            UNWIND $recipe_ids AS rid
            MATCH (r:Recipe {{id: rid}})
            WHERE {prop} IS NOT NULL AND {prop} > $threshold_value
            RETURN DISTINCT r.id AS flagged_id
            """
        params = {"recipe_ids": recipe_ids, "threshold_value": float(value)}
        nutrient_name = nutrient
    else:
        # Fiber, sodium, etc.: use HAS_NUTRITION (amount in grams/mg)
        nutrient_map = {
            "dietary fiber": "Dietary Fiber",
            "fiber": "Dietary Fiber",
            "fibre": "Dietary Fiber",
            "total sugars": "Total Sugars",
            "sugar": "Total Sugars",
            "sugars": "Total Sugars",
            "sodium": "Sodium",
            "salt": "Sodium",
            "energy": "Energy",
            "calories": "Energy",
        }
        nutrient_name = nutrient_map.get(nutrient_lower, nutrient)
        if operator == "gt":
            cypher = """
            UNWIND $recipe_ids AS rid
            MATCH (r:Recipe {id: rid})
                  -[:HAS_NUTRITION]->(nv:NutritionValue)
                  -[:OF_NUTRIENT]->(nd:NutrientDefinition)
            WHERE nd.nutrient_name = $nutrient_name
              AND nv.amount < $threshold_value
            RETURN DISTINCT r.id AS flagged_id
            """
        else:
            cypher = """
            UNWIND $recipe_ids AS rid
            MATCH (r:Recipe {id: rid})
                  -[:HAS_NUTRITION]->(nv:NutritionValue)
                  -[:OF_NUTRIENT]->(nd:NutrientDefinition)
            WHERE nd.nutrient_name = $nutrient_name
              AND nv.amount > $threshold_value
            RETURN DISTINCT r.id AS flagged_id
            """
        params = {
            "recipe_ids": recipe_ids,
            "nutrient_name": nutrient_name,
            "threshold_value": float(value),
        }

    try:
        with driver.session(database=database) as session:
            rows = session.run(cypher, **params)
            return {str(row["flagged_id"]) for row in rows}
    except Exception as e:
        logger.warning(
            "Nutrient threshold filter DB call failed — skipping: %s", e,
            extra={"component": "constraint_filter", "nutrient": nutrient},
        )
        return set()


def _filter_nutrient_threshold(
    fused: list[dict[str, Any]],
    threshold: dict[str, Any],
    driver: Driver,
    database: str | None,
) -> list[dict[str, Any]]:
    """Drop recipes that do not meet the nutrient threshold."""
    nutrient = threshold.get("nutrient")
    operator = threshold.get("operator", "gt")
    value = threshold.get("value")
    if not nutrient or value is None or operator not in ("gt", "lt"):
        return fused

    recipe_ids = _recipe_ids_from_fused(fused)
    violating_ids = _fetch_nutrient_threshold_violating_ids(
        driver, recipe_ids, nutrient, operator, value, database
    )

    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in fused:
        payload = item.get("payload") or {}
        nested = payload.get("payload") or {}
        rid = str(
            payload.get("id")
            or payload.get("r.id")
            or nested.get("id")
            or nested.get("r.id")
            or ""
        )
        if not rid:
            item = dict(item)
            sources = list(item.get("sources", []))
            if "unverified_nutrient" not in sources:
                sources.append("unverified_nutrient")
            item["sources"] = sources
            kept.append(item)
        elif rid in violating_ids:
            dropped += 1
            logger.debug(
                "Nutrient filter dropped: id=%s (nutrient=%s %s %s)",
                rid, nutrient, operator, value,
            )
        else:
            kept.append(item)

    if dropped:
        logger.info(
            "Nutrient threshold filter: dropped %d / %d (nutrient=%s %s %s)",
            dropped, len(fused), nutrient, operator, value,
        )
    return kept


# ── Filter F — Health condition compliance (PLACEHOLDER) ──────────────────────
# Health conditions are mapped to diet labels via _HEALTH_TO_DIET_MAP in
# profile_enrichment.py and then treated as dietary preferences.
# This filter activates automatically once Filter D is enabled and FORBIDDEN
# relationships are populated — no separate implementation needed.


_USDA_GROUPS: tuple[str, ...] = (
    "protein",
    "dairy",
    "vegetables",
    "fruits",
    "whole_grains",
)


def food_group_balance_score(
    payload: dict[str, Any],
    *,
    min_mult: float = 0.9,
    max_mult: float = 1.2,
) -> float:
    """
    Compute USDA food-group diversity multiplier from recipe payload.

    Expects payload["food_groups"] as list[str]. Returns 1.0 when unavailable or
    invalid so callers can safely apply this as a no-op.
    """
    if min_mult <= 0 or max_mult <= 0 or max_mult < min_mult:
        return 1.0

    raw_groups = payload.get("food_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        return 1.0

    present: set[str] = set()
    for g in raw_groups:
        if not isinstance(g, str):
            continue
        norm = g.strip().lower()
        if norm in _USDA_GROUPS:
            present.add(norm)

    if not present:
        return 1.0

    coverage = len(present) / float(len(_USDA_GROUPS))
    return min_mult + (max_mult - min_mult) * coverage


def apply_usda_food_group_bonus(
    fused: list[dict[str, Any]],
    entities: dict[str, Any],
    intent: str,
) -> list[dict[str, Any]]:
    """
    Apply a bounded USDA diversity multiplier after hard constraints.

    Ordering target (Phase C):
      1) hard constraints
      2) USDA food-group bonus
      3) contextual rerank

    This step is fail-safe: if data is missing or malformed, it returns fused
    unchanged for backward compatibility.
    """
    if os.getenv("ENABLE_USDA_FOOD_GROUP_BONUS", "").strip() != "1":
        return fused
    if not fused or intent not in RECIPE_INTENTS:
        return fused
    if not isinstance(entities.get("usda_guidelines"), dict):
        return fused

    scored: list[dict[str, Any]] = []
    changed = 0
    for item in fused:
        item_copy = dict(item)
        payload = item_copy.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        base = float(item_copy.get("rrf_score", item_copy.get("score", 0.0)))
        mult = food_group_balance_score(payload)
        adjusted = base * mult
        if adjusted != base:
            changed += 1

        item_copy["score"] = adjusted
        item_copy["rrf_score"] = adjusted
        scored.append(item_copy)

    if changed:
        logger.info(
            "USDA diversity bonus applied",
            extra={
                "component": "constraint_filter",
                "intent": intent,
                "items_adjusted": changed,
                "total_items": len(scored),
                "counter": "usda_bonus_applied_count",
                "value": 1,
            },
        )
    return sorted(scored, key=lambda x: -(x.get("score", 0)))


# ── Contextual rerank (PRD-33: soft ranking by context) ───────────────────────

def contextual_rerank(
    fused: list[dict[str, Any]],
    entities: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Soft rerank fused results by contextual signals (recent meals, calorie target,
    cuisine preference). Does not remove items — only adjusts scores and sort order.

    PRD logic:
      - Penalize recent meals (×0.3)
      - Penalize high-calorie recipes when over target (×0.7)
      - Boost cuisine match (×1.3)

    No-op when entities lack any of: exclude_recipe_ids, calorie_target,
    cuisine_preference.

    Fused item shape: canonical payload fields only
    (id, calories, cuisine_code).
    """
    exclude_ids: set[str] = set()
    exclude_raw = entities.get("exclude_recipe_ids")
    if isinstance(exclude_raw, list):
        exclude_ids = {str(rid).strip().lower() for rid in exclude_raw if rid}
    elif exclude_raw:
        exclude_ids = {str(exclude_raw).strip().lower()}

    cal_target = entities.get("calorie_target")
    if cal_target is not None:
        try:
            cal_target = float(cal_target)
        except (TypeError, ValueError):
            cal_target = None

    cuisines: list[str] = []
    cp = entities.get("cuisine_preference")
    if isinstance(cp, list):
        cuisines = [str(c).strip().lower() for c in cp if c]
    elif cp:
        cuisines = [str(cp).strip().lower()]

    if not exclude_ids and cal_target is None and not cuisines:
        return fused

    scored: list[dict[str, Any]] = []
    for item in fused:
        item = dict(item)
        base = float(item.get("rrf_score", item.get("score", 0.5)))

        payload = item.get("payload") or {}
        recipe_id = str(
            payload.get("id")
            or item.get("key", "")
        ).strip().lower()

        mult = 1.0
        if recipe_id and recipe_id in exclude_ids:
            mult *= 0.3
        if cal_target is not None:
            cal = payload.get("calories")
            if cal is not None:
                try:
                    if float(cal) > cal_target:
                        mult *= 0.7
                except (TypeError, ValueError):
                    pass
        if cuisines:
            recipe_cuisine = payload.get("cuisine_code")
            if recipe_cuisine:
                rc = str(recipe_cuisine).strip().lower()
                if rc and any(c in rc or rc in c for c in cuisines):
                    mult *= 1.3

        adjusted = base * mult
        item["score"] = adjusted
        item["rrf_score"] = adjusted
        scored.append(item)

    return sorted(scored, key=lambda x: -(x.get("score", 0)))


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
        tags ('unverified_allergen', 'unverified_calories')
        so the prompt builder can warn the LLM.
    """
    if intent not in RECIPE_INTENTS or not fused:
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

    # ── E: Nutrient threshold (high protein, low fat, etc.) ───────────────────
    nutrient_threshold: dict[str, Any] | None = entities.get("nutrient_threshold")
    if nutrient_threshold and isinstance(nutrient_threshold, dict):
        result = _filter_nutrient_threshold(result, nutrient_threshold, driver, database)

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
                "nutrient_threshold": bool(nutrient_threshold and isinstance(nutrient_threshold, dict)),
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
    lines: list[str] = []
    if cal_limit:
        lines.append(f"No recipes found under {cal_limit} kcal after constraints.")
    lines.append(f"No {searched_for} were found in the knowledge base.")

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


# ── Safety compliance checker (for eval) ───────────────────────────────────────

def check_safety_compliance(
    fused: list[dict[str, Any]],
    entities: dict[str, Any],
    intent: str,
    driver: Driver,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Verify that returned results do not violate safety constraints (allergens,
    diet, course, calories). Used for evaluation to compute safety_compliance_score.

    Returns:
        {
            "passed": bool,
            "score": 0.0 or 1.0,
            "violations": list[str],
        }
    """
    violations: list[str] = []

    if intent not in RECIPE_INTENTS or not fused:
        return {"passed": True, "score": 1.0, "violations": []}

    recipe_ids = _recipe_ids_from_fused(fused)

    # A: Course
    course = entities.get("course")
    if course:
        course_lower = course.lower()
        for item in fused:
            payload = item.get("payload") or {}
            meal_type_raw = payload.get("meal_type")
            meal_type = str(meal_type_raw).strip().lower() if meal_type_raw is not None else ""
            if not meal_type:
                violations.append(
                    f"course_missing_meal_type: required={course_lower} "
                    f"(title={item.get('title', '?')})"
                )
            elif meal_type != course_lower:
                violations.append(
                    f"course_mismatch: meal_type={meal_type} required={course_lower} "
                    f"(title={item.get('title', '?')})"
                )

    # B: Allergens
    allergens: list[str] = entities.get("exclude_ingredient") or []
    if allergens and recipe_ids:
        expanded = list(
            {v for t in allergens for v in _expand_exclude_term_variants(t)}
        )
        violating_ids = _fetch_allergen_violating_ids(
            driver, recipe_ids, expanded, database
        )
        for item in fused:
            rid = _get_recipe_id(item)
            if rid and rid in violating_ids:
                violations.append(
                    f"allergen_violation: recipe_id={rid} (exclude={allergens})"
                )

    # C: Calorie limit
    cal_limit = entities.get("cal_upper_limit")
    if cal_limit is not None and recipe_ids:
        calorie_violating = _fetch_calorie_violating_ids(
            driver, recipe_ids, cal_limit, database
        )
        for item in fused:
            rid = _get_recipe_id(item)
            if rid and rid in calorie_violating:
                violations.append(
                    f"calorie_violation: recipe_id={rid} exceeds limit={cal_limit}"
                )

    # D: Diet compliance
    diets = entities.get("diet") or []
    if isinstance(diets, str):
        diets = [diets] if diets else []
    if diets and recipe_ids:
        diet_violating = _fetch_diet_violating_ids(
            driver, recipe_ids, diets, database
        )
        for item in fused:
            rid = _get_recipe_id(item)
            if rid and rid in diet_violating:
                violations.append(
                    f"diet_violation: recipe_id={rid} (diet={diets})"
                )

    # Title-based diet blocklist (vegan/vegetarian)
    if diets:
        diet_set = {d.strip().lower() for d in diets if d and isinstance(d, str)}
        if diet_set & {"vegan", "vegetarian"}:
            for item in fused:
                payload = item.get("payload") or {}
                nested = payload.get("payload") or {}
                title = (
                    item.get("title")
                    or payload.get("title")
                    or payload.get("r.title")
                    or nested.get("title")
                    or nested.get("name")
                )
                if title:
                    title_lower = str(title).lower()
                    if any(term in title_lower for term in _VEGAN_VEGETARIAN_BLOCKLIST):
                        violations.append(
                            f"diet_title_violation: title contains meat/fish "
                            f"for diet={list(diet_set)} (title={str(title)[:60]})"
                        )

    passed = len(violations) == 0
    score = 1.0 if passed else 0.0
    return {"passed": passed, "score": score, "violations": violations}


def _get_recipe_id(item: dict[str, Any]) -> str | None:
    """Extract recipe UUID from a fused item."""
    payload = item.get("payload") or {}
    nested = payload.get("payload") or {}
    rid = (
        payload.get("id")
        or payload.get("r.id")
        or nested.get("id")
        or nested.get("r.id")
    )
    return str(rid) if rid else None
