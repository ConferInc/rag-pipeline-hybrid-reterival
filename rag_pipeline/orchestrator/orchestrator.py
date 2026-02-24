from __future__ import annotations

import json
import sys
import os
from dataclasses import dataclass, field
from typing import Any

import yaml
from neo4j import Driver

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from extractor_classifier import extract_intent, sanity_check

from rag_pipeline.augmentation.condense import condense_for_llm, format_semantic_results_as_text
from rag_pipeline.config import EmbeddingConfig
from rag_pipeline.embeddings.base import QueryEmbedder
from rag_pipeline.orchestrator.cypher_runner import run_cypher_retrieval
from rag_pipeline.retrieval.service import SemanticRetrievalRequest, retrieve_semantic
from rag_pipeline.retrieval.structural import (
    get_seed_embedding,
    structural_search_with_expansion,
)


# Intents that benefit from structural (collaborative filtering) retrieval
STRUCTURAL_INTENTS = {"find_recipe", "find_recipe_by_pantry"}


@dataclass
class OrchestratorResult:
    intent: str
    entities: dict[str, Any]
    semantic_results: list[Any] = field(default_factory=list)
    structural_results: dict[str, Any] = field(default_factory=dict)
    cypher_results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def orchestrate(
    driver: Driver,
    *,
    cfg: EmbeddingConfig,
    embedder: QueryEmbedder,
    user_query: str,
    customer_node_id: str | None = None,
    top_k: int = 5,
    database: str | None = None,
    config_path: str = "embedding_config.yaml",
) -> OrchestratorResult:
    """
    Run all three retrievals for a user query and return merged results.

    Args:
        driver: Neo4j driver
        cfg: Embedding config
        embedder: Query embedder for semantic search
        user_query: Free-text user query
        customer_node_id: Neo4j elementId of logged-in customer (for structural)
        top_k: Number of results per retrieval
        database: Neo4j database name
        config_path: Path to embedding_config.yaml

    Returns:
        OrchestratorResult with all three retrieval outputs
    """
    with open(config_path) as f:
        raw_cfg = yaml.safe_load(f)

    intent_semantic_labels: dict[str, str] = raw_cfg.get("intent_semantic_labels", {})
    intent_structural: dict[str, Any] = raw_cfg.get("intent_structural", {})

    # ── Step 1: Extract intent + entities ────────────────────────────────────
    result = OrchestratorResult(intent="unknown", entities={})
    try:
        raw_response = extract_intent(user_query)
        parsed = json.loads(raw_response)
        check = sanity_check(parsed)
        if check is not True:
            result.errors.append(f"Intent extraction failed: {check[1]}")
            return result
        result.intent = parsed["intent"]
        result.entities = parsed["entities"]
    except Exception as e:
        result.errors.append(f"Intent extraction error: {e}")
        return result

    # ── Step 2: Semantic retrieval ────────────────────────────────────────────
    semantic_label = intent_semantic_labels.get(result.intent, "Recipe")
    try:
        result.semantic_results = retrieve_semantic(
            driver,
            cfg=cfg,
            embedder=embedder,
            request=SemanticRetrievalRequest(
                query=user_query,
                top_k=top_k,
                label=semantic_label,
            ),
            database=database,
        )
    except Exception as e:
        result.errors.append(f"Semantic retrieval error: {e}")

    # ── Step 3: Structural retrieval (only for relevant intents) ──────────────
    if result.intent in STRUCTURAL_INTENTS and customer_node_id:
        struct_cfg = intent_structural.get(result.intent, {})
        seed_label = struct_cfg.get("seed_label", "B2C_Customer")
        expand_labels: list[str] | None = struct_cfg.get("expand_labels")
        expand_rels: list[str] | None = struct_cfg.get("expand_relationships") or None

        try:
            seed_emb = get_seed_embedding(
                driver,
                cfg=cfg,
                label=seed_label,
                node_id=customer_node_id,
                database=database,
            )
            if seed_emb:
                result.structural_results = structural_search_with_expansion(
                    driver,
                    cfg=cfg,
                    label=seed_label,
                    seed_vector=seed_emb,
                    top_k=top_k,
                    allowed_labels=expand_labels,
                    allowed_relationships=expand_rels,
                    database=database,
                )
        except Exception as e:
            result.errors.append(f"Structural retrieval error: {e}")

    # ── Step 4: Cypher retrieval ──────────────────────────────────────────────
    try:
        result.cypher_results = run_cypher_retrieval(
            driver,
            intent=result.intent,
            entities=result.entities,
            database=database,
        )
    except Exception as e:
        result.errors.append(f"Cypher retrieval error: {e}")

    return result
