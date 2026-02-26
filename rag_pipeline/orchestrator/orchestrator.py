from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import yaml
from neo4j import Driver

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from extractor_classifier import extract_intent, extract_intent_with_retry, parse_extractor_output, sanity_check

from rag_pipeline.augmentation.condense import condense_for_llm, format_semantic_results_as_text
from rag_pipeline.augmentation.fusion import apply_rrf
from rag_pipeline.config import EmbeddingConfig
from rag_pipeline.logging_utils import truncate_for_log
from rag_pipeline.embeddings.base import QueryEmbedder
from rag_pipeline.orchestrator.cypher_runner import run_cypher_retrieval
from rag_pipeline.orchestrator.entity_enrichment import enrich_entities
from rag_pipeline.retrieval.service import SemanticRetrievalRequest, retrieve_semantic
from rag_pipeline.retrieval.structural import (
    get_seed_embedding,
    structural_search_with_expansion,
)


# Intents that benefit from structural (collaborative filtering) retrieval
STRUCTURAL_INTENTS = {"find_recipe", "find_recipe_by_pantry"}

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorResult:
    intent: str
    entities: dict[str, Any]
    semantic_results: list[Any] = field(default_factory=list)
    structural_results: dict[str, Any] = field(default_factory=dict)
    cypher_results: list[dict[str, Any]] = field(default_factory=list)
    fused_results: list[dict[str, Any]] = field(default_factory=list)
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
    guardrails: dict[str, Any] = raw_cfg.get("retrieval_guardrails", {})
    semantic_min_score: float = (guardrails.get("semantic") or {}).get("min_score", 0.5)
    structural_min_score: float = (guardrails.get("structural") or {}).get("min_score", 0.3)
    cypher_max_rows: int | None = (guardrails.get("cypher") or {}).get("max_rows")
    intent_cfg: dict[str, Any] = raw_cfg.get("intent_extraction", {}) or {}
    query_truncated = truncate_for_log(user_query)

    # ── Step 1: Extract intent + entities ────────────────────────────────────
    t_extract = time.perf_counter()
    result = OrchestratorResult(intent="unknown", entities={})
    on_parse_failure = intent_cfg.get("on_parse_failure", "abort")

    try:
        raw_response: str
        parsed: dict[str, Any] | None = None

        if on_parse_failure == "retry":
            max_retries = intent_cfg.get("max_retries", 1)
            retry_msg = intent_cfg.get("retry_user_message", "Return only valid JSON with keys 'intent' and 'entities'. No markdown.")
            raw_response, parse_ok = extract_intent_with_retry(
                user_query,
                max_retries=max_retries,
                retry_message=retry_msg,
                config_path=config_path,
            )
            parsed = parse_extractor_output(raw_response) if parse_ok else None
        else:
            raw_response = extract_intent(user_query, config_path=config_path)
            parsed = parse_extractor_output(raw_response)

        if parsed is None:
            if on_parse_failure == "fallback":
                result.intent = intent_cfg.get("fallback_intent", "find_recipe")
                result.entities = dict(intent_cfg.get("fallback_entities", {}))
                logger.warning("Intent parse failed; using fallback intent=%s", result.intent)
            else:
                result.errors.append("Intent extraction error: invalid JSON")
                return result
        else:
            check = sanity_check(parsed)
            if check is not True:
                logger.warning(
                    "Intent extraction failed",
                    extra={
                        "component": "orchestrator",
                        "query": query_truncated,
                        "error": check[1],
                    },
                )
                result.errors.append(f"Intent extraction failed: {check[1]}")
                return result
            result.intent = parsed["intent"]
            result.entities = parsed["entities"]

        # Entity enrichment: add missing diet/course from query keywords (when enabled)
        result.entities = enrich_entities(user_query, result.entities, intent_cfg)

        t_extract_ms = (time.perf_counter() - t_extract) * 1000
        entity_keys = list(result.entities.keys()) if result.entities else []
        logger.debug(
            "Extraction complete",
            extra={
                "component": "orchestrator",
                "query": query_truncated,
                "intent": result.intent,
                "entity_keys": entity_keys,
                "latency_ms": round(t_extract_ms, 1),
            },
        )

    except Exception as e:
        if on_parse_failure == "fallback":
            result.intent = intent_cfg.get("fallback_intent", "find_recipe")
            result.entities = dict(intent_cfg.get("fallback_entities", {}))
            logger.warning("Intent extraction exception; using fallback: %s", e)
        else:
            logger.error(
                "Intent extraction error",
                extra={
                    "component": "orchestrator",
                    "query": query_truncated,
                    "error": str(e),
                },
                exc_info=True,
            )
            result.errors.append(f"Intent extraction error: {e}")
            return result

    # ── Step 2: Semantic retrieval ────────────────────────────────────────────
    t_semantic = time.perf_counter()
    semantic_label = intent_semantic_labels.get(result.intent, "Recipe")
    broaden = intent_cfg.get("broaden_on_low_confidence", False)
    broaden_max_words = intent_cfg.get("broaden_max_word_count", 3)
    broaden_labels_list: list[str] = intent_cfg.get("broaden_labels") or ["Recipe", "Ingredient"]

    labels_to_search: list[str]
    if broaden and len(user_query.split()) <= broaden_max_words:
        labels_to_search = broaden_labels_list if broaden_labels_list else [semantic_label]
    else:
        labels_to_search = [semantic_label]

    result.semantic_results = []
    try:
        for lbl in labels_to_search:
            try:
                results_lbl = retrieve_semantic(
                    driver,
                    cfg=cfg,
                    embedder=embedder,
                    request=SemanticRetrievalRequest(
                        query=user_query,
                        top_k=top_k,
                        label=lbl,
                    ),
                    database=database,
                )
                result.semantic_results.extend(results_lbl)
            except Exception as e:
                logger.warning("Semantic retrieval failed for label %s: %s", lbl, e)
    except Exception as e:
        logger.warning("Semantic retrieval failed: %s", e)
        result.errors.append(f"Semantic retrieval error: {e}")
        result.semantic_results = []
    else:
        result.semantic_results = [
            r for r in result.semantic_results
            if getattr(r, "score_raw", 1.0) >= semantic_min_score
        ]

    t_semantic_ms = (time.perf_counter() - t_semantic) * 1000
    semantic_count = len(result.semantic_results)
    logger.info(
        "Semantic retrieval complete",
        extra={
            "component": "orchestrator",
            "query": query_truncated,
            "intent": result.intent,
            "label": semantic_label,
            "count": semantic_count,
            "latency_ms": round(t_semantic_ms, 1),
        },
    )
    if semantic_count == 0:
        logger.warning(
            "Semantic retrieval empty",
            extra={"component": "orchestrator", "query": query_truncated, "label": semantic_label},
        )

    # ── Step 3: Structural retrieval (only for relevant intents) ──────────────
    t_structural = time.perf_counter()
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
                    min_score=structural_min_score,
                )
        except Exception as e:
            logger.warning(
                "Structural retrieval failed",
                extra={"component": "orchestrator", "query": query_truncated, "error": str(e)},
            )
            result.errors.append(f"Structural retrieval error: {e}")
            result.structural_results = {}

    t_structural_ms = (time.perf_counter() - t_structural) * 1000
    structural_count = len(result.structural_results.get("expanded_context", []))
    logger.info(
        "Structural retrieval complete",
        extra={
            "component": "orchestrator",
            "query": query_truncated,
            "intent": result.intent,
            "count": structural_count,
            "latency_ms": round(t_structural_ms, 1),
        },
    )

    # ── Step 4: Cypher retrieval ──────────────────────────────────────────────
    t_cypher = time.perf_counter()
    try:
        result.cypher_results = run_cypher_retrieval(
            driver,
            intent=result.intent,
            entities=result.entities,
            database=database,
            max_rows=cypher_max_rows,
        )
    except Exception as e:
        logger.warning(
            "Cypher retrieval failed",
            extra={"component": "orchestrator", "query": query_truncated, "intent": result.intent, "error": str(e)},
        )
        result.errors.append(f"Cypher retrieval error: {e}")
        result.cypher_results = []

    t_cypher_ms = (time.perf_counter() - t_cypher) * 1000
    cypher_count = len(result.cypher_results)
    logger.info(
        "Cypher retrieval complete",
        extra={
            "component": "orchestrator",
            "query": query_truncated,
            "intent": result.intent,
            "count": cypher_count,
            "latency_ms": round(t_cypher_ms, 1),
        },
    )

    # ── Step 5: RRF fusion ───────────────────────────────────────────────────
    rrf_cfg = raw_cfg.get("retrieval_guardrails", {}).get("rrf", {})
    rrf_k = rrf_cfg.get("k", 60)
    rrf_max_items = rrf_cfg.get("max_items", 15)
    t_fusion = time.perf_counter()
    try:
        result.fused_results = apply_rrf(
            result.semantic_results,
            result.structural_results,
            result.cypher_results,
            result.intent,
            k=rrf_k,
            max_items=rrf_max_items,
        )
    except Exception as e:
        logger.warning(
            "RRF fusion failed",
            extra={"component": "orchestrator", "query": query_truncated, "error": str(e)},
        )
        result.errors.append(f"RRF fusion error: {e}")
        result.fused_results = []

    t_fusion_ms = (time.perf_counter() - t_fusion) * 1000
    fused_count = len(result.fused_results)
    logger.info(
        "RRF fusion complete",
        extra={
            "component": "orchestrator",
            "query": query_truncated,
            "intent": result.intent,
            "retrieval_counts": {"semantic": len(result.semantic_results), "structural": len(result.structural_results.get("expanded_context", [])), "cypher": len(result.cypher_results)},
            "fused_count": fused_count,
            "latency_ms": round(t_fusion_ms, 1),
        },
    )

    return result
