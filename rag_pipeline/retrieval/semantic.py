from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from neo4j import Driver

from rag_pipeline.config import EmbeddingConfig, get_semantic_index_spec
from rag_pipeline.retrieval.types import RetrievalResult

logger = logging.getLogger(__name__)


def semantic_search_by_label(
    driver: Driver,
    *,
    cfg: EmbeddingConfig,
    label: str,
    query_vector: Sequence[float],
    top_k: int = 10,
    database: str | None = None,
) -> list[RetrievalResult]:
    """
    Semantic vector search for a single node label using Neo4j native vector indexes.

    Returns raw similarity scores only (score_raw).
    """
    spec = get_semantic_index_spec(cfg, label=label, require_index_name=True)
    vector = list(query_vector)
    if len(vector) != spec.dimensions:
        raise ValueError(
            f"Query vector dimension mismatch for label={label!r}: "
            f"expected {spec.dimensions}, got {len(vector)}"
        )

    cypher = """
    CALL db.index.vector.queryNodes($index_name, $top_k, $vector)
    YIELD node, score
    RETURN elementId(node) AS node_id, labels(node) AS labels, node AS node, score AS score
    ORDER BY score DESC
    """

    results: list[RetrievalResult] = []
    with driver.session(database=database) as session:
        rows = session.run(
            cypher,
            index_name=spec.index_name,
            top_k=int(top_k),
            vector=vector,
        )
        for row in rows:
            node = row["node"]
            node_labels: Iterable[str] = row["labels"]
            score = float(row["score"])

            node_label = label if label in node_labels else (next(iter(node_labels), label))

            payload = _build_payload_from_rules(cfg, node_label, dict(node))
            if node_label == "Recipe":
                # Enforce canonical recipe payload contract at semantic source.
                payload = _canonicalize_recipe_payload(payload, dict(node))
                if payload is None:
                    logger.warning(
                        "Dropping semantic recipe result due to payload contract violation",
                        extra={"component": "semantic", "node_id": str(row["node_id"])},
                    )
                    continue

            results.append(
                RetrievalResult(
                    node_id=str(row["node_id"]),
                    label=str(node_label),
                    score_raw=score,
                    source="semantic",
                    index_name=str(spec.index_name),
                    payload=payload,
                )
            )

    return results


def _build_payload_from_rules(
    cfg: EmbeddingConfig, label: str, node_properties: dict[str, Any]
) -> dict[str, Any]:
    rules = cfg.semantic.label_text_rules.get(label) or {}
    props = rules.get("properties")
    if not props:
        return node_properties

    payload: dict[str, Any] = {}
    for p in props:
        if p in node_properties:
            payload[p] = node_properties[p]
    return payload


def _canonicalize_recipe_payload(
    payload: dict[str, Any], node_properties: dict[str, Any]
) -> dict[str, Any] | None:
    """
    Build canonical recipe payload fields from semantic hit properties.

    Mandatory: id, title, meal_type.
    Optional: total_time_minutes, cuisine_code, calories.
    """
    recipe_id = payload.get("id") or node_properties.get("id")
    title = payload.get("title") or node_properties.get("title")
    meal_type = payload.get("meal_type") or node_properties.get("meal_type")

    if not recipe_id or not title or not meal_type:
        return None

    payload["id"] = recipe_id
    payload["title"] = title
    payload["meal_type"] = meal_type
    payload["total_time_minutes"] = payload.get(
        "total_time_minutes", node_properties.get("total_time_minutes")
    )
    payload["cuisine_code"] = (
        payload.get("cuisine_code")
        or payload.get("cuisine")
        or node_properties.get("cuisine_code")
        or node_properties.get("cuisine")
    )
    payload["calories"] = payload.get("calories", node_properties.get("calories"))
    return payload

