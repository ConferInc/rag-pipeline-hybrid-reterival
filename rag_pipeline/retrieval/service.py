from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import yaml
from neo4j import Driver

from rag_pipeline.config import EmbeddingConfig
from rag_pipeline.embeddings.base import QueryEmbedder
from rag_pipeline.retrieval.label_inference import infer_label_with_llm
from rag_pipeline.retrieval.semantic import semantic_search_by_label
from rag_pipeline.retrieval.types import RetrievalResult

logger = logging.getLogger(__name__)


def _infer_label_heuristics(query: str) -> str | None:
    """Heuristic label inference from query keywords. First match wins."""
    q = query.lower()
    # Order matters: more specific patterns first
    if any(w in q for w in ["customer", "similar users", "like me", "my profile", "user"]):
        return "B2C_Customer"
    if any(w in q for w in ["ingredient", "nutrient", "nutrition", "protein", "calories", "sodium", "fiber", "vitamin", "mineral"]):
        return "Ingredient"
    if any(w in q for w in ["product", "brand"]):
        return "Product"
    if any(w in q for w in ["cuisine", "italian", "mexican", "indian", "mediterranean", "american", "asian"]):
        return "Cuisine"
    if any(w in q for w in ["recipe", "meal", "dish", "dinner", "breakfast", "lunch", "cook", "bake", "prepare"]):
        return "Recipe"
    return None


def infer_label_from_query(
    query: str,
    *,
    config_path: str | Path = "embedding_config.yaml",
    use_llm_fallback: bool | None = None,
) -> str | None:
    """
    Infer semantic search label from query: heuristics first, LLM fallback when needed.

    Args:
        query: User query text
        config_path: Path to embedding_config.yaml for label_inference settings
        use_llm_fallback: Override config (None = use config value)

    Returns:
        Label string or None
    """
    # Heuristics first
    label = _infer_label_heuristics(query)
    if label:
        return label

    # Load config for fallback and defaults
    cfg_path = Path(config_path)
    fallback_to_llm = True
    allowed_labels = ["Recipe", "Ingredient", "Product", "B2C_Customer", "Cuisine"]
    default_label = "Recipe"

    if cfg_path.exists():
        try:
            with open(cfg_path) as f:
                raw = yaml.safe_load(f)
            li = raw.get("label_inference", {}) or {}
            allowed_labels = li.get("allowed_labels", allowed_labels)
            fallback_to_llm = li.get("fallback_to_llm", True)
            default_label = li.get("default_label", default_label)
        except Exception:
            pass

    if use_llm_fallback is not None:
        fallback_to_llm = use_llm_fallback

    if fallback_to_llm:
        label = infer_label_with_llm(query, allowed_labels)
        if label:
            logger.warning(
                "Label inference fallback used",
                extra={"component": "semantic", "inferred_label": label},
            )
            return label

    return default_label if default_label else None


@dataclass(frozen=True)
class SemanticRetrievalRequest:
    query: str
    top_k: int = 10
    label: str | None = None
    config_path: str | Path = "embedding_config.yaml"


def retrieve_semantic(
    driver: Driver,
    *,
    cfg: EmbeddingConfig,
    embedder: QueryEmbedder,
    request: SemanticRetrievalRequest,
    database: str | None = None,
) -> list[RetrievalResult]:
    label = request.label or infer_label_from_query(
        request.query,
        config_path=request.config_path,
    )
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

