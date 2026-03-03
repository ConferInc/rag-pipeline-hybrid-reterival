import asyncio
import json
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI

from rag_pipeline.config import load_embedding_config
from rag_pipeline.embeddings.caching_embedder import CachingQueryEmbedder
from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder
from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
from rag_pipeline.orchestrator.orchestrator import orchestrate
from rag_pipeline.augmentation.prompt_builder import build_augmented_prompt
from rag_pipeline.generation.generator import generate_response

# Import our LLM Judge helper
from eval_llm_judge import evaluate_generation_llm_judge

def run_evaluation(queries_file: str, output_file: str):
    """
    Reads evaluation queries from a JSON file, runs the full RAG pipeline
    end-to-end, evaluates the generated output via LLM-as-a-judge, 
    and saves the results to another JSON file.
    """
    load_dotenv()
    
    # 1. Load queries context
    if not os.path.exists(queries_file):
        raise FileNotFoundError(f"Queries file '{queries_file}' not found.")
        
    with open(queries_file, 'r', encoding='utf-8') as f:
        queries_data = json.load(f)
        
    cfg = load_embedding_config(Path("embedding_config.yaml"))
    
    model = os.environ.get("OPENAI_EMBEDDING_MODEL")
    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    
    if not api_key:
        raise EnvironmentError("Missing required environment variable: OPENAI_API_KEY")
        
    base_embedder = OpenAIQueryEmbedder(
        client=OpenAI(base_url=base_url, api_key=api_key),
        model=model or "text-embedding-3-small",  # Default if missing just to pass initialization
    )
    config_path = Path("embedding_config.yaml")
    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        cache_cfg = (raw or {}).get("embedding_cache", {}) or {}
    except Exception:
        cache_cfg = {}
    embedder = (
        CachingQueryEmbedder(
            base_embedder,
            max_size=cache_cfg.get("max_size", 500),
            key_normalize=cache_cfg.get("key_normalize", "strip_lower"),
        )
        if cache_cfg.get("enabled", False)
        else base_embedder
    )

    neo_settings = neo4j_settings_from_env()
    driver = create_neo4j_driver(neo_settings)
    
    results = []
    
    try:
        for idx, item in enumerate(queries_data):
            query = item["query"]
            expected_intent = item.get("expected_intent", "")
            print(f"[{idx+1}/{len(queries_data)}] Evaluating: '{query}' ({expected_intent})")
            
            # Step A: Run Orchestrator Pipeline
            try:
                orch_result = asyncio.run(orchestrate(
                    driver,
                    cfg=cfg,
                    embedder=embedder,
                    user_query=query,
                    customer_node_id=item.get("customer_id", None),
                    top_k=5,
                    database=neo_settings.database,
                    config_path="embedding_config.yaml",
                ))
                
                # Step B: Gather Context for Judge
                # Use fused_results (RRF) so judge sees the SAME context as the generator.
                if orch_result.fused_results:
                    retrieved_context = [
                        {"title": item.get("title") or item.get("key"), "label": item.get("label"), **item.get("payload", {})}
                        for item in orch_result.fused_results
                    ]
                    retrieved_context = [{k: v for k, v in d.items() if v is not None} for d in retrieved_context]
                else:
                    # Fallback: concatenate semantic + cypher + structural
                    retrieved_context = []
                    if orch_result.semantic_results:
                        retrieved_context.extend([r.to_dict() for r in orch_result.semantic_results])
                    if isinstance(orch_result.cypher_results, list):
                        retrieved_context.extend(orch_result.cypher_results)
                    struct_context = orch_result.structural_results.get("expanded_context", [])
                    if struct_context:
                        retrieved_context.extend(struct_context)
                    
                # Step C: Formulate final prompt and generate answer
                prompt = build_augmented_prompt(orch_result, query)
                
                try:
                    generated_response = generate_response(prompt)
                except Exception as gen_err:
                    print(f"   => Generation error: {gen_err}")
                    generated_response = f"Generation Error: {gen_err}"
                
                # Step D: Evaluate with LLM Judge
                if generated_response and "Error" not in generated_response:
                    evaluate_scores = evaluate_generation_llm_judge(
                        query=query,
                        retrieved_recipes=retrieved_context,
                        generated_response=generated_response
                    )
                else:
                    evaluate_scores = {"relevance": 0.0, "faithfulness": 0.0}
                    
                # Record
                record = {
                    "query": query,
                    "expected_intent": expected_intent,
                    "detected_intent": orch_result.intent,
                    "category": item.get("category", ""),
                    "context_items_count": len(retrieved_context),
                    "generated_response": generated_response,
                    "evaluation_scores": evaluate_scores,
                    "pipeline_errors": orch_result.errors
                }
                
                # Optionally print immediate results
                print(f"   => Rel: {evaluate_scores.get('relevance', 0.0):.2f} | Faith: {evaluate_scores.get('faithfulness', 0.0):.2f} | Intent: {orch_result.intent}")

            except Exception as e:
                print(f"   => Pipeline failed for query '{query}': {e}")
                record = {
                    "query": query,
                    "expected_intent": expected_intent,
                    "error": str(e)
                }
                
            results.append(record)
            
            # Save progressively
            with open(output_file, 'w', encoding='utf-8') as out_f:
                json.dump(results, out_f, indent=2, ensure_ascii=False)
                
    finally:
        driver.close()
        
    print(f"\nEvaluation complete. Wrote {len(results)} results to {output_file}")
    
    # Static Analysis - compute averages
    valid_results = [r for r in results if r.get("evaluation_scores")]
    if valid_results:
        avg_rel = sum(r["evaluation_scores"].get("relevance", 0.0) for r in valid_results) / len(valid_results)
        avg_faith = sum(r["evaluation_scores"].get("faithfulness", 0.0) for r in valid_results) / len(valid_results)
        print(f"Overall Relevance Average: {avg_rel:.2f}")
        print(f"Overall Faithfulness Average: {avg_faith:.2f}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Evaluate RAG Pipeline using LLM-as-a-judge")
    parser.add_argument("--queries", default="eval_queries.json", help="Path to input dataset JSON")
    parser.add_argument("--output", default="eval_results.json", help="Path to output results JSON")
    args = parser.parse_args()
    
    run_evaluation(args.queries, args.output)
