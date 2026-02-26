from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from argparse import ArgumentParser
from pathlib import Path

import yaml

from dotenv import load_dotenv

from rag_pipeline.logging_utils import (
    set_request_id,
    setup_pipeline_logging,
    truncate_for_log,
)
from openai import OpenAI

from rag_pipeline.config import load_embedding_config
from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder
from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
from rag_pipeline.retrieval.service import SemanticRetrievalRequest, retrieve_semantic
from rag_pipeline.retrieval.structural import (
    get_seed_embedding,
    structural_search_by_label,
    structural_search_with_expansion,
)


def _print_structural_prompt_preview(condensed: list, *, user_query: str = "Recommend me some recipes") -> None:
    """Print how structural context appears in the augmented LLM prompt."""
    from rag_pipeline.augmentation.prompt_builder import SYSTEM_PROMPT
    from rag_pipeline.augmentation.condense import format_context_as_text

    structural_text = format_context_as_text(
        condensed,
        header="Recipes liked by similar users:",
    )
    sections = [
        f"[SYSTEM]\n{SYSTEM_PROMPT}",
        f"[COLLABORATIVE CONTEXT]\n{structural_text}",
        f"[USER QUERY]\n{user_query}",
    ]
    print("\n" + "=" * 60)
    print("PROMPT PREVIEW (structural context as sent to LLM):")
    print("=" * 60)
    print("\n\n".join(sections))
    print("=" * 60)


def build_parser() -> ArgumentParser:
    p = ArgumentParser(description="RAG Pipeline CLI")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("semantic-search", help="Run semantic vector search in Neo4j")
    s.add_argument("--config", default="embedding_config.yaml", help="Path to embedding config YAML")
    s.add_argument("--query", required=True, help="User query text")
    s.add_argument("--label", default=None, help="Optional label override (e.g., Recipe)")
    s.add_argument("--top-k", type=int, default=10, help="Top K results")
    s.add_argument("--format", choices=["json", "text"], default="json", help="Output format")
    s.add_argument("--max-items", type=int, default=10, help="Max items shown in text format")

    st = sub.add_parser("structural-search", help="Run structural (GraphSAGE) vector search in Neo4j")
    st.add_argument("--config", default="embedding_config.yaml", help="Path to embedding config YAML")
    st.add_argument("--seed-id", required=True, help="Neo4j elementId of the seed node")
    st.add_argument("--label", required=True, help="Node label to search (e.g., B2C_Customer, Recipe)")
    st.add_argument("--top-k", type=int, default=10, help="Top K results")

    se = sub.add_parser("structural-expand", help="Structural search + k-hop expansion with intent filtering")
    se.add_argument("--config", default="embedding_config.yaml", help="Path to embedding config YAML")
    se.add_argument("--seed-id", required=True, help="Neo4j elementId of the seed node")
    se.add_argument("--label", required=True, help="Seed node label (e.g., B2C_Customer)")
    se.add_argument("--top-k", type=int, default=5, help="Top K similar nodes")
    se.add_argument("--intent", default=None, help="Intent filter (e.g., recommend_recipe, check_allergen)")
    se.add_argument("--filter-labels", default=None, help="Comma-separated labels to keep (overrides intent)")
    se.add_argument("--filter-rels", default=None, help="Comma-separated relationships to keep (overrides intent)")
    se.add_argument("--condense", action="store_true", help="Condense output for LLM (dedupe, rank, trim)")
    se.add_argument("--format", choices=["json", "text"], default="json", help="Output format (json or text for LLM)")
    se.add_argument("--max-items", type=int, default=10, help="Max items in condensed output")
    se.add_argument("--show-prompt", action="store_true", help="Show how structural context appears in the augmented LLM prompt")

    fr = sub.add_parser("full-retrieval", help="Run all 3 retrievals + build augmented LLM prompt")
    fr.add_argument("--config", default="embedding_config.yaml", help="Path to embedding config YAML")
    fr.add_argument("--query", required=True, help="User query text")
    fr.add_argument("--customer-id", default=None, help="Neo4j elementId of the customer (for structural)")
    fr.add_argument("--top-k", type=int, default=5, help="Top K results per retrieval")
    fr.add_argument("--format", choices=["prompt", "json"], default="prompt", help="Output as augmented prompt or raw JSON")

    ask = sub.add_parser("ask", help="End-to-end: retrieve + augment + generate answer")
    ask.add_argument("--config", default="embedding_config.yaml", help="Path to embedding config YAML")
    ask.add_argument("--query", required=True, help="User query text")
    ask.add_argument("--customer-id", default=None, help="Neo4j elementId of the customer (for structural)")
    ask.add_argument("--top-k", type=int, default=5, help="Top K results per retrieval")
    ask.add_argument("--show-prompt", action="store_true", help="Also print the augmented prompt before the answer")
    return p


