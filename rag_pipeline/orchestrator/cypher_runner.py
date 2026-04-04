from __future__ import annotations

import logging
import os
import sys
from typing import Any

from neo4j import Driver

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from cypher_query_generator import generate_cypher_query

logger = logging.getLogger(__name__)

_RECIPE_INTENTS = {
    "find_recipe",
    "find_recipe_by_pantry",
    "rank_results",
    "recipes_for_cuisine",
    "recipes_by_nutrient",
    "ingredient_in_recipes",
    "cuisine_recipes",
}


def _canonicalize_cypher_row(
    row: dict[str, Any],
    *,
    intent: str,
    rank: int,
) -> dict[str, Any]:
    """
    Normalize Cypher recipe rows into canonical keys while preserving raw columns.
    """
    if intent not in _RECIPE_INTENTS:
        return row

    recipe_id = row.get("id") or row.get("r.id")
    title = row.get("title") or row.get("r.title")
    meal_type = row.get("meal_type") or row.get("r.meal_type")
    total_time_minutes = row.get("total_time_minutes") or row.get("r.total_time_minutes")
    cuisine_code = row.get("cuisine_code") or row.get("cuisine") or row.get("cuisine_name")

    # Keep raw row fields for backward compatibility during migration.
    canonical = dict(row)
    canonical["id"] = recipe_id
    canonical["title"] = title
    canonical["meal_type"] = meal_type
    canonical["total_time_minutes"] = total_time_minutes
    canonical["cuisine_code"] = cuisine_code
    canonical["source"] = "cypher"
    canonical["score_raw"] = 1.0 / float(max(rank, 1))
    canonical["payload"] = {
        "id": recipe_id,
        "title": title,
        "meal_type": meal_type,
        "total_time_minutes": total_time_minutes,
        "cuisine_code": cuisine_code,
    }
    return canonical


def run_cypher_retrieval(
    driver: Driver,
    *,
    intent: str,
    entities: dict[str, Any],
    database: str | None = None,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """
    Generate and execute a Cypher query based on intent + entities.

    Args:
        driver: Neo4j driver instance
        intent: Extracted intent from extractor_classifier
        entities: Extracted entities from extractor_classifier
        database: Neo4j database name (optional)
        max_rows: Maximum rows to return. Also used as the Cypher LIMIT for
                  recipe-returning intents so Neo4j only fetches what we need.
                  Defaults to 50 when not specified.

    Returns:
        List of result rows as dicts
    """
    cypher_limit = max_rows if max_rows is not None else 50
    try:
        cypher, params = generate_cypher_query(intent, entities, limit=cypher_limit)
    except ValueError:
        return []

    try:
        results: list[dict[str, Any]] = []
        with driver.session(database=database) as session:
            rows = session.run(cypher, **params)
            for idx, row in enumerate(rows, start=1):
                results.append(
                    _canonicalize_cypher_row(dict(row), intent=intent, rank=idx)
                )
                if max_rows is not None and len(results) >= max_rows:
                    break
        return results
    except Exception as e:
        logger.warning(
            "Cypher execution failed",
            extra={"component": "cypher", "intent": intent, "error": str(e)},
        )
        return []
