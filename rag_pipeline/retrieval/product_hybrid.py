"""
PRD-40: semantic retrieval over :Product nodes.

Mirrors the recipe semantic-search pattern but targets the Product vector index
(`vec_product_semanticembedding`, see embedding_config.yaml). Used by the new
`/search/products` endpoint as a *supplement* to the backend's Tier-1 Postgres
lexical match — it is NOT the source of truth for availability (RAG is
stock-blind by design).

Defensive: returns [] on any failure (missing index, products without
embeddings, embedder error) so the orchestrator degrades to seed/annotate-only
rather than erroring. This is what keeps product search working when semantic
retrieval is unavailable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neo4j import Driver
    from rag_pipeline.embeddings.base import QueryEmbedder

logger = logging.getLogger(__name__)

PRODUCT_VECTOR_INDEX = "vec_product_semanticembedding"

# Vector search over Product nodes. We over-fetch (LIMIT in two stages) because
# exclude_ids / filters prune candidates after the index returns its top-k.
_CYPHER = """
CALL db.index.vector.queryNodes($index_name, $k, $emb)
YIELD node, score
WITH coalesce(node.id, elementId(node)) AS product_id, node AS node, score AS score
WHERE NOT product_id IN $exclude_ids
  AND (size($vendor_ids) = 0 OR node.vendor_id IN $vendor_ids)
  AND (
        size($category_ids) = 0
        OR node.category_id IN $category_ids
        OR node.category IN $category_ids
      )
RETURN product_id, score
ORDER BY score DESC
LIMIT $limit
"""


def semantic_search_products(
    driver: "Driver",
    embedder: "QueryEmbedder",
    query: str,
    *,
    exclude_ids: set[str] | None = None,
    limit: int = 20,
    filters: dict[str, Any] | None = None,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Return [{product_id, score}] ranked by semantic similarity to `query`.

    Empty list on any error or when `query`/`embedder` is missing.
    """
    if not query or not query.strip() or embedder is None:
        return []

    exclude = [str(x) for x in (exclude_ids or set()) if x]
    filters = filters or {}
    vendor_ids = [str(v) for v in (filters.get("vendor_ids") or []) if v]
    category_ids = [str(c) for c in (filters.get("category_ids") or []) if c]
    safe_limit = max(1, int(limit))
    # Over-fetch so post-filter pruning still leaves `limit` results.
    k = safe_limit + len(exclude) + 10

    try:
        emb = embedder.embed_query(query)
    except Exception as e:  # embedding provider error → degrade
        logger.warning("semantic_search_products embed failed: %s", e)
        return []

    try:
        with driver.session(database=database) as session:
            rows = session.run(
                _CYPHER,
                index_name=PRODUCT_VECTOR_INDEX,
                emb=list(emb),
                k=k,
                exclude_ids=exclude,
                vendor_ids=vendor_ids,
                category_ids=category_ids,
                limit=safe_limit,
            )
            out: list[dict[str, Any]] = []
            for row in rows:
                pid = row["product_id"]
                if not pid:
                    continue
                out.append({"product_id": str(pid), "score": float(row["score"])})
            return out
    except Exception as e:  # missing index / no embeddings / cypher error → degrade
        logger.warning("semantic_search_products query failed: %s", e)
        return []
