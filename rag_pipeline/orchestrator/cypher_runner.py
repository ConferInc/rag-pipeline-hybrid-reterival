from __future__ import annotations

import logging
import os
import sys
from typing import Any

from neo4j import Driver

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from cypher_query_generator import generate_cypher_query

logger = logging.getLogger(__name__)


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

    Returns:
        List of result rows as dicts
    """
    try:
        cypher, params = generate_cypher_query(intent, entities)
    except ValueError:
        return []

    try:
        results: list[dict[str, Any]] = []
        with driver.session(database=database) as session:
            rows = session.run(cypher, **params)
            for row in rows:
                results.append(dict(row))
                if max_rows is not None and len(results) >= max_rows:
                    break
        return results
    except Exception as e:
        logger.warning(
            "Cypher execution failed",
            extra={"component": "cypher", "intent": intent, "error": str(e)},
        )
        return []
