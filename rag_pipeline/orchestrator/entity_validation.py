"""
Entity validation: strip incompatible filter combinations.

When diet and include_ingredient contradict each other (e.g. Vegan + chicken),
we strip the conflicting include items so retrieval returns sensible results.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Ingredients incompatible with Vegan/Vegetarian
MEAT_INGREDIENTS: frozenset[str] = frozenset({
    "chicken", "beef", "pork", "lamb", "turkey", "duck", "venison",
    "bacon", "ham", "sausage", "meat", "fish", "salmon", "tuna", "shrimp",
    "lobster", "crab", "scallop", "seafood", "anchovy", "cod", "tilapia",
})

# Ingredients incompatible with Keto / Low-Carb
SUGARY_INGREDIENTS: frozenset[str] = frozenset({
    "sugar", "honey", "maple syrup", "agave", "molasses", "corn syrup",
    "rice", "pasta", "bread", "flour", "potato", "potatoes", "oat",
    "oatmeal", "quinoa", "beans", "lentils", "fruit juice",
})

# Ingredients incompatible with Gluten-Free
GLUTEN_INGREDIENTS: frozenset[str] = frozenset({
    "wheat", "barley", "rye", "flour", "bread", "pasta", "couscous",
    "semolina", "breadcrumbs", "seitan", "malt",
})


def _ingredient_conflicts_with_terms(ingredient: str, forbidden_terms: frozenset[str]) -> bool:
    """True if ingredient (or any word in it) matches a forbidden term."""
    ing_lower = str(ingredient).lower().strip()
    if not ing_lower:
        return False
    words = set(ing_lower.replace("-", " ").split())
    for term in forbidden_terms:
        if term in ing_lower or term in words:
            return True
    return False


def validate_entity_compatibility(entities: dict[str, Any]) -> dict[str, Any]:
    """
    Strip include_ingredient items that conflict with diet.

    - Vegan/Vegetarian + meat → strip meat
    - Keto/Low-Carb + sugary/carby items → strip those
    - Gluten-Free + gluten sources → strip those

    Returns a new dict; does not mutate input.
    """
    result = dict(entities)
    include = result.get("include_ingredient")
    if not include or not isinstance(include, list):
        return result

    diets = result.get("diet") or []
    if not isinstance(diets, list):
        diets = [diets] if diets else []
    diet_set = {str(d).strip().lower() for d in diets if d}

    to_remove: set[int] = set()

    for i, ing in enumerate(include):
        ing_str = str(ing).strip().lower()
        if not ing_str:
            continue

        # Vegan / Vegetarian: strip meat
        if diet_set & {"vegan", "vegetarian"}:
            if _ingredient_conflicts_with_terms(ing_str, MEAT_INGREDIENTS):
                to_remove.add(i)
                logger.debug(
                    "Entity validation: stripping %r (incompatible with Vegan/Vegetarian)",
                    ing,
                    extra={"component": "entity_validation"},
                )

        # Keto / Low-Carb: strip sugary/carby
        if diet_set & {"keto", "low-carb", "low carb"}:
            if _ingredient_conflicts_with_terms(ing_str, SUGARY_INGREDIENTS):
                to_remove.add(i)
                logger.debug(
                    "Entity validation: stripping %r (incompatible with Keto/Low-Carb)",
                    ing,
                    extra={"component": "entity_validation"},
                )

        # Gluten-Free: strip gluten sources
        if "gluten-free" in diet_set or "gluten free" in diet_set:
            if _ingredient_conflicts_with_terms(ing_str, GLUTEN_INGREDIENTS):
                to_remove.add(i)
                logger.debug(
                    "Entity validation: stripping %r (incompatible with Gluten-Free)",
                    ing,
                    extra={"component": "entity_validation"},
                )

    if to_remove:
        new_include = [x for j, x in enumerate(include) if j not in to_remove]
        result["include_ingredient"] = new_include
        logger.info(
            "Entity validation: stripped %d conflicting include_ingredient(s)",
            len(to_remove),
            extra={"component": "entity_validation", "diets": list(diet_set), "removed_count": len(to_remove)},
        )

    return result
