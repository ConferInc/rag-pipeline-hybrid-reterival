"""
Keyword retrieval using Neo4j Fulltext Index (Lucene-powered BM25).

4th retrieval lane — runs in parallel with semantic, structural, and Cypher.
Results feed into RRF fusion with a configurable weight boost.

Index: recipe_title_ft (title-only, english analyzer with stemming).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from neo4j import Driver

logger = logging.getLogger(__name__)

# Default index name — can be overridden via embedding_config.yaml
_DEFAULT_INDEX_NAME = "recipe_title_ft"

# Lucene special characters that must be escaped
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def _sanitize_lucene_query(query: str) -> str:
    """Escape Lucene special characters for safe fulltext queries."""
    return _LUCENE_SPECIAL.sub(r"\\\1", query.strip())


def _build_lucene_query(query: str) -> str:
    """
    Build Lucene query from sanitized user input.

    Strategy:
    - Escape special chars
    - Append wildcard to terms >= 3 chars for partial matching
    - "chicken curry" → "chicken* curry*"
    """
    safe = _sanitize_lucene_query(query)
    if not safe:
        return ""
    terms = safe.split()
    return " ".join(f"{t}*" if len(t) >= 3 else t for t in terms)


def keyword_search(
    driver: Driver,
    *,
    query: str,
    top_k: int = 10,
    database: str | None = None,
    min_score: float = 0.3,
    index_name: str = _DEFAULT_INDEX_NAME,
) -> list[dict[str, Any]]:
    """
    Run keyword search using Neo4j Fulltext Index.

    Returns list of dicts compatible with RRF fusion:
      { key, source, label, title, bm25_score, payload: {id, title, meal_type, ...} }

    Fail-open: returns [] on any error.
    """
    if not query or not query.strip():
        return []

    lucene_query = _build_lucene_query(query)
    if not lucene_query:
        return []

    cypher = """
    CALL db.index.fulltext.queryNodes($index_name, $search_query)
    YIELD node, score
    WHERE score >= $min_score
      AND node.id IS NOT NULL
      AND node.title IS NOT NULL
    RETURN
      node.id AS id,
      node.title AS title,
      node.meal_type AS meal_type,
      node.total_time_minutes AS total_time_minutes,
      node.cuisine_code AS cuisine_code,
      score AS bm25_score
    ORDER BY score DESC
    LIMIT $top_k
    """

    results: list[dict[str, Any]] = []
    try:
        with driver.session(database=database) as session:
            rows = session.run(
                cypher,
                index_name=index_name,
                search_query=lucene_query,
                min_score=min_score,
                top_k=int(top_k),
            )
            for row in rows:
                rid = row["id"]
                if not rid:
                    continue
                results.append({
                    "key": str(rid),
                    "source": "keyword",
                    "label": "Recipe",
                    "title": row["title"] or "",
                    "bm25_score": float(row["bm25_score"]),
                    "payload": {
                        "id": str(rid),
                        "title": row["title"],
                        "meal_type": row.get("meal_type"),
                        "total_time_minutes": row.get("total_time_minutes"),
                        "cuisine_code": row.get("cuisine_code"),
                    },
                })
        logger.info(
            "Keyword search complete",
            extra={
                "component": "keyword",
                "query": query[:50],
                "lucene_query": lucene_query[:80],
                "result_count": len(results),
                "min_score": min_score,
                "index_name": index_name,
            },
        )
    except Exception as e:
        logger.warning(
            "Keyword search failed (fail-open): %s", e,
            extra={"component": "keyword", "query": query[:50]},
        )
    return results
