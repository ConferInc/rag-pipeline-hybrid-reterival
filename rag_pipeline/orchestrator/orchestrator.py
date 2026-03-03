from __future__ import annotations

import asyncio
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
from rag_pipeline.orchestrator.constraint_filter import apply_hard_constraints, build_zero_results_message
from rag_pipeline.orchestrator.cypher_runner import run_cypher_retrieval
from rag_pipeline.orchestrator.entity_enrichment import enrich_entities
from rag_pipeline.orchestrator.profile_enrichment import merge_profile_into_entities
from rag_pipeline.retrieval.service import SemanticRetrievalRequest, retrieve_semantic
from rag_pipeline.retrieval.structural import (
    get_seed_embedding,
    structural_search_with_expansion,
)


# Intents that benefit from structural (collaborative filtering) retrieval
STRUCTURAL_INTENTS = {"find_recipe", "find_recipe_by_pantry"}

logger = logging.getLogger(__name__)


def _run_semantic(
    driver: Driver,
    cfg: EmbeddingConfig,
    embedder: QueryEmbedder,
    user_query: str,
    intent: str,
    intent_semantic_labels: dict[str, str],
    intent_cfg: dict[str, Any],
    top_k: int,
    semantic_min_score: float,
    database: str | None,
    config_path: str,
) -> list[Any]:
    """Run semantic retrieval (sync worker for asyncio.to_thread)."""
    semantic_label = intent_semantic_labels.get(intent, "Recipe")
    broaden = intent_cfg.get("broaden_on_low_confidence", False)
    broaden_max_words = intent_cfg.get("broaden_max_word_count", 3)
    broaden_labels_list: list[str] = intent_cfg.get("broaden_labels") or ["Recipe", "Ingredient"]

    labels_to_search: list[str]
    if broaden and len(user_query.split()) <= broaden_max_words:
        labels_to_search = broaden_labels_list if broaden_labels_list else [semantic_label]
    else:
        labels_to_search = [semantic_label]

    results: list[Any] = []
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
                results.extend(results_lbl)
            except Exception as e:
                logger.warning("Semantic retrieval failed for label %s: %s", lbl, e)
    except Exception as e:
        logger.warning("Semantic retrieval failed: %s", e)
        return []

    return [r for r in results if getattr(r, "score_raw", 1.0) >= semantic_min_score]