logger_cli = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "semantic-search":
        cfg = load_embedding_config(Path(args.config))

        model = os.environ.get("OPENAI_EMBEDDING_MODEL")
        if not model:
            raise EnvironmentError("Missing required environment variable: OPENAI_EMBEDDING_MODEL")

        base_url = os.environ.get("OPENAI_BASE_URL")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError("Missing required environment variable: OPENAI_API_KEY")

        embedder = OpenAIQueryEmbedder(
            client=OpenAI(base_url=base_url, api_key=api_key),
            model=model,
        )

        neo_settings = neo4j_settings_from_env()
        driver = create_neo4j_driver(neo_settings)

        try:
            results = retrieve_semantic(
                driver,
                cfg=cfg,
                embedder=embedder,
                request=SemanticRetrievalRequest(
                    query=str(args.query),
                    top_k=int(args.top_k),
                    label=args.label,
                    config_path=str(args.config),
                ),
                database=neo_settings.database,
            )
        finally:
            driver.close()

        if args.format == "text":
            from rag_pipeline.augmentation.condense import format_semantic_results_as_text
            print(format_semantic_results_as_text(results, max_items=int(args.max_items)))
        else:
            print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))

    elif args.command == "structural-search":
        cfg = load_embedding_config(Path(args.config))

        neo_settings = neo4j_settings_from_env()
        driver = create_neo4j_driver(neo_settings)

        try:
            seed_emb = get_seed_embedding(
                driver,
                cfg=cfg,
                label=str(args.label),
                node_id=str(args.seed_id),
                database=neo_settings.database,
            )
            if seed_emb is None:
                print(json.dumps({"error": f"No GraphSAGE embedding found for node {args.seed_id}"}))
                return

            results = structural_search_by_label(
                driver,
                cfg=cfg,
                label=str(args.label),
                seed_vector=seed_emb,
                top_k=int(args.top_k),
                database=neo_settings.database,
            )
        finally:
            driver.close()

        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))

    elif args.command == "structural-expand":
        from rag_pipeline.augmentation.condense import condense_for_llm, format_context_as_text

        cfg = load_embedding_config(Path(args.config))

        neo_settings = neo4j_settings_from_env()
        driver = create_neo4j_driver(neo_settings)

        allowed_labels: list[str] | None = None
        allowed_rels: list[str] | None = None

        if args.filter_labels:
            allowed_labels = [lbl.strip() for lbl in args.filter_labels.split(",")]
        if args.filter_rels:
            allowed_rels = [rel.strip() for rel in args.filter_rels.split(",")]

        if args.intent and not (args.filter_labels or args.filter_rels):
            import yaml
            with open(args.config) as f:
                raw_cfg = yaml.safe_load(f)
            intent_filters = raw_cfg.get("intent_filters", {})
            intent_cfg = intent_filters.get(args.intent, {})
            allowed_labels = intent_cfg.get("labels")
            allowed_rels = intent_cfg.get("relationships") or None

        try:
            seed_emb = get_seed_embedding(
                driver,
                cfg=cfg,
                label=str(args.label),
                node_id=str(args.seed_id),
                database=neo_settings.database,
            )
            if seed_emb is None:
                print(json.dumps({"error": f"No GraphSAGE embedding found for node {args.seed_id}"}))
                return

            result = structural_search_with_expansion(
                driver,
                cfg=cfg,
                label=str(args.label),
                seed_vector=seed_emb,
                top_k=int(args.top_k),
                allowed_labels=allowed_labels,
                allowed_relationships=allowed_rels,
                database=neo_settings.database,
            )
        finally:
            driver.close()

        if args.condense:
            condensed = condense_for_llm(
                result["expanded_context"],
                max_items=int(args.max_items),
            )
            if args.format == "text":
                print(format_context_as_text(condensed, header="Recipes from similar users:"))
                if args.show_prompt:
                    _print_structural_prompt_preview(condensed)
            else:
                print(json.dumps(condensed, indent=2, ensure_ascii=False))
                if args.show_prompt:
                    _print_structural_prompt_preview(condensed)
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if args.show_prompt and result.get("expanded_context"):
                condensed = condense_for_llm(
                    result["expanded_context"],
                    max_items=int(args.max_items),
                )
                _print_structural_prompt_preview(condensed)

    elif args.command == "full-retrieval":
        from rag_pipeline.orchestrator.orchestrator import orchestrate
        from rag_pipeline.augmentation.prompt_builder import build_augmented_prompt

        cfg = load_embedding_config(Path(args.config))

        model = os.environ.get("OPENAI_EMBEDDING_MODEL")
        if not model:
            raise EnvironmentError("Missing required environment variable: OPENAI_EMBEDDING_MODEL")

        base_url = os.environ.get("OPENAI_BASE_URL")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError("Missing required environment variable: OPENAI_API_KEY")

        embedder = OpenAIQueryEmbedder(
            client=OpenAI(base_url=base_url, api_key=api_key),
            model=model,
        )

        neo_settings = neo4j_settings_from_env()
        driver = create_neo4j_driver(neo_settings)

        try:
            orch_result = orchestrate(
                driver,
                cfg=cfg,
                embedder=embedder,
                user_query=str(args.query),
                customer_node_id=args.customer_id,
                top_k=int(args.top_k),
                database=neo_settings.database,
                config_path=str(args.config),
            )
        finally:
            driver.close()

        if args.format == "prompt":
            prompt = build_augmented_prompt(orch_result, str(args.query))
            print(prompt)
        else:
            print(json.dumps({
                "intent": orch_result.intent,
                "entities": orch_result.entities,
                "semantic_count": len(orch_result.semantic_results),
                "structural_count": len(orch_result.structural_results.get("expanded_context", [])),
                "cypher_count": len(orch_result.cypher_results),
                "fused_count": len(orch_result.fused_results),
                "errors": orch_result.errors,
            }, indent=2, ensure_ascii=False))

    elif args.command == "ask":
        from rag_pipeline.orchestrator.orchestrator import orchestrate
        from rag_pipeline.augmentation.prompt_builder import build_augmented_prompt
        from rag_pipeline.generation.generator import generate_response

        setup_pipeline_logging(args.config)
        request_id = str(uuid.uuid4())
        set_request_id(request_id)

        cfg = load_embedding_config(Path(args.config))

        model = os.environ.get("OPENAI_EMBEDDING_MODEL")
        if not model:
            raise EnvironmentError("Missing required environment variable: OPENAI_EMBEDDING_MODEL")

        base_url = os.environ.get("OPENAI_BASE_URL")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError("Missing required environment variable: OPENAI_API_KEY")

        embedder = OpenAIQueryEmbedder(
            client=OpenAI(base_url=base_url, api_key=api_key),
            model=model,
        )

        neo_settings = neo4j_settings_from_env()
        driver = create_neo4j_driver(neo_settings)

        query_truncated = truncate_for_log(str(args.query))
        logger_cli.info(
            "Request start",
            extra={"component": "cli", "query": query_truncated},
        )
        t_start = time.perf_counter()

        try:
            orch_result = orchestrate(
                driver,
                cfg=cfg,
                embedder=embedder,
                user_query=str(args.query),
                customer_node_id=args.customer_id,
                top_k=int(args.top_k),
                database=neo_settings.database,
                config_path=str(args.config),
            )
        finally:
            driver.close()

        t_retrieval_ms = (time.perf_counter() - t_start) * 1000
        semantic_count = len(orch_result.semantic_results)
        structural_count = len(orch_result.structural_results.get("expanded_context", []))
        cypher_count = len(orch_result.cypher_results)
        fused_count = len(orch_result.fused_results)

        logger_cli.info(
            "Retrieval complete",
            extra={
                "component": "cli",
                "query": query_truncated,
                "intent": orch_result.intent,
                "retrieval_counts": {
                    "semantic": semantic_count,
                    "structural": structural_count,
                    "cypher": cypher_count,
                },
                "fused_count": fused_count,
                "latency_ms": round(t_retrieval_ms, 1),
            },
        )

        # Check if all retrieval context is empty; skip LLM if configured
        has_context = (
            bool(orch_result.fused_results)
            or bool(orch_result.semantic_results)
            or bool(orch_result.structural_results.get("expanded_context"))
            or bool(orch_result.cypher_results)
        )
        if not has_context:
            failure_cfg = {}
            try:
                with open(args.config) as f:
                    raw = yaml.safe_load(f)
                failure_cfg = raw.get("retrieval_failure_handling", {}) or {}
            except Exception:
                pass
            all_empty = failure_cfg.get("all_empty", {}) or {}
            if all_empty.get("skip_generation", True):
                msg = all_empty.get(
                    "user_message",
                    "No recipes could be loaded right now. Please try again.",
                )
                logger_cli.warning(
                    "All retrievals empty; skipping LLM generation",
                    extra={"component": "cli", "query": query_truncated},
                )
                print(msg)
                return

        prompt = build_augmented_prompt(orch_result, str(args.query))

        t_gen_start = time.perf_counter()
        if args.show_prompt:
            print("=" * 60)
            print("AUGMENTED PROMPT:")
            print("=" * 60)
            print(prompt)
            print("=" * 60)
            print("LLM RESPONSE:")
            print("=" * 60)

        try:
            answer = generate_response(prompt, config_path=str(args.config))
        except Exception as e:
            t_gen_ms = (time.perf_counter() - t_gen_start) * 1000
            logger_cli.error(
                "Generation failed",
                extra={
                    "component": "generation",
                    "query": query_truncated,
                    "error": str(e),
                    "latency_ms": round(t_gen_ms, 1),
                },
                exc_info=True,
            )
            print(f"[ERROR] Generation failed: {e}", file=sys.stderr)
            raise

        t_gen_ms = (time.perf_counter() - t_gen_start) * 1000

        # Response post-validation (forbidden items)
        validation_cfg = {}
        try:
            with open(args.config) as f:
                raw_cfg = yaml.safe_load(f)
            validation_cfg = raw_cfg.get("response_validation", {}) or {}
        except Exception:
            pass
        if validation_cfg.get("enabled", False) and answer:
            from rag_pipeline.validation.response_validator import validate_response
            is_valid, violations, validated = validate_response(
                answer, orch_result.entities, validation_cfg
            )
            answer = validated

        if not answer:
            logger_cli.warning(
                "LLM returned empty response",
                extra={
                    "component": "generation",
                    "query": query_truncated,
                    "latency_ms": round(t_gen_ms, 1),
                },
            )
            print("[WARN] LLM returned an empty response. Check API key, model, and base URL.", file=sys.stderr)
        else:
            t_total_ms = (time.perf_counter() - t_start) * 1000
            logger_cli.info(
                "Request complete",
                extra={
                    "component": "cli",
                    "query": query_truncated,
                    "intent": orch_result.intent,
                    "latency_ms": round(t_total_ms, 1),
                    "generation_latency_ms": round(t_gen_ms, 1),
                },
            )
            print(answer)


if __name__ == "__main__":
    main()

