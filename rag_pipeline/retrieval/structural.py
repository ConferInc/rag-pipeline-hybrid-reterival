from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from neo4j import Driver

from rag_pipeline.config import EmbeddingConfig, get_structural_index_spec
from rag_pipeline.retrieval.types import RetrievalResult

logger = logging.getLogger(__name__)


def structural_search_by_label(
    driver: Driver,
    *,
    cfg: EmbeddingConfig,
    label: str,
    seed_vector: Sequence[float],
    top_k: int = 10,
    database: str | None = None,
) -> list[RetrievalResult]:
    """
    Structural vector search using GraphSAGE embeddings.

    Given a seed node's GraphSAGE embedding, find topologically similar nodes
    of the specified label.

    Args:
        driver: Neo4j driver instance
        cfg: Embedding configuration
        label: Node label to search within
        seed_vector: GraphSAGE embedding of the seed node (128-d)
        top_k: Number of results to return
        database: Neo4j database name (optional)

    Returns:
        List of RetrievalResult with source="structural"
    """
    spec = get_structural_index_spec(cfg, label=label, require_index_name=True)
    vector = list(seed_vector)
    if len(vector) != spec.dimensions:
        raise ValueError(
            f"Seed vector dimension mismatch for label={label!r}: "
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
                    source="structural",
                    index_name=str(spec.index_name),
                    payload=_build_structural_payload(node_label, dict(node)),
                )
            )

    logger.debug(
        "Structural search complete",
        extra={"component": "structural", "label": label, "count": len(results)},
    )
    return results


def get_seed_embedding(
    driver: Driver,
    *,
    cfg: EmbeddingConfig,
    label: str,
    node_id: str,
    database: str | None = None,
) -> list[float] | None:
    """
    Fetch the GraphSAGE embedding for a specific node.

    Tries two lookup strategies in order:
      1. Match by node's `id` property (PostgreSQL UUID from Express backend)
      2. Match by Neo4j internal elementId (legacy fallback)

    Args:
        driver: Neo4j driver instance
        cfg: Embedding configuration
        label: Node label (used to get the embedding property name)
        node_id: PostgreSQL UUID or Neo4j elementId of the node
        database: Neo4j database name (optional)

    Returns:
        The GraphSAGE embedding vector, or None if not found / not yet generated
    """
    spec = get_structural_index_spec(cfg, label=label, require_index_name=False)

    # Strategy 1: match by id property (UUID from PostgreSQL — what Express sends)
    cypher_by_id = f"""
    MATCH (n:{label} {{id: $node_id}})
    RETURN n.{spec.property} AS embedding
    """

    # Strategy 2: match by Neo4j elementId (internal — legacy fallback)
    cypher_by_element_id = f"""
    MATCH (n)
    WHERE elementId(n) = $node_id
    RETURN n.{spec.property} AS embedding
    """

    with driver.session(database=database) as session:
        for cypher in (cypher_by_id, cypher_by_element_id):
            try:
                result = session.run(cypher, node_id=node_id)
                record = result.single()
                if record and record["embedding"]:
                    return list(record["embedding"])
            except Exception:
                continue
    return None


def expand_from_seeds(
    driver: Driver,
    *,
    seed_node_ids: list[str],
    hops: int = 1,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """
    K-hop expansion from seed nodes. Returns all connected nodes and relationships.

    Args:
        driver: Neo4j driver instance
        seed_node_ids: List of Neo4j elementIds to expand from
        hops: Number of hops to traverse (default 1)
        database: Neo4j database name (optional)

    Returns:
        List of connected node info with relationship context
    """
    if not seed_node_ids:
        return []

    cypher = """
    UNWIND $seed_ids AS seed_id
    MATCH (seed) WHERE elementId(seed) = seed_id
    MATCH (seed)-[r]-(connected)
    RETURN DISTINCT
        elementId(seed) AS seed_id,
        elementId(connected) AS connected_id,
        labels(connected) AS connected_labels,
        type(r) AS relationship,
        connected AS connected_node
    """

    results: list[dict[str, Any]] = []
    with driver.session(database=database) as session:
        rows = session.run(cypher, seed_ids=seed_node_ids)
        for row in rows:
            connected_node = dict(row["connected_node"])
            results.append({
                "seed_id": str(row["seed_id"]),
                "connected_id": str(row["connected_id"]),
                "connected_labels": list(row["connected_labels"]),
                "relationship": str(row["relationship"]),
                "payload": _build_structural_payload("", connected_node),
            })

    return results


def filter_by_intent(
    expanded_results: list[dict[str, Any]],
    *,
    allowed_labels: list[str] | None = None,
    allowed_relationships: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Filter expanded results based on intent.

    Args:
        expanded_results: Results from expand_from_seeds()
        allowed_labels: Only keep nodes with these labels (None = keep all)
        allowed_relationships: Only keep these relationship types (None = keep all)

    Returns:
        Filtered list of connected nodes
    """
    filtered: list[dict[str, Any]] = []
    for item in expanded_results:
        if allowed_labels:
            if not any(lbl in allowed_labels for lbl in item["connected_labels"]):
                continue
        if allowed_relationships:
            if item["relationship"] not in allowed_relationships:
                continue
        filtered.append(item)
    return filtered


def structural_search_with_expansion(
    driver: Driver,
    *,
    cfg: EmbeddingConfig,
    label: str,
    seed_vector: Sequence[float],
    top_k: int = 5,
    allowed_labels: list[str] | None = None,
    allowed_relationships: list[str] | None = None,
    database: str | None = None,
    min_score: float | None = None,
) -> dict[str, Any]:
    """
    Combined structural search + k-hop expansion + intent filtering.

    Single function that:
    1. Finds similar nodes via GraphSAGE
    2. Expands 1-hop from those nodes
    3. Filters by intent (labels/relationships)

    Args:
        driver: Neo4j driver
        cfg: Embedding config
        label: Seed node label
        seed_vector: GraphSAGE embedding
        top_k: Number of similar nodes
        allowed_labels: Filter to these node types
        allowed_relationships: Filter to these relationships
        database: Neo4j database
        min_score: Minimum GraphSAGE similarity score (filter similar nodes before expansion)

    Returns:
        Dict with 'similar_nodes' and 'expanded_context'
    """
    similar = structural_search_by_label(
        driver,
        cfg=cfg,
        label=label,
        seed_vector=seed_vector,
        top_k=top_k,
        database=database,
    )

    if min_score is not None:
        similar = [r for r in similar if r.score_raw >= min_score]

    seed_ids = [r.node_id for r in similar]

    expanded = expand_from_seeds(
        driver,
        seed_node_ids=seed_ids,
        hops=1,
        database=database,
    )

    filtered = filter_by_intent(
        expanded,
        allowed_labels=allowed_labels,
        allowed_relationships=allowed_relationships,
    )

    return {
        "similar_nodes": [r.to_dict() for r in similar],
        "expanded_context": filtered,
    }


def _build_structural_payload(label: str, node_properties: dict[str, Any]) -> dict[str, Any]:
    """
    Build payload for structural results.
    Excludes embedding vectors (large float arrays) from payload.
    """
    excluded_suffixes = ("Embedding", "embedding", "vector", "Vector")
    payload: dict[str, Any] = {}
    for key, value in node_properties.items():
        if key.endswith(excluded_suffixes):
            continue
        if isinstance(value, list) and len(value) > 50 and all(isinstance(v, (int, float)) for v in value[:5]):
            continue
        payload[key] = value
    return payload
