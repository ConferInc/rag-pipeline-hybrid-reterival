from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from neo4j import Driver

from rag_pipeline.config import EmbeddingConfig
from rag_pipeline.embeddings.base import QueryEmbedder
from rag_pipeline.retrieval.semantic import semantic_search_by_label
from rag_pipeline.retrieval.types import RetrievalResult


@dataclass(frozen=True)
class SemanticRetrievalRequest:
    query: str
    top_k: int = 10
    label: str | None = None


def infer_label_from_query(query: str) -> str | None:
    q = query.lower()
    if "recipe" in q:
        return "Recipe"
    return None


def retrieve_semantic(
    driver: Driver,
    *,
    cfg: EmbeddingConfig,
    embedder: QueryEmbedder,
    request: SemanticRetrievalRequest,
    database: str | None = None,
) -> list[RetrievalResult]:
    label = request.label or infer_label_from_query(request.query)
    if not label:
        raise ValueError("No label provided and could not infer label from query.")

    vector: Sequence[float] = embedder.embed_query(request.query)
    return semantic_search_by_label(
        driver,
        cfg=cfg,
        label=label,
        query_vector=vector,
        top_k=request.top_k,
        database=database,
    )