def _run_structural(
    driver: Driver,
    cfg: EmbeddingConfig,
    customer_node_id: str | None,
    intent: str,
    intent_structural: dict[str, Any],
    top_k: int,
    structural_min_score: float,
    database: str | None,
) -> dict[str, Any]:
    """Run structural retrieval (sync worker for asyncio.to_thread). Returns {} if skipped or on error."""
    if intent not in STRUCTURAL_INTENTS or not customer_node_id:
        return {}

    struct_cfg = intent_structural.get(intent, {})
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
            return structural_search_with_expansion(
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
        logger.warning("Structural retrieval failed: %s", e)
    return {}


def _run_cypher(
    driver: Driver,
    intent: str,
    entities: dict[str, Any],
    cypher_max_rows: int | None,
    database: str | None,
) -> list[dict[str, Any]]:
    """Run Cypher retrieval (sync worker for asyncio.to_thread). Returns [] on error."""
    try:
        return run_cypher_retrieval(
            driver,
            intent=intent,
            entities=entities,
            database=database,
            max_rows=cypher_max_rows,
        )
    except Exception as e:
        logger.warning("Cypher retrieval failed: %s", e)
        return []


@dataclass
class OrchestratorResult:
    intent: str
    entities: dict[str, Any]
    semantic_results: list[Any] = field(default_factory=list)
    structural_results: dict[str, Any] = field(default_factory=dict)
    cypher_results: list[dict[str, Any]] = field(default_factory=list)
    fused_results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Set when post-fusion hard filters reduce the result list to zero.
    # The prompt builder injects this as [NO RESULTS] so the LLM explains
    # why nothing was found and suggests what to relax.
    fallback_message: str | None = None


async def orchestrate(
    driver: Driver,
    *,
    cfg: EmbeddingConfig,
    embedder: QueryEmbedder,
    user_query: str,
    customer_node_id: str | None = None,
    customer_profile: dict[str, Any] | None = None,
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
        customer_node_id: Neo4j elementId of logged-in customer (for structural retrieval)
        customer_profile: Full profile dict from fetch_customer_profile() — when provided,
                          the customer's stored diets, allergens, and health conditions are
                          merged into the extracted entities before Cypher retrieval so that
                          personalisation constraints are always enforced silently.
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

        # Profile enrichment: silently merge the logged-in customer's stored diets,
        # allergens, and health conditions into the extracted entities so the Cypher
        # generator and prompt builder always enforce personalisation constraints even
        # when the user didn't mention them in this query.
        if customer_profile:
            result.entities = merge_profile_into_entities(result.entities, customer_profile)
            logger.debug(
                "Profile enrichment applied",
                extra={
                    "component": "orchestrator",
                    "query": query_truncated,
                    "profile_diets": customer_profile.get("diets"),
                    "profile_allergens": customer_profile.get("allergens"),
                    "profile_conditions": customer_profile.get("health_conditions"),
                },
            )

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

    # ── Step 2–4: Parallel retrieval (semantic, structural, cypher) ────────────
    # Run all three paths concurrently via asyncio.to_thread. Structural is skipped
    # entirely (no task launched) when customer_node_id is missing or intent is not
    # in STRUCTURAL_INTENTS. Each path has a timeout; on timeout, that path returns
    # empty (best-effort) and others continue.
    t_retrieval = time.perf_counter()
    timeout_s = (guardrails.get("timeout_ms") or 15000) / 1000.0

    async def _with_timeout(awaitable, path: str, default: Any):
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "Retrieval timeout",
                extra={"component": "orchestrator", "path": path, "timeout_s": timeout_s},
            )
            return default

    async def _empty_structural() -> dict[str, Any]:
        return {}

    skip_structural = (
        result.intent not in STRUCTURAL_INTENTS or not customer_node_id
    )
    if skip_structural:
        logger.debug(
            "Skipping structural retrieval",
            extra={
                "component": "orchestrator",
                "intent": result.intent,
                "has_customer": bool(customer_node_id),
            },
        )

    semantic_task = asyncio.to_thread(
        _run_semantic,
        driver,
        cfg,
        embedder,
        user_query,
        result.intent,
        intent_semantic_labels,
        intent_cfg,
        top_k,
        semantic_min_score,
        database,
        config_path,
    )
    structural_task = (
        _empty_structural()
        if skip_structural
        else asyncio.to_thread(
            _run_structural,
            driver,
            cfg,
            customer_node_id,
            result.intent,
            intent_structural,
            top_k,
            structural_min_score,
            database,
        )
    )
    cypher_task = asyncio.to_thread(
        _run_cypher,
        driver,
        result.intent,
        result.entities,
        cypher_max_rows,
        database,
    )

    semantic_results, structural_results, cypher_results = await asyncio.gather(
        _with_timeout(semantic_task, "semantic", []),
        _with_timeout(structural_task, "structural", {}),
        _with_timeout(cypher_task, "cypher", []),
    )

    result.semantic_results = semantic_results or []
    result.structural_results = structural_results or {}
    result.cypher_results = cypher_results or []

    # Surface errors for failed paths (workers return empty on error; we infer from counts)
    semantic_count = len(result.semantic_results)
    structural_count = len(result.structural_results.get("expanded_context", []))
    cypher_count = len(result.cypher_results)
    semantic_label = intent_semantic_labels.get(result.intent, "Recipe")

    t_retrieval_ms = (time.perf_counter() - t_retrieval) * 1000
    logger.info(
        "Parallel retrieval complete",
        extra={
            "component": "orchestrator",
            "query": query_truncated,
            "intent": result.intent,
            "semantic_count": semantic_count,
            "structural_count": structural_count,
            "cypher_count": cypher_count,
            "latency_ms": round(t_retrieval_ms, 1),
        },
    )
    if semantic_count == 0:
        logger.warning(
            "Semantic retrieval empty",
            extra={"component": "orchestrator", "query": query_truncated, "label": semantic_label},
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

    # ── Step 6: Post-fusion hard constraint filter ────────────────────────────
    # Enforces allergen exclusion, course match, and calorie limit across ALL
    # fused results — including those from semantic and structural retrieval
    # that bypassed the Cypher WHERE clauses.
    # Diet/health condition filters are placeholders until FORBIDS relationships
    # are populated in Neo4j (see constraint_filter.py for details).
    t_filter = time.perf_counter()
    try:
        result.fused_results = apply_hard_constraints(
            result.fused_results,
            result.entities,
            result.intent,
            driver,
            database=database,
        )
    except Exception as e:
        logger.warning(
            "Post-fusion constraint filter failed — results unfiltered: %s", e,
            extra={"component": "orchestrator", "query": query_truncated},
        )
        result.errors.append(f"Constraint filter error: {e}")

    t_filter_ms = (time.perf_counter() - t_filter) * 1000
    logger.info(
        "Post-fusion filter complete",
        extra={
            "component": "orchestrator",
            "query": query_truncated,
            "intent": result.intent,
            "before": fused_count,
            "after": len(result.fused_results),
            "latency_ms": round(t_filter_ms, 1),
        },
    )

    # ── Step 7: Zero-results fallback ─────────────────────────────────────────
    # When hard filters reduce the list to zero, build a structured explanation
    # instead of passing an empty context to the LLM.  The prompt builder injects
    # this as [NO RESULTS] so the LLM can explain why and suggest alternatives.
    if not result.fused_results:
        result.fallback_message = build_zero_results_message(result.entities, result.intent)
        logger.info(
            "Zero results after filtering — fallback message set",
            extra={
                "component": "orchestrator",
                "query": query_truncated,
                "intent": result.intent,
                "entities": list(result.entities.keys()),
            },
        )

    return result
