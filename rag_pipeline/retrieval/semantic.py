from __future__ import annotations

from typing import Any, Iterable, Sequence

from neo4j import Driver

from rag_pipeline.config import EmbeddingConfig, get_semantic_index_spec
from rag_pipeline.retrieval.types import RetrievalResult


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

            results.append(
                RetrievalResult(
                    node_id=str(row["node_id"]),
                    label=str(node_label),
                    score_raw=score,
                    source="semantic",
                    index_name=str(spec.index_name),
                    payload=_build_payload_from_rules(cfg, node_label, dict(node)),
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

